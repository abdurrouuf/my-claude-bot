# Телеграм-бот ВЕТОП: накладные, склады по регионам, долги клиентов.
import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta

from anthropic import AsyncAnthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import db
import prices
from db import BISHKEK
from invoice_pdf import (fmt_num, generate_act_pdf, generate_pdf_invoice,
                         safe_filename)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("vetop")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")

# Переходный режим: черновики выглядят как обычные накладные (без водяного
# знака «ЧЕРНОВИК»), но базу по-прежнему не трогают. Когда полностью перейдёте
# на учёт через бота — поставьте на Railway переменную DRAFT_WATERMARK=1.
DRAFT_WATERMARK = os.environ.get("DRAFT_WATERMARK", "0") == "1"

# Переходный период: сотрудникам доступны ТОЛЬКО черновики и вопросы о прайсе.
# Накладные, оплаты, перемещения и создание клиентов заблокированы (админу — нет).
# Когда база будет готова — поставьте на Railway переменную TRANSITION_MODE=0.
TRANSITION_MODE = os.environ.get("TRANSITION_MODE", "1") == "1"

TRANSITION_HINT = (
    "⏳ Идёт настройка базы данных — бот пока работает только в режиме черновика.\n\n"
    "Добавьте слово «черновик» в начало сообщения:\n"
    "черновик: Асан, Албенивер 200мл 1к, долг 31470, приход 5000\n\n"
    "Придёт готовая PDF-накладная, ничего в базу не запишется."
)


def transition_blocked(actor) -> bool:
    return TRANSITION_MODE and not is_admin(actor)


# Час вечерней сводки по времени Бишкека (0-23)
SUMMARY_HOUR = int(os.environ.get("SUMMARY_HOUR", "20"))

# Долг считается «старым», если клиент не платил столько дней
DEBT_ALERT_DAYS = int(os.environ.get("DEBT_ALERT_DAYS", "30"))

# Час ежедневного бэкапа базы админу в личку (по Бишкеку)
BACKUP_HOUR = int(os.environ.get("BACKUP_HOUR", "3"))

ADMIN_ID = 632294583  # Абдурроууф

# Рабочие склады компании
WAREHOUSE_NAMES = ["Бишкек", "Кара-Балта", "Каракол", "Манас"]

# Постоянные сотрудники: склад по умолчанию + доступ к другим складам.
# Применяется только при первом создании записи — дальше рулят команды админа.
STAFF = {
    632294583:  {"name": "Абдурроууф", "warehouse": "Бишкек",  "access": []},
    607647629:  {"name": "Жуми",       "warehouse": "Бишкек",  "access": ["Кара-Балта"]},
    5808155644: {"name": "Бека",       "warehouse": "Бишкек",  "access": ["Кара-Балта"]},
    1616348285: {"name": "Данияр",     "warehouse": "Каракол", "access": []},
    6525019701: {"name": "Азамат",     "warehouse": "Манас",   "access": []},
}

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

chat_histories = {}
HISTORY_LIMIT = 20

# Неподтверждённые заявки (накладные/приходы/перемещения) до нажатия кнопки.
PENDING = {}
PENDING_TTL = 15 * 60        # обычная заявка живёт 15 минут
APPROVAL_TTL = 24 * 60 * 60  # заявка на перемещение ждёт админа сутки
UNDO_WINDOW = 15 * 60        # сотрудник может отменить свою операцию 15 минут


def esc(s) -> str:
    return html.escape(str(s))


def money(n) -> str:
    return f"{fmt_num(n)} сом"


def is_admin(user_row) -> bool:
    return user_row["role"] == "admin"


def can_transfer(user_row) -> bool:
    return user_row["role"] in ("admin", "senior")


async def get_actor(update: Update):
    """Активный пользователь из базы; иначе отказ."""
    tg_user = update.effective_user
    if tg_user is None:
        return None
    row = db.get_user(tg_user.id)
    if row is None or not row["active"]:
        msg = update.effective_message
        if msg:
            await msg.reply_text("⛔ У вас нет доступа к этому боту.")
        return None
    return row


async def send_long(message, text: str):
    """Отправляет длинный текст кусками до 4000 символов (по строкам)."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 4000:
            await message.reply_text(chunk, parse_mode="HTML")
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        await message.reply_text(chunk, parse_mode="HTML")


def new_pending(payload: dict, ttl: int = PENDING_TTL) -> str:
    now = time.monotonic()
    for k in [k for k, v in PENDING.items() if now - v["created"] > v.get("ttl", PENDING_TTL)]:
        PENDING.pop(k, None)
    token = secrets.token_hex(6)
    payload["created"] = now
    payload["ttl"] = ttl
    PENDING[token] = payload
    return token


def get_pending(token: str):
    p = PENDING.get(token)
    if p is None:
        return None
    if time.monotonic() - p["created"] > p.get("ttl", PENDING_TTL):
        PENDING.pop(token, None)
        return None
    return p


def confirm_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Провести", callback_data=f"ok:{token}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}"),
    ]])


async def notify_admin(context, actor_row, text: str):
    """Дублирует админу каждую операцию, проведённую не им самим."""
    if actor_row["id"] == ADMIN_ID:
        return
    try:
        await context.bot.send_message(
            ADMIN_ID, f"🔔 <b>{esc(actor_row['name'])}</b>: {esc(text)}", parse_mode="HTML"
        )
    except Exception as e:
        log.warning("Не удалось уведомить админа: %s", e)


async def post_feed(context, wh_ids, text: str):
    """Постит сводку операции в чаты-ленты складов, которых она коснулась."""
    chats = set()
    for wh_id in wh_ids:
        wh = db.warehouse_by_id(wh_id)
        if wh and wh["feed_chat_id"]:
            chats.add(wh["feed_chat_id"])
    for chat_id in chats:
        try:
            await context.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:
            log.warning("Не удалось отправить в ленту %s: %s", chat_id, e)


async def feed_operation(context, op_id: int, actor_name: str, prefix: str, note: str = ""):
    op = db.get_operation(op_id)
    if op is None:
        return
    text = f"{prefix} <b>{esc(actor_name)}</b> — {esc(op['summary'])}"
    if note:
        text += f"\n{esc(note)}"
    await post_feed(context, db.operation_warehouses(op), text)


# ---------- Системный промпт ----------

def build_system_prompt(actor) -> str:
    own = db.warehouse_of(actor["id"])
    own_name = own["name"] if own else "—"
    visible = db.visible_warehouses(actor)
    visible_names = ", ".join(f"«{w['name']}»" for w in visible) or "—"
    transfer_allowed = can_transfer(actor)

    parts = []
    parts.append('Ты — помощник компании ОсОО «ВЕТОП», оптового поставщика ветеринарных препаратов. '
                 'Ты разбираешь сообщения сотрудников и превращаешь их в структурированные действия.')
    parts.append(f"Сегодня: {datetime.now(BISHKEK).strftime('%d.%m.%Y')}. "
                 f"Сотрудник: {actor['name']}. Его склад по умолчанию: «{own_name}». "
                 f"Склады, доступные сотруднику: {visible_names}.")
    parts.append("")
    parts.append("=== РЕЖИМ 1: ВОПРОСЫ О ПРАЙСЕ ===")
    parts.append("Отвечай на вопросы о ценах, фасовках, составе препаратов. Кратко и по делу, обычным текстом.")
    parts.append("")
    parts.append("=== РЕЖИМ 2: НАКЛАДНАЯ ===")
    parts.append("Когда сотрудник перечисляет клиента и товары — верни ТОЛЬКО JSON, без пояснений и без ```:")
    parts.append('{"action": "invoice", "client": "Имя контрагента", "warehouse": null, "debt": 0, "payment": 0, '
                 '"items": [{"name": "точное название из прайса", "volume": "фасовка", "qty": количество_в_штуках, '
                 '"box_qty": количество_коробок_или_null, "price": цена_из_прайса}]}')
    parts.append('- "warehouse": если сотрудник явно написал «со склада X» — подставь точное имя склада X, иначе null.')
    parts.append('- "debt": заполняй ТОЛЬКО если сотрудник сам явно указал долг числом (например «долг 31470»). '
                 'Старый долг существующих клиентов бот подставит из базы автоматически — не выдумывай его.')
    parts.append('- "payment": если вместе с накладной указан приход/оплата (например «приход 5000»), иначе 0.')
    parts.append("")
    parts.append("=== РЕЖИМ 3: ПРИХОД ДЕНЕГ (без товаров) ===")
    parts.append('Если сообщение — только оплата без товаров («Асан приход 5000», «Асан оплатил 3000»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "payment", "client": "Имя контрагента", "amount": сумма, "warehouse": null}')
    parts.append('- "warehouse": имя склада, если явно указан («со склада Ош»), иначе null.')
    all_whs = db.all_warehouses()
    emp_lines = []
    for u in db.list_users():
        w = db.warehouse_of(u["id"])
        if w:
            emp_lines.append(f"- {u['name']} → склад «{w['name']}»")
    parts.append("")
    parts.append("=== РЕЖИМ 4: ПРИХОД / ПЕРЕМЕЩЕНИЕ ТОВАРА ===")
    parts.append('Если сообщение — пополнение склада товаром или перемещение между складами '
                 '(например «Беке: Альтопен 100мл 2к», «на склад Манас: ...», '
                 '«с Бишкека на Каракол: ...», «нужно на Каракол: ...»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "transfer", "from_warehouse": null_или_имя_склада, "to_warehouse": "имя склада", '
                 '"items": [{"name": "...", "volume": "...", "qty": штук, "box_qty": коробок_или_null, "price": цена}]}')
    parts.append('- Если назван сотрудник — подставь имя склада этого сотрудника в to_warehouse.')
    parts.append('- "from_warehouse" заполняй только при явном перемещении «с X на Y»; '
                 'приход товара извне (с завода/базы) — null.')
    parts.append("Сотрудники и их склады по умолчанию:")
    parts.extend(emp_lines)
    parts.append("Все склады: " + ", ".join(f"«{w['name']}»" for w in all_whs))
    parts.append("ВАЖНО: если первое слово — имя сотрудника из списка выше, это перемещение (transfer), а не накладная.")
    if not transfer_allowed:
        parts.append("Примечание: перемещение между складами проводит только админ — "
                     "заявка сотрудника уйдёт ему на подтверждение, это нормально.")
    if transfer_allowed:
        parts.append("")
        parts.append("=== РЕЖИМ 5: МИНИМАЛЬНЫЕ ОСТАТКИ (только админ и старшие) ===")
        parts.append('Если сообщение задаёт неснижаемый остаток («минимум для Каракола: '
                     'Альтопен 100мл 20 шт, Дексатоп 50мл 10 шт»), верни ТОЛЬКО JSON:')
        parts.append('{"action": "set_min", "warehouse": "имя склада", '
                     '"items": [{"name": "точное название из прайса", "volume": "фасовка", "qty": число}]}')
        parts.append('- qty: 0 означает убрать порог для товара.')
    parts.append("")
    parts.append("=== РЕЖИМ 6: ИНВЕНТАРИЗАЦИЯ ===")
    parts.append('Если сообщение — пересчёт фактических остатков («инвентаризация: '
                 'Альтопен 100мл 18, Дексатоп 50мл 9», «инвентаризация Каракол: ...», '
                 '«фактический остаток: ...»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "inventory", "warehouse": null_или_имя_склада, '
                 '"items": [{"name": "точное название из прайса", "volume": "фасовка", "qty": фактическое_количество}]}')
    parts.append('- qty — сколько товара РЕАЛЬНО насчитали на складе (может быть 0).')
    parts.append('- Здесь коробки тоже переводи в штуки по прайсу.')
    parts.append("")
    parts.append("=== ВАЖНО: КОРОБКИ ===")
    parts.append('Сотрудники могут писать количество коробками: "1к", "2к", "3к" и т.д.')
    parts.append('В прайсе у каждого товара есть "шт/кор" — количество штук в одной коробке.')
    parts.append("Ты ОБЯЗАН перевести коробки в штуки: qty = количество_коробок × шт_в_коробке")
    parts.append("Примеры:")
    parts.append('- "Албенивер 200мл 1к" → 1 коробка × 50 шт/кор = qty: 50, box_qty: 1')
    parts.append('- "Альтопен 100мл 2к" → 2 коробки × 80 шт/кор = qty: 160, box_qty: 2')
    parts.append('- "Дексатоп 50мл 10 шт" → qty: 10, box_qty: null (просто штуки, без пересчёта)')
    parts.append('Другие обозначения коробок: "к", "кор", "коробка", "коробок", "box"')
    parts.append("")
    parts.append("=== ДРУГИЕ ПРАВИЛА ===")
    parts.append("- Цены бери СТРОГО из прайса")
    parts.append("- Если товар не найден — напиши текстом что не нашёл")
    parts.append("- Если что-то неясно — уточни у сотрудника текстом")
    parts.append("- Имя контрагента обязательно для накладной и прихода денег")
    parts.append("")
    parts.append("ПРАЙС-ЛИСТ (формат: №. Название | Фасовка | шт/кор | цена):")
    parts.append(prices.PRICE_LIST_TEXT)
    parts.append("")
    parts.append("Общайся на русском. Отвечай кратко.")
    return "\n".join(parts)


def extract_action(reply: str):
    """Достаёт из ответа модели первый JSON-объект с полем action."""
    decoder = json.JSONDecoder()
    i = reply.find("{")
    while i != -1:
        try:
            obj, _ = decoder.raw_decode(reply[i:])
            if isinstance(obj, dict) and "action" in obj:
                return obj
        except json.JSONDecodeError:
            pass
        i = reply.find("{", i + 1)
    return None


async def ask_claude(history: list, system: str) -> str:
    resp = await anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=system,
        messages=history,
    )
    return resp.content[0].text.strip()


# ---------- Разбор позиций ----------

def parse_items(raw_items: list):
    """Проверяет позиции от Claude и сопоставляет их с прайсом.

    Возвращает (items, warnings) или бросает ValueError с текстом для пользователя.
    """
    items, warnings = [], []
    for it in raw_items:
        name = str(it.get("name") or "").strip()
        volume = str(it.get("volume") or "").strip()
        try:
            qty = int(float(it.get("qty")))
            price = float(it.get("price"))
        except (TypeError, ValueError):
            raise ValueError(f"Не понял количество или цену у позиции «{name}».")
        if qty <= 0 or price < 0:
            raise ValueError(f"Странное количество/цена у позиции «{name}» — проверьте сообщение.")
        box_qty = it.get("box_qty")
        try:
            box_qty = int(box_qty) if box_qty else None
        except (TypeError, ValueError):
            box_qty = None
        product = prices.match_product(name, volume)
        if product is None:
            warnings.append(f"«{name} {volume}» не найден в прайсе — остаток по нему не изменится")
        items.append({
            "name": name, "volume": volume, "qty": qty, "price": price,
            "box_qty": box_qty, "product_id": product["id"] if product else None,
        })
    if not items:
        raise ValueError("Не распознал ни одной позиции.")
    return items, warnings


def resolve_warehouse(actor, wh_name: str):
    """Склад для операции: указанный (с проверкой доступа) или собственный.

    Возвращает (warehouse_row, error_text)."""
    if wh_name:
        wh = db.warehouse_by_name(wh_name)
        if wh is None:
            return None, f"Склад «{esc(wh_name)}» не найден. Доступные: " + \
                ", ".join(f"«{esc(w['name'])}»" for w in db.visible_warehouses(actor))
        if not db.can_use_warehouse(actor, wh["id"]):
            return None, f"⛔ У вас нет доступа к складу «{esc(wh['name'])}»."
        return wh, None
    own = db.warehouse_of(actor["id"])
    if own is None:
        return None, "У вас нет собственного склада — обратитесь к администратору."
    return own, None


# ---------- Накладная ----------

def invoice_summary(p) -> str:
    lines = ["📋 <b>Проверьте накладную</b>", f"🏬 Склад: <b>{esc(p['wh_name'])}</b>"]
    if p["client_id"]:
        c = db.client_get(p["client_id"])
        old_debt = c["debt"]
        lines.append(f"👤 Клиент: <b>{esc(c['name'])}</b> (текущий долг: {money(old_debt)})")
    else:
        old_debt = p["parsed_debt"]
        extra = f", начальный долг {money(old_debt)}" if old_debt else ""
        lines.append(f"👤 Клиент: <b>{esc(p['client_name'])}</b> — 🆕 новый{extra}")
    lines.append("")
    total = 0
    for i, it in enumerate(p["items"], 1):
        sub = it["qty"] * it["price"]
        total += sub
        box = f"{it['box_qty']} кор / " if it.get("box_qty") else ""
        lines.append(f"{i}. {esc(it['name'])} {esc(it['volume'])} — {box}{it['qty']} шт × "
                     f"{fmt_num(it['price'])} = <b>{money(sub)}</b>")
    lines.append("")
    lines.append(f"🧾 Сумма накладной: <b>{money(total)}</b>")
    if old_debt:
        lines.append(f"📌 Старый долг: {money(old_debt)}")
    if p["payment"]:
        lines.append(f"💵 Приход: {money(p['payment'])}")
    new_debt = old_debt + total - p["payment"]
    if new_debt <= 0 and p["payment"]:
        lines.append("🎉 Долг будет полностью погашен")
        if new_debt < 0:
            lines.append(f"(переплата {money(-new_debt)})")
    else:
        lines.append(f"📊 Итог долг: <b>{money(new_debt)}</b>")

    warns = list(p["warnings"])
    for it in p["items"]:
        if it.get("product_id"):
            have = db.stock_qty(p["wh_id"], it["product_id"])
            if have < it["qty"]:
                warns.append(f"{it['name']} {it['volume']}: на складе {have} шт, в накладной {it['qty']} шт")
    if warns:
        lines.append("")
        for w in warns:
            lines.append(f"⚠️ {esc(w)}")
    lines.append("")
    lines.append("Провести накладную?")
    return "\n".join(lines)


def commit_invoice(p):
    """Проводит накладную: клиент, остатки, долг, журнал. Возвращает детали для PDF."""
    total = sum(it["qty"] * it["price"] for it in p["items"])
    cid = p["client_id"]
    create = None
    if cid is None:
        create = (p["wh_id"], p["client_name"], p["parsed_debt"])
        old_debt = p["parsed_debt"]
        client_label = p["client_name"]
    else:
        c = db.client_get(cid)
        old_debt = c["debt"]
        client_label = c["name"]
    stock_deltas = [(p["wh_id"], it["product_id"], -it["qty"])
                    for it in p["items"] if it.get("product_id")]
    debt_delta = total - p["payment"]
    summary = f"Накладная: {client_label} — {fmt_num(total)} сом (склад {p['wh_name']})"
    if p["payment"]:
        summary += f", приход {fmt_num(p['payment'])} сом"
    extra = {
        "items": [{k: it[k] for k in ("name", "volume", "qty", "price", "box_qty")} for it in p["items"]],
        "total": total, "payment": p["payment"], "old_debt": old_debt,
    }
    op_id, _ = db.commit_operation(
        p["user_id"], "invoice", p["wh_id"], cid, summary,
        stock_deltas, [(cid, debt_delta)], extra, create_client=create,
    )
    return op_id, client_label, old_debt, total, summary


async def send_invoice_pdf(context, chat_id, client_label, p, old_debt, total, draft=False):
    pdf = generate_pdf_invoice(
        client_label, p["items"], total,
        prev_debt=old_debt, payment=p["payment"], is_payment=p["payment"] > 0,
        warehouse_name=p["wh_name"], draft=draft, watermark=DRAFT_WATERMARK,
    )
    date_str = datetime.now(BISHKEK).strftime("%d%m%Y")
    marked = draft and DRAFT_WATERMARK
    prefix = "черновик" if marked else "накладная"
    filename = f"{prefix}_{safe_filename(client_label)}_{date_str}.pdf"
    caption = ("📝 Черновик — не проведено, остатки и долги не изменены"
               if draft else f"📄 Накладная для {client_label}")
    await context.bot.send_document(
        chat_id=chat_id, document=InputFile(pdf, filename=filename), caption=caption
    )


async def start_invoice(update, context, actor, data, draft=False):
    if not draft and transition_blocked(actor):
        await update.message.reply_text(TRANSITION_HINT)
        return
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    client_name = str(data.get("client") or "").strip()
    if not client_name:
        await update.message.reply_text("Не понял имя клиента — напишите ещё раз, имя обязательно.")
        return
    try:
        items, warnings = parse_items(data.get("items") or [])
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    payment = float(data.get("payment") or 0)
    parsed_debt = float(data.get("debt") or 0)

    if draft:
        c = db.client_exact(wh["id"], client_name)
        old_debt = c["debt"] if c else parsed_debt
        total = sum(it["qty"] * it["price"] for it in items)
        p = {"items": items, "payment": payment, "wh_name": wh["name"]}
        await send_invoice_pdf(context, update.effective_chat.id,
                               c["name"] if c else client_name, p, old_debt, total, draft=True)
        return

    payload = {
        "kind": "invoice", "user_id": actor["id"], "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"],
        "client_name": client_name, "client_id": None,
        "items": items, "warnings": warnings,
        "payment": payment, "parsed_debt": parsed_debt,
    }

    exact = db.client_exact(wh["id"], client_name)
    if exact:
        payload["client_id"] = exact["id"]
        token = new_pending(payload)
        await update.message.reply_text(invoice_summary(payload), parse_mode="HTML",
                                        reply_markup=confirm_kb(token))
        return

    token = new_pending(payload)
    candidates = db.fuzzy_clients(wh["id"], client_name)
    rows = []
    for c in candidates:
        rows.append([InlineKeyboardButton(
            f"👤 {c['name']} (долг {fmt_num(c['debt'])})",
            callback_data=f"pk:{token}:{c['id']}")])
    rows.append([InlineKeyboardButton(f"➕ Новый клиент: {client_name[:30]}",
                                      callback_data=f"nw:{token}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")])
    if candidates:
        text = (f"Клиент «<b>{esc(client_name)}</b>» не найден на складе «{esc(wh['name'])}».\n"
                f"Возможно, вы имели в виду:")
    else:
        text = (f"Клиент «<b>{esc(client_name)}</b>» не найден на складе «{esc(wh['name'])}».\n"
                f"Создать нового?")
    await update.message.reply_text(text, parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(rows))


# ---------- Приход денег ----------

def payment_summary(p) -> str:
    c = db.client_get(p["client_id"])
    old_debt = c["debt"]
    remainder = old_debt - p["amount"]
    lines = [
        "💵 <b>Подтвердите приход</b>",
        f"🏬 Склад: <b>{esc(p['wh_name'])}</b>",
        f"👤 Клиент: <b>{esc(c['name'])}</b>",
        f"⚠️ Текущий долг: {money(old_debt)}",
        f"✅ Оплата: <b>{money(p['amount'])}</b>",
    ]
    if remainder <= 0:
        lines.append("🎉 Долг будет полностью погашен"
                     + (f" (переплата {money(-remainder)})" if remainder < 0 else ""))
    else:
        lines.append(f"📌 Остаток долга: <b>{money(remainder)}</b>")
    lines.append("")
    lines.append("Провести оплату?")
    return "\n".join(lines)


def payment_receipt(client_name, old_debt, amount) -> str:
    remainder = old_debt - amount
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "💵 <b>ПРИХОД — ВЕТОП</b>",
        f"📅 {date_str}",
        f"👤 Контрагент: <b>{esc(client_name)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"⚠️ Долг: <b>{money(old_debt)}</b>",
        f"✅ Оплата: <b>{money(amount)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if remainder <= 0:
        lines.append("🎉 Долг полностью погашен!")
    else:
        lines.append(f"📌 Остаток долга: <b>{money(remainder)}</b>")
    return "\n".join(lines)


def commit_payment(p):
    c = db.client_get(p["client_id"])
    old_debt = c["debt"]
    summary = f"Приход: {c['name']} — {fmt_num(p['amount'])} сом (склад {p['wh_name']})"
    op_id, _ = db.commit_operation(
        p["user_id"], "payment", p["wh_id"], p["client_id"], summary,
        [], [(p["client_id"], -p["amount"])],
        {"amount": p["amount"], "old_debt": old_debt},
    )
    return op_id, c["name"], old_debt, summary


async def start_payment(update, context, actor, data):
    if transition_blocked(actor):
        await update.message.reply_text(TRANSITION_HINT)
        return
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    client_name = str(data.get("client") or "").strip()
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if not client_name or amount <= 0:
        await update.message.reply_text("Не понял клиента или сумму. Пример: «Асан приход 5000».")
        return

    payload = {
        "kind": "payment", "user_id": actor["id"], "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"],
        "client_name": client_name, "client_id": None, "amount": amount,
    }

    exact = db.client_exact(wh["id"], client_name)
    if exact:
        payload["client_id"] = exact["id"]
        token = new_pending(payload)
        await update.message.reply_text(payment_summary(payload), parse_mode="HTML",
                                        reply_markup=confirm_kb(token))
        return

    candidates = db.fuzzy_clients(wh["id"], client_name)
    if not candidates:
        await update.message.reply_text(
            f"❌ Клиент «{esc(client_name)}» не найден на складе «{esc(wh['name'])}». "
            f"Приход можно принять только от существующего клиента.",
            parse_mode="HTML")
        return
    token = new_pending(payload)
    rows = [[InlineKeyboardButton(f"👤 {c['name']} (долг {fmt_num(c['debt'])})",
                                  callback_data=f"pk:{token}:{c['id']}")]
            for c in candidates]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")])
    await update.message.reply_text(
        f"Клиент «<b>{esc(client_name)}</b>» не найден на складе «{esc(wh['name'])}».\n"
        f"Возможно, вы имели в виду:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


# ---------- Перемещение / приход товара ----------

def transfer_summary(p) -> str:
    header = "📦 <b>Приход товара</b>" if not p["from_wh_id"] else "📦 <b>Перемещение товара</b>"
    lines = [header]
    if p.get("requester_name"):
        lines.append(f"✍️ Заявка от: <b>{esc(p['requester_name'])}</b>")
    if p["from_wh_id"]:
        lines.append(f"🏬 Со склада: <b>{esc(p['from_wh_name'])}</b>")
    lines.append(f"🏬 На склад: <b>{esc(p['wh_name'])}</b>")
    lines.append("")
    for i, it in enumerate(p["items"], 1):
        box = f"{it['box_qty']} кор / " if it.get("box_qty") else ""
        lines.append(f"{i}. {esc(it['name'])} {esc(it['volume'])} — {box}{it['qty']} шт")
    warns = list(p["warnings"])
    if p["from_wh_id"]:
        for it in p["items"]:
            if it.get("product_id"):
                have = db.stock_qty(p["from_wh_id"], it["product_id"])
                if have < it["qty"]:
                    warns.append(f"{it['name']} {it['volume']}: на складе «{p['from_wh_name']}» "
                                 f"{have} шт, перемещаете {it['qty']} шт")
    if warns:
        lines.append("")
        for w in warns:
            lines.append(f"⚠️ {esc(w)}")
    lines.append("")
    lines.append("Провести?")
    return "\n".join(lines)


def commit_transfer(p):
    stock_deltas = []
    for it in p["items"]:
        if not it.get("product_id"):
            continue
        stock_deltas.append((p["wh_id"], it["product_id"], it["qty"]))
        if p["from_wh_id"]:
            stock_deltas.append((p["from_wh_id"], it["product_id"], -it["qty"]))
    n = len(p["items"])
    if p["from_wh_id"]:
        summary = f"Перемещение: {p['from_wh_name']} → {p['wh_name']} ({n} поз.)"
    else:
        summary = f"Приход товара на склад {p['wh_name']} ({n} поз.)"
    extra = {"items": [{k: it[k] for k in ("name", "volume", "qty", "box_qty")} for it in p["items"]]}
    if p.get("approver_id"):
        extra["approved_by"] = p["approver_id"]
    op_id, _ = db.commit_operation(
        p["user_id"], "transfer", p["wh_id"], None, summary, stock_deltas, [], extra,
    )
    return op_id, summary


async def start_transfer(update, context, actor, data):
    if transition_blocked(actor):
        await update.message.reply_text(TRANSITION_HINT)
        return
    to_name = str(data.get("to_warehouse") or "").strip()
    to_wh = db.warehouse_by_name(to_name) if to_name else None
    if to_wh is None:
        await update.message.reply_text(
            f"Склад «{esc(to_name)}» не найден. Склады: "
            + ", ".join(f"«{esc(w['name'])}»" for w in db.all_warehouses()),
            parse_mode="HTML")
        return
    from_name = str(data.get("from_warehouse") or "").strip()
    from_wh = None
    if from_name:
        from_wh = db.warehouse_by_name(from_name)
        if from_wh is None:
            await update.message.reply_text(f"Склад «{esc(from_name)}» не найден.", parse_mode="HTML")
            return
        if from_wh["id"] == to_wh["id"]:
            await update.message.reply_text("Склад-источник и склад-получатель совпадают.")
            return
    # Приход извне (без склада-источника) — только админ или старший.
    if from_wh is None and not can_transfer(actor):
        await update.message.reply_text(
            "⛔ Приход товара извне может внести только админ или старший.\n"
            "Если вам нужен товар с другого склада — напишите перемещение: "
            "«с Бишкека на Каракол: ...» — заявка уйдёт админу на подтверждение.")
        return
    try:
        items, warnings = parse_items(data.get("items") or [])
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    matched = [it for it in items if it.get("product_id")]
    if not matched:
        await update.message.reply_text("⚠️ Ни один товар не распознан по прайсу — приход не создан.")
        return

    payload = {
        "kind": "transfer", "user_id": actor["id"], "chat_id": update.effective_chat.id,
        "wh_id": to_wh["id"], "wh_name": to_wh["name"],
        "from_wh_id": from_wh["id"] if from_wh else None,
        "from_wh_name": from_wh["name"] if from_wh else None,
        "items": items, "warnings": warnings,
    }

    # Перемещение между складами подтверждает ТОЛЬКО админ.
    if from_wh is not None and not is_admin(actor):
        payload["approver_id"] = ADMIN_ID
        payload["requester_name"] = actor["name"]
        token = new_pending(payload, ttl=APPROVAL_TTL)
        try:
            await context.bot.send_message(
                ADMIN_ID, transfer_summary(payload), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Провести", callback_data=f"ok:{token}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{token}"),
                ]]))
        except Exception:
            log.exception("Не удалось отправить заявку админу")
            PENDING.pop(token, None)
            await update.message.reply_text("⚠️ Не удалось отправить заявку админу. Попробуйте позже.")
            return
        await update.message.reply_text(
            f"📨 Заявка на перемещение «{esc(payload['from_wh_name'])}» → "
            f"«{esc(payload['wh_name'])}» отправлена админу на подтверждение.\n"
            f"Я сообщу вам результат.", parse_mode="HTML")
        return

    token = new_pending(payload)
    await update.message.reply_text(transfer_summary(payload), parse_mode="HTML",
                                    reply_markup=confirm_kb(token))


# ---------- Инвентаризация ----------

def inventory_summary(p) -> str:
    """Расхождения считаются по текущей базе — на момент показа/подтверждения."""
    lines = [f"📋 <b>Инвентаризация — склад «{esc(p['wh_name'])}»</b>"]
    if p.get("requester_name"):
        lines.append(f"✍️ Провёл: <b>{esc(p['requester_name'])}</b>")
    lines.append("")
    diffs = 0
    for it in p["items"]:
        base = db.stock_qty(p["wh_id"], it["product_id"])
        delta = it["fact"] - base
        if delta == 0:
            continue
        sign = f"+{delta}" if delta > 0 else str(delta)
        lines.append(f"• {esc(it['name'])} {esc(it['volume'])}: по базе <b>{base}</b>, "
                     f"по факту <b>{it['fact']}</b> ({sign})")
        diffs += 1
    matched = len(p["items"]) - diffs
    if matched:
        lines.append(f"✅ Без расхождений: {matched} поз.")
    for w in p.get("warnings", []):
        lines.append(f"⚠️ {esc(w)}")
    lines.append("")
    if diffs:
        lines.append("Провести корректировку остатков?")
    else:
        lines.append("Всё сходится — корректировка не нужна.")
    return "\n".join(lines)


def commit_inventory(p):
    """Ставит остатки в фактические значения (дельты считаются на момент проведения)."""
    stock_deltas = []
    detail = []
    for it in p["items"]:
        base = db.stock_qty(p["wh_id"], it["product_id"])
        delta = it["fact"] - base
        if delta:
            stock_deltas.append((p["wh_id"], it["product_id"], delta))
            detail.append({"name": it["name"], "volume": it["volume"],
                           "base": base, "fact": it["fact"]})
    if not stock_deltas:
        return None, "расхождений уже нет"
    summary = f"Инвентаризация: склад {p['wh_name']} ({len(stock_deltas)} корректир.)"
    extra = {"items": detail}
    if p.get("approver_id"):
        extra["approved_by"] = p["approver_id"]
    op_id, _ = db.commit_operation(
        p["user_id"], "inventory", p["wh_id"], None, summary, stock_deltas, [], extra)
    return op_id, summary


async def start_inventory(update, context, actor, data):
    if transition_blocked(actor):
        await update.message.reply_text(TRANSITION_HINT)
        return
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    items, warnings = [], []
    for it in (data.get("items") or []):
        name = str(it.get("name") or "").strip()
        volume = str(it.get("volume") or "").strip()
        try:
            fact = int(float(it.get("qty")))
        except (TypeError, ValueError):
            fact = -1
        product = prices.match_product(name, volume)
        if product is None or fact < 0:
            warnings.append(f"«{name} {volume}» не распознан — пропущен")
            continue
        items.append({"product_id": product["id"], "name": product["name"],
                      "volume": product["volume"], "fact": fact})
    if not items:
        await update.message.reply_text("⚠️ Ни один товар не распознан по прайсу.")
        return

    payload = {
        "kind": "inventory", "user_id": actor["id"], "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"], "items": items, "warnings": warnings,
    }
    has_diffs = any(db.stock_qty(wh["id"], it["product_id"]) != it["fact"] for it in items)
    if not has_diffs:
        await update.message.reply_text(
            f"✅ Инвентаризация склада «{esc(wh['name'])}»: всё сходится "
            f"({len(items)} поз.), корректировка не нужна.", parse_mode="HTML")
        await notify_admin(context, actor,
                           f"инвентаризация склада {wh['name']}: всё сходится ({len(items)} поз.)")
        return

    # Корректировку остатков подтверждает только админ.
    if not is_admin(actor):
        payload["approver_id"] = ADMIN_ID
        payload["requester_name"] = actor["name"]
        token = new_pending(payload, ttl=APPROVAL_TTL)
        try:
            await context.bot.send_message(
                ADMIN_ID, inventory_summary(payload), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Провести", callback_data=f"ok:{token}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{token}"),
                ]]))
        except Exception:
            log.exception("Не удалось отправить инвентаризацию админу")
            PENDING.pop(token, None)
            await update.message.reply_text("⚠️ Не удалось отправить админу. Попробуйте позже.")
            return
        await update.message.reply_text(
            f"📨 Результаты инвентаризации склада «{esc(wh['name'])}» отправлены админу "
            f"на подтверждение. Я сообщу результат.", parse_mode="HTML")
        return

    token = new_pending(payload)
    await update.message.reply_text(inventory_summary(payload), parse_mode="HTML",
                                    reply_markup=confirm_kb(token))


# ---------- Минимальные остатки ----------

def low_stock_hits(stock_deltas):
    """Товары, у которых списание пробило порог минимума (было >= min, стало < min)."""
    hits = []
    for wh_id, pid, delta in stock_deltas:
        if delta >= 0:
            continue
        m = db.min_stock_get(wh_id, pid)
        if not m:
            continue
        new_qty = db.stock_qty(wh_id, pid)
        old_qty = new_qty - delta
        if new_qty < m <= old_qty:
            hits.append((wh_id, pid, new_qty, m))
    return hits


async def alert_low_stock(context, stock_deltas):
    """Предупреждает админа и чат-ленту склада о пробитии минимума."""
    for wh_id, pid, new_qty, m in low_stock_hits(stock_deltas):
        wh = db.warehouse_by_id(wh_id)
        p = prices.BY_ID.get(pid)
        if wh is None or p is None:
            continue
        text = (f"📉 <b>Заканчивается товар</b> на складе «{esc(wh['name'])}»:\n"
                f"{esc(p['name'])} {esc(p['volume'])} — осталось <b>{new_qty} шт</b> "
                f"(минимум {m}).\nПора пополнить перемещением или приходом.")
        targets = {ADMIN_ID}
        if wh["feed_chat_id"]:
            targets.add(wh["feed_chat_id"])
        for t in targets:
            try:
                await context.bot.send_message(t, text, parse_mode="HTML")
            except Exception as e:
                log.warning("Не удалось отправить предупреждение о минимуме: %s", e)


def set_min_summary(p) -> str:
    lines = [f"📉 <b>Минимальные остатки — склад «{esc(p['wh_name'])}»</b>", ""]
    for it in p["items"]:
        if it["qty"] > 0:
            lines.append(f"• {esc(it['name'])} {esc(it['volume'])} — минимум <b>{it['qty']} шт</b>")
        else:
            lines.append(f"• {esc(it['name'])} {esc(it['volume'])} — <i>убрать порог</i>")
    lines.append("")
    lines.append("Когда остаток опустится ниже минимума, бот предупредит вас и чат склада.")
    lines.append("Сохранить?")
    return "\n".join(lines)


async def start_set_min(update, context, actor, data):
    if not can_transfer(actor):
        await update.message.reply_text("⛔ Минимальные остатки настраивает админ или старший.")
        return
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    items, missing = [], []
    for it in (data.get("items") or []):
        name = str(it.get("name") or "").strip()
        volume = str(it.get("volume") or "").strip()
        try:
            qty = int(float(it.get("qty")))
        except (TypeError, ValueError):
            qty = -1
        product = prices.match_product(name, volume)
        if product is None or qty < 0:
            missing.append(f"{name} {volume}")
            continue
        items.append({"product_id": product["id"], "name": product["name"],
                      "volume": product["volume"], "qty": qty})
    if not items:
        await update.message.reply_text("⚠️ Ни один товар не распознан по прайсу.")
        return
    payload = {
        "kind": "set_min", "user_id": actor["id"], "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"], "items": items,
    }
    token = new_pending(payload)
    text = set_min_summary(payload)
    if missing:
        text = "⚠️ Не найдены в прайсе: " + ", ".join(esc(x) for x in missing) + "\n\n" + text
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=confirm_kb(token))


async def minstock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if arg:
        wh = db.warehouse_by_name(arg)
        if wh is None or not db.can_use_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(arg)}» не найден или нет доступа.", parse_mode="HTML")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
    lines = []
    for wh in whs:
        mmap = db.min_stock_map(wh["id"])
        lines.append(f"📉 <b>Минимумы — склад «{esc(wh['name'])}»</b>")
        if not mmap:
            lines.append("— пороги не заданы —")
        else:
            smap = db.stock_map(wh["id"])
            for p in prices.PRICE_LIST_DATA:
                m = mmap.get(p["id"])
                if m is None:
                    continue
                have = smap.get(p["id"], 0)
                mark = " ⚠️ ниже минимума!" if have < m else ""
                lines.append(f"{esc(p['name'])} {esc(p['volume'])} — {have} шт "
                             f"(минимум {m}){mark}")
        lines.append("")
    lines.append("Задать: напишите «минимум для Каракола: Альтопен 100мл 20 шт» (админ/старший)")
    await send_long(update.message, "\n".join(lines))


# ---------- Кнопки ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    actor = db.get_user(q.from_user.id)
    if actor is None or not actor["active"]:
        await q.answer("⛔ Нет доступа", show_alert=True)
        return
    try:
        parts = q.data.split(":")
        kind, token = parts[0], parts[1]
    except (AttributeError, IndexError):
        await q.answer()
        return

    p = get_pending(token)
    if p is None:
        await q.answer("Заявка устарела")
        try:
            await q.edit_message_text("⌛ Заявка устарела или уже обработана. Отправьте сообщение заново.")
        except Exception:
            pass
        return
    if p.get("approver_id"):
        # Заявка на перемещение: решает только назначенный подтверждающий.
        if q.from_user.id != p["approver_id"]:
            await q.answer("Эту заявку подтверждает только админ", show_alert=True)
            return
    elif q.from_user.id != p["user_id"] and not is_admin(actor):
        await q.answer("Это не ваша операция", show_alert=True)
        return

    if kind == "no":
        PENDING.pop(token, None)
        await q.answer()
        if p.get("approver_id"):
            await q.edit_message_text("❌ Заявка отклонена.")
            if p["kind"] == "inventory":
                what = f"инвентаризацию склада «{esc(p['wh_name'])}»"
            else:
                what = (f"перемещение «{esc(p.get('from_wh_name') or '')}» → "
                        f"«{esc(p['wh_name'])}»")
            try:
                await context.bot.send_message(
                    p["chat_id"], f"❌ Админ отклонил {what}.", parse_mode="HTML")
            except Exception:
                log.warning("Не удалось уведомить заявителя об отклонении")
        else:
            await q.edit_message_text("❌ Отменено. Ничего не изменено.")
        return

    if kind == "pk":  # выбран существующий клиент
        p["client_id"] = int(parts[2])
        await q.answer()
        summary = invoice_summary(p) if p["kind"] == "invoice" else payment_summary(p)
        await q.edit_message_text(summary, parse_mode="HTML", reply_markup=confirm_kb(token))
        return

    if kind == "nw":  # создаём нового клиента (только накладная)
        p["client_id"] = None
        await q.answer()
        await q.edit_message_text(invoice_summary(p), parse_mode="HTML", reply_markup=confirm_kb(token))
        return

    if kind == "ok":
        PENDING.pop(token, None)  # сразу, чтобы не провести дважды
        await q.answer()
        try:
            if p["kind"] == "invoice":
                op_id, client_label, old_debt, total, summary = commit_invoice(p)
                await q.edit_message_text(f"✅ Накладная №{op_id} проведена.")
                await send_invoice_pdf(context, p["chat_id"], client_label, p, old_debt, total)
                await notify_admin(context, actor, summary)
                await feed_operation(context, op_id, db.get_user(p["user_id"])["name"], "🧾")
                await alert_low_stock(context, [
                    (p["wh_id"], it["product_id"], -it["qty"])
                    for it in p["items"] if it.get("product_id")])
            elif p["kind"] == "payment":
                op_id, client_label, old_debt, summary = commit_payment(p)
                await q.edit_message_text(
                    f"✅ Оплата проведена (операция №{op_id}).\n\n"
                    + payment_receipt(client_label, old_debt, p["amount"]),
                    parse_mode="HTML")
                await notify_admin(context, actor, summary)
                await feed_operation(context, op_id, db.get_user(p["user_id"])["name"], "💵")
            elif p["kind"] == "transfer":
                op_id, summary = commit_transfer(p)
                await q.edit_message_text(f"✅ {esc(summary)} — проведено (операция №{op_id}).",
                                          parse_mode="HTML")
                note = ""
                if p.get("approver_id"):
                    note = f"Заявка: {p.get('requester_name', '')}, подтвердил админ"
                    try:
                        await context.bot.send_message(
                            p["chat_id"],
                            f"✅ Ваша заявка проведена (операция №{op_id}): {esc(summary)}",
                            parse_mode="HTML")
                    except Exception:
                        log.warning("Не удалось уведомить заявителя")
                else:
                    await notify_admin(context, actor, summary)
                actor_name = db.get_user(p["user_id"])["name"]
                await feed_operation(context, op_id, actor_name, "📦", note)
                if p["from_wh_id"]:
                    await alert_low_stock(context, [
                        (p["from_wh_id"], it["product_id"], -it["qty"])
                        for it in p["items"] if it.get("product_id")])
            elif p["kind"] == "inventory":
                op_id, summary = commit_inventory(p)
                if op_id is None:
                    await q.edit_message_text("✅ Расхождений уже нет — корректировка не нужна.")
                else:
                    await q.edit_message_text(
                        f"✅ {esc(summary)} — проведено (операция №{op_id}).", parse_mode="HTML")
                    if p.get("approver_id"):
                        try:
                            await context.bot.send_message(
                                p["chat_id"],
                                f"✅ Админ подтвердил вашу инвентаризацию "
                                f"(операция №{op_id}). Остатки скорректированы.",
                                parse_mode="HTML")
                        except Exception:
                            log.warning("Не удалось уведомить заявителя")
                    else:
                        await notify_admin(context, actor, summary)
                    actor_name = db.get_user(p["user_id"])["name"]
                    note = "Подтвердил админ" if p.get("approver_id") else ""
                    await feed_operation(context, op_id, actor_name, "📋", note)
            elif p["kind"] == "set_min":
                for it in p["items"]:
                    db.set_min_stock(p["wh_id"], it["product_id"], it["qty"])
                await q.edit_message_text(
                    f"✅ Минимальные остатки для склада «{esc(p['wh_name'])}» сохранены "
                    f"({len(p['items'])} поз.). Посмотреть: /minstock", parse_mode="HTML")
        except Exception as e:
            log.exception("Ошибка проведения операции")
            await context.bot.send_message(p["chat_id"], f"⚠️ Ошибка при проведении: {e}")
        return

    await q.answer()


# ---------- Сообщения ----------

async def process_text(update, context, actor, text, draft=False):
    chat_id = update.effective_chat.id
    history = chat_histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})
    chat_histories[chat_id] = history[-HISTORY_LIMIT:]

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        reply = await ask_claude(chat_histories[chat_id], build_system_prompt(actor))
    except Exception as e:
        log.exception("Claude API error")
        await update.message.reply_text(f"⚠️ Ошибка при обращении к ИИ: {e}")
        return
    chat_histories[chat_id].append({"role": "assistant", "content": reply})
    chat_histories[chat_id] = chat_histories[chat_id][-HISTORY_LIMIT:]

    data = extract_action(reply)
    if data is None:
        await update.message.reply_text(reply)
        return
    action = data.get("action")
    try:
        if action == "invoice":
            await start_invoice(update, context, actor, data, draft=draft)
        elif action == "payment":
            await start_payment(update, context, actor, data)
        elif action == "transfer":
            await start_transfer(update, context, actor, data)
        elif action == "set_min":
            await start_set_min(update, context, actor, data)
        elif action == "inventory":
            await start_inventory(update, context, actor, data)
        else:
            await update.message.reply_text(reply)
    except Exception as e:
        log.exception("Ошибка обработки действия %s", action)
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


DRAFT_RE = re.compile(r"^черновик[:,\s]*", re.IGNORECASE)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not update.message.text:
        return
    # В группах бот только публикует ленту операций — сообщения не обрабатывает.
    if update.effective_chat.type != "private":
        return
    actor = await get_actor(update)
    if actor is None:
        return
    text = update.message.text.strip()
    draft = False
    m = DRAFT_RE.match(text)
    if m:
        draft = True
        text = text[m.end():].strip()
        if not text:
            await update.message.reply_text(
                "После слова «черновик» напишите накладную.\n"
                "Пример: черновик: Асан, Албенивер 200мл 1к")
            return
    await process_text(update, context, actor, text, draft=draft)


# ---------- Команды ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    chat_histories[update.effective_chat.id] = []
    own = db.warehouse_of(actor["id"])
    lines = [
        "👋 Привет! Я бот компании <b>ВЕТОП</b> 🐄💊",
        f"Ваш склад: <b>{esc(own['name']) if own else '—'}</b>",
        "",
    ]
    if transition_blocked(actor):
        lines += [
            "⏳ <b>Сейчас переходный период</b> — работаем только черновиками:",
            "<i>черновик: Асан, Албенивер 200мл 1к, долг 31470</i>",
            "Придёт PDF-накладная для клиента, база не меняется.",
            "Ещё можно спрашивать цены: <i>сколько стоит Альтопен 1л?</i> и /price",
            "",
            "Остальные функции откроются после настройки базы:",
        ]
    lines += [
        "📋 <b>Накладная</b> — просто напишите:",
        "<i>Асан, Албенивер 200мл 1к, Дексатоп 50мл 5 шт</i>",
        "С другого склада: <i>со склада Ош: Асан, ...</i>",
        "📝 <b>Черновик</b> (без проведения): <i>черновик: Асан, ...</i>",
        "💵 <b>Оплата</b>: <i>Асан приход 5000</i>",
        "",
        "📌 <b>Команды:</b>",
        "/stock — остатки склада",
        "/debts — долги клиентов",
        "/report — отчёт (можно: /report неделя, /report Бишкек месяц)",
        "/olddebts — кто давно не платил (можно: /olddebts 45)",
        "/client Имя — карточка клиента (долг, история)",
        "/act Имя — акт сверки в PDF",
        "/price — прайс-лист",
        "/log — последние операции",
        "/undo — отменить свою последнюю операцию (15 минут)",
        "/clear — очистить историю разговора",
    ]
    lines += [
        "",
        "📦 <b>Перемещение товара</b> (заявка, подтверждает админ):",
        "<i>с Бишкека на Каракол: Альтопен 100мл 2к</i>",
        "📋 <b>Инвентаризация</b> (корректировку подтверждает админ):",
        "<i>инвентаризация: Альтопен 100мл 18, Дексатоп 50мл 9</i>",
    ]
    if can_transfer(actor):
        lines += [
            "📦 <b>Приход товара извне</b>: <i>Беке: Альтопен 100мл 2к</i> "
            "или <i>на склад Манас: ...</i>",
        ]
    if is_admin(actor):
        lines += [
            "",
            "👑 <b>Админ:</b>",
            "/users — сотрудники и склады",
            "/warehouses — склады, сотрудники, ленты",
            "/add ID Имя — добавить сотрудника",
            "/remove ID — убрать сотрудника",
            "/setwh Имя Склад — назначить сотруднику склад",
            "/grant Имя | /ungrant Имя — право прихода товара",
            "/access Имя Склад | /noaccess Имя Склад — доступ к складу",
            "/whadd Имя | /whname Старое Новое — создать/переименовать склад",
            "/feed Склад... (в групповом чате) — подключить ленту операций",
            "/backup — прислать копию базы сейчас (и так каждый день в 03:00)",
            "/minstock — пороги «заканчивается товар» "
            "(задать: «минимум для Каракола: Альтопен 100мл 20 шт»)",
            "/undo N — отменить любую операцию",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    chat_histories[update.effective_chat.id] = []
    await update.message.reply_text("История очищена ✅")


async def show_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    lines = ["📋 <b>Прайс ВЕТОП</b>", ""]
    for p in prices.PRICE_LIST_DATA:
        lines.append(f"{p['id']}. {esc(p['name'])} | {esc(p['volume'])} | "
                     f"{p['box']} шт/кор | <b>{p['price']} сом</b>")
    await send_long(update.message, "\n".join(lines))


async def show_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if arg and arg.lower() != "all":
        wh = db.warehouse_by_name(arg)
        if wh is None or not db.can_use_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(arg)}» не найден или нет доступа.", parse_mode="HTML")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
    lines = []
    for wh in whs:
        smap = db.stock_map(wh["id"])
        lines.append(f"📦 <b>Склад «{esc(wh['name'])}»</b>")
        any_row = False
        for p in prices.PRICE_LIST_DATA:
            qty = smap.get(p["id"], 0)
            if qty:
                mark = " ⚠️" if qty < 0 else ""
                lines.append(f"{esc(p['name'])} {esc(p['volume'])} — <b>{qty} шт</b>{mark}")
                any_row = True
        if not any_row:
            lines.append("— пусто —")
        lines.append("")
    await send_long(update.message, "\n".join(lines))


async def show_debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if arg and arg.lower() != "all":
        wh = db.warehouse_by_name(arg)
        if wh is None or not db.can_use_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(arg)}» не найден или нет доступа.", parse_mode="HTML")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
    lines = []
    grand_total = 0
    for wh in whs:
        clients = [c for c in db.clients_of(wh["id"]) if c["debt"] != 0]
        lines.append(f"🏬 <b>Склад «{esc(wh['name'])}»</b>")
        if not clients:
            lines.append("✅ Долгов нет")
        else:
            total = 0
            for c in sorted(clients, key=lambda r: -r["debt"]):
                if c["debt"] > 0:
                    lines.append(f"👤 {esc(c['name'])}: <b>{money(c['debt'])}</b>")
                    total += c["debt"]
                else:
                    lines.append(f"👤 {esc(c['name'])}: переплата {money(-c['debt'])}")
            lines.append(f"💰 Итого: <b>{money(total)}</b>")
            grand_total += total
        lines.append("")
    if len(whs) > 1:
        lines.append(f"💰💰 Всего по складам: <b>{money(grand_total)}</b>")
    await send_long(update.message, "\n".join(lines))


async def payment_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /payment Имя Сумма\nПример: /payment Асан 5000\n"
            "Или просто напишите: «Асан приход 5000»")
        return
    try:
        amount = float(args[-1])
    except ValueError:
        await update.message.reply_text("❌ Неверная сумма. Пример: /payment Асан 5000")
        return
    name = " ".join(args[:-1])
    await start_payment(update, context, actor, {"client": name, "amount": amount})


PERIODS = {
    "день": (0, "за сегодня"), "сегодня": (0, "за сегодня"),
    "неделя": (6, "за 7 дней"), "неделю": (6, "за 7 дней"),
    "месяц": (29, "за 30 дней"),
}


def build_report(warehouses, days_back: int, label: str) -> str:
    """Отчёт по складам: продажи, деньги, долги, топ товаров."""
    start = (datetime.now(BISHKEK) - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    ops = db.operations_since(start.isoformat(timespec="seconds"))
    lines = []
    grand_sales = grand_money = 0
    for wh in warehouses:
        inv_data, pay_sum, transfers = [], 0.0, 0
        for op in ops:
            try:
                data = json.loads(op["data"])
            except (ValueError, TypeError):
                continue
            if op["type"] == "invoice" and op["warehouse_id"] == wh["id"]:
                inv_data.append(data)
            elif op["type"] == "payment" and op["warehouse_id"] == wh["id"]:
                pay_sum += data.get("amount", 0)
            elif op["type"] == "transfer" and wh["id"] in db.operation_warehouses(op):
                transfers += 1
        lines.append(f"📊 <b>«{esc(wh['name'])}» {esc(label)}</b>")
        if not inv_data and not pay_sum and not transfers:
            lines.append("— операций не было —")
            lines.append("")
            continue
        sales = sum(d.get("total", 0) for d in inv_data)
        inv_payments = sum(d.get("payment", 0) for d in inv_data)
        money_recv = inv_payments + pay_sum
        debt_added = sales - inv_payments
        lines.append(f"🧾 Накладных: {len(inv_data)} на <b>{money(sales)}</b>")
        lines.append(f"💵 Принято денег: <b>{money(money_recv)}</b>")
        if debt_added > 0:
            lines.append(f"📈 Выдано в долг: {money(debt_added)}")
        if transfers:
            lines.append(f"📦 Приходов/перемещений товара: {transfers}")
        top = {}
        for d in inv_data:
            for it in d.get("items", []):
                key = f"{it.get('name', '')} {it.get('volume', '')}".strip()
                top[key] = top.get(key, 0) + (it.get("qty") or 0)
        if top:
            lines.append("🔝 Топ товаров:")
            for i, (name, qty) in enumerate(
                    sorted(top.items(), key=lambda x: -x[1])[:5], 1):
                lines.append(f"  {i}. {esc(name)} — {qty} шт")
        lines.append("")
        grand_sales += sales
        grand_money += money_recv
    if len(warehouses) > 1:
        lines.append(f"💰 <b>Итого {esc(label)}: продажи {money(grand_sales)}, "
                     f"деньги {money(grand_money)}</b>")
    return "\n".join(lines).strip()


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    days_back, label = 0, "за сегодня"
    wh_words = []
    for word in (context.args or []):
        w = word.lower()
        if w in PERIODS:
            days_back, label = PERIODS[w]
        else:
            wh_words.append(word)
    if wh_words:
        wh = db.warehouse_by_name(" ".join(wh_words))
        if wh is None or not db.can_use_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(' '.join(wh_words))}» не найден или нет доступа.\n"
                "Использование: /report [Склад] [день|неделя|месяц]",
                parse_mode="HTML")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
    await send_long(update.message, build_report(whs, days_back, label))


async def send_evening_summaries(bot):
    """Вечерняя сводка дня в каждый чат-ленту (если были операции)."""
    by_chat = {}
    for wh in db.all_warehouses():
        if wh["feed_chat_id"]:
            by_chat.setdefault(wh["feed_chat_id"], []).append(wh)
    for chat_id, whs in by_chat.items():
        text = build_report(whs, 0, "за сегодня")
        if "Накладных" not in text and "Приходов" not in text:
            continue  # день без операций — не шумим
        try:
            await bot.send_message(chat_id, "🌆 Итоги дня\n\n" + text, parse_mode="HTML")
        except Exception as e:
            log.warning("Не удалось отправить сводку в %s: %s", chat_id, e)


async def evening_summary_loop(app):
    while True:
        now = datetime.now(BISHKEK)
        target = now.replace(hour=SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await send_evening_summaries(app.bot)
        except Exception:
            log.exception("Ошибка вечерней сводки")


def find_client_visible(actor, name: str):
    """Ищет клиента по точному имени среди складов, доступных сотруднику."""
    for wh in db.visible_warehouses(actor):
        c = db.client_exact(wh["id"], name)
        if c:
            return c, wh
    return None, None


def client_statement(cid: int):
    """История долга клиента: строки акта сверки + начальный долг.

    Возвращает (rows, start_debt): rows = [(дата, документ, товар+, оплата−, долг_после)].
    """
    ops = db.client_operations(cid)
    client = db.client_get(cid)
    total_delta = 0.0
    parsed = []
    for op in ops:
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        delta = sum(d for c_id, d in data.get("debt_deltas", []) if c_id == cid)
        total_delta += delta
        parsed.append((op, data, delta))
    start_debt = client["debt"] - total_delta  # долг, внесённый при создании клиента
    rows = []
    balance = start_debt
    for op, data, delta in parsed:
        try:
            date_str = datetime.fromisoformat(op["ts"]).strftime("%d.%m.%Y")
        except ValueError:
            date_str = op["ts"][:10]
        balance += delta
        if op["type"] == "invoice":
            plus = data.get("total", 0)
            minus = data.get("payment", 0)
            doc = f"Накладная №{op['id']}"
        elif op["type"] == "payment":
            plus = 0
            minus = data.get("amount", 0)
            doc = f"Оплата №{op['id']}"
        else:
            plus, minus = max(delta, 0), max(-delta, 0)
            doc = f"Операция №{op['id']}"
        rows.append((date_str, doc, plus, minus, balance))
    return rows, start_debt


async def resolve_client_arg(update, actor, args, usage: str):
    """Общий разбор аргумента-имени для /client и /act."""
    if not args:
        await update.message.reply_text(usage)
        return None, None
    name = " ".join(args)
    c, wh = find_client_visible(actor, name)
    if c is None:
        hints = []
        for w in db.visible_warehouses(actor):
            hints += [f"{x['name']} ({w['name']})" for x in db.fuzzy_clients(w["id"], name)]
        msg = f"❌ Клиент «{esc(name)}» не найден."
        if hints:
            msg += "\nВозможно: " + ", ".join(esc(h) for h in hints[:5])
        await update.message.reply_text(msg, parse_mode="HTML")
        return None, None
    return c, wh


async def client_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    c, wh = await resolve_client_arg(update, actor, context.args,
                                     "Использование: /client Имя\nПример: /client Асан")
    if c is None:
        return
    rows, start_debt = client_statement(c["id"])
    now = datetime.now(BISHKEK)
    lines = [f"👤 <b>{esc(c['name'])}</b> · склад «{esc(wh['name'])}»"]
    if c["debt"] > 0:
        lines.append(f"⚠️ Текущий долг: <b>{money(c['debt'])}</b>")
    elif c["debt"] < 0:
        lines.append(f"💚 Переплата: <b>{money(-c['debt'])}</b>")
    else:
        lines.append("✅ Долга нет")
    last_pay = None
    for op in reversed(db.client_operations(c["id"])):
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        if op["type"] == "payment" or (op["type"] == "invoice" and data.get("payment", 0) > 0):
            last_pay = op["ts"]
            break
    if last_pay:
        try:
            days = (now - datetime.fromisoformat(last_pay)).days
            lines.append(f"💵 Последняя оплата: {days} дн. назад")
        except ValueError:
            pass
    elif c["debt"] > 0:
        lines.append("💵 Оплат ещё не было")
    if start_debt:
        lines.append(f"📌 Начальный долг (до бота): {money(start_debt)}")
    if rows:
        lines.append("")
        lines.append(f"🗒 <b>Последние операции ({min(len(rows), 15)} из {len(rows)}):</b>")
        for date_str, doc, plus, minus, balance in rows[-15:]:
            parts = [f"{date_str} {doc}:"]
            if plus:
                parts.append(f"+{fmt_num(plus)}")
            if minus:
                parts.append(f"−{fmt_num(minus)}")
            parts.append(f"→ долг {fmt_num(balance)}")
            lines.append(esc(" ".join(parts)))
    lines.append("")
    lines.append(f"📄 Акт сверки в PDF: /act {esc(c['name'])}")
    await send_long(update.message, "\n".join(lines))


async def act_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    c, wh = await resolve_client_arg(update, actor, context.args,
                                     "Использование: /act Имя\nПример: /act Асан")
    if c is None:
        return
    rows, start_debt = client_statement(c["id"])
    if rows:
        period = f"{rows[0][0]} — {datetime.now(BISHKEK).strftime('%d.%m.%Y')}"
    else:
        period = "операций не было"
    pdf = generate_act_pdf(c["name"], wh["name"], rows, start_debt, c["debt"], period)
    filename = f"акт_сверки_{safe_filename(c['name'])}_{datetime.now(BISHKEK).strftime('%d%m%Y')}.pdf"
    await update.message.reply_document(
        document=InputFile(pdf, filename=filename),
        caption=f"📄 Акт сверки: {c['name']} — долг {fmt_num(c['debt'])} сом")


def build_overdue(warehouses, min_days: int) -> str:
    """Список должников, не плативших min_days и более дней."""
    now = datetime.now(BISHKEK)
    lines = [f"⏰ <b>Давно не платили ({min_days}+ дней):</b>"]
    found = 0
    total = 0.0
    for wh in warehouses:
        rows = []
        for c, ref_ts, has_paid in db.debtors_with_age(wh["id"]):
            if ref_ts is None:
                days = None  # операций нет вовсе — долг занесён вручную давно
            else:
                try:
                    days = (now - datetime.fromisoformat(ref_ts)).days
                except ValueError:
                    days = None
            if days is not None and days < min_days:
                continue
            rows.append((days, c, has_paid))
        if not rows:
            continue
        lines.append("")
        lines.append(f"🏬 <b>Склад «{esc(wh['name'])}»</b>")
        for days, c, has_paid in sorted(rows, key=lambda r: -(r[0] if r[0] is not None else 10**6)):
            age = f"{days} дн. без оплат" if days is not None else "давно (дата неизвестна)"
            mark = "" if has_paid else " — ни одной оплаты"
            lines.append(f"👤 {esc(c['name'])}: <b>{money(c['debt'])}</b> · {age}{mark}")
            found += 1
            total += c["debt"]
    if not found:
        return ""
    lines.append("")
    lines.append(f"💰 Итого зависших долгов: <b>{money(total)}</b>")
    return "\n".join(lines)


async def olddebts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    min_days = DEBT_ALERT_DAYS
    if context.args:
        try:
            min_days = max(1, int(context.args[0]))
        except ValueError:
            await update.message.reply_text(
                "Использование: /olddebts [дней]\nПример: /olddebts 45")
            return
    text = build_overdue(db.visible_warehouses(actor), min_days)
    if not text:
        await update.message.reply_text(
            f"✅ Нет клиентов без оплат дольше {min_days} дней.")
        return
    await send_long(update.message, text)


async def send_debt_alerts(bot):
    """Еженедельное напоминание о старых долгах: админу — всё, сотруднику — своё."""
    admin_text = build_overdue(db.all_warehouses(), DEBT_ALERT_DAYS)
    if admin_text:
        try:
            await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML")
        except Exception as e:
            log.warning("Не удалось отправить напоминание админу: %s", e)
    for u in db.list_users():
        if u["id"] == ADMIN_ID:
            continue
        text = build_overdue(db.visible_warehouses(u), DEBT_ALERT_DAYS)
        if not text:
            continue
        try:
            await bot.send_message(u["id"], text + "\n\nПора напомнить клиентам об оплате 📞",
                                   parse_mode="HTML")
        except Exception as e:
            log.warning("Не удалось отправить напоминание %s: %s", u["name"], e)


async def send_backup(bot):
    """Копия базы админу в личку."""
    import tempfile
    stamp = datetime.now(BISHKEK).strftime("%d%m%Y_%H%M")
    path = os.path.join(tempfile.gettempdir(), f"vetop_backup_{stamp}.db")
    try:
        db.backup_to(path)
        with open(path, "rb") as f:
            await bot.send_document(
                ADMIN_ID,
                document=InputFile(f, filename=f"vetop_backup_{stamp}.db"),
                caption=("💾 Резервная копия базы. Храните — если что-то случится "
                         "с сервером, по этому файлу восстановим весь учёт."))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def daily_backup_loop(app):
    while True:
        now = datetime.now(BISHKEK)
        target = now.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await send_backup(app.bot)
        except Exception:
            log.exception("Ошибка ежедневного бэкапа")


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    await send_backup(context.bot)
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("💾 Копия отправлена админу.")


async def weekly_debt_loop(app):
    """Каждый понедельник в 10:00 по Бишкеку."""
    while True:
        now = datetime.now(BISHKEK)
        days_ahead = (0 - now.weekday()) % 7
        target = (now + timedelta(days=days_ahead)).replace(
            hour=10, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=7)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await send_debt_alerts(app.bot)
        except Exception:
            log.exception("Ошибка напоминания о долгах")


async def show_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    try:
        n = min(int(context.args[0]), 30) if context.args else 10
    except ValueError:
        n = 10
    ops = db.recent_operations(n, None if is_admin(actor) else actor["id"])
    if not ops:
        await update.message.reply_text("Журнал пуст.")
        return
    lines = ["🗒 <b>Последние операции:</b>"]
    for op in ops:
        try:
            ts = datetime.fromisoformat(op["ts"]).strftime("%d.%m %H:%M")
        except ValueError:
            ts = op["ts"]
        u = db.get_user(op["user_id"])
        who = u["name"] if u else str(op["user_id"])
        cancelled = " ❌ <i>отменена</i>" if op["status"] == "cancelled" else ""
        lines.append(f"№{op['id']} · {ts} · {esc(who)}: {esc(op['summary'])}{cancelled}")
    await send_long(update.message, "\n".join(lines))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    if context.args and is_admin(actor):
        try:
            op = db.get_operation(int(context.args[0]))
        except ValueError:
            await update.message.reply_text("Использование: /undo или /undo НомерОперации")
            return
        if op is None:
            await update.message.reply_text("Операция не найдена.")
            return
    else:
        op = db.last_done_operation(actor["id"])
        if op is None:
            await update.message.reply_text("Нет операций для отмены.")
            return
        if not is_admin(actor):
            try:
                approved_by = json.loads(op["data"]).get("approved_by")
            except (ValueError, TypeError):
                approved_by = None
            if approved_by:
                await update.message.reply_text(
                    "⛔ Это перемещение подтверждал админ — отменить его может только он.")
                return
            try:
                age = (datetime.now(BISHKEK) - datetime.fromisoformat(op["ts"])).total_seconds()
            except ValueError:
                age = UNDO_WINDOW + 1
            if age > UNDO_WINDOW:
                await update.message.reply_text(
                    "⛔ Отменить операцию можно только в течение 15 минут. "
                    "Обратитесь к администратору.")
                return
    ok, msg = db.cancel_operation(op["id"])
    if not ok:
        await update.message.reply_text(f"⚠️ {msg}")
        return
    await update.message.reply_text(
        f"↩️ Операция №{op['id']} отменена: {esc(msg)}\n"
        f"Остатки и долги возвращены как было.", parse_mode="HTML")
    await notify_admin(context, actor, f"отменил операцию №{op['id']}: {msg}")
    cancelled = db.get_operation(op["id"])
    if cancelled:
        await post_feed(context, db.operation_warehouses(cancelled),
                        f"↩️ <b>{esc(actor['name'])}</b> отменил операцию №{op['id']}: "
                        f"{esc(msg)}")


# ---------- Админ-команды ----------

async def _require_admin(update):
    actor = await get_actor(update)
    if actor is None:
        return None
    if not is_admin(actor):
        await update.message.reply_text("⛔ Только администратор.")
        return None
    return actor


async def add_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    args = context.args
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text("Использование: /add 123456789 Имя")
        return
    uid = int(args[0])
    name = " ".join(args[1:])
    db.add_user(uid, name)
    await update.message.reply_text(
        f"✅ Сотрудник <b>{esc(name)}</b> (<code>{uid}</code>) добавлен.\n"
        f"Теперь назначьте ему склад: /setwh {esc(name)} Бишкек",
        parse_mode="HTML")


async def remove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /remove 123456789")
        return
    uid = int(context.args[0])
    if uid == ADMIN_ID:
        await update.message.reply_text("❌ Нельзя удалить администратора.")
        return
    db.deactivate_user(uid)
    await update.message.reply_text(f"✅ Пользователь <code>{uid}</code> отключён. "
                                    f"Его склад и история сохранены.", parse_mode="HTML")


async def list_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    lines = ["👥 <b>Сотрудники:</b>"]
    role_icons = {"admin": "👑", "senior": "⭐", "employee": "👤"}
    for u in db.list_users():
        wh = db.warehouse_of(u["id"])
        extra_access = db.access_warehouses(u["id"])
        line = (f"{role_icons.get(u['role'], '👤')} <b>{esc(u['name'])}</b> "
                f"(<code>{u['id']}</code>) — склад «{esc(wh['name']) if wh else '⚠️ не назначен'}»")
        if extra_access:
            line += " + доступ: " + ", ".join(f"«{esc(w['name'])}»" for w in extra_access)
        lines.append(line)
    await send_long(update.message, "\n".join(lines))


async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    if not context.args:
        await update.message.reply_text("Использование: /grant Имя (или ID) — право вносить приход товара")
        return
    u = db.user_by_ref(" ".join(context.args))
    if u is None:
        await update.message.reply_text("Сотрудник не найден.")
        return
    if u["role"] == "admin":
        await update.message.reply_text("Это администратор — у него и так все права.")
        return
    db.set_role(u["id"], "senior")
    await update.message.reply_text(
        f"⭐ <b>{esc(u['name'])}</b> теперь старший: может вносить приход и перемещение товара.",
        parse_mode="HTML")


async def ungrant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    if not context.args:
        await update.message.reply_text("Использование: /ungrant Имя (или ID)")
        return
    u = db.user_by_ref(" ".join(context.args))
    if u is None:
        await update.message.reply_text("Сотрудник не найден.")
        return
    if u["role"] == "admin":
        await update.message.reply_text("Нельзя понизить администратора.")
        return
    db.set_role(u["id"], "employee")
    await update.message.reply_text(f"✅ <b>{esc(u['name'])}</b> — обычный сотрудник.", parse_mode="HTML")


async def access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /access Имя Склад\nПример: /access Бека Ош")
        return
    u = db.user_by_ref(args[0])
    if u is None:
        await update.message.reply_text(f"Сотрудник «{esc(args[0])}» не найден.", parse_mode="HTML")
        return
    wh = db.warehouse_by_name(" ".join(args[1:]))
    if wh is None:
        await update.message.reply_text(
            "Склад не найден. Склады: " + ", ".join(f"«{esc(w['name'])}»" for w in db.all_warehouses()),
            parse_mode="HTML")
        return
    if wh["owner_id"] == u["id"]:
        await update.message.reply_text("Это его собственный склад — доступ уже есть.")
        return
    db.grant_access(u["id"], wh["id"])
    await update.message.reply_text(
        f"✅ <b>{esc(u['name'])}</b> получил доступ к складу «{esc(wh['name'])}».", parse_mode="HTML")


async def noaccess_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /noaccess Имя Склад")
        return
    u = db.user_by_ref(args[0])
    if u is None:
        await update.message.reply_text(f"Сотрудник «{esc(args[0])}» не найден.", parse_mode="HTML")
        return
    wh = db.warehouse_by_name(" ".join(args[1:]))
    if wh is None:
        await update.message.reply_text("Склад не найден.")
        return
    db.revoke_access(u["id"], wh["id"])
    await update.message.reply_text(
        f"✅ У <b>{esc(u['name'])}</b> больше нет доступа к складу «{esc(wh['name'])}».", parse_mode="HTML")


async def whname_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /whname СтароеИмя НовоеИмя\nПример: /whname Манас Сокулук")
        return
    wh = db.warehouse_by_name(args[0])
    if wh is None:
        await update.message.reply_text(f"Склад «{esc(args[0])}» не найден.", parse_mode="HTML")
        return
    new_name = " ".join(args[1:])
    if db.rename_warehouse(wh["id"], new_name):
        await update.message.reply_text(f"✅ Склад теперь называется «{esc(new_name)}».",
                                        parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Такое имя склада уже занято.")


async def setwh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /setwh Имя Склад\nПример: /setwh Бека Бишкек")
        return
    u = db.user_by_ref(args[0])
    if u is None:
        await update.message.reply_text(f"Сотрудник «{esc(args[0])}» не найден.", parse_mode="HTML")
        return
    wh = db.warehouse_by_name(" ".join(args[1:]))
    if wh is None:
        await update.message.reply_text(
            "Склад не найден. Склады: " + ", ".join(f"«{esc(w['name'])}»" for w in db.all_warehouses()),
            parse_mode="HTML")
        return
    db.set_default_warehouse(u["id"], wh["id"])
    await update.message.reply_text(
        f"✅ Склад по умолчанию для <b>{esc(u['name'])}</b> — «{esc(wh['name'])}».",
        parse_mode="HTML")


async def whadd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    if not context.args:
        await update.message.reply_text("Использование: /whadd ИмяСклада")
        return
    name = " ".join(context.args)
    if db.create_warehouse(name):
        await update.message.reply_text(f"✅ Склад «{esc(name)}» создан.", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Склад с таким именем уже есть.")


async def warehouses_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    lines = ["🏬 <b>Склады:</b>"]
    for w in db.all_warehouses():
        workers = [u["name"] for u in db.list_users()
                   if (db.warehouse_of(u["id"]) or {"id": None})["id"] == w["id"]]
        feed = f" · лента: {esc(w['feed_chat_title'])}" if w["feed_chat_id"] else " · лента не подключена"
        who = (", ".join(esc(n) for n in workers)) if workers else "—"
        lines.append(f"• <b>{esc(w['name'])}</b> — сотрудники: {who}{feed}")
    lines.append("")
    lines.append("Подключить ленту: добавьте бота в чат и напишите там /feed ИмяСклада")
    await send_long(update.message, "\n".join(lines))


async def feed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    if not is_admin(actor):
        await update.message.reply_text("⛔ Только администратор.")
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text(
            "Эту команду нужно писать в групповом чате, куда бот будет слать ленту операций.\n"
            "Добавьте бота в чат и напишите там: /feed Бишкек Кара-Балта")
        return
    if not context.args:
        linked = db.warehouses_of_feed(chat.id)
        if linked:
            await update.message.reply_text(
                "К этому чату привязаны склады: "
                + ", ".join(f"«{esc(w['name'])}»" for w in linked)
                + "\nОтвязать: /nofeed", parse_mode="HTML")
        else:
            await update.message.reply_text(
                "Использование: /feed ИмяСклада [ещё склады]\n"
                "Пример: /feed Бишкек Кара-Балта")
        return
    done, missing = [], []
    for name in context.args:
        wh = db.warehouse_by_name(name)
        if wh is None:
            missing.append(name)
        else:
            db.set_feed_chat(wh["id"], chat.id, chat.title or str(chat.id))
            done.append(wh["name"])
    text = ""
    if done:
        text += ("✅ В этот чат будет приходить лента операций складов: "
                 + ", ".join(f"«{esc(n)}»" for n in done))
    if missing:
        text += "\n⚠️ Не найдены склады: " + ", ".join(esc(n) for n in missing)
    await update.message.reply_text(text.strip(), parse_mode="HTML")


async def nofeed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    if not is_admin(actor):
        await update.message.reply_text("⛔ Только администратор.")
        return
    names = db.unlink_feed_chat(update.effective_chat.id)
    if names:
        await update.message.reply_text(
            "✅ Лента отключена для складов: " + ", ".join(f"«{esc(n)}»" for n in names),
            parse_mode="HTML")
    else:
        await update.message.reply_text("К этому чату склады не привязаны.")


async def invoice_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    chat_histories[update.effective_chat.id] = []
    await update.message.reply_text(
        "📋 <b>Создание накладной</b>\n\n"
        "Напишите в свободной форме:\n\n"
        "<b>Штуками:</b>\n<i>Асан, Альтопен 100мл 10 шт, Дексатоп 50мл 5 шт</i>\n\n"
        "<b>Коробками (к = коробка):</b>\n<i>Асан, Албенивер 200мл 1к, Топмектин 100мл 2к</i>\n\n"
        "<b>С другого склада:</b>\n<i>со склада Ош: Асан, Албенивер 200мл 1к</i>\n\n"
        "💡 Старый долг клиента бот подставит сам. Перед проведением покажу накладную на проверку.",
        parse_mode="HTML")


async def draft_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    if not context.args:
        await update.message.reply_text(
            "📝 Черновик — накладная без проведения (остатки и долги не меняются).\n"
            "Пример: /draft Асан, Албенивер 200мл 1к\n"
            "Или просто: черновик: Асан, Албенивер 200мл 1к")
        return
    await process_text(update, context, actor, " ".join(context.args), draft=True)


async def _post_init(app):
    app.create_task(evening_summary_loop(app))
    app.create_task(weekly_debt_loop(app))
    app.create_task(daily_backup_loop(app))


if __name__ == "__main__":
    db.init(ADMIN_ID, WAREHOUSE_NAMES, STAFF)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("price", show_price))
    app.add_handler(CommandHandler("stock", show_stock))
    app.add_handler(CommandHandler("debts", show_debts))
    app.add_handler(CommandHandler("payment", payment_cmd))
    app.add_handler(CommandHandler("invoice", invoice_hint))
    app.add_handler(CommandHandler("draft", draft_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("olddebts", olddebts_cmd))
    app.add_handler(CommandHandler("client", client_cmd))
    app.add_handler(CommandHandler("act", act_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("minstock", minstock_cmd))
    app.add_handler(CommandHandler("log", show_log))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("add", add_user_cmd))
    app.add_handler(CommandHandler("remove", remove_user_cmd))
    app.add_handler(CommandHandler("users", list_users_cmd))
    app.add_handler(CommandHandler("grant", grant_cmd))
    app.add_handler(CommandHandler("ungrant", ungrant_cmd))
    app.add_handler(CommandHandler("access", access_cmd))
    app.add_handler(CommandHandler("noaccess", noaccess_cmd))
    app.add_handler(CommandHandler("whname", whname_cmd))
    app.add_handler(CommandHandler("setwh", setwh_cmd))
    app.add_handler(CommandHandler("whadd", whadd_cmd))
    app.add_handler(CommandHandler("warehouses", warehouses_cmd))
    app.add_handler(CommandHandler("feed", feed_cmd))
    app.add_handler(CommandHandler("nofeed", nofeed_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, handle_message))
    print("Бот запущен...")
    app.run_polling(stop_signals=None)
