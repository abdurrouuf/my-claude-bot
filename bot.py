import os
import json
import io
import anthropic
from datetime import datetime
from reportlab.lib.pagesizes import A5
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from telegram import InputFile
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
USERS_FILE = "/tmp/allowed_users.json"

def load_users() -> set:
    """Загружаем список пользователей из файла."""
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                return set(json.load(f))
        except:
            pass
    return set([ADMIN_ID])

def save_users(users: set):
    """Сохраняем список пользователей в файл."""
    try:
        with open(USERS_FILE, "w") as f:
            json.dump(list(users), f)
    except:
        pass

allowed_users = load_users()
# Постоянные сотрудники — всегда имеют доступ
PERMANENT_USERS = {632294583, 607647629, 6525019701, 5808155644, 1616348285}
allowed_users.update(PERMANENT_USERS)
save_users(allowed_users)

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
    {"id": 101,"name": "ГАБИВИТ",                                               "volume": "100 мл",        "box": 50,  "price": 780},
    {"id": 102,"name": "КАЛЬФОТОН",                                             "volume": "100 мл",        "box": 50,  "price": 980},
    {"id": 103,"name": "СУРФАГОН УЛЬТРА 10 мкг",                                "volume": "50 мл",         "box": 50,  "price": 480},
    {"id": 104,"name": "СУРФАГОН УЛЬТРА 50 мкг",                                "volume": "20 мл",         "box": 162, "price": 580},
]

PRICE_LIST_TEXT = "\n".join(
    f"{p['id']}. {p['name']} | {p['volume']} | {p['box']} шт/кор | {p['price']} сом"
    for p in PRICE_LIST_DATA
)

def format_invoice(client_name: str, items: list, prev_debt: float = 0) -> str:
    date_str = datetime.now().strftime("%d.%m.%Y")
    invoice_total = sum(it["qty"] * it["price"] for it in items)
    grand_total = invoice_total + prev_debt

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📋 *НАКЛАДНАЯ — ВЕТОП*")
    lines.append(f"📅 {date_str}")
    lines.append(f"👤 Контрагент: *{client_name}*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    for i, it in enumerate(items, 1):
        subtotal = it["qty"] * it["price"]
        box_qty = it.get("box_qty")
        box_str = f"({box_qty} кор) " if box_qty else ""
        lines.append(f"*{i}. {it['name']} - {it['volume']}*")
        lines.append(f"   📦 {box_str}{it['qty']} шт × {it['price']} сом = *{subtotal:,} сом*")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🧾 Сумма накладной: *{invoice_total:,} сом*")
    if prev_debt > 0:
        lines.append(f"⚠️ Остаток долга: *{prev_debt:,} сом*")
        lines.append(f"📌 Общий итоговый долг: *{grand_total:,} сом*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("☎️ +996 700 99 88 11 | +996 700 887 666")
    return "\n".join(lines)

def format_invoice_with_payment(client_name: str, items: list, prev_debt: float, payment: float) -> str:
    date_str = datetime.now().strftime("%d.%m.%Y")
    invoice_total = sum(it["qty"] * it["price"] for it in items)
    total_debt = invoice_total + prev_debt
    remainder = total_debt - payment

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📋 *НАКЛАДНАЯ — ВЕТОП*")
    lines.append(f"📅 {date_str}")
    lines.append(f"👤 Контрагент: *{client_name}*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    for i, it in enumerate(items, 1):
        subtotal = it["qty"] * it["price"]
        box_qty = it.get("box_qty")
        box_str = f"({box_qty} кор) " if box_qty else ""
        lines.append(f"*{i}. {it['name']} - {it['volume']}*")
        lines.append(f"   📦 {box_str}{it['qty']} шт × {it['price']} сом = *{subtotal:,} сом*")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🧾 Сумма накладной: *{invoice_total:,} сом*")
    if prev_debt > 0:
        lines.append(f"⚠️ Старый долг: *{prev_debt:,} сом*")
    lines.append(f"📊 Итого долг: *{total_debt:,} сом*")
    lines.append(f"💵 Приход: *{payment:,} сом*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    if remainder <= 0:
        lines.append(f"🎉 Долг полностью погашен!")
    else:
        lines.append(f"📌 Остаток долга: *{remainder:,} сом*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("☎️ +996 700 99 88 11 | +996 700 887 666")
    return "\n".join(lines)


def format_payment(client_name: str, debt: float, payment: float) -> str:
    remainder = debt - payment
    date_str = datetime.now().strftime("%d.%m.%Y")
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💵 *ПРИХОД — ВЕТОП*")
    lines.append(f"📅 {date_str}")
    lines.append(f"👤 Контрагент: *{client_name}*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⚠️ Долг: *{debt:,.0f} сом*")
    lines.append(f"✅ Оплата: *{payment:,.0f} сом*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    if remainder <= 0:
        lines.append(f"🎉 Долг полностью погашен!")
    else:
        lines.append(f"📌 Остаток долга: *{remainder:,.0f} сом*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("☎️ +996 700 99 88 11 | +996 700 887 666")
    return "\n".join(lines)


def fmt_num(n) -> str:
    """Форматирует число с апострофом: 7800 -> 7'800"""
    try:
        n = int(round(float(n)))
    except (ValueError, TypeError):
        return str(n)
    return f"{n:,}".replace(",", "'")


def _register_fonts():
    """Находит и регистрирует шрифты DejaVu."""
    import logging
    base = os.path.dirname(os.path.abspath(__file__))
    normal_candidates = [
        os.path.join(base, "DejaVuSans.ttf"),
        "/app/DejaVuSans.ttf",
        "/app/fonts/DejaVuSans.ttf",
    ]
    bold_candidates = [
        os.path.join(base, "DejaVuSans-Bold.ttf"),
        "/app/DejaVuSans-Bold.ttf",
        "/app/fonts/DejaVuSans-Bold.ttf",
    ]
    font_path = next((p for p in normal_candidates if os.path.exists(p)), None)
    font_bold_path = next((p for p in bold_candidates if os.path.exists(p)), None)
    if font_path and font_bold_path:
        try:
            pdfmetrics.registerFont(TTFont("DejaVu", font_path))
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", font_bold_path))
            return "DejaVu", "DejaVu-Bold"
        except Exception as e:
            logging.error(f"Font load error: {e}")
    return "Helvetica", "Helvetica-Bold"


def _find_logo():
    """Находит файл логотипа в репозитории."""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "IMG_3248.jpeg"),
        os.path.join(base, "logo.jpg"),
        os.path.join(base, "logo.jpeg"),
        os.path.join(base, "logo.png"),
        "/app/IMG_3248.jpeg",
        "/app/logo.jpg",
        "/app/logo.png",
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


def _find_qr():
    """Находит QR-код Instagram."""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "qr_instagram.jpeg"),
        os.path.join(base, "qr_instagram.png"),
        "/app/qr_instagram.jpeg",
        "/app/qr_instagram.png",
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


def generate_pdf_invoice(client_name, items, invoice_total, prev_debt=0,
                         payment=0, is_payment=False) -> io.BytesIO:
    """Красивый PDF: логотип, таблица товаров, итоги."""
    from reportlab.platypus import Table, TableStyle, Image
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    font_name, font_bold_name = _register_fonts()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A5,
        rightMargin=8*mm, leftMargin=8*mm,
        topMargin=8*mm, bottomMargin=8*mm
    )

    HEADER_GREEN = colors.HexColor("#1b5e20")
    LIGHT_ROW = colors.HexColor("#f1f8e9")

    title_style = ParagraphStyle('title', fontName=font_bold_name, fontSize=14,
                                 leading=18, alignment=TA_CENTER, spaceAfter=2)
    sub_style = ParagraphStyle('sub', fontName=font_name, fontSize=9,
                               leading=13, alignment=TA_LEFT)
    cell_style = ParagraphStyle('cell', fontName=font_name, fontSize=8, leading=10)
    cell_bold = ParagraphStyle('cellb', fontName=font_bold_name, fontSize=8,
                               leading=10, textColor=colors.white, alignment=TA_CENTER)
    total_style = ParagraphStyle('total', fontName=font_bold_name, fontSize=10,
                                 leading=14, alignment=TA_LEFT)
    slogan_style = ParagraphStyle('slogan', fontName=font_name, fontSize=9,
                                  leading=12, alignment=TA_CENTER,
                                  textColor=HEADER_GREEN, spaceAfter=3)

    story = []

    # Слоган над логотипом
    story.append(Paragraph("Здоровье ваших животных — наш приоритет", slogan_style))

    logo = _find_logo()
    if logo:
        try:
            img = Image(logo)
            iw, ih = img.imageWidth, img.imageHeight
            target_w = 45*mm
            img.drawWidth = target_w
            img.drawHeight = target_w * ih / iw
            img.hAlign = "CENTER"
            story.append(img)
        except Exception:
            pass

    story.append(Paragraph("НАКЛАДНАЯ", title_style))
    story.append(Paragraph(datetime.now().strftime("%d.%m.%Y"), sub_style))
    story.append(Paragraph(f"Контрагент: <b>{client_name}</b>", sub_style))
    story.append(Spacer(1, 3*mm))

    header = [
        Paragraph("№", cell_bold),
        Paragraph("Название", cell_bold),
        Paragraph("Фасовка", cell_bold),
        Paragraph("Кол-во", cell_bold),
        Paragraph("Цена", cell_bold),
        Paragraph("Сумма", cell_bold),
    ]
    table_data = [header]
    for i, it in enumerate(items, 1):
        subtotal = it["qty"] * it["price"]
        box_qty = it.get("box_qty")
        qty_text = f"{it['qty']} шт"
        if box_qty:
            qty_text = f"{box_qty} кор<br/>({it['qty']} шт)"
        table_data.append([
            Paragraph(str(i), cell_style),
            Paragraph(it["name"], cell_style),
            Paragraph(it["volume"], cell_style),
            Paragraph(qty_text, cell_style),
            Paragraph(fmt_num(it["price"]), cell_style),
            Paragraph(fmt_num(subtotal), cell_style),
        ])

    col_widths = [8*mm, 48*mm, 18*mm, 18*mm, 16*mm, 18*mm]
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_GREEN),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("ALIGN", (3, 1), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for r in range(1, len(table_data)):
        if r % 2 == 0:
            style.append(("BACKGROUND", (0, r), (-1, r), LIGHT_ROW))
    table.setStyle(TableStyle(style))
    story.append(table)
    story.append(Spacer(1, 4*mm))

    story.append(HRFlowable(width="100%", thickness=0.6, color=HEADER_GREEN))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(f"Сумма накладной: <b>{fmt_num(invoice_total)} сом</b>", total_style))

    if is_payment:
        total_debt = invoice_total + prev_debt
        remainder = total_debt - payment
        if prev_debt > 0:
            story.append(Paragraph(f"Старый долг: <b>{fmt_num(prev_debt)} сом</b>", total_style))
        story.append(Paragraph(f"Итого долг: <b>{fmt_num(total_debt)} сом</b>", total_style))
        story.append(Paragraph(f"Приход: <b>{fmt_num(payment)} сом</b>", total_style))
        if remainder <= 0:
            story.append(Paragraph("Долг полностью погашен!", total_style))
        else:
            story.append(Paragraph(f"Остаток долга: <b>{fmt_num(remainder)} сом</b>", total_style))
    elif prev_debt > 0:
        grand_total = invoice_total + prev_debt
        story.append(Paragraph(f"Остаток долга: <b>{fmt_num(prev_debt)} сом</b>", total_style))
        story.append(Paragraph(f"Общий итоговый долг: <b>{fmt_num(grand_total)} сом</b>", total_style))

    story.append(Spacer(1, 2*mm))
    story.append(HRFlowable(width="100%", thickness=0.6, color=HEADER_GREEN))
    story.append(Spacer(1, 2*mm))

    qr_path = _find_qr()
    if qr_path:
        try:
            contact_style = ParagraphStyle('contact', fontName=font_name, fontSize=9,
                                           leading=13, alignment=TA_LEFT)
            qr_img = Image(qr_path)
            qr_size = 20*mm
            qr_img.drawWidth = qr_size
            qr_img.drawHeight = qr_size
            contact_table = Table(
                [[Paragraph("+996 700 99 88 11<br/>+996 700 887 666<br/>Instagram: @vetop.kg", contact_style), qr_img]],
                colWidths=[None, 22*mm]
            )
            contact_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]))
            story.append(contact_table)
        except Exception:
            story.append(Paragraph("+996 700 99 88 11  |  +996 700 887 666", sub_style))
            story.append(Paragraph("Instagram: @vetop.kg", sub_style))
    else:
        story.append(Paragraph("+996 700 99 88 11  |  +996 700 887 666", sub_style))
        story.append(Paragraph("Instagram: @vetop.kg", sub_style))

    doc.build(story)
    buffer.seek(0)
    return buffer

SYSTEM_PROMPT = f"""Ты помощник компании ОсОО "ВЕТОП" — оптового поставщика ветеринарных препаратов.

У тебя два режима:

=== РЕЖИМ 1: ВОПРОСЫ О ПРАЙСЕ ===
Отвечай на вопросы о ценах, фасовках, составе препаратов. Кратко и по делу.

=== РЕЖИМ 2: НАКЛАДНАЯ (с долгом и/или приходом) ===
Когда сотрудник перечисляет товары — верни ТОЛЬКО JSON, без пояснений:

{{
  "action": "invoice",
  "client": "Имя контрагента",
  "debt": старый_долг_или_0,
  "payment": приход_или_0,
  "items": [
    {{"name": "точное название из прайса", "volume": "фасовка", "qty": количество_в_штуках, "box_qty": количество_коробок_или_null, "price": цена_из_прайса}}
  ]
}}

Примеры заполнения полей:
- "долг 31470" → debt: 31470
- "приход 5000" → payment: 5000
- Если не указано → 0

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

=== ДОЛГ ===
Если сотрудник упоминает долг контрагента (например "долг 10670" или "остаток 5000"), 
занеси его в поле "debt". Если долг не указан — debt: 0.



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
        "/invoice — создать накладную\n"
        "/debts — все долги контрагентов\n"
        "/payment Имя Сумма — принять оплату\n"
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
            "Использование: `/payment Name Sum`\nПример: `/payment Asan 5000`",
            parse_mode="Markdown"
        )
        return
    amount_str = args[-1]
    name = " ".join(args[:-1])
    try:
        amount = float(amount_str)
    except ValueError:
        await update.message.reply_text("❌ Неверная сумма. Пример: `/payment Asan 5000`", parse_mode="Markdown")
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

async def invoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        # Убираем ```json обёртку если есть
        clean_reply = reply.strip()
        if clean_reply.startswith("```"):
            clean_reply = clean_reply.split("```")[1]
            if clean_reply.startswith("json"):
                clean_reply = clean_reply[4:]
            clean_reply = clean_reply.strip()

        if '"action": "payment"' in clean_reply:
            try:
                data = json.loads(clean_reply)
                client_name = data["client"]
                debt = float(data.get("debt", 0) or 0)
                payment = float(data.get("payment", 0) or 0)
                payment_text = format_payment(client_name, debt, payment)
                await update.message.reply_text(payment_text, parse_mode="Markdown")
                return
            except (json.JSONDecodeError, KeyError) as e:
                await update.message.reply_text(f"⚠️ Ошибка при обработке прихода: {e}")
                return

        if '"action": "invoice"' in clean_reply:
            try:
                data = json.loads(clean_reply)
                client_name = data["client"]
                items = data["items"]
                prev_debt = float(data.get("debt", 0) or 0)
                payment = float(data.get("payment", 0) or 0)
                invoice_total = sum(it["qty"] * it["price"] for it in items)

                # Только PDF, без текстового сообщения
                try:
                    pdf_buffer = generate_pdf_invoice(
                        client_name, items, invoice_total,
                        prev_debt=prev_debt, payment=payment,
                        is_payment=(payment > 0)
                    )
                    date_str = datetime.now().strftime("%d%m%Y")
                    filename = f"nakладная_{client_name}_{date_str}.pdf"
                    await update.message.reply_document(
                        document=InputFile(pdf_buffer, filename=filename),
                        caption=f"📄 Накладная для {client_name}"
                    )
                except Exception as pdf_err:
                    await update.message.reply_text(f"⚠️ Не удалось создать PDF: {pdf_err}")
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
        await update.message.reply_text("Использование: `/add 123456789`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
        allowed_users.add(uid)
        save_users(allowed_users)
        await update.message.reply_text(f"✅ Пользователь `{uid}` добавлен.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Неверный ID. Пример: `/add 123456789`", parse_mode="Markdown")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только администратор может удалять пользователей.")
        return
    if not context.args:
        await update.message.reply_text("Использование: `/remove 123456789`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
        if uid == ADMIN_ID:
            await update.message.reply_text("❌ Нельзя удалить администратора.")
            return
        allowed_users.discard(uid)
        save_users(allowed_users)
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
    app.add_handler(CommandHandler("debts", show_debts))
    app.add_handler(CommandHandler("payment", handle_payment))
    app.add_handler(CommandHandler("invoice", invoice_command))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CommandHandler("users", list_users))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен...")
    app.run_polling(stop_signals=None)
