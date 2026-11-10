# Экспорт учёта в Excel: операции за период + текущие долги, остатки, кассы.
import io
import json
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import db
import prices

OP_TYPES = {
    "invoice": "Накладная",
    "payment": "Оплата",
    "transfer": "Перемещение",
    "return": "Возврат",
    "inventory": "Инвентаризация",
    "handover": "Инкассация",
    "writeoff": "Списание",
    "fix_expiry": "Исправление срока",
}

HEADER_FILL = PatternFill("solid", fgColor="1B5E20")
HEADER_FONT = Font(bold=True, color="FFFFFF")


def _txt(v):
    """Строка, начинающаяся с «=», стала бы в Excel живой формулой —
    экранируем апострофом (имя клиента может задать сотрудник)."""
    if isinstance(v, str) and v.startswith("="):
        return " " + v   # пробел вместо апострофа: апостроф был бы виден
    return v


def _sheet_header(ws, headers, widths):
    ws.append(headers)
    for col, width in enumerate(widths, 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"


def build_export(start_iso: str, period_label: str) -> io.BytesIO:
    wb = Workbook()

    # --- Операции ---
    ws = wb.active
    ws.title = "Операции"
    _sheet_header(
        ws,
        ["№", "Дата", "Тип", "Сотрудник", "Склад", "Клиент",
         "Сумма, сом", "Оплата, сом", "Статус", "Описание"],
        [7, 17, 15, 14, 12, 18, 12, 12, 11, 45],
    )
    for op in db.operations_all_since(start_iso):
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            data = {}
        try:
            dt = datetime.fromisoformat(op["ts"]).strftime("%d.%m.%Y %H:%M")
            if data.get("ship_date"):
                dt += f" (отгр. {data['ship_date'][8:10]}.{data['ship_date'][5:7]})"
        except (ValueError, TypeError):
            dt = op["ts"]
        user = db.get_user(op["user_id"])
        wh = db.warehouse_by_id(op["warehouse_id"]) if op["warehouse_id"] else None
        client = db.client_get(op["client_id"]) if op["client_id"] else None
        total = ""
        payment = ""
        # У отменённых операций суммы не заполняем: сумма по колонке в
        # Excel должна сходиться с учётом, а сторнированные суммы в ней
        # завышали итог (сумма видна в «Описании», статус — «отменена»).
        if op["status"] == "done":
            if op["type"] == "invoice":
                total = data.get("total", "")
                payment = data.get("payment", "") or ""
            elif op["type"] in ("payment", "handover"):
                payment = data.get("amount", "")
            elif op["type"] in ("return", "writeoff"):
                total = -data.get("total", 0)
        ws.append([
            op["id"], dt, OP_TYPES.get(op["type"], op["type"]),
            user["name"] if user else op["user_id"],
            wh["name"] if wh else "",
            _txt(client["name"]) if client else "",
            total, payment,
            "проведена" if op["status"] == "done" else "отменена",
            _txt(op["summary"]),
        ])

    # --- Долги ---
    ws = wb.create_sheet("Долги")
    _sheet_header(ws, ["Склад", "Клиент", "Долг, сом"], [14, 24, 14])
    total_debt = 0.0
    for wh in db.all_warehouses():
        for c in db.clients_of(wh["id"]):
            if c["debt"]:
                ws.append([wh["name"], _txt(c["name"]), c["debt"]])
                total_debt += c["debt"]
    ws.append([])
    ws.append(["", "ИТОГО", total_debt])
    ws.cell(row=ws.max_row, column=2).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=3).font = Font(bold=True)

    # --- Остатки ---
    ws = wb.create_sheet("Остатки")
    _sheet_header(
        ws,
        ["Склад", "Товар", "Фасовка", "Кол-во, шт", "Цена, сом", "Сумма, сом"],
        [14, 45, 14, 12, 12, 14],
    )
    grand = 0.0
    for wh in db.all_warehouses():
        smap = db.stock_map(wh["id"])
        for p in prices.PRICE_LIST_DATA:
            qty = smap.get(p["id"], 0)
            if qty:
                amount = qty * p["price"]
                grand += amount
                ws.append([wh["name"], _txt(p["name"]), p["volume"], qty, p["price"], amount])
        # Товар, выведенный из прайса, но с остатком: раньше молча выпадал
        # из экспорта, и остатки «не бились» с /stock.
        known = {p["id"] for p in prices.PRICE_LIST_DATA}
        for pid, qty in sorted(smap.items()):
            if qty and pid not in known:
                row = db.connect().execute(
                    "SELECT name, volume, price FROM products WHERE id=?",
                    (pid,)).fetchone()
                name = (row["name"] + " (вне прайса)") if row else f"товар №{pid} (вне прайса)"
                price = row["price"] if row else 0
                amount = qty * price
                grand += amount
                ws.append([wh["name"], _txt(name), row["volume"] if row else "",
                           qty, price, amount])
    ws.append([])
    ws.append(["", "ИТОГО по прайсу", "", "", "", grand])
    ws.cell(row=ws.max_row, column=2).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=6).font = Font(bold=True)

    # --- Кассы ---
    ws = wb.create_sheet("Кассы")
    _sheet_header(ws, ["Сотрудник", "Наличные на руках, сом"], [20, 24])
    total_cash = 0.0
    # Включая уволенных: несданная касса бывшего сотрудника — всё ещё
    # деньги компании, раньше она молча пропадала из экспорта.
    for u in db.list_users(active_only=False):
        cash = db.cash_on_hand(u["id"])
        if cash:
            name = u["name"] if u["active"] else f"{u['name']} (уволен)"
            ws.append([name, cash])
            total_cash += cash
    ws.append([])
    ws.append(["ИТОГО", total_cash])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=2).font = Font(bold=True)

    # --- Инфо ---
    ws = wb.create_sheet("Инфо")
    ws.append(["Экспорт ВЕТОП"])
    ws.append([f"Период операций: {period_label}"])
    ws.append([f"Сформирован: {datetime.now(db.BISHKEK).strftime('%d.%m.%Y %H:%M')}"])
    ws.append(["Долги, остатки и кассы — на момент выгрузки."])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


# ---------- /to1c — выгрузка для переноса в 1С ----------
# Просьба владельца 10.11.2026: он вручную переносит накладные бота в 1С
# и сверяет по одной. Файл повторяет документы 1С: расходные накладные
# построчно (номенклатура / характеристика / количество / цена / сумма),
# поступления в кассу, возвраты, списания, перемещения и лист сверки
# долгов с пустой колонкой «Долг в 1С».

def _d1c(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return str(ts or "")[:10]


def _item_names(it, prods):
    """(номенклатура, характеристика) как в 1С: имя без состава в скобках
    и фасовка отдельной колонкой."""
    pr = prods.get(it.get("product_id"))
    name = pr["name"] if pr else str(it.get("name") or "")
    vol = pr["volume"] if pr else str(it.get("volume") or "")
    return _txt(name.split("(")[0].strip()), vol


def _merged_items(items):
    """Строки документа; один товар по одной цене, разбитый ботом на две
    строки (разные партии), в 1С вносится одной строкой."""
    out, idx = [], {}
    for it in items or []:
        try:
            qty, price = float(it.get("qty") or 0), float(it.get("price") or 0)
        except (TypeError, ValueError):
            continue
        key = (it.get("product_id") or (it.get("name"), it.get("volume")), price)
        if key in idx:
            out[idx[key]]["qty"] += qty
        else:
            idx[key] = len(out)
            out.append({**it, "qty": qty, "price": price})
    return out


def _bold_row(ws, cols):
    for c in cols:
        ws.cell(row=ws.max_row, column=c).font = Font(bold=True)


def build_1c(start_iso: str, end_iso: str = None, wh_ids=None,
             client_id: int = None, skip_wh_ids=None):
    """Excel для переноса в 1С. wh_ids — только эти склады (None = все,
    кроме skip_wh_ids); client_id — только документы этого клиента.
    Возвращает (файл, счётчики по листам)."""
    skip = set(skip_wh_ids or ())
    whs = {w["id"]: w["name"] for w in db.all_warehouses() if w["id"] not in skip}
    if wh_ids is not None:
        whs = {k: v for k, v in whs.items() if k in set(wh_ids)}
    users = {u["id"]: u["name"] for u in db.list_users(active_only=False)}
    prods = {p["id"]: p for p in prices.PRICE_LIST_DATA}
    cl_cache = {}

    def cname(cid):
        if cid not in cl_cache:
            c = db.client_get(cid) if cid else None
            cl_cache[cid] = c["name"] if c else "—"
        return cl_cache[cid]

    wb = Workbook()
    ws_inv = wb.active
    ws_inv.title = "Расходные накладные"
    doc_cols = ["Дата", "№ бота", "Склад", "Покупатель", "Номенклатура",
                "Характеристика", "Количество", "Цена", "Сумма"]
    doc_w = [12, 9, 13, 28, 30, 16, 12, 11, 14]
    _sheet_header(ws_inv, doc_cols, doc_w)
    ws_pay = wb.create_sheet("Поступления в кассу")
    _sheet_header(ws_pay, ["Дата", "№ бота", "Склад", "Покупатель", "Сумма",
                           "Принял", "Примечание"], [12, 9, 13, 28, 14, 14, 30])
    ws_ret = wb.create_sheet("Возвраты")
    _sheet_header(ws_ret, doc_cols, doc_w)
    ws_wo = wb.create_sheet("Списания")
    _sheet_header(ws_wo, ["Дата", "№ бота", "Склад", "Номенклатура",
                          "Характеристика", "Количество", "Причина"],
                  [12, 9, 13, 30, 16, 12, 34])
    ws_tr = wb.create_sheet("Перемещения")
    _sheet_header(ws_tr, ["Дата", "№ бота", "Откуда", "Куда", "Номенклатура",
                          "Характеристика", "Количество"],
                  [12, 9, 22, 14, 30, 16, 12])

    n = {"inv": 0, "inv_sum": 0.0, "pay": 0, "pay_sum": 0.0, "ret": 0,
         "ret_sum": 0.0, "wo": 0, "tr": 0}

    def doc_rows(ws, op, data, label):
        total = 0.0
        d, who = _d1c(db.op_ts(op)), _txt(cname(op["client_id"]))
        for it in _merged_items(data.get("items")):
            nm, vol = _item_names(it, prods)
            sub = it["qty"] * it["price"]
            total += sub
            ws.append([d, op["id"], whs[op["warehouse_id"]], who, nm, vol,
                       it["qty"], it["price"], sub])
        ws.append([d, op["id"], "", "", f"{label} №{op['id']}", "", "", "", total])
        _bold_row(ws, (5, 9))
        ws.append([])
        return total

    for op in db.operations_since(start_iso, end_iso):
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        t = op["type"]
        if client_id is not None and op["client_id"] != client_id:
            continue
        if t == "transfer":
            deltas = data.get("stock_deltas") or []
            touched = {w for w, _p, _d in deltas}
            if not (touched & set(whs)):
                continue
            src = sorted({w for w, _p, dl in deltas if dl < 0})
            src_name = ", ".join((db.warehouse_by_id(w) or {"name": f"склад {w}"})["name"]
                                 for w in src) or "поставка (приход извне)"
            dst = db.warehouse_by_id(op["warehouse_id"])
            for it in data.get("items") or []:
                nm, vol = _item_names(it, prods)
                ws_tr.append([_d1c(db.op_ts(op)), op["id"], src_name,
                              dst["name"] if dst else "", nm, vol, it.get("qty")])
            n["tr"] += 1
            continue
        if op["warehouse_id"] not in whs:
            continue
        if t == "invoice":
            n["inv"] += 1
            n["inv_sum"] += doc_rows(ws_inv, op, data, "Итого по накладной")
            pay = float(data.get("payment") or 0)
            if pay > 0:
                ws_pay.append([_d1c(db.op_ts(op)), op["id"], whs[op["warehouse_id"]],
                               _txt(cname(op["client_id"])), pay,
                               users.get(op["user_id"], ""),
                               f"оплата при накладной №{op['id']}"])
                n["pay"] += 1
                n["pay_sum"] += pay
        elif t == "payment":
            amt = float(data.get("amount") or 0)
            ws_pay.append([_d1c(db.op_ts(op)), op["id"], whs[op["warehouse_id"]],
                           _txt(cname(op["client_id"])), amt,
                           users.get(op["user_id"], ""), "отдельная оплата"])
            n["pay"] += 1
            n["pay_sum"] += amt
        elif t == "return":
            n["ret"] += 1
            n["ret_sum"] += doc_rows(ws_ret, op, data, "Итого по возврату")
        elif t == "writeoff":
            reason = (data.get("reason") or "").strip()
            for it in data.get("items") or []:
                nm, vol = _item_names(it, prods)
                ws_wo.append([_d1c(db.op_ts(op)), op["id"], whs[op["warehouse_id"]],
                              nm, vol, it.get("qty"), _txt(reason)])
            n["wo"] += 1

    for ws, lab, col, key in ((ws_inv, 5, 9, "inv_sum"), (ws_pay, 4, 5, "pay_sum"),
                              (ws_ret, 5, 9, "ret_sum")):
        row = [""] * col
        row[lab - 1], row[col - 1] = "ВСЕГО", n[key]
        ws.append(row)
        _bold_row(ws, (lab, col))

    # --- Сверка долгов: колонку «Долг в 1С» владелец заполняет сам ---
    ws = wb.create_sheet("Сверка долгов")
    _sheet_header(ws, ["Склад", "Покупатель", "Долг в боте (сейчас)",
                       "Долг в 1С (впишите)", "Разница (бот − 1С)"],
                  [13, 30, 20, 20, 20])
    debt_total = 0.0
    for wid, wname in whs.items():
        for c in db.clients_of(wid):
            if client_id is not None and c["id"] != client_id:
                continue
            if not c["debt"] and client_id is None:
                continue
            r = ws.max_row + 1
            ws.append([wname, _txt(c["name"]), c["debt"], None,
                       f'=IF(D{r}="","",C{r}-D{r})'])
            debt_total += c["debt"]
    ws.append(["", "ИТОГО", debt_total])
    _bold_row(ws, (2, 3))
    n["debt_sum"] = debt_total

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf, n


# ---------- /history — движение и продажи ВСЕХ товаров по датам ----------
# Просьба владельца 18.09.2026: «все движения, все препараты, по дням, по
# месяцам — сколько продалось в августе, в сентябре… чтобы знать, что и
# когда заказывать из Китая». Файл он скидывает Джарвису на разбор, поэтому
# формат — плоские таблицы Excel, а не PDF.

def _month_key(ts: str) -> str:
    return ts[:7]            # «2026-08»


def _day_key(ts: str) -> str:
    return ts[:10]           # «2026-08-14»


def build_history(training_wh_ids=None) -> io.BytesIO:
    """Excel: продажи по месяцам (товар × месяц), по месяцам и складам,
    по дням, все движения товара, остатки со скоростью продаж, справка.
    Только проведённые операции (отменённые не искажают спрос); учебные
    склады (training_wh_ids) не входят."""
    skip = set(training_wh_ids or ())
    whs = {w["id"]: w["name"] for w in db.all_warehouses() if w["id"] not in skip}
    users = {u["id"]: u["name"] for u in db.list_users(active_only=False)}
    clients = {r["id"]: r["name"] for r in
               db.connect().execute("SELECT id, name FROM clients")}
    prods = {p["id"]: p for p in prices.PRICE_LIST_DATA}
    order = {p["id"]: i for i, p in enumerate(prices.PRICE_LIST_DATA)}

    # sold[(wh, pid)][month] = чисто продано (накладные − возвраты)
    sold_m, sold_d, money_d = {}, {}, {}
    months = set()
    moves = []
    for op in db.operations_since("2000-01-01"):
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        t, ts = op["type"], db.op_ts(op)     # день отгрузки, если задан
        deltas = [(w, p, d) for w, p, d in data.get("stock_deltas", [])
                  if w in whs and d]
        if not deltas:
            continue
        # продажи: накладная (минус) и возврат (плюс)
        if t in ("invoice", "return"):
            # Сумма — по строкам документа (qty × цена каждой строки): один
            # товар двумя строками с разными ценами считался по последней
            # (ревизия 18.10.2026). Знак — как у штук: накладная +, возврат −.
            money_by_pid = {}
            for it in data.get("items") or []:
                if it.get("product_id"):
                    try:
                        sub = float(it.get("qty") or 0) * float(it.get("price") or 0)
                    except (TypeError, ValueError):
                        sub = 0.0
                    money_by_pid[it["product_id"]] = money_by_pid.get(it["product_id"], 0.0) + sub
            wh_cnt = len({w for w, _p, _d in deltas})
            for w, p, d in deltas:
                q = -d if t == "invoice" else -d      # накладная −d>0; возврат d>0 → −d<0
                mk, dk = _month_key(ts), _day_key(ts)
                months.add(mk)
                sold_m.setdefault((w, p), {})
                sold_m[(w, p)][mk] = sold_m[(w, p)].get(mk, 0) + q
                key = (dk, w, p)
                cur = sold_d.setdefault(key, [0, 0])
                if t == "invoice":
                    cur[0] += -d
                else:
                    cur[1] += d
                sign = 1.0 if t == "invoice" else -1.0
                money_d[key] = money_d.get(key, 0.0) + sign * money_by_pid.get(p, 0.0) / max(wh_cnt, 1)
        # все движения
        for w, p, d in deltas:
            others = sorted({x for x, _p, _d in data.get("stock_deltas", []) if x != w})
            if t in ("invoice", "return"):
                who = clients.get(op["client_id"], "—")
            elif t == "transfer":
                if not others:
                    who = "поставка (приход извне)"
                else:
                    names = ", ".join(whs.get(x) or db.warehouse_by_id(x)["name"] for x in others)
                    who = ("из " if d > 0 else "в ") + names
            elif t == "writeoff":
                who = (data.get("reason") or "").strip() or "—"
            elif t == "inventory":
                s = (op["summary"] or "").lower()
                who = "стартовая загрузка" if ("загрузка" in s or "стартов" in s) else "корректировка"
            else:
                who = "—"
            pr = prods.get(p)
            moves.append([
                _day_key(ts), ts[11:16], op["id"], whs[w],
                _txt(pr["name"].split(" (")[0] if pr else f"товар №{p}"),
                pr["volume"] if pr else "", OP_TYPES.get(t, t),
                d if d > 0 else None, -d if d < 0 else None,
                _txt(who), users.get(op["user_id"], "—"),
            ])

    months = sorted(months)
    wb = Workbook()

    # --- 1. Продажи по месяцам, все склады вместе ---
    ws = wb.active
    ws.title = "Продажи по месяцам"
    stock_total = {}
    for w in whs:
        for p, q in db.stock_map(w).items():
            stock_total[p] = stock_total.get(p, 0) + q
    _sheet_header(ws, ["№", "Товар", "Фасовка"] + months + ["Итого", "Остаток сейчас"],
                  [5, 34, 14] + [10] * len(months) + [10, 14])
    by_pid = {}
    for (w, p), mm in sold_m.items():
        for mk, q in mm.items():
            by_pid.setdefault(p, {})[mk] = by_pid.get(p, {}).get(mk, 0) + q
    for p in sorted(prods, key=order.get):
        pr = prods[p]
        row_m = [by_pid.get(p, {}).get(mk, 0) for mk in months]
        ws.append([p, _txt(pr["name"].split(" (")[0]), pr["volume"]]
                  + row_m + [sum(row_m), stock_total.get(p, 0)])
    ws.append([])
    ws.append(["", "ИТОГО", ""] + [sum(by_pid.get(p, {}).get(mk, 0) for p in prods) for mk in months]
              + [sum(sum(v.values()) for v in by_pid.values()), sum(stock_total.values())])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)

    # --- 2. Продажи по месяцам и складам ---
    ws2 = wb.create_sheet("По складам и месяцам")
    _sheet_header(ws2, ["Склад", "№", "Товар", "Фасовка"] + months + ["Итого", "Остаток"],
                  [12, 5, 34, 14] + [10] * len(months) + [10, 10])
    for w in sorted(whs, key=lambda x: whs[x]):
        smap = db.stock_map(w)
        for p in sorted(prods, key=order.get):
            mm = sold_m.get((w, p), {})
            if not mm and not smap.get(p):
                continue
            pr = prods[p]
            row_m = [mm.get(mk, 0) for mk in months]
            ws2.append([whs[w], p, _txt(pr["name"].split(" (")[0]), pr["volume"]]
                       + row_m + [sum(row_m), smap.get(p, 0)])

    # --- 3. Продажи по дням ---
    ws3 = wb.create_sheet("Продажи по дням")
    _sheet_header(ws3, ["Дата", "Склад", "№", "Товар", "Фасовка", "Продано",
                        "Возвращено", "Чистыми", "Сумма, сом"],
                  [12, 12, 5, 34, 14, 10, 11, 10, 12])
    for (dk, w, p), (s, r) in sorted(sold_d.items(), key=lambda kv: (kv[0][0], whs[kv[0][1]], order.get(kv[0][2], 999))):
        pr = prods.get(p)
        ws3.append([dk, whs[w], p, _txt(pr["name"].split(" (")[0] if pr else f"товар №{p}"),
                    pr["volume"] if pr else "", s, r, s - r, round(money_d.get((dk, w, p), 0.0))])

    # --- 4. Все движения ---
    ws4 = wb.create_sheet("Все движения")
    _sheet_header(ws4, ["Дата", "Время", "№ оп.", "Склад", "Товар", "Фасовка", "Тип",
                        "Приход", "Расход", "Контрагент / откуда-куда / причина", "Сотрудник"],
                  [12, 7, 7, 12, 34, 14, 16, 9, 9, 34, 14])
    for row in moves:
        ws4.append(row)

    # --- 5. Остатки и скорость ---
    ws5 = wb.create_sheet("Остатки и скорость")
    _sheet_header(ws5, ["Склад", "№", "Товар", "Фасовка", "Остаток",
                        "Продано 30 дн", "Продано 60 дн", "Продано 90 дн",
                        "Хватит дней (по 90)"],
                  [12, 5, 34, 14, 10, 13, 13, 13, 16])
    today = datetime.now(db.BISHKEK).date()

    def _sold_since(w, p, days):
        cut = (today - timedelta(days=days)).isoformat()
        return sum(s - r for (dk, ww, pp), (s, r) in sold_d.items()
                   if ww == w and pp == p and dk >= cut)
    for w in sorted(whs, key=lambda x: whs[x]):
        smap = db.stock_map(w)
        for p in sorted(prods, key=order.get):
            q = smap.get(p, 0)
            s30, s60, s90 = (_sold_since(w, p, n) for n in (30, 60, 90))
            if not q and not s90:
                continue
            pr = prods[p]
            days_left = round(q / (s90 / 90), 0) if s90 > 0 and q > 0 else None
            ws5.append([whs[w], p, _txt(pr["name"].split(" (")[0]), pr["volume"], q,
                        s30, s60, s90, days_left if days_left is not None else ("—" if q > 0 else "0")])

    # --- 6. Справка ---
    ws6 = wb.create_sheet("Справка")
    ws6.column_dimensions["A"].width = 110
    for line in [
        f"ИСТОРИЯ ДВИЖЕНИЯ ТОВАРА · ОсОО «ВЕТОП» · сформировано {datetime.now(db.BISHKEK).strftime('%d.%m.%Y %H:%M')}",
        "",
        "Продажи = накладные минус возвраты, в штуках (единицах прайса: для 10 мл — пачки по 10 флаконов).",
        "Только проведённые операции; отменённые (/undo, замены) не считаются. Черновики переходного периода не входят.",
        "Учебные склады исключены." if skip else "Учебных складов нет.",
        f"Период журнала: {months[0] if months else '—'} … {months[-1] if months else '—'}. "
        "ВНИМАНИЕ: первые месяцы у складов неполные — учёт по складам запускался постепенно "
        "(Каракол 21.07, Кара-Балта ~02.08, Манас 05.08, Бишкек 13.08.2026).",
        "",
        "Листы:",
        "1. Продажи по месяцам — товар × месяц, все склады вместе; последняя колонка — остаток сейчас по всем складам.",
        "2. По складам и месяцам — то же с разбивкой по складам (строки без продаж и без остатка скрыты).",
        "3. Продажи по дням — дата, склад, товар: продано / возвращено / чистыми / сумма по ценам накладных.",
        "4. Все движения — каждая операция, задевшая остаток: приход/расход, контрагент, сотрудник.",
        "5. Остатки и скорость — остаток и продажи за 30/60/90 дней по складу, «хватит дней» по скорости за 90 дней.",
        "",
        "Для анализа Джарвисом: скинуть файл в чат Claude — читается напрямую.",
    ]:
        ws6.append([line])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
