"""Разбор заказа клиента по бэкапу базы (ритуал FEFO, см. CLAUDE.md
«Ритуал разбора заказа»). Запуск из корня репозитория:

    ORDER_DB=/path/to/vetop_backup.db python3 tools/order_tool.py "Клиент" "Склад" < заказ.txt

Строки заказа: "Товар фасовка N к|шт|пач [срок]" (срок: 06/29, 06.2029).
ORDER_DB — КОПИЯ бэкапа (db.init применит миграции к файлу; рядом не должно
лежать чужих *-wal/*-shm). Выдаёт таблицу (штуки, цены спец/прайс, партии,
сверка срока с FEFO, нехватка), блок цен прайса, заготовку «спеццена для …»,
сообщение боту и сборочный лист «Заказ …».
"""
import os, sys, re
os.environ.setdefault("TELEGRAM_TOKEN", "x"); os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ["DB_PATH"] = os.environ.get("ORDER_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "work.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, prices, bot
db.init(bot.ADMIN_ID, bot.WAREHOUSE_NAMES, bot.STAFF); bot._refresh_price_dependents() if hasattr(bot, "_refresh_price_dependents") else None

def norm_exp(s):
    m = re.match(r"(\d{1,2})[./](\d{2,4})$", s)
    if not m: return None
    mm, yy = int(m.group(1)), int(m.group(2))
    if yy < 100: yy += 2000
    return f"{mm:02d}.{yy}"

def bn(name):
    return re.sub(r"\s*\(.*?\)", "", name).strip().title().replace("Мл","мл")

def fmt(n): return f"{int(round(n)):,}".replace(",", "'")

def main():
    cname, whname = sys.argv[1], sys.argv[2]
    wh = db.warehouse_by_name(whname); assert wh, "склад?"
    cli = db.client_exact(wh["id"], cname)
    if not cli:
        print("КЛИЕНТ НЕ НАЙДЕН точно; похожие:", [c["name"] for c in db.fuzzy_clients(wh["id"], cname)]); return
    spec = db.client_prices_map(cli["id"])
    print(f"Клиент: {cli['name']} ({wh['name']}), долг {fmt(cli['debt'])}, спеццен: {len(spec)}\n")
    rows, errs = [], []
    for line in sys.stdin:
        line = line.strip().lstrip("-•– ").strip()
        if not line: continue
        m = re.match(r"^(.*?)\s*[-–—:]?\s*(\d+)\s*(к|кор|коробк\w*|шт|пач|уп)?\.?\s*(?:[-–(]?\s*(\d{1,2}[./]\d{2,4})\)?)?\s*$", line, re.I)
        if not m: errs.append(f"не разобрал: {line}"); continue
        pname, n, unit, exp = m.group(1).strip(), int(m.group(2)), (m.group(3) or "шт").lower(), m.group(4)
        label, pids, rest, cands = bot._parse_sales_query(pname)
        if not pids or len(pids) != 1 or rest:
            errs.append(f"товар не однозначен: {line} -> {label} {pids} rest={rest} {cands}"); continue
        p = prices.BY_ID[pids[0]]
        if unit.startswith("к"): qty, utxt = n * p["box"], f"{n} к"
        elif unit in ("пач", "уп"): ps = prices.pack_size(p["id"]) or 1; qty, utxt = n * ps, f"{n} пач"
        else: qty, utxt = n, f"{n} шт"
        price = spec.get(p["id"]); 
        batches = db.product_batches_of(wh["id"], p["id"])
        rows.append(dict(p=p, qty=qty, utxt=utxt, exp=norm_exp(exp) if exp else None,
                         price=price if price is not None else p["price"], spec=price is not None,
                         batches=batches, stock=db.stock_qty(wh["id"], p["id"])))
    for e in errs: print("⚠️", e)
    print("\n=== ТАБЛИЦА ===")
    total = 0; boxes = 0
    for r in rows:
        p = r["p"]; s = r["qty"] * r["price"]; total += s
        fefo = r["batches"][0]["expiry"] if r["batches"] else "—"
        bt = ", ".join(f"{b['expiry'] or 'без срока'}:{b['qty']}" for b in r["batches"]) or "нет партий"
        warn = ""
        if r["stock"] < r["qty"]: warn += f" ‼️ НЕ ХВАТАЕТ (остаток {r['stock']})"
        if r["exp"] and r["exp"] != (fefo or ""): warn += f" ⚠️ срок {r['exp']} ≠ FEFO {fefo or 'без срока'}"
        print(f"{p['id']:>3}. {bn(p['name'])} {p['volume']} — {r['utxt']} = {r['qty']} шт × {fmt(r['price'])}{' (спец)' if r['spec'] else ''} = {fmt(s)} | партии: {bt}{warn}")
    print(f"ИТОГО {fmt(total)} сом, {sum(r['qty'] for r in rows)} шт")
    bb = bot.box_note_text([{"product_id": r["p"]["id"], "qty": r["qty"]} for r in rows], True, "")
    print(bb)
    print("\n=== ЦЕНЫ ПО ПРАЙСУ (столбиком) ===\n```")
    for r in rows:
        p = r["p"]; tag = f"  ← спеццена {fmt(r['price'])}" if r["spec"] else ""
        print(f"{bn(p['name'])} {p['volume']} — {fmt(p['price'])}{tag}")
    print("```")
    nospec = [r for r in rows if not r["spec"]]
    print("\n=== ЗАГОТОВКА СПЕЦЦЕН (цены прайса — правьте) ===\n```")
    if nospec:
        print(f"спеццена для {cli['name']}: " + ", ".join(f"{bn(r['p']['name'])} {r['p']['volume']} {fmt(r['p']['price'])}" for r in nospec))
    else: print("(спеццены есть на все позиции)")
    print("```\n\n=== СООБЩЕНИЕ БОТУ (накладная, без сроков) ===\n```")
    print(f"{cli['name']}\n" + "\n".join(f"{bn(r['p']['name'])} {r['p']['volume']} {r['utxt']}" for r in rows))
    print("```\n\n=== СБОРОЧНЫЙ ЛИСТ (в чат склада) ===\n```")
    print(f"Заказ {cli['name']}")
    for r in rows:
        fefo = r["batches"][0]["expiry"] if r["batches"] else ""
        note = f" (не {r['exp']}!)" if r["exp"] and fefo and r["exp"] != fefo else ""
        print(f"{bn(r['p']['name'])} {r['p']['volume']} — {r['utxt']} · срок {fefo or 'без срока'}{note}")
    print("```")
main()
