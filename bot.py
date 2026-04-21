import os
import json
import anthropic
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

chat_histories = {}

# Долги контрагентов: {"Имя": сумма}
debts = {}

# Доступ
ADMIN_ID = 632294583
allowed_users = set([ADMIN_ID])

def is_allowed(user_id: int) -> bool:
    return user_id in allowed_users

async def check_access(update) -> bool:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return False
    return True

PRICE_LIST_DATA = [
    {"id": 1,  "name": "АЛЬТОПЕН-ФОРТЕ (альбен, левамизоль суспензия)",          "volume": "100 мл",        "box": 80,  "price": 160},
    {"id": 2,  "name": "АЛЬТОПЕН-ФОРТЕ (альбен, левамизоль суспензия)",          "volume": "1 л",           "box": 12,  "price": 1060},
    {"id": 3,  "name": "АЛЬТОПЕН-ФОРТЕ ПЛЮС (альбен, оксиклозанид)",             "volume": "100 мл",        "box": 80,  "price": 125},
    {"id": 4,  "name": "АЛЬТОПЕН-ФОРТЕ ПЛЮС (альбен, оксиклозанид)",             "volume": "500 мл",        "box": 20,  "price": 480},
    {"id": 5,  "name": "АЛЬТОПЕН-ФОРТЕ ПЛЮС (альбен, оксиклозанид)",             "volume": "1 л",           "box": 12,  "price": 860},
    {"id": 6,  "name": "АЛЬТОПЕН (альбендазол 10%)",                             "volume": "100 мл",        "box": 80,  "price": 90},
    {"id": 7,  "name": "АЛЬТОПЕН (альбендазол 10%)",                             "volume": "500 мл",        "box": 20,  "price": 370},
    {"id": 8,  "name": "АЛЬТОПЕН (альбендазол 10%)",                             "volume": "1 л",           "box": 12,  "price": 570},
    {"id": 9,  "name": "АЛЬТОПЕН 600 мг (альбен таблетки)",                      "volume": "55 таб",        "box": 40,  "price": 270},
    {"id": 10, "name": "АЛЬТОПЕР (альбен, ивермек суспензия)",                   "volume": "100 мл",        "box": 80,  "price": 140},
    {"id": 11, "name": "АЛЬТОПЕР (альбен, ивермек суспензия)",                   "volume": "200 мл",        "box": 50,  "price": 260},
    {"id": 12, "name": "АЛЬТОПЕР (альбен, ивермек суспензия)",                   "volume": "500 мл",        "box": 20,  "price": 580},
    {"id": 13, "name": "АЛЬТОПЕР (альбен, ивермек суспензия)",                   "volume": "1 л",           "box": 12,  "price": 950},
    {"id": 14, "name": "БУТАТОП (бутафосфан, витамин B12)",                      "volume": "50 мл",         "box": 100, "price": 180},
    {"id": 15, "name": "БУТАТОП (бутафосфан, витамин B12)",                      "volume": "100 мл",        "box": 80,  "price": 290},
    {"id": 16, "name": "ДЕКСАТОП (дексаметазон)",                                "volume": "50 мл",         "box": 100, "price": 180},
    {"id": 17, "name": "ДЕКСАТОП (дексаметазон)",                                "volume": "100 мл",        "box": 80,  "price": 280},
    {"id": 18, "name": "КЛОЗАТОП (оксфендазол, оксиклозанид суспензия)",         "volume": "100 мл",        "box": 80,  "price": 140},
    {"id": 19, "name": "КЛОЗАТОП (оксфендазол, оксиклозанид суспензия)",         "volume": "500 мл",        "box": 20,  "price": 580},
    {"id": 20, "name": "КЛОЗАТОП (оксфендазол, оксиклозанид суспензия)",         "volume": "1 л",           "box": 12,  "price": 960},
    {"id": 21, "name": "КЛОЗАТОП-ФОРТЕ (оксфендазол, оксиклозанид табл)",        "volume": "100 таб",       "box": 50,  "price": 790},
    {"id": 22, "name": "МУЛЬТИ-ВИТАТОП (мультивитамин)",                         "volume": "50 мл",         "box": 100, "price": 130},
    {"id": 23, "name": "МУЛЬТИ-ВИТАТОП (мультивитамин)",                         "volume": "100 мл",        "box": 80,  "price": 230},
    {"id": 24, "name": "ОКСИТОП (окситетрациклин 20%)",                          "volume": "10 мл (10 фл)", "box": 40,  "price": 230},
    {"id": 25, "name": "ОКСИТОП (окситетрациклин 20%)",                          "volume": "50 мл",         "box": 100, "price": 100},
    {"id": 26, "name": "ОКСИТОП (окситетрациклин 20%)",                          "volume": "100 мл",        "box": 80,  "price": 160},
    {"id": 27, "name": "ПЕНСТОП-G (пенстреп 20:25)",                             "volume": "50 мл",         "box": 100, "price": 250},
    {"id": 28, "name": "ПЕНСТОП-G (пенстреп 20:25)",                             "volume": "100 мл",        "box": 80,  "price": 440},
    {"id": 29, "name": "ПРАЗИМЕКТОП (ивермек, празиквантел)",                    "volume": "100 мл",        "box": 80,  "price": 170},
    {"id": 30, "name": "ПРАЗИМЕКТОП (ивермек, празиквантел)",                    "volume": "200 мл",        "box": 50,  "price": 320},
    {"id": 31, "name": "ПРАЗИМЕКТОП (ивермек, празиквантел)",                    "volume": "500 мл",        "box": 20,  "price": 700},
    {"id": 32, "name": "ПРАЗИМЕКТОП (ивермек, празиквантел)",                    "volume": "1 л",           "box": 12,  "price": 1350},
    {"id": 33, "name": "ТИЛТОПЗИН-200 (тилозин 20%)",                            "volume": "50 мл",         "box": 100, "price": 170},
    {"id": 34, "name": "ТИЛТОПЗИН-200 (тилозин 20%)",                            "volume": "100 мл",        "box": 80,  "price": 270},
    {"id": 35, "name": "ТОПМЕКТИН (ивермектин 1%)",                              "volume": "10 мл (10 фл)", "box": 40,  "price": 200},
    {"id": 36, "name": "ТОПМЕКТИН (ивермектин 1%)",                              "volume": "50 мл",         "box": 100, "price": 90},
    {"id": 37, "name": "ТОПМЕКТИН (ивермектин 1%)",                              "volume": "100 мл",        "box": 80,  "price": 130},
    {"id": 38, "name": "ТОП-ГЕЛМИЦИД (альбен, оксиклозанид гранулы)",           "volume": "100 г",         "box": 60,  "price": 240},
    {"id": 39, "name": "ТОП-ГЕЛМИЦИД (альбен, оксиклозанид гранулы)",           "volume": "500 г",         "box": 20,  "price": 980},
    {"id": 40, "name": "ТОПЗАНТЕЛ (клозантел 5%)",                               "volume": "50 мл",         "box": 100, "price": 140},
    {"id": 41, "name": "ТОПЗАНТЕЛ (клозантел 5%)",                               "volume": "100 мл",        "box": 80,  "price": 240},
    {"id": 42, "name": "ТОПЗАНТЕЛ (клозантел 5%)",                               "volume": "250 мл",        "box": 40,  "price": 560},
    {"id": 43, "name": "ТОПЛАМОКС (амоксициллин 15%)",                           "volume": "100 мл",        "box": 80,  "price": 270},
    {"id": 44, "name": "ТОПМЕКТИН ГЕЛЬ (ивермек гель)",                          "volume": "30 мл",         "box": 100, "price": 220},
    {"id": 45, "name": "ТОП-СУПЕРВИТ (витаминный премикс)",                      "volume": "100 г",         "box": 60,  "price": 100},
    {"id": 46, "name": "ТОП-СУПЕРВИТ (витаминный премикс)",                      "volume": "200 г",         "box": 50,  "price": 170},
    {"id": 47, "name": "ТОП-СУПЕРВИТ (витаминный премикс)",                      "volume": "500 г",         "box": 20,  "price": 360},
    {"id": 48, "name": "ТОП-СУПЕРВИТ (витаминный премикс)",                      "volume": "1 кг",          "box": 10,  "price": 650},
    {"id": 49, "name": "ТОПФЛУНЕКС (флунексин меглюмин 5%)",                    "volume": "100 мл",        "box": 80,  "price": 550},
    {"id": 50, "name": "ТРИВИТОП-AD3E (витамины AD3E)",                          "volume": "50 мл",         "box": 100, "price": 120},
    {"id": 51, "name": "ТРИВИТОП-AD3E (витамины AD3E)",                          "volume": "100 мл",        "box": 80,  "price": 190},
    {"id": 52, "name": "ФЛУМЕТОП (акарвил, флюметрин 2%)",                      "volume": "100 мл",        "box": 100, "price": 330},
    {"id": 53, "name": "ЭНРОТОП (энрофлоксацин 10%)",                            "volume": "10 мл (10 фл)", "box": 40,  "price": 250},
    {"id": 54, "name": "ЭНРОТОП (энрофлоксацин 10%)",                            "volume": "50 мл",         "box": 100, "price": 110},
    {"id": 55, "name": "ЭНРОТОП (энрофлоксацин 10%)",                            "volume": "100 мл",        "box": 80,  "price": 190},
    {"id": 56, "name": "ЭНРОТОП ФОРТЕ (энрофлоксацин 10%) оральный",             "volume": "1 л",           "box": 12,  "price": 1250},
    {"id": 57, "name": "БУТАСТИМ (бутафосфан, витамин B12)",                     "volume": "100 мл",        "box": 50,  "price": 450},
    {"id": 58, "name": "ЭЛЕОВИТ (витамин)",                                      "volume": "100 мл",        "box": 50,  "price": 450},
    {"id": 59, "name": "ТЕТРАВИТАМ (витамины AD3E)",                             "volume": "100 мл",        "box": 50,  "price": 340},
    {"id": 60, "name": "МУЛЬТИВИТ + МИНЕРАЛЫ (мультивитамин + минералы)",        "volume": "100 мл",        "box": 50,  "price": 460},
    {"id": 61, "name": "КЕТОПРОФ (кетопрофен)",                                  "volume": "100 мл",        "box": 50,  "price": 680},
    {"id": 62, "name": "МУЛЬТИТОНИК (витаминно-тонизирующий комплекс)",          "volume": "1 л",           "box": 12,  "price": 1800},
    {"id": 63, "name": "КИЛЛЕР ФЛАЙ (инсектицид)",                              "volume": "400 г",         "box": 24,  "price": 1200},
    {"id": 64, "name": "ТОНОКАРД (биостимулятор, кардиотоник)",                  "volume": "100 мл",        "box": 50,  "price": 780},
    {"id": 65, "name": "АВЕРТОП (авермектин 5%)",                                "volume": "100 мл",        "box": 80,  "price": 250},
    {"id": 66, "name": "АВЕРТОП (авермектин 5%)",                                "volume": "200 мл",        "box": 50,  "price": 450},
    {"id": 67, "name": "АВЕРТОП (авермектин 5%)",                                "volume": "500 мл",        "box": 20,  "price": 950},
    {"id": 68, "name": "АЛЬБЕН ПЛЮС 100 (альбендазол 10%)",                     "volume": "100 мл",        "box": 80,  "price": 90},
    {"id": 69, "name": "АЛЬБЕН ПЛЮС 100 (альбендазол 10%)",                     "volume": "200 мл",        "box": 50,  "price": 170},
    {"id": 70, "name": "АЛЬБЕН ПЛЮС 100 (альбендазол 10%)",                     "volume": "500 мл",        "box": 20,  "price": 370},
    {"id": 71, "name": "АЛЬБЕН ПЛЮС 100 (альбендазол 10%)",                     "volume": "1 л",           "box": 12,  "price": 570},
    {"id": 72, "name": "АЛБЕНИВЕР (альбен, ивермек суспензия)",                  "volume": "100 мл",        "box": 80,  "price": 140},
    {"id": 73, "name": "АЛБЕНИВЕР (альбен, ивермек суспензия)",                  "volume": "200 мл",        "box": 50,  "price": 260},
    {"id": 74, "name": "АЛБЕНИВЕР (альбен, ивермек суспензия)",                  "volume": "500 мл",        "box": 20,  "price": 580},
    {"id": 75, "name": "АЛБЕНИВЕР (альбен, ивермек суспензия)",                  "volume": "1 л",           "box": 12,  "price": 950},
    {"id": 76, "name": "ДОКЦИЛИН 200 (доксициклин 20%)",                         "volume": "100 мл",        "box": 80,  "price": 450},
    {"id": 77, "name": "ДОРАМЕК ПЛЮС 315 (дорамектин 3.15%)",                   "volume": "50 мл",         "box": 150, "price": 490},
    {"id": 78, "name": "ИВЕР ПЛЮС 2 (ивермектин 2%)",                           "volume": "50 мл",         "box": 150, "price": 110},
    {"id": 79, "name": "ИВЕР ПЛЮС 2 (ивермектин 2%)",                           "volume": "100 мл",        "box": 80,  "price": 190},
    {"id": 80, "name": "ИВЕРВИТ-Е (ивермектин 1% + витамин Е 4%)",              "volume": "10 мл (10 фл)", "box": 40,  "price": 260},
    {"id": 81, "name": "ИВЕРВИТ-Е (ивермектин 1% + витамин Е 4%)",              "volume": "50 мл",         "box": 150, "price": 110},
    {"id": 82, "name": "ИВЕРВИТ-Е (ивермектин 1% + витамин Е 4%)",              "volume": "100 мл",        "box": 80,  "price": 180},
    {"id": 83, "name": "ИВЕРКЛОЗ (ивермектин 1% + клозантел 10%)",              "volume": "50 мл",         "box": 150, "price": 150},
    {"id": 84, "name": "ИВЕРКЛОЗ (ивермектин 1% + клозантел 10%)",              "volume": "100 мл",        "box": 80,  "price": 280},
    {"id": 85, "name": "КЛОЗАН ПЛЮС 100 (клозантел 10%)",                       "volume": "100 мл",        "box": 80,  "price": 330},
    {"id": 86, "name": "КЛОЗАН ПЛЮС 100 (клозантел 10%)",                       "volume": "250 мл",        "box": 40,  "price": 760},
    {"id": 87, "name": "ОКСИЛИН 300 LA (окситетрациклин 30%)",                  "volume": "10 мл (10 фл)", "box": 40,  "price": 290},
    {"id": 88, "name": "ОКСИЛИН 300 LA (окситетрациклин 30%)",                  "volume": "50 мл",         "box": 150, "price": 130},
    {"id": 89, "name": "ОКСИЛИН 300 LA (окситетрациклин 30%)",                  "volume": "100 мл",        "box": 80,  "price": 230},
    {"id": 90, "name": "ПЕНСТРЕП ПЛЮС LA (пенстреп 20:25)",                     "volume": "10 мл (10 фл)", "box": 40,  "price": 550},
    {"id": 91, "name": "ПЕНСТРЕП ПЛЮС LA (пенстреп 20:25)",                     "volume": "50 мл",         "box": 150, "price": 250},
    {"id": 92, "name": "ПЕНСТРЕП ПЛЮС LA (пенстреп 20:25)",                     "volume": "100 мл",        "box": 80,  "price": 440},
    {"id": 93, "name": "ФЕНБЕНЗОЛ 100 (фенбендазол 10%)",                       "volume": "100 мл",        "box": 80,  "price": 165},
    {"id": 94, "name": "ФЕНБЕНЗОЛ 100 (фенбендазол 10%)",                       "volume": "200 мл",        "box": 50,  "price": 310},
    {"id": 95, "name": "ФЕНБЕНЗОЛ 100 (фенбендазол 10%)",                       "volume": "500 мл",        "box": 20,  "price": 680},
    {"id": 96, "name": "ФЕНБЕНЗОЛ 100 (фенбендазол 10%)",                       "volume": "1 л",           "box": 12,  "price": 1240},
    {"id": 97, "name": "ФЛОРФЕН ПЛЮС 300 (флорфеникол 30%)",                    "volume": "100 мл",        "box": 80,  "price": 480},
    {"id": 98, "name": "ЦЕФНОМ 25 (цефкинома 2.5%)",                            "volume": "50 мл",         "box": 150, "price": 530},
    {"id": 99, "name": "ЦЕФНОМ LC (шприц / 75 мг цефкинома)",                   "volume": "8 г",           "box": 24,  "price": 135},
    {"id": 100,"name": "ЦЕФТИ DC (шприц / 500 мг гидрохлорид цефтиофура)",      "volume": "10 мл",         "box": 24,  "price": 165},
]

PRICE_LIST_TEXT = "\n".join(
    f"{p['id']}. {p['name']} | {p['volume']} | {p['box']} шт/кор | {p['price']} сом"
    for p in PRICE_LIST_DATA
)

def format_invoice(client_name: str, items: list, prev_debt: float = 0) -> str:
    date_str = datetime.now().strftime("%d.%m.%Y")
    total = sum(it["qty"] * it["price"] for it in items)
    grand_total = total + prev_debt

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📋 *НАКЛАДНАЯ — ВЕТОП*")
    lines.append(f"📅 {date_str}")
    lines.append(f"👤 Контрагент: *{client_name}*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    for i, it in enumerate(items, 1):
        subtotal = it["qty"] * it["price"]
        box_qty = it.get("box_qty")
        qty_str = f"{it['qty']} шт"
        if box_qty:
            qty_str += f" ({box_qty} кор)"
        lines.append(f"*{i}. {it['name']}*")
        lines.append(f"   📦 {it['volume']} × {qty_str} = *{subtotal:,} сом*")
        lines.append(f"   _(цена: {it['price']} сом/шт)_")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💰 Сумма: *{total:,} сом*")

    if prev_debt > 0:
        lines.append(f"⚠️ Старый долг: *{prev_debt:,} сом*")
        lines.append(f"📌 ИТОГО к оплате: *{grand_total:,} сом*")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("☎️ +996 700 99 88 11 | +996 555 62 78 32")
    return "\n".join(lines)

SYSTEM_PROMPT = f"""Ты помощник компании ОсОО "ВЕТОП" — оптового поставщика ветеринарных препаратов.

У тебя два режима:

=== РЕЖИМ 1: ВОПРОСЫ О ПРАЙСЕ ===
Отвечай на вопросы о ценах, фасовках, составе препаратов. Кратко и по делу.

=== РЕЖИМ 2: СОЗДАНИЕ НАКЛАДНОЙ ===
Когда сотрудник перечисляет товары для накладной — верни ТОЛЬКО JSON, без пояснений:

{{
  "action": "invoice",
  "client": "Имя контрагента",
  "items": [
    {{"name": "точное название из прайса", "volume": "фасовка", "qty": количество_в_штуках, "box_qty": количество_коробок_или_null, "price": цена_из_прайса}}
  ]
}}

=== ВАЖНО: КОРОБКИ ===
Сотрудники могут писать количество коробками: "1к", "2к", "3к" и т.д.
В прайсе у каждого товара есть "шт/кор" — количество штук в одной коробке.
Ты ОБЯЗАН перевести коробки в штуки: qty = количество_коробок × шт_в_коробке

Примеры:
- "Албенивер 200мл 1к" → 1 коробка × 50 шт/кор = qty: 50
- "Альтопен 100мл 2к" → 2 коробки × 80 шт/кор = qty: 160
- "Топмектин 1% 100мл 3к" → 3 коробки × 80 шт/кор = qty: 240
- "Дексатоп 50мл 10 шт" → qty: 10 (просто штуки, без пересчёта)

Другие обозначения коробок: "к", "кор", "коробка", "коробок", "box"

=== ДРУГИЕ ПРАВИЛА ===
- Цены бери СТРОГО из прайса
- Если товар не найден — напиши текстом что не нашёл
- Если неясно что-то — уточни у сотрудника
- Имя контрагента обязательно

ПРАЙС-ЛИСТ (формат: №. Название | Фасовка | шт/кор | цена):
{PRICE_LIST_TEXT}

Общайся на русском. Отвечай кратко.
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    chat_id = update.effective_chat.id
    chat_histories[chat_id] = []
    await update.message.reply_text(
        "👋 Привет! Я бот компании *ВЕТОП* 🐄💊\n\n"
        "📌 *Команды:*\n"
        "/накладная — создать накладную\n"
        "/долги — все долги контрагентов\n"
        "/оплата Имя Сумма — принять оплату\n"
        "/price — полный прайс-лист\n"
        "/clear — очистить историю\n\n"
        "Или просто напишите что нужно!",
        parse_mode="Markdown"
    )

async def show_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    chunk = "📋 *Прайс ВЕТОП от 22.04.2026*\n\n"
    for p in PRICE_LIST_DATA:
        line = f"{p['id']}. {p['name']} | {p['volume']} | {p['box']} шт/кор | *{p['price']} сом*\n"
        if len(chunk) + len(line) > 4000:
            await update.message.reply_text(chunk, parse_mode="Markdown")
            chunk = line
        else:
            chunk += line
    if chunk:
        await update.message.reply_text(chunk, parse_mode="Markdown")

async def show_debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    if not debts:
        await update.message.reply_text("✅ Долгов нет!")
        return
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "📊 *ДОЛГИ КОНТРАГЕНТОВ*", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    total = 0
    for name, amount in sorted(debts.items()):
        lines.append(f"👤 {name}: *{amount:,} сом*")
        total += amount
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💰 Всего: *{total:,} сом*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: `/оплата Имя Сумма`\nПример: `/оплата Асан 5000`",
            parse_mode="Markdown"
        )
        return
    amount_str = args[-1]
    name = " ".join(args[:-1])
    try:
        amount = float(amount_str)
    except ValueError:
        await update.message.reply_text("❌ Неверная сумма. Пример: `/оплата Асан 5000`", parse_mode="Markdown")
        return

    matched = next((k for k in debts if k.lower() == name.lower()), None)
    if matched is None:
        await update.message.reply_text(f"❌ Контрагент *{name}* не найден в долгах.", parse_mode="Markdown")
        return

    debts[matched] -= amount
    if debts[matched] <= 0:
        del debts[matched]
        await update.message.reply_text(f"✅ *{matched}* полностью погасил долг!", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"✅ Оплата *{amount:,.0f} сом* от *{matched}* принята\n"
            f"📌 Остаток долга: *{debts[matched]:,.0f} сом*",
            parse_mode="Markdown"
        )

async def nakладная_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    chat_id = update.effective_chat.id
    chat_histories[chat_id] = []
    await update.message.reply_text(
        "📋 *Создание накладной*\n\n"
        "Напишите в свободной форме:\n\n"
        "*Штуками:*\n"
        "_Асан, Альтопен 100мл 10 шт, Дексатоп 50мл 5 шт_\n\n"
        "*Коробками (к = коробка):*\n"
        "_Асан, Албенивер 200мл 1к, Топмектин 100мл 2к_\n\n"
        "💡 Бот сам переведёт коробки в штуки по прайсу.",
        parse_mode="Markdown"
    )

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    chat_id = update.effective_chat.id
    chat_histories[chat_id] = []
    await update.message.reply_text("История очищена ✅")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    chat_id = update.effective_chat.id
    user_text = update.message.text

    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    chat_histories[chat_id].append({"role": "user", "content": user_text})
    history = chat_histories[chat_id][-20:]

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=history
        )
        reply = response.content[0].text.strip()
        chat_histories[chat_id].append({"role": "assistant", "content": reply})

        # Проверяем — накладная?
        if reply.startswith("{") and '"action": "invoice"' in reply:
            try:
                data = json.loads(reply)
                client_name = data["client"]
                items = data["items"]
                prev_debt = debts.get(client_name, 0)
                invoice_total = sum(it["qty"] * it["price"] for it in items)

                # Обновляем долг
                debts[client_name] = prev_debt + invoice_total

                invoice_text = format_invoice(client_name, items, prev_debt)
                await update.message.reply_text(invoice_text, parse_mode="Markdown")

                new_total = debts[client_name]
                await update.message.reply_text(
                    f"📌 Долг *{client_name}* теперь: *{new_total:,} сом*",
                    parse_mode="Markdown"
                )
                return
            except (json.JSONDecodeError, KeyError) as e:
                await update.message.reply_text(f"⚠️ Ошибка при создании накладной: {e}")
                return

        await update.message.reply_text(reply)

    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)}")


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только администратор может добавлять пользователей.")
        return
    if not context.args:
        await update.message.reply_text("Использование: `/добавить 123456789`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
        allowed_users.add(uid)
        await update.message.reply_text(f"✅ Пользователь `{uid}` добавлен.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Неверный ID. Пример: `/добавить 123456789`", parse_mode="Markdown")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только администратор может удалять пользователей.")
        return
    if not context.args:
        await update.message.reply_text("Использование: `/удалить 123456789`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
        if uid == ADMIN_ID:
            await update.message.reply_text("❌ Нельзя удалить администратора.")
            return
        allowed_users.discard(uid)
        await update.message.reply_text(f"✅ Пользователь `{uid}` удалён.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.", parse_mode="Markdown")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только администратор.")
        return
    lines = ["👥 *Пользователи с доступом:*"]
    for uid in allowed_users:
        label = " _(админ)_" if uid == ADMIN_ID else ""
        lines.append(f"• `{uid}`{label}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("price", show_price))
    app.add_handler(CommandHandler("долги", show_debts))
    app.add_handler(CommandHandler("оплата", handle_payment))
    app.add_handler(CommandHandler("накладная", nakладная_command))
    app.add_handler(CommandHandler("добавить", add_user))
    app.add_handler(CommandHandler("удалить", remove_user))
    app.add_handler(CommandHandler("пользователи", list_users))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен...")
    app.run_polling(stop_signals=None)
