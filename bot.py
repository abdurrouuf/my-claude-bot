# Телеграм-бот ВЕТОП: накладные, склады по регионам, долги клиентов.
import asyncio
import difflib
import html
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from anthropic import AsyncAnthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import db
import prices
from db import BISHKEK
from invoice_pdf import (fmt_num, generate_act_pdf, generate_pdf_invoice,
                         generate_price_pdf, generate_report_pdf, safe_filename)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("vetop")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Sonnet 5: сильная модель по цене $2/$10 за млн токенов (акция до 31.08.2026,
# потом $3/$15) — в 2.5 раза дешевле Opus. Переопределяется переменной
# окружения CLAUDE_MODEL (например, claude-haiku-4-5 — ещё дешевле).
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

# Переходный режим: черновики выглядят как обычные накладные (без водяного
# знака «ЧЕРНОВИК»), но базу по-прежнему не трогают. Когда полностью перейдёте
# на учёт через бота — поставьте на Railway переменную DRAFT_WATERMARK=1.
DRAFT_WATERMARK = os.environ.get("DRAFT_WATERMARK", "0") == "1"

# Переходный период: сотрудникам доступны ТОЛЬКО черновики и вопросы о прайсе.
# Накладные, оплаты, перемещения и создание клиентов заблокированы (админу — нет).
# Когда база будет готова — поставьте на Railway переменную TRANSITION_MODE=0.
TRANSITION_MODE = os.environ.get("TRANSITION_MODE", "1") == "1"

TRANSITION_HINT = (
    "⏳ Ваш склад пока работает в режиме черновиков — эта операция для него "
    "недоступна.\n\n"
    "Накладные выписывать можно — просто напишите как обычно:\n"
    "Асан, Албенивер 200мл 1к, долг 31470, приход 5000\n\n"
    "Придёт готовая PDF-накладная, остатки и долги не изменятся."
)


def transition_blocked(actor) -> bool:
    return TRANSITION_MODE and not is_admin(actor)


# Час вечерней сводки по времени Бишкека (0-23)
SUMMARY_HOUR = int(os.environ.get("SUMMARY_HOUR", "20"))

# Час вечерней сводки по черновикам админу в личку (переходный период)
DRAFT_SUMMARY_HOUR = int(os.environ.get("DRAFT_SUMMARY_HOUR", "19"))

# Долг считается «старым», если клиент не платил столько дней
DEBT_ALERT_DAYS = int(os.environ.get("DEBT_ALERT_DAYS", "30"))

# Окно раздела «Растущие долги» в отчёте о должниках, дней
DEBT_GROWTH_DAYS = int(os.environ.get("DEBT_GROWTH_DAYS", "30"))

# Час ежедневного бэкапа базы админу в личку (по Бишкеку)
BACKUP_HOUR = int(os.environ.get("BACKUP_HOUR", "3"))

# Товар считается «мёртвым», если не продавался столько дней
DEADSTOCK_DAYS = int(os.environ.get("DEADSTOCK_DAYS", "60"))

# --- Распознавание голосовых сообщений (речь -> текст, Whisper) ---
# Claude аудио не принимает, поэтому используется отдельный сервис.
# Рекомендуется Groq (бесплатно): зарегистрироваться на console.groq.com,
# создать ключ и добавить на Railway переменную GROQ_API_KEY.
# Альтернатива — OpenAI (платно): переменная OPENAI_API_KEY.
STT_API_KEY = (os.environ.get("STT_API_KEY") or os.environ.get("GROQ_API_KEY")
               or os.environ.get("OPENAI_API_KEY"))
_STT_VIA_OPENAI = (not os.environ.get("STT_API_KEY")
                   and not os.environ.get("GROQ_API_KEY")
                   and bool(os.environ.get("OPENAI_API_KEY")))
STT_BASE_URL = os.environ.get("STT_BASE_URL") or (
    "https://api.openai.com/v1" if _STT_VIA_OPENAI
    else "https://api.groq.com/openai/v1")
STT_MODEL = os.environ.get("STT_MODEL") or (
    "whisper-1" if _STT_VIA_OPENAI else "whisper-large-v3")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "ru")  # "auto" = определять язык самому
MAX_VOICE_SECONDS = int(os.environ.get("MAX_VOICE_SECONDS", "300"))

# ElevenLabs Scribe — распознавание с поддержкой кыргызского (Whisper его не
# знает). Если задан ELEVENLABS_API_KEY, голосовые идут через ElevenLabs
# с автоопределением языка (русский/кыргызский вперемешку — ок).
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v1")

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
HISTORY_LIMIT = 10  # история пересылается с каждым запросом — короче = дешевле
# Старые сообщения истории режутся до этого размера: полный JSON накладной
# на 20 позиций весит ~1000 токенов и был главным пожирателем баланса
# (~46% стоимости запроса по аудиту 27.07.2026). Текущее сообщение
# пользователя уходит целиком — режется только прошлое.
HISTORY_MSG_MAX = 700


def _request_history(chat_id) -> list:
    """Окно истории для запроса к ИИ.

    - Начинается с user-сообщения: срез [-10:] мог начинаться с ответа
      ассистента, а Messages API требует первым user — каждый второй запрос
      в длинном диалоге падал с 400 (баг найден аудитом 27.07.2026).
    - Все сообщения, кроме последнего (текущего), обрезаются до
      HISTORY_MSG_MAX символов — для контекста хватает начала."""
    h = chat_histories.get(chat_id, [])[-HISTORY_LIMIT:]
    while h and h[0]["role"] != "user":
        h = h[1:]
    out = []
    for i, m in enumerate(h):
        c = m["content"]
        if i < len(h) - 1 and isinstance(c, str) and len(c) > HISTORY_MSG_MAX:
            c = c[:HISTORY_MSG_MAX] + " …[обрезано]"
        out.append({"role": m["role"], "content": c})
    return out

# Неподтверждённые заявки (накладные/приходы/перемещения) до нажатия кнопки.
class _PendingStore(dict):
    """Заявки-карточки: память + зеркало в базе, чтобы кнопки переживали
    перезапуск бота при деплое (просьба владельца 08.08.2026 — «Заявка
    устарела» после наших обновлений). pop чистит и снимок в базе."""

    def pop(self, token, *default):
        try:
            db.pending_delete(token)
        except Exception:
            pass
        return super().pop(token, *default)


PENDING = _PendingStore()
PENDING_TTL = 15 * 60        # обычная заявка живёт 15 минут
APPROVAL_TTL = 24 * 60 * 60  # заявка на перемещение ждёт админа сутки
# Сотрудник может отменить/заменить свою операцию в течение часа (просьба
# владельца 20.07.2026; настраивается: UNDO_MINUTES)
UNDO_WINDOW = int(os.environ.get("UNDO_MINUTES", "60")) * 60


def esc(s) -> str:
    return html.escape(str(s))


def money(n) -> str:
    return f"{fmt_num(n)} сом"


def fmt_usd(x) -> str:
    """$0.47, $1.7, $87.5 — без хвостовых нулей (fmt_num округляет до целых)."""
    return f"{float(x):.2f}".rstrip("0").rstrip(".")


def usd_rate() -> float:
    """Курс сом/$ для закупочных цен (settings usd_rate, задаёт админ)."""
    try:
        return float(db.get_setting("usd_rate") or 0)
    except ValueError:
        return 0.0


def buy_markup_pct() -> float:
    """Накладные расходы на закуп в % (таможня, доставка) — settings."""
    try:
        return float(db.get_setting("buy_markup_pct") or 0)
    except ValueError:
        return 0.0


def buy_som_map() -> dict:
    """{product_id: закупочная цена в СОМАХ с учётом курса и накладных
    расходов}. Пусто, если курс не задан. Только для отчётов админа —
    в промпт ИИ закупочные цены не попадают (секрет + лишние токены)."""
    rate = usd_rate()
    if rate <= 0:
        return {}
    k = rate * (1 + buy_markup_pct() / 100)
    return {pid: usd * k for pid, usd in db.products_buy_map().items()}


def is_admin(user_row) -> bool:
    return user_row["role"] == "admin"


def is_training_wh(wh) -> bool:
    """Учебный склад-полигон (имя начинается с «Учеб», договорённость
    03.08.2026): секции в отчётах показываются, но в общие итоги /report,
    /margin и /stockcost учебные цифры не входят — полигон можно держать
    постоянно, не пачкая настоящие деньги."""
    return str(wh["name"]).strip().lower().startswith("учеб")


def training_wh_ids() -> set:
    return {w["id"] for w in db.all_warehouses() if is_training_wh(w)}


def can_transfer(user_row) -> bool:
    return user_row["role"] in ("admin", "senior")


async def get_actor(update: Update):
    """Активный пользователь из базы; иначе отказ."""
    tg_user = update.effective_user
    if tg_user is None:
        return None
    row = db.get_user(tg_user.id)
    if row is None or not row["active"]:
        # Посторонним — ПОЛНАЯ тишина, и в группах, и в личке (решение
        # владельца 07.08.2026): молчащий бот не интересен любопытным.
        # Раньше в личке отвечали «⛔ нет доступа».
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


async def send_long_bot(bot, chat_id, text: str):
    """То же, что send_long, но для фоновых рассылок (bot.send_message).

    Лимит Telegram — 4096 символов: длинное напоминание о должниках раньше
    просто не доходило (BadRequest глотался логом)."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 4000:
            await bot.send_message(chat_id, chunk, parse_mode="HTML")
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        await bot.send_message(chat_id, chunk, parse_mode="HTML")


def _pending_key(payload: dict):
    """Смысловой отпечаток заявки (без служебных полей) для дедупликации."""
    try:
        return json.dumps({k: v for k, v in payload.items()
                           if k not in ("created", "ttl")},
                          sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def new_pending(payload: dict, ttl: int = PENDING_TTL) -> str:
    now = time.monotonic()
    for k in [k for k, v in PENDING.items() if now - v["created"] > v.get("ttl", PENDING_TTL)]:
        PENDING.pop(k, None)
    # Одинаковое сообщение, отправленное дважды (двойное касание, повтор
    # голосового), создавало ДВЕ живые карточки — нажатие обеих задваивало
    # операцию. Новая идентичная заявка гасит старую, но только СВЕЖУЮ
    # (окно 2 минуты): «сдал 5000» утром и «сдал 5000» вечером — это две
    # настоящие операции, старая заявка должна жить.
    key = _pending_key(payload)
    if key is not None:
        for k in [k for k, v in PENDING.items()
                  if now - v["created"] < 120 and _pending_key(v) == key]:
            PENDING.pop(k, None)
    token = secrets.token_hex(6)
    payload["created"] = now
    payload["ttl"] = ttl
    PENDING[token] = payload
    _persist_pending(token)
    return token


def _persist_pending(token: str):
    """Снимок заявки в базу — кнопка сработает и после перезапуска бота.
    Несериализуемая заявка (не должно случаться) живёт только в памяти."""
    p = PENDING.get(token)
    if p is None:
        return
    try:
        blob = json.dumps(p, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return
    left = p.get("ttl", PENDING_TTL) - (time.monotonic() - p["created"])
    if left <= 0:
        return
    try:
        db.pending_save(token, blob, time.time() + left)
    except Exception as e:
        log.warning("Не удалось сохранить заявку %s: %s", token, e)


def _restore_pending(token: str):
    """Заявка не в памяти (бот перезапускался) — поднимаем снимок из базы."""
    try:
        row = db.pending_load(token)
    except Exception:
        return None
    if row is None:
        return None
    blob, expires = row
    left = expires - time.time()
    if left <= 0:
        try:
            db.pending_delete(token)
        except Exception:
            pass
        return None
    try:
        p = json.loads(blob)
    except (ValueError, TypeError):
        return None
    p["created"] = time.monotonic()
    p["ttl"] = left
    dict.__setitem__(PENDING, token, p)
    return p


def get_pending(token: str):
    p = PENDING.get(token)
    if p is None:
        p = _restore_pending(token)
    if p is None:
        return None
    if time.monotonic() - p["created"] > p.get("ttl", PENDING_TTL):
        PENDING.pop(token, None)
        return None
    # Обновляем снимок: правки заявки прошлых шагов (выбор клиента, партии)
    # не потеряются при перезапуске между нажатиями кнопок
    _persist_pending(token)
    return p


def confirm_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Провести", callback_data=f"ok:{token}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}"),
    ]])


async def _group_only_feed_whs(update, actor):
    """Область отчётной команды в ГРУППЕ: только склад(ы) этого чата-ленты,
    и только доступные автору (просьба владельца 03.08.2026 — «в общем чате
    показывать только этот склад, а не все»). Аргумент-склад в группе
    игнорируется. Возвращает (склады, in_group): в личке — (None, False);
    в группе без привязки/доступа отказ уже отправлен — ([], True)."""
    chat = update.effective_chat
    if chat is None or chat.type == "private":
        return None, False
    whs = [w for w in db.warehouses_of_feed(chat.id)
           if db.can_view_warehouse(actor, w["id"])]
    if not whs:
        try:
            await update.message.reply_text(
                "🔒 В группе я показываю только склад этого чата. Тут склад "
                "не привязан (или у вас нет к нему доступа) — напишите мне "
                "в личку.")
        except Exception:
            pass
        return [], True
    return whs, True


async def _require_private(update) -> bool:
    """Отчёты с чувствительными данными (телефоны, долги, кассы, журнал) —
    только в личке: в группе PDF увидели бы все участники чата, в том числе
    по чужим складам (аудит 27.07.2026). True = можно продолжать."""
    if update.effective_chat and update.effective_chat.type != "private":
        try:
            await update.message.reply_text(
                "🔒 Эта команда работает в личке с ботом — напишите мне напрямую.")
        except Exception:
            pass
        return False
    return True


KNOWN_COMMANDS: set = set()  # заполняется в main() из зарегистрированных команд


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Опечатка в команде (/infobd вместо /dbinfo) — подсказываем ближайшие
    команды (просьба владельца 07.08.2026: бот молчал на опечатку, выглядело
    как поломка). Отвечаем только сотрудникам; в группе — только если команда
    адресована именно нашему боту (/команда@имябота), чтобы не встревать
    в команды других ботов чата."""
    msg = update.effective_message
    if msg is None or not msg.text or not msg.text.startswith("/"):
        return
    raw = msg.text.split()[0][1:]
    cmd, _, target = raw.partition("@")
    if update.effective_chat and update.effective_chat.type != "private":
        if target.lower() != (context.bot.username or "").lower():
            return
    tg_user = update.effective_user
    row = db.get_user(tg_user.id) if tg_user else None
    if row is None or not row["active"]:
        return  # незнакомцам не подсказываем — как и везде, тишина
    cmd_l = cmd.lower()
    if not cmd_l or cmd_l in KNOWN_COMMANDS:
        return
    matches = difflib.get_close_matches(cmd_l, sorted(KNOWN_COMMANDS),
                                        n=3, cutoff=0.6)
    # Перестановка букв целиком (/infobd -> /dbinfo) difflib может недооценить
    for known in KNOWN_COMMANDS:
        if known not in matches and sorted(known) == sorted(cmd_l):
            matches.insert(0, known)
    if matches:
        hint = " или ".join("/" + m for m in matches[:3])
        text = (f"🤔 Не знаю команду /{esc(cmd_l)}. "
                f"Возможно, вы имели в виду {hint}?")
    else:
        text = (f"🤔 Не знаю команду /{esc(cmd_l)}. "
                "Список команд — в /start.")
    try:
        await msg.reply_text(text)
    except Exception:
        pass


async def on_chat_migrated(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Группа стала супергруппой — Telegram меняет id чата, и привязка
    ленты (/feed) молча слетала: команды в чате отвечали «склад не привязан»,
    лента операций пропадала. Теперь привязка переезжает автоматически."""
    msg = update.effective_message
    if msg is None:
        return
    if msg.migrate_to_chat_id:            # служебное сообщение в СТАРОМ чате
        old_id, new_id = msg.chat.id, msg.migrate_to_chat_id
    elif msg.migrate_from_chat_id:        # служебное сообщение в НОВОМ чате
        old_id, new_id = msg.migrate_from_chat_id, msg.chat.id
    else:
        return
    names = db.migrate_feed_chat(old_id, new_id)
    if not names:
        return                            # второе из двух служебных сообщений
    log.info("Чат %s переехал в %s, лента складов перенесена: %s",
             old_id, new_id, ", ".join(names))
    try:
        await context.bot.send_message(
            ADMIN_ID,
            "ℹ️ Групповой чат «" + esc(msg.chat.title or str(new_id))
            + "» переехал в супергруппу — привязка ленты складов ("
            + ", ".join(f"«{esc(n)}»" for n in names)
            + ") перенесена автоматически, ничего делать не нужно.",
            parse_mode="HTML")
    except Exception as e:
        log.warning("Не удалось сообщить админу о переезде чата: %s", e)


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


async def post_feed(context, wh_ids, text: str, exclude_chat_id=None):
    """Постит сводку операции в чаты-ленты складов, которых она коснулась.

    exclude_chat_id — чат, где операцию и так уже видели (операция сделана
    прямо в чате склада), туда дубль не шлём."""
    chats = set()
    for wh_id in wh_ids:
        wh = db.warehouse_by_id(wh_id)
        if wh and wh["feed_chat_id"]:
            chats.add(wh["feed_chat_id"])
    chats.discard(exclude_chat_id)
    for chat_id in chats:
        try:
            await context.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:
            log.warning("Не удалось отправить в ленту %s: %s", chat_id, e)


async def feed_invoice_pdf(context, wh_id, client_label, p, old_debt, total,
                           caption=None, exclude_chat_id=None, op_id=None):
    """PDF проведённой накладной — в чат-ленту склада (просьба владельца:
    накладные Каракола видны всей команде склада). Сводка операции идёт
    подписью к файлу — отдельное текстовое сообщение не шлём (дубль)."""
    wh = db.warehouse_by_id(wh_id)
    if not wh or not wh["feed_chat_id"] or wh["feed_chat_id"] == exclude_chat_id:
        return
    try:
        await send_invoice_pdf(context, wh["feed_chat_id"], client_label, p,
                               old_debt, total, caption=caption, op_id=op_id)
    except Exception as e:
        log.warning("Не удалось отправить PDF в ленту склада %s: %s", wh_id, e)


async def feed_operation(context, op_id: int, actor_name: str, prefix: str,
                         note: str = "", exclude_chat_id=None):
    op = db.get_operation(op_id)
    if op is None:
        return
    text = f"{prefix} <b>{esc(actor_name)}</b> — {esc(op['summary'])}"
    if note:
        text += f"\n{esc(note)}"
    await post_feed(context, db.operation_warehouses(op), text,
                    exclude_chat_id=exclude_chat_id)


# ---------- Системный промпт ----------
# Разделён на две части ради кэширования (экономия ~90% на повторных чтениях):
# статичная (правила + прайс, одинаковые для всех и помеченные cache_control)
# и динамичная (дата, сотрудник, склады) — она идёт ПОСЛЕ статичной,
# чтобы не ломать кэш-префикс.

def _build_static_system() -> str:
    parts = []
    parts.append('Ты — помощник компании ОсОО «ВЕТОП», оптового поставщика ветеринарных препаратов. '
                 'Ты разбираешь сообщения сотрудников и превращаешь их в структурированные действия.')
    parts.append('Данные о сегодняшней дате, сотруднике и складах — в отдельном системном блоке ниже.')
    parts.append("")
    parts.append("=== РЕЖИМ 1: ВОПРОСЫ О ПРАЙСЕ ===")
    parts.append("Отвечай на вопросы о ценах, фасовках, составе препаратов. Кратко и по делу, обычным текстом.")
    parts.append("")
    parts.append("=== РЕЖИМ 2: НАКЛАДНАЯ ===")
    parts.append("Когда сотрудник перечисляет клиента и товары — верни ТОЛЬКО JSON, без пояснений и без ```:")
    parts.append('{"action": "invoice", "client": "Имя контрагента", "warehouse": null, "debt": 0, "payment": 0, '
                 '"phone": null, '
                 '"items": [{"name": "точное название из прайса", "volume": "фасовка", "qty": количество_в_штуках, '
                 '"box_qty": количество_коробок_или_null, "price": цена_из_прайса}]}')
    parts.append('- "phone": если сотрудник указал телефон клиента — строкой, иначе null.')
    parts.append('- "client": полное имя контрагента, как его назвал сотрудник. У многих клиентов '
                 'в имени есть город или село, где находится их аптека («Асан Токмок», '
                 '«Джумгалбек Кара-Балта») — это ЧАСТЬ ИМЕНИ клиента, включай её в "client".')
    parts.append('- "warehouse": заполняй ТОЛЬКО если явно прозвучало слово «склад» '
                 '(«со склада Ош», «склад Бишкек»). Просто название города рядом с именем '
                 'клиента — НЕ склад, а часть имени; в этом случае warehouse = null '
                 '(бот сам подставит склад сотрудника).')
    parts.append('- "debt": заполняй ТОЛЬКО если сотрудник сам явно указал долг числом (например «долг 31470»). '
                 'Старый долг существующих клиентов бот подставит из базы автоматически — не выдумывай его.')
    parts.append('- "payment": если вместе с накладной указан приход/оплата (например «приход 5000»), иначе 0.')
    parts.append("")
    parts.append("=== РЕЖИМ 3: ПРИХОД ДЕНЕГ (без товаров) ===")
    parts.append('Если сообщение — только оплата без товаров («Асан приход 5000», «Асан оплатил 3000»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "payment", "client": "Имя контрагента", "amount": сумма, "warehouse": null}')
    parts.append('- "warehouse": имя склада, если явно указан («со склада Ош»), иначе null.')
    parts.append("")
    parts.append("=== РЕЖИМ 4: ПРИХОД / ПЕРЕМЕЩЕНИЕ ТОВАРА ===")
    parts.append('Если сообщение — пополнение склада товаром или перемещение между складами '
                 '(например «Беке: Альтопен 100мл 2к», «на склад Манас: ...», '
                 '«с Бишкека на Каракол: ...», «нужно на Каракол: ...»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "transfer", "from_warehouse": null_или_имя_склада, "to_warehouse": "имя склада", '
                 '"items": [{"name": "...", "volume": "...", "qty": штук, "box_qty": коробок_или_null, '
                 '"price": цена, "expiry": null_или_срок}]}')
    parts.append('- Если назван сотрудник — подставь имя склада этого сотрудника в to_warehouse '
                 '(список сотрудников и складов — в блоке ниже).')
    parts.append('- "from_warehouse" заполняй только при явном перемещении «с X на Y»; '
                 'приход товара извне (с завода/базы) — null.')
    parts.append('- "expiry": срок годности, если указан рядом с товаром («срок 11.2028», '
                 '«11/28», «до 12.27») — строкой в формате "MM.YYYY" (месяц двумя цифрами, '
                 'год четырьмя); не указан — null.')
    parts.append("ВАЖНО: не путай накладную с приходом. Выбирай transfer ТОЛЬКО если есть "
                 "явные слова прихода («приход», «привезли», «пополнение», «на склад X: ...» "
                 "в начале), перемещения («с X на Y»), или первое слово — точное имя "
                 "сотрудника из списка в блоке ниже.")
    parts.append("Если в сообщении имя человека и товары — это накладная (invoice), ДАЖЕ если "
                 "упомянут склад: склад пиши в поле warehouse, а не делай transfer. Имена "
                 "клиентов бывают похожи на названия складов и имена сотрудников (учти, что "
                 "голосовые распознаются с ошибками). Если сомневаешься, накладная это или "
                 "приход — не выбирай ничего, спроси текстом.")
    parts.append("Перемещение между складами проводит только админ — заявка обычного "
                 "сотрудника уйдёт ему на подтверждение, это нормально.")
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
    parts.append("=== РЕЖИМ 7: СПЕЦЦЕНЫ КЛИЕНТА (только админ и старшие) ===")
    parts.append('Если сообщение задаёт индивидуальную цену («цена для Асана: '
                 'Альтопен 100мл 85», «спеццена для Асана ...»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "set_price", "client": "Имя", "warehouse": null, '
                 '"items": [{"name": "точное название из прайса", "volume": "фасовка", "price": цена}]}')
    parts.append('- price: 0 означает убрать спеццену (вернётся цена прайса).')
    parts.append("")
    parts.append("=== РЕЖИМ 8: ВОЗВРАТ ТОВАРА ОТ КЛИЕНТА ===")
    parts.append('Если клиент возвращает товар («возврат от Асана: Альтопен 100мл 5 шт»), '
                 'верни ТОЛЬКО JSON:')
    parts.append('{"action": "return", "client": "Имя", "warehouse": null, '
                 '"items": [{"name": "точное название из прайса", "volume": "фасовка", '
                 '"qty": штук, "box_qty": коробок_или_null, "price": цена, '
                 '"price_explicit": false, "expiry": null_или_срок}]}')
    parts.append('- price бери из прайса; если сотрудник сам явно написал цену возврата '
                 '(например «по 85») — поставь её и "price_explicit": true.')
    parts.append('- "expiry": срок годности, если указан рядом с товаром '
                 '(«срок 11.2028», «11/28») — строкой "MM.YYYY"; нет — null '
                 '(бот сам спросит срок с упаковки, это нормально).')
    parts.append('- Коробки переводи в штуки как обычно. Возврат подтверждает админ.')
    parts.append("")
    parts.append("=== РЕЖИМ 10: ТЕЛЕФОН КЛИЕНТА ===")
    parts.append('Если сообщение сохраняет номер клиента («телефон Асана: 0700 12 34 56», '
                 '«номер Асана 0555443322»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "set_phone", "client": "Имя", "phone": "номер строкой", "warehouse": null}')
    parts.append("")
    parts.append("=== РЕЖИМ 9: СДАЧА ВЫРУЧКИ (ИНКАССАЦИЯ) ===")
    parts.append('Если сотрудник сдаёт наличные руководителю («сдал 50000», «сдаю выручку '
                 '50 000», «инкассация 30000»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "handover", "amount": сумма}')
    parts.append("")
    parts.append("=== РЕЖИМ 11: ИЗМЕНЕНИЕ ОБЩЕГО ПРАЙСА (только админ) ===")
    parts.append('Если админ меняет цену в общем прайсе — БЕЗ имени клиента '
                 '(«новая цена Альтопен 100мл — 95», «поменяй цену Дексатоп 50мл на 190», '
                 '«теперь Клозатоп 1л стоит 990»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "change_price", "items": [{"name": "точное название из прайса", '
                 '"volume": "фасовка", "price": новая_цена}]}')
    parts.append('- Не путай со спецценой клиента (set_price): там всегда названо имя клиента. '
                 'Здесь меняется прайс для всех.')
    parts.append("")
    parts.append("=== РЕЖИМ 12: ОБЕЩАНИЕ ОПЛАТЫ ===")
    parts.append('Если клиент пообещал заплатить («Асан обещал 50000 в пятницу», '
                 '«Болот заплатит 20000 пятнадцатого», «Асан обещал рассчитаться завтра»), '
                 'верни ТОЛЬКО JSON:')
    parts.append('{"action": "promise", "client": "Имя", "amount": сумма_или_0, "date": "YYYY-MM-DD"}')
    parts.append('- Дату вычисли из сегодняшней даты (в блоке ниже): «завтра», «в пятницу», '
                 '«15 числа» → конкретная дата ISO. Если дата не названа — поставь завтра.')
    parts.append('- Если сумма не названа — amount: 0.')
    parts.append('Если клиент ВЫПОЛНИЛ обещание («Асан выполнил обещание», «Асан закрыл '
                 'обещание») — верни ТОЛЬКО JSON: {"action": "promise_done", "client": "Имя"}')
    parts.append('- promise_done выбирай только при слове «обещание». Если названа сумма '
                 'принесённых денег («Асан принёс 5000») — это оплата, режим 3.')
    parts.append("")
    parts.append("=== РЕЖИМ 13: СПИСОК НОВЫХ КЛИЕНТОВ (только админ) ===")
    parts.append('Если админ передаёт список клиентов для справочника («добавь клиентов '
                 'на Каракол: Асан, Болот, ...», «новые клиенты: ...», или фото списка '
                 'клиентов), верни ТОЛЬКО JSON:')
    parts.append('{"action": "add_clients", "warehouse": null_или_имя_склада, '
                 '"clients": [{"name": "Полное имя", "debt": 0, "phone": null_или_телефон}]}')
    parts.append('- Имена пиши полностью, включая город/село («Джумгалбек Кара-Балта»).')
    parts.append('- "debt": стартовый долг клиента, если указан рядом с именем '
                 '(«Асан — 31470», «Болот долг 12000»); не указан — 0.')
    parts.append('- "phone": телефон, если указан рядом с именем; не указан — null.')
    parts.append('- Выбирай этот режим только при явных словах о добавлении клиентов, '
                 'без товаров.')
    parts.append("")
    parts.append("=== РЕЖИМ 14: ДОБАВИТЬ ТОВАР В ПОСЛЕДНЮЮ НАКЛАДНУЮ ===")
    parts.append('Если сотрудник дополняет уже выписанную накладную («добавь в последнюю '
                 'накладную Мустанга: Дексатоп 50мл 5 шт», «допиши к накладной Асана ...», '
                 '«забыл в накладную Асана добавить ...»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "amend_invoice", "client": "Имя", "warehouse": null, '
                 '"op_id": номер_накладной_или_null, '
                 '"items": [{"name": "точное название из прайса", "volume": "фасовка", '
                 '"qty": штук, "box_qty": коробок_или_null, "price": цена}]}')
    parts.append('- "op_id": если назван НОМЕР накладной («добавь в накладную '
                 '№45: ...») — обязательно заполни его; не назван — null '
                 '(возьмётся последняя накладная клиента).')
    parts.append('- В items — ТОЛЬКО добавляемые товары; старые бот подставит сам. '
                 'Коробки переводи в штуки как обычно. Если нужно изменить '
                 'количество или убрать товар — это режим 16 (замена целиком).')
    parts.append("")
    parts.append("=== РЕЖИМ 16: ЗАМЕНИТЬ НАКЛАДНУЮ ЦЕЛИКОМ ===")
    parts.append('Если сотрудник исправляет уже проведённую накладную — меняет '
                 'количество, убирает товар, переделывает состав («замени '
                 'накладную №45: Асан, Альтопен 100мл 10 шт, ...», «исправь '
                 'накладную Асана: ...», «в накладной №45 должно быть ...»), '
                 'верни ТОЛЬКО JSON:')
    parts.append('{"action": "replace_invoice", "op_id": номер_накладной_или_null, '
                 '"client": "Имя_или_null", "warehouse": null, '
                 '"items": [полный НОВЫЙ состав накладной, поля как в режиме 2]}')
    parts.append('- "op_id": номер накладной, если назван («накладную №45», '
                 '«накладная 45»); не назван — null.')
    parts.append('- Без номера обязательно имя клиента — заменится его ПОСЛЕДНЯЯ '
                 'накладная.')
    parts.append('- items — полный новый состав (все товары, которые должны '
                 'остаться), цены из прайса, коробки переводи в штуки.')
    parts.append('- Не путай с режимом 14: там товары ДОБАВЛЯЮТСЯ к старым, '
                 'здесь состав заменяется целиком.')
    parts.append("")
    parts.append("=== РЕЖИМ 15: СПИСАНИЕ ТОВАРА СО СКЛАДА ===")
    parts.append('Если товар убирают со склада без покупателя и без денег («спиши '
                 'Альтопен 100мл 5 шт — просрочка», «списание Каракол: Дексатоп 50мл '
                 '3 шт, бой», «утилизация ...», «разбилось 2 флакона ...», '
                 '«испортился ...»), верни ТОЛЬКО JSON:')
    parts.append('{"action": "writeoff", "warehouse": null_или_имя_склада, '
                 '"reason": "причина", '
                 '"items": [{"name": "точное название из прайса", "volume": "фасовка", '
                 '"qty": штук, "box_qty": коробок_или_null, "price": цена, '
                 '"expiry": null_или_срок}]}')
    parts.append('- "reason": причина коротко словами («просрочка», «бой», «порча», '
                 '«недостача», «утилизация»); не названа — "не указана".')
    parts.append('- "expiry": срок годности списываемой партии, если назван («срок '
                 '11.2028», «11/28») — строкой "MM.YYYY"; не назван — null '
                 '(бот спросит сам, это нормально).')
    parts.append('- price бери из прайса — она нужна для суммы списания. '
                 'Коробки переводи в штуки как обычно.')
    parts.append('- Не путай с возвратом (там есть клиент, товар приходит НА склад) '
                 'и с инвентаризацией (там фактический остаток, а не сколько убрать).')
    parts.append('- Списание проводит только админ — заявка сотрудника уйдёт ему '
                 'на подтверждение, это нормально.')
    parts.append("")
    parts.append("=== РЕЖИМ 17: ЗАКУП И КУРС (только админ) ===")
    parts.append('Закупочная цена товара — слова «закуп», «закупочная цена», '
                 '«себестоимость» («закупочная цена Дексатоп 50мл 0.47», '
                 '«закуп Пенстоп-G 100мл 2.5 доллара», можно списком), '
                 'верни ТОЛЬКО JSON:')
    parts.append('{"action": "set_buy_price", "currency": "usd", '
                 '"items": [{"name": "точное название из прайса", '
                 '"volume": "фасовка", "price": цена_за_штуку}]}')
    parts.append('- currency: "usd" по умолчанию (закупают в долларах); '
                 'если явно сказано «сом» — "som".')
    parts.append('- price — цена за ОДНУ штуку (единицу прайса); количества '
                 'здесь нет, коробки в штуки не переводи.')
    parts.append('Курс доллара («курс 88», «курс доллара 87.5») → '
                 '{"action": "set_usd_rate", "rate": число}')
    parts.append('Накладные расходы на закуп («накладные расходы 10%», '
                 '«расходы на растаможку 12 процентов») → '
                 '{"action": "set_buy_markup", "percent": число}')
    parts.append('- Не путай с режимом 11: там ПРОДАЖНАЯ цена прайса, здесь — '
                 'закупочная (слова «закуп», «курс», «расходы»).')
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
    parts.append("=== РЕЖИМ: ПСЕВДОНИМЫ КЛИЕНТОВ (только админ) ===")
    parts.append('«Вика Уманец — она же Виктория» / «запомни: Валя это Валентина и Валя '
                 'Липатова» → верни ТОЛЬКО JSON: {"action": "client_alias", '
                 '"client": "основное имя клиента", "warehouse": null, '
                 '"aliases": ["Имя1", "Имя2"]}')
    parts.append('- client — имя из справочника; aliases — дополнительные имена. '
                 'Если в сообщении назван склад («склад Каракол», «на Караколе») — '
                 'заполни warehouse; иначе null (бот сам найдёт клиента по складам). '
                 'Несколько клиентов за раз — несколько сообщений, обработай первого '
                 'и попроси прислать остальных отдельно.')
    parts.append("")
    parts.append("=== ДРУГИЕ ПРАВИЛА ===")
    parts.append("- Цены бери СТРОГО из прайса")
    parts.append("- Если товар не найден — напиши текстом что не нашёл")
    parts.append("- Если что-то неясно — уточни у сотрудника текстом")
    parts.append("- Имя контрагента обязательно для накладной и прихода денег")
    parts.append('- Если сообщение начинается с «проведи за <Имя>:» или «за <Имя>:», '
                 'где <Имя> — имя СОТРУДНИКА (из списка сотрудников в динамическом '
                 'блоке), добавь в JSON действия поле "as_employee": "<Имя>". '
                 'В остальных случаях это поле не добавляй.')
    parts.append('- В динамическом блоке будет список «Известные клиенты». '
                 'Сообщения часто приходят из распознавания голоса с ошибками '
                 'в именах: если имя клиента в сообщении созвучно одному из '
                 'списка — подставь точное имя из списка. Если не похоже ни '
                 'на одно — это новый клиент: оформляй с именем как есть, '
                 'НЕ спрашивай подтверждения и не проверяй существование.')
    parts.append("")
    parts.append("ПРАЙС-ЛИСТ (формат: №. Название | Фасовка | шт/кор | цена):")
    parts.append(prices.PRICE_LIST_TEXT)
    parts.append("")
    parts.append("Сотрудники пишут и диктуют по-русски или по-кыргызски (бывает "
                 "вперемешку) — понимай оба языка. Названия товаров всё равно "
                 "сопоставляй с прайсом, числительные переводи в цифры.")
    parts.append("Отвечай кратко, на языке сотрудника (по умолчанию — на русском).")
    return "\n".join(parts)


# Строится один раз: байт-в-байт одинаковый для всех запросов — иначе кэш не работает.
STATIC_SYSTEM = _build_static_system()


def _static_prompt_problems() -> list:
    """Самопроверка экономии при старте: статичный блок обязан быть
    байт-в-байт одинаковым от вызова к вызову и без сегодняшней даты —
    иначе кэш промпта (90% скидка на правила+прайс) молча перестаёт
    работать, и каждый запрос платит полную цену. Список проблем пустой,
    когда всё в порядке; найденное уходит тревогой админу в _post_init."""
    problems = []
    if _build_static_system() != _build_static_system():
        problems.append("статичный блок промпта собирается по-разному "
                        "от вызова к вызову")
    if datetime.now(BISHKEK).strftime("%d.%m.%Y") in STATIC_SYSTEM:
        problems.append("в статичный блок промпта попала сегодняшняя дата")
    return problems


def _refresh_price_dependents():
    """После изменения прайса: пересобрать системный промпт и подсказку Whisper.
    Кэш промпта при этом обновится один раз — это нормально."""
    global STATIC_SYSTEM, STT_PROMPT
    STATIC_SYSTEM = _build_static_system()
    STT_PROMPT = _build_stt_prompt()


# Известные клиенты дёргаются на каждое сообщение (фильтр чатов складов,
# динамический промпт) — а данные меняются редко. Кэш на 2 минуты.
_KNOWN_CLIENTS_CACHE = {"ts": 0.0, "data": {}}


def known_clients_cached(limit: int, wh_ids: list = None) -> list:
    """Имена клиентов с кэшем 2 мин; wh_ids — только эти склады (для
    промпта сотрудника), None — вся база (фильтр чатов, подсказка STT)."""
    now = time.monotonic()
    if now - _KNOWN_CLIENTS_CACHE["ts"] > 120:
        _KNOWN_CLIENTS_CACHE["data"] = {}
        _KNOWN_CLIENTS_CACHE["ts"] = now
    key = tuple(sorted(wh_ids)) if wh_ids is not None else None
    if key not in _KNOWN_CLIENTS_CACHE["data"]:
        _KNOWN_CLIENTS_CACHE["data"][key] = db.known_client_names(300, wh_ids)
    return _KNOWN_CLIENTS_CACHE["data"][key][:limit]


def build_dynamic_system(actor) -> str:
    """Маленький изменяемый блок: дата, сотрудник, склады."""
    own = db.warehouse_of(actor["id"])
    visible = db.visible_warehouses(actor)
    role = {"admin": "админ", "senior": "старший"}.get(actor["role"], "обычный сотрудник")
    lines = [
        f"Сегодня: {datetime.now(BISHKEK).strftime('%d.%m.%Y')}.",
        f"Сотрудник: {actor['name']} (роль: {role}).",
        f"Его склад по умолчанию: «{own['name'] if own else '—'}».",
        "Склады, доступные сотруднику: "
        + (", ".join(f"«{w['name']}»" for w in visible) or "—") + ".",
        "Сотрудники и их склады по умолчанию:",
    ]
    for u in db.list_users():
        w = db.warehouse_of(u["id"])
        if w:
            lines.append(f"- {u['name']} → склад «{w['name']}»")
    lines.append("Все склады: " + ", ".join(f"«{w['name']}»" for w in db.all_warehouses()))
    # Только имена клиентов СКЛАДОВ СОТРУДНИКА (правила про список — в
    # статичном кэшируемом блоке; чужие склады сотруднику не показываем).
    wh_ids = None if is_admin(actor) else [w["id"] for w in visible]
    known = known_clients_cached(40, wh_ids)
    if known:
        lines.append("Известные клиенты: " + ", ".join(known) + ".")
    return "\n".join(lines)


def system_blocks(actor) -> list:
    """Системный промпт с кэшированием статичной части (~90% скидка на чтениях).

    TTL 1 час (а не 5 минут по умолчанию): сообщения приходят с перерывами
    больше 5 минут, и короткий кэш почти всегда протухал — каждый запрос
    платил за запись. Часовой кэш пишется один раз (2x вместо 1.25x), дальше
    чтения по 0.1x, и каждое обращение продлевает его ещё на час."""
    return [
        {"type": "text", "text": STATIC_SYSTEM,
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": build_dynamic_system(actor)},
    ]


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


# Цены Anthropic, $ за миллион токенов: (вход, выход).
# Кэш: чтение = 10% от входа, запись часового кэша = 200% от входа.
MODEL_PRICES_USD = {
    "claude-sonnet-5": (2.0, 10.0),    # акция до 31.08.2026, потом (3.0, 15.0)
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
}


def _request_cost_usd(model: str, input_tokens: int, output_tokens: int,
                      cache_read: int, cache_write: int) -> float:
    p_in, p_out = MODEL_PRICES_USD.get(model, (3.0, 15.0))
    return (input_tokens * p_in + output_tokens * p_out
            + cache_read * p_in * 0.1 + cache_write * p_in * 2.0) / 1_000_000


def track_usage(resp):
    """Пишет стоимость каждого запроса к API в базу (для /api)."""
    try:
        u = resp.usage
        inp = u.input_tokens or 0
        out = u.output_tokens or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        cost = _request_cost_usd(CLAUDE_MODEL, inp, out, cr, cw)
        db.record_api_usage(CLAUDE_MODEL, inp, out, cr, cw, cost)
    except Exception:
        log.warning("Не удалось записать расход API", exc_info=True)


def _response_text(resp) -> str:
    """Текст ответа. У Sonnet 5 первым блоком может идти размышление —
    берём первый текстовый блок, а не content[0]."""
    return next((b.text for b in resp.content if b.type == "text"), "").strip()


# Лимит длины ответа. Оплачиваются только реально сгенерированные токены,
# поэтому запас ничего не стоит; 2500 не хватало на длинные списки с фото
# (21.07.2026 ответ обрезался на середине JSON — бот показал сырой JSON).
CLAUDE_MAX_TOKENS = 8000


async def ask_claude(history: list, actor) -> str:
    resp = await anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=system_blocks(actor),
        messages=history,
    )
    track_usage(resp)
    return _response_text(resp)


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
            "price_explicit": bool(it.get("price_explicit")),
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
                ", ".join(f"«{esc(w['name'])}»" for w in db.operable_warehouses(actor))
        if not db.can_use_warehouse(actor, wh["id"]):
            if db.can_view_warehouse(actor, wh["id"]):
                return None, (f"👁 У вас доступ к складу «{esc(wh['name'])}» только "
                              "на ПРОСМОТР — операции проводит команда склада "
                              "(или админ).")
            return None, f"⛔ У вас нет доступа к складу «{esc(wh['name'])}»."
        return wh, None
    own = db.warehouse_of(actor["id"])
    if own is None:
        return None, "У вас нет собственного склада — обратитесь к администратору."
    return own, None


# ---------- Накладная ----------

def apply_client_prices(p):
    """Подставляет спеццены клиента в позиции накладной (после выбора клиента)."""
    if not p.get("client_id"):
        return
    special = db.client_prices_map(p["client_id"])
    if not special:
        return
    for it in p["items"]:
        pid = it.get("product_id")
        # Явно названную сотрудником цену не трогаем
        if pid in special and not it.get("price_explicit"):
            it["price"] = special[pid]
            it["special"] = True


def insufficient_stock(wh_id: int, items, extra_available: dict | None = None):
    """Каких позиций не хватает на складе: [(name, volume, есть, нужно)].
    extra_available — количества, которые вернутся на склад перед проведением
    (например, при замене накладной)."""
    need = {}
    for it in items:
        pid = it.get("product_id")
        if pid:
            need[pid] = need.get(pid, 0) + it["qty"]
    lack = []
    for pid, qty in need.items():
        have = db.stock_qty(wh_id, pid) + (extra_available or {}).get(pid, 0)
        if have < qty:
            p = prices.BY_ID.get(pid)
            lack.append((p["name"] if p else f"товар №{pid}",
                         p["volume"] if p else "", have, qty))
    return lack


def lack_message(lack) -> str:
    lines = ["⛔ Не хватает товара на складе — накладная НЕ проведена:"]
    for name, volume, have, qty in lack:
        lines.append(f"• {esc(name)} {esc(volume)}: на складе {have} шт, "
                     f"в накладной {qty} шт")
    lines.append("Уменьшите количество или пополните склад. "
                 "Черновик выписать можно всегда.")
    return "\n".join(lines)


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
        special = " 💲спеццена" if it.get("special") else ""
        lines.append(f"{i}. {esc(it['name'])} {esc(it['volume'])} — {box}{it['qty']} шт × "
                     f"{fmt_num(it['price'])}{special} = <b>{money(sub)}</b>")
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


def _debt_suffix(new_debt: float) -> str:
    """Хвост сводки операции: долг клиента после неё — виден сразу в ленте,
    уведомлении админу и /log (просьба владельца 08.08.2026)."""
    if new_debt > 0:
        return f", долг: {fmt_num(new_debt)} сом"
    if new_debt < 0:
        return f", переплата: {fmt_num(-new_debt)} сом"
    return ", долг погашен"


def commit_invoice(p, replace_op_id=None):
    """Проводит накладную: клиент, остатки, долг, журнал. Возвращает детали для PDF.

    replace_op_id — замена накладной (amend): сторно старой и проведение новой
    идут одной транзакцией в базе, чтобы сбой между ними не оставил учёт
    со снятой накладной без замены."""
    total = sum(it["qty"] * it["price"] for it in p["items"])
    cid = p["client_id"]
    create = None
    debt_delta = total - p["payment"]
    if cid is None:
        # Клиент создаётся с долгом 0, а стартовый долг входит в дельту
        # операции: тогда /undo снимает и его, и долг восстановим из журнала.
        create = (p["wh_id"], p["client_name"], 0, p.get("phone"))
        debt_delta += p["parsed_debt"]
        old_debt = p["parsed_debt"]
        client_label = p["client_name"]
    else:
        c = db.client_get(cid)
        if c is None:
            # Клиента могли удалить, пока заявка ждала кнопки (/resetwh)
            raise ValueError("клиент не найден — возможно, справочник "
                             "сбрасывали; отправьте накладную заново")
        old_debt = c["debt"]
        client_label = c["name"]
        if p.get("phone"):
            db.client_set_phone(cid, p["phone"])
        if replace_op_id:
            # Долг «до» для PDF/подписей — БЕЗ заменяемой накладной: её
            # сторно происходит внутри replace_operation, а c["debt"] здесь
            # ещё содержит старую накладную. Иначе итог на бумаге завышен
            # на её сумму (жалоба владельца 22.07.2026), хотя база верна.
            old = db.get_operation(replace_op_id)
            if old:
                try:
                    odata = json.loads(old["data"])
                    old_part = (float(odata.get("total") or 0)
                                - float(odata.get("payment") or 0))
                    for ocid, d in odata.get("debt_deltas", []):
                        if ocid == cid:
                            # Дельта старой накладной могла включать стартовый
                            # долг нового клиента (сверх «товар − оплата») —
                            # сторно снимет её целиком, поэтому остаток
                            # переносим в новую операцию, иначе стартовый долг
                            # молча пропадал при замене (найдено 02.08.2026).
                            old_debt -= old_part
                            debt_delta += d - old_part
                except (ValueError, TypeError):
                    pass
    # Дельты агрегируются по товару: план партий применяется один раз на
    # товар, и товар двумя строками (две партии) списывается правильно.
    qty_by_pid = {}
    for it in p["items"]:
        if it.get("product_id"):
            qty_by_pid[it["product_id"]] = (qty_by_pid.get(it["product_id"], 0)
                                            + it["qty"])
    stock_deltas = [(p["wh_id"], pid, -q) for pid, q in qty_by_pid.items()]
    summary = f"Накладная: {client_label} — {fmt_num(total)} сом (склад {p['wh_name']})"
    if p["payment"]:
        summary += f", приход {fmt_num(p['payment'])} сом"
    summary += _debt_suffix(old_debt + total - p["payment"])
    extra = {
        "items": [{**{k: it[k] for k in ("name", "volume", "qty", "price",
                                         "box_qty")},
                   "product_id": it.get("product_id")} for it in p["items"]],
        "total": total, "payment": p["payment"], "old_debt": old_debt,
    }
    # Выбор партий продавцом (кнопки «какую партию продать?»): пустой выбор
    # или его отсутствие = «сначала старые» (FEFO внутри _apply_batches).
    batch_plan = {}
    for i, it in enumerate(p["items"]):
        exp = _choice_batch((p.get("batch_choices") or {}).get(str(i)))
        if exp is not None and it.get("product_id"):
            batch_plan.setdefault((p["wh_id"], it["product_id"]), []).append(
                (exp, it["qty"]))
    if replace_op_id:
        op_id, _ = db.replace_operation(
            replace_op_id, p.get("op_user_id") or p["user_id"], "invoice",
            p["wh_id"], cid, summary, stock_deltas, [(cid, debt_delta)], extra,
            batch_plan=batch_plan or None,
        )
    else:
        op_id, _ = db.commit_operation(
            p["user_id"], "invoice", p["wh_id"], cid, summary,
            stock_deltas, [(cid, debt_delta)], extra, create_client=create,
            batch_plan=batch_plan or None,
        )
    return op_id, client_label, old_debt, total, summary


async def send_invoice_pdf(context, chat_id, client_label, p, old_debt, total,
                           draft=False, caption=None, op_id=None):
    pdf = generate_pdf_invoice(
        client_label, p["items"], total,
        prev_debt=old_debt, payment=p["payment"], is_payment=p["payment"] > 0,
        warehouse_name=p["wh_name"], draft=draft, watermark=DRAFT_WATERMARK,
        doc_number=op_id,  # у черновика номера нет — он не операция журнала
    )
    date_str = datetime.now(BISHKEK).strftime("%d%m%Y")
    marked = draft and DRAFT_WATERMARK
    prefix = "черновик" if marked else "накладная"
    filename = f"{prefix}_{safe_filename(client_label)}_{date_str}.pdf"
    if caption is None:
        caption = ("📝 Черновик — не проведено, остатки и долги не изменены"
                   if draft else f"📄 Накладная для {client_label}")
        parse_mode = None
    else:
        parse_mode = "HTML"  # подпись ленты приходит уже с разметкой
    await context.bot.send_document(
        chat_id=chat_id, document=InputFile(pdf, filename=filename),
        caption=caption, parse_mode=parse_mode,
    )


async def start_invoice(update, context, actor, data, draft=False):
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    if not draft and TRANSITION_MODE and not wh["full_mode"]:
        # Переходный период: накладная автоматически становится черновиком.
        # Склады в полном режиме (/fullmode) работают по-настоящему.
        draft = True
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
    phone = str(data.get("phone") or "").strip() or None

    if draft:
        c = db.client_exact(wh["id"], client_name)
        old_debt = c["debt"] if c else parsed_debt
        if c:
            apply_client_prices({"client_id": c["id"], "items": items})
        total = sum(it["qty"] * it["price"] for it in items)
        p = {"items": items, "payment": payment, "wh_name": wh["name"]}
        await send_invoice_pdf(context, update.effective_chat.id,
                               c["name"] if c else client_name, p, old_debt, total, draft=True)
        # В журнал — после успешной отправки, чтобы при сбое не задвоить сводку
        db.log_draft(actor["id"], c["name"] if c else client_name, total,
                     [{"name": it["name"], "volume": it["volume"], "qty": it["qty"],
                       "sum": it["qty"] * it["price"]} for it in items])
        return

    # Реальная накладная: больше, чем есть на складе, выписать нельзя
    # (решение владельца 21.07.2026).
    lack = insufficient_stock(wh["id"], items)
    if lack:
        await update.message.reply_text(lack_message(lack), parse_mode="HTML")
        return

    payload = {
        "kind": "invoice", "user_id": actor["id"], "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"],
        "client_name": client_name, "client_id": None,
        "items": items, "warnings": warnings,
        "payment": payment, "parsed_debt": parsed_debt, "phone": phone,
    }

    exact = db.client_exact(wh["id"], client_name)
    if exact:
        payload["client_id"] = exact["id"]
        apply_client_prices(payload)
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


# ---------- Сертификаты соответствия ----------
# На каждый препарат/поставку есть сертификат минсельхоза (просьба владельца
# 08.08.2026): админ шлёт боту PDF/фото с подписью «сертификат Название»,
# сотрудники получают его командой /cert Название и пересылают клиентам.
# Файл храним на серверах Telegram (file_id) — база не растёт.

CERT_CAPTION_RE = re.compile(r"^\s*сертификат(?:ы)?\s*(?:на\s+)?(.*)$",
                             re.IGNORECASE | re.DOTALL)
# «сертификат поставки F260403» — файл на ВСЮ поставку, без привязки
# к препарату (просьба владельца 08.08.2026: один PDF на поставку)
CERT_SUPPLY_RE = re.compile(r"^поставк\w*\s+(.+)$", re.IGNORECASE | re.DOTALL)

# Последний файл админа в личке: подпись «сертификат …» часто приходит
# ОТДЕЛЬНЫМ сообщением после файла (так удобнее с телефона, случай
# владельца 08.08.2026 — файл молча игнорировался, текст уходил в ИИ).
CERT_PENDING_DOC = {}              # (chat_id, user_id) -> данные файла
CERT_PENDING_TTL = 15 * 60


def _remember_cert_doc(update, file_id, kind, file_name):
    key = (update.effective_chat.id, update.effective_user.id)
    CERT_PENDING_DOC[key] = {"file_id": file_id, "kind": kind,
                             "file_name": file_name, "ts": time.time()}


async def cert_text_reply(update, actor, text) -> bool:
    """Текст «сертификат …» в личке (не подпись к файлу). Админ с недавно
    присланным файлом — привязываем файл; иначе подсказка. True = учтено."""
    key = (update.effective_chat.id, update.effective_user.id)
    pend = CERT_PENDING_DOC.get(key)
    if is_admin(actor) and pend and time.time() - pend["ts"] <= CERT_PENDING_TTL:
        CERT_PENDING_DOC.pop(key, None)
        await _handle_cert_upload(update, pend["file_id"], pend["kind"],
                                  pend["file_name"], text)
        return True
    if is_admin(actor):
        await update.message.reply_text(
            "Получить сертификат: /cert Название (список: /cert).\n"
            "Добавить: пришлите файл с подписью «сертификат Название» — "
            "или сначала файл, а подпись следующим сообщением.")
    else:
        await update.message.reply_text(
            "Сертификаты выдаёт команда /cert Название.\nЧто есть: /cert")
    return True


def _cert_short_names():
    """Названия препаратов без фасовок и расшифровок в скобках."""
    names, seen = [], set()
    for pr in prices.PRICE_LIST_DATA:
        nm = pr["name"].split("(")[0].strip()
        if nm and nm.lower() not in seen:
            seen.add(nm.lower())
            names.append(nm)
    for nm, _ in db.cert_names():   # препараты со старыми сертификатами
        if nm.lower() not in seen:
            seen.add(nm.lower())
            names.append(nm)
    return names


def _cert_product_match(query: str):
    """Препарат по названию: точно → подстрока → difflib.
    Возвращает (имя | None, кандидаты для уточнения)."""
    q = query.strip().lower()
    if not q:
        return None, []
    names = _cert_short_names()
    for nm in names:
        if nm.lower() == q:
            return nm, []
    subs = [nm for nm in names if q in nm.lower()]
    if len(subs) == 1:
        return subs[0], []
    if subs:
        return None, subs[:6]
    by_lower = {nm.lower(): nm for nm in names}
    close = difflib.get_close_matches(q, list(by_lower), n=3, cutoff=0.6)
    if len(close) == 1:
        return by_lower[close[0]], []
    return None, [by_lower[c] for c in close]


async def _handle_cert_upload(update, file_id, kind, file_name, caption):
    """PDF/фото с подписью «сертификат …» от админа — сохранить привязку."""
    actor = await get_actor(update)
    if actor is None:
        return
    if not is_admin(actor):
        await update.message.reply_text(
            "Сертификаты добавляет только администратор.")
        return
    m = CERT_CAPTION_RE.match(caption)
    rest = (m.group(1) if m else "").strip()
    note = None
    for dash in ("—", " - ", "–"):
        if dash in rest:
            rest, note = [x.strip() for x in rest.split(dash, 1)]
            break
    if not rest:
        await update.message.reply_text(
            "Подпишите файл так: «сертификат Альтопен» (несколько препаратов — "
            "через запятую, примечание — после тире: «сертификат Альтопен — "
            "поставка 07.2026»).")
        return
    m_sup = CERT_SUPPLY_RE.match(rest)
    if m_sup:
        label = " ".join(m_sup.group(1).split())
        name = f"Поставка {label}"
        db.cert_add(name, file_id, kind, file_name, note)
        await update.message.reply_text(
            f"✅ Сертификат сохранён: {name}\n"
            f"Сотрудники получат его командой /cert поставка {label}")
        return
    ok, bad = [], []
    for part in [p.strip() for p in rest.split(",") if p.strip()]:
        name, cands = _cert_product_match(part)
        if name is None:
            hint = f" (похоже на: {', '.join(cands)})" if cands else ""
            bad.append(part + hint)
        else:
            db.cert_add(name, file_id, kind, file_name, note)
            ok.append(name)
    lines = []
    if ok:
        lines.append("✅ Сертификат сохранён: " + ", ".join(ok))
        lines.append(f"Сотрудники получат его командой /cert {ok[0].lower()}")
    if bad:
        lines.append("⚠️ Не распознал препарат: " + "; ".join(bad)
                     + ". Напишите название как в прайсе.")
    await update.message.reply_text("\n".join(lines))


# Контракты СТАРЫХ поставок: их сертификаты догружены позже свежих и не
# должны становиться дефолтом /cert (видны через «/cert Название все»).
# Новую старую поставку достаточно вписать сюда либо добавить слово
# «старая» в примечание подписи.
CERT_OLD_SUPPLIES = ("F241009",)


def _cert_default(certs):
    """Какой сертификат слать по умолчанию: самый свежий ЗАГРУЖЕННЫЙ,
    кроме помеченных старыми (словом «старая» или контрактом из
    CERT_OLD_SUPPLIES) — те только по запросу «все»."""
    def is_old(c):
        note = c["note"] or ""
        return ("стар" in note.lower()
                or any(s in note for s in CERT_OLD_SUPPLIES))
    fresh = [c for c in certs if not is_old(c)]
    return (fresh or certs)[:1]


async def _send_cert_files(update, name, certs):
    for c in certs:
        cap = f"📜 Сертификат: {name}"
        if c["note"]:
            cap += f" — {c['note']}"
        try:
            cap += " (добавлен " + datetime.fromisoformat(
                c["ts"]).strftime("%d.%m.%Y") + ")"
        except ValueError:
            pass
        if c["file_kind"] == "photo":
            await update.message.reply_photo(c["file_id"], caption=cap)
        else:
            await update.message.reply_document(c["file_id"], caption=cap)


async def cert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cert Название[, ещё название...] — сертификаты соответствия."""
    actor = await get_actor(update)
    if actor is None:
        return
    args = list(context.args or [])
    send_all = False
    if args and args[-1].lower() in ("все", "всё", "all"):
        send_all = True
        args = args[:-1]
    query = " ".join(args).strip()
    if not query:
        have = db.cert_names()
        if not have:
            await update.message.reply_text(
                "📜 Сертификатов в боте пока нет.\n"
                "Админ добавляет их так: прислать боту PDF или фото с подписью "
                "«сертификат Название препарата».")
            return
        lines = ["📜 Сертификаты есть на препараты:"]
        lines += [f"• {nm}" + (f" — {k} шт" if k > 1 else "")
                  for nm, k in have]
        lines += ["", "Получить: /cert Название"]
        await send_long(update.message, "\n".join(lines))
        return
    # Несколько препаратов через запятую — пачкой одной командой
    # (просьба владельца 08.08.2026): /cert альтопен, албенивер, дексатоп
    parts = [x.strip() for x in query.split(",") if x.strip()]
    if len(parts) > 1:
        missing = []
        for part in parts:
            name, cands = _cert_product_match(part)
            if name is None:
                hint = f" (уточните: {', '.join(cands)})" if cands else ""
                missing.append(part + hint)
                continue
            certs = db.certs_of(name)
            if not certs:
                missing.append(f"{part} (сертификата пока нет)")
                continue
            await _send_cert_files(update, name, _cert_default(certs))
        if missing:
            await update.message.reply_text(
                "⚠️ Не отправил: " + "; ".join(missing))
        return
    name, cands = _cert_product_match(query)
    if name is None:
        if cands:
            await update.message.reply_text(
                "Уточните препарат: " + ", ".join(cands))
        else:
            await update.message.reply_text(
                f"Препарат «{query}» не нашёл. Список с сертификатами: /cert")
        return
    certs = db.certs_of(name)
    if not certs:
        await update.message.reply_text(
            f"На «{name}» сертификата в боте пока нет. Что есть: /cert")
        return
    await _send_cert_files(update, name,
                           certs if send_all else _cert_default(certs))
    if not send_all and len(certs) > 1:
        await update.message.reply_text(
            f"Это самый свежий из {len(certs)}. Прислать все: "
            f"/cert {name.lower()} все")


async def certdel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/certdel Название — админ удаляет сертификаты препарата."""
    if await _require_admin(update) is None:
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text("Использование: /certdel Название")
        return
    name, cands = _cert_product_match(query)
    if name is None:
        await update.message.reply_text(
            "Уточните препарат: " + ", ".join(cands) if cands
            else f"Препарат «{query}» не нашёл.")
        return
    n = db.certs_delete(name)
    await update.message.reply_text(
        f"🗑 Удалено сертификатов «{name}»: {n}." if n
        else f"У «{name}» сертификатов не было.")


# ---------- Телефон клиента ----------

def amend_summary(p, old_total: float) -> str:
    c = db.client_get(p["client_id"])
    total = sum(it["qty"] * it["price"] for it in p["items"])
    # долг клиента без старой накладной (её сторнируем при проведении)
    debt_before = c["debt"] - (old_total - p["payment"])
    what = ("будет отменена, вместо неё НОВЫЙ состав:" if p.get("full_replace")
            else "будет отменена, вместо неё:")
    lines = [f"🔁 <b>Замена накладной №{p['old_op_id']}</b> — склад «{esc(p['wh_name'])}»",
             f"👤 Клиент: <b>{esc(c['name'])}</b>",
             f"Старая накладная на {money(old_total)} {what}", ""]
    for i, it in enumerate(p["items"], 1):
        sub = it["qty"] * it["price"]
        box = f"{it['box_qty']} кор / " if it.get("box_qty") else ""
        mark = " 🆕" if i > p["old_count"] and not p.get("full_replace") else ""
        lines.append(f"{i}. {esc(it['name'])} {esc(it['volume'])} — {box}{it['qty']} шт × "
                     f"{fmt_num(it['price'])} = <b>{money(sub)}</b>{mark}")
    lines += ["", f"🧾 Сумма новой накладной: <b>{money(total)}</b>"]
    if p["payment"]:
        lines.append(f"💵 Приход (из старой накладной): {money(p['payment'])}")
    lines.append(f"📊 Итог долг клиента: <b>{money(debt_before + total - p['payment'])}</b>")
    if p["warnings"]:
        lines.append("")
        lines += [f"⚠️ {esc(w)}" for w in p["warnings"]]
    lines += ["", "Заменить накладную?"]
    return "\n".join(lines)


async def _invoice_by_op_id(update, actor, op_id_raw):
    """Накладная по номеру для дополнения/замены: проверки типа, статуса и
    доступа к складу. Возвращает (op, wh) или (None, None) — сообщение об
    ошибке уже отправлено."""
    try:
        op = db.get_operation(int(str(op_id_raw).lstrip("№# ").strip()))
    except (TypeError, ValueError, OverflowError):
        op = None
    if op is None or op["type"] != "invoice":
        await update.message.reply_text(
            f"Накладная №{esc(str(op_id_raw).lstrip('№# '))} не найдена. "
            "Номер смотрите в /log, /invoices или на PDF накладной.",
            parse_mode="HTML")
        return None, None
    if op["status"] != "done":
        await update.message.reply_text(
            f"Накладная №{op['id']} уже отменена — просто выпишите новую.")
        return None, None
    wh = db.warehouse_by_id(op["warehouse_id"])
    if wh is None:
        await update.message.reply_text("Склад этой накладной не найден.")
        return None, None
    if not is_admin(actor) and not db.can_use_warehouse(actor, wh["id"]):
        await update.message.reply_text(
            "⛔ Эта накладная со склада, где вы не проводите операции.")
        return None, None
    return op, wh


def _replace_rights_error(actor, op):
    """Кто вправе заменить накладную: админ — любую, сотрудник — только
    СВОЮ и не старше часа (окно UNDO_MINUTES). None — можно."""
    if is_admin(actor):
        return None
    if op["user_id"] != actor["id"]:
        return ("⛔ Эту накладную выписал другой сотрудник — заменить её "
                "может только админ.")
    try:
        ts = datetime.fromisoformat(op["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=BISHKEK)
        age = (datetime.now(BISHKEK) - ts).total_seconds()
    except ValueError:
        age = UNDO_WINDOW + 1  # непонятная дата — считаем, что окно вышло
    if age > UNDO_WINDOW:
        return ("⛔ С момента накладной прошло больше часа — заменить её "
                "может только админ (попросите его).")
    return None


async def start_amend_invoice(update, context, actor, data):
    if data.get("op_id"):
        # «Добавь в накладную №45: …» — номер важнее имени клиента (раньше
        # номер молча терялся и дополнялась ПОСЛЕДНЯЯ накладная клиента —
        # аудит 02.08.2026). Склад определяет сама накладная.
        op, wh = await _invoice_by_op_id(update, actor, data["op_id"])
        if op is None:
            return
        if TRANSITION_MODE and not wh["full_mode"]:
            await update.message.reply_text(
                "На этом складе сейчас черновики — просто выпишите черновик "
                "заново целиком, старый PDF выбросьте.")
            return
        c = db.client_get(op["client_id"]) if op["client_id"] else None
        if c is None:
            await update.message.reply_text(
                "Клиент этой накладной не найден в справочнике — дополнить нельзя.")
            return
    else:
        wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
        if err:
            await update.message.reply_text(err, parse_mode="HTML")
            return
        if TRANSITION_MODE and not wh["full_mode"]:
            await update.message.reply_text(
                "На этом складе сейчас черновики — просто выпишите черновик заново "
                "целиком, старый PDF выбросьте.")
            return
        client_name = str(data.get("client") or "").strip()
        if not client_name:
            await update.message.reply_text("Не понял, чью накладную дополнить.")
            return
        c = db.client_exact(wh["id"], client_name)
        if c is None:
            cand = db.fuzzy_clients(wh["id"], client_name)
            if len(cand) == 1:
                c = cand[0]
            else:
                await update.message.reply_text(
                    f"Клиент «{esc(client_name)}» не найден на складе «{esc(wh['name'])}»."
                    + (" Похожие: " + ", ".join(x["name"] for x in cand) if cand else ""),
                    parse_mode="HTML")
                return
        op = db.last_invoice_for_client(c["id"])
        if op is None:
            await update.message.reply_text(
                f"У клиента «{esc(c['name'])}» нет проведённых накладных — "
                "выпишите обычную накладную.", parse_mode="HTML")
            return
    err = _replace_rights_error(actor, op)
    if err:
        await update.message.reply_text(err)
        return
    try:
        new_items, warnings = parse_items(data.get("items") or [])
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    old = json.loads(op["data"])
    old_items = []
    for it in old.get("items", []):
        product = prices.match_product(str(it.get("name") or ""), str(it.get("volume") or ""))
        old_items.append({
            "name": it.get("name"), "volume": it.get("volume"), "qty": it.get("qty"),
            "price": it.get("price"), "box_qty": it.get("box_qty"),
            "product_id": product["id"] if product else None,
            "price_explicit": True,  # цены старой накладной не пересчитываем
        })
    old_total = sum((it["qty"] or 0) * (it["price"] or 0) for it in old_items)
    back = {}
    for it in old_items:
        if it.get("product_id"):
            back[it["product_id"]] = back.get(it["product_id"], 0) + it["qty"]
    lack = insufficient_stock(wh["id"], old_items + new_items, extra_available=back)
    if lack:
        await update.message.reply_text(lack_message(lack), parse_mode="HTML")
        return
    payload = {
        "kind": "amend_invoice", "user_id": actor["id"],
        # Новая накладная проводится от имени АВТОРА старой (иначе при замене
        # админом приход «переехал» бы из кассы сотрудника в кассу админа).
        "op_user_id": op["user_id"],
        "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"],
        "client_name": c["name"], "client_id": c["id"],
        "items": old_items + new_items, "warnings": warnings,
        "payment": float(old.get("payment") or 0), "parsed_debt": 0, "phone": None,
        "old_op_id": op["id"], "old_count": len(old_items),
    }
    apply_client_prices(payload)
    token = new_pending(payload)
    await update.message.reply_text(amend_summary(payload, old_total),
                                    parse_mode="HTML", reply_markup=confirm_kb(token))


async def start_replace_invoice(update, context, actor, data):
    """«Замени накладную №45: <полный новый состав>» — сторно старой и
    проведение новой одной транзакцией (тот же механизм и та же карточка
    подтверждения, что у «добавь в последнюю», kind amend_invoice), но
    состав заменяется целиком: так можно изменить количество и убрать
    товар. Без номера — последняя накладная названного клиента."""
    op_id_raw = data.get("op_id")
    if op_id_raw:
        op, wh = await _invoice_by_op_id(update, actor, op_id_raw)
        if op is None:
            return
    else:
        wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
        if err:
            await update.message.reply_text(err, parse_mode="HTML")
            return
        client_name = str(data.get("client") or "").strip()
        if not client_name:
            await update.message.reply_text(
                "Не понял, какую накладную заменить. Назовите номер "
                "(«замени накладную №45: ...») или клиента "
                "(«замени накладную Асана: ...»).")
            return
        c0 = db.client_exact(wh["id"], client_name)
        if c0 is None:
            cand = db.fuzzy_clients(wh["id"], client_name)
            if len(cand) == 1:
                c0 = cand[0]
            else:
                await update.message.reply_text(
                    f"Клиент «{esc(client_name)}» не найден на складе «{esc(wh['name'])}»."
                    + (" Похожие: " + ", ".join(x["name"] for x in cand) if cand else ""),
                    parse_mode="HTML")
                return
        op = db.last_invoice_for_client(c0["id"])
        if op is None:
            await update.message.reply_text(
                f"У клиента «{esc(c0['name'])}» нет проведённых накладных.",
                parse_mode="HTML")
            return
    if TRANSITION_MODE and not wh["full_mode"]:
        await update.message.reply_text(
            "На этом складе сейчас черновики — просто выпишите черновик заново "
            "целиком, старый PDF выбросьте.")
        return
    err = _replace_rights_error(actor, op)
    if err:
        await update.message.reply_text(err)
        return
    c = db.client_get(op["client_id"]) if op["client_id"] else None
    if c is None:
        await update.message.reply_text(
            "Клиент этой накладной не найден в справочнике — заменить нельзя.")
        return
    try:
        new_items, warnings = parse_items(data.get("items") or [])
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    if not new_items:
        await update.message.reply_text(
            "Перечислите ПОЛНЫЙ новый состав накладной — все товары, которые "
            "должны в ней остаться. Отменить её целиком может админ: /undo "
            f"{op['id']}.")
        return
    old = json.loads(op["data"])
    old_total = float(old.get("total") or 0)
    # Сторно старой накладной вернёт её товар на склад — этот запас доступен
    # новой (как и в «добавь в последнюю», где old_count строк — старые).
    back = {}
    for wh_id_, pid_, d_ in old.get("stock_deltas", []):
        if wh_id_ == wh["id"] and d_ < 0:
            back[pid_] = back.get(pid_, 0) - d_
    lack = insufficient_stock(wh["id"], new_items, extra_available=back)
    if lack:
        await update.message.reply_text(lack_message(lack), parse_mode="HTML")
        return
    payload = {
        "kind": "amend_invoice", "user_id": actor["id"],
        # Проводится от имени АВТОРА старой накладной — деньги и журнал
        # остаются на нём (та же логика, что у дополнения накладной).
        "op_user_id": op["user_id"],
        "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"],
        "client_name": c["name"], "client_id": c["id"],
        "items": new_items, "warnings": warnings,
        "payment": float(old.get("payment") or 0), "parsed_debt": 0, "phone": None,
        "old_op_id": op["id"], "old_count": 0,
        "full_replace": True, "returned_qty": back,
    }
    apply_client_prices(payload)
    token = new_pending(payload)
    await update.message.reply_text(amend_summary(payload, old_total),
                                    parse_mode="HTML", reply_markup=confirm_kb(token))


def _clean_phone(raw: str):
    phone = re.sub(r"[^\d+]", "", str(raw or ""))
    return phone if len(re.sub(r"\D", "", phone)) >= 6 else None


async def start_set_phone(update, context, actor, data):
    wh_name = str(data.get("warehouse") or "").strip()
    wh, err = resolve_warehouse(actor, wh_name)
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    client_name = str(data.get("client") or "").strip()
    phone = _clean_phone(data.get("phone"))
    if not client_name or not phone:
        await update.message.reply_text(
            "Не понял клиента или номер. Пример: «телефон Асана: 0700 12 34 56»")
        return
    exact = db.client_exact(wh["id"], client_name)
    if exact is None and not wh_name:
        # Склад не указан, и на складе по умолчанию клиента нет — ищем точное
        # имя по всем ОПЕРАЦИОННЫМ складам (запись телефона — не просмотр;
        # у админа это все склады).
        hits = []
        for w in db.operable_warehouses(actor):
            c_ = db.client_exact(w["id"], client_name)
            if c_ is not None:
                hits.append((w, c_))
        if len(hits) > 1:
            await update.message.reply_text(
                f"Клиент «{esc(client_name)}» есть на нескольких складах: "
                + ", ".join(f"«{esc(w['name'])}»" for w, _ in hits)
                + ". Укажите склад: «Каракол: телефон Асана 0700 12 34 56»",
                parse_mode="HTML")
            return
        if hits:
            wh, exact = hits[0]
    if exact:
        db.client_set_phone(exact["id"], phone)
        await update.message.reply_text(
            f"✅ Телефон клиента <b>{esc(exact['name'])}</b>: {esc(phone)}", parse_mode="HTML")
        return
    candidates = db.fuzzy_clients(wh["id"], client_name)
    if not candidates:
        await update.message.reply_text(
            f"❌ Клиент «{esc(client_name)}» не найден на складе «{esc(wh['name'])}».",
            parse_mode="HTML")
        return
    payload = {"kind": "set_phone", "user_id": actor["id"],
               "chat_id": update.effective_chat.id,
               "wh_id": wh["id"], "wh_name": wh["name"],
               "client_name": client_name, "client_id": None, "phone": phone}
    token = new_pending(payload)
    rows = [[InlineKeyboardButton(f"👤 {c['name']}", callback_data=f"pk:{token}:{c['id']}")]
            for c in candidates]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")])
    await update.message.reply_text(
        f"Клиент «<b>{esc(client_name)}</b>» не найден. Кому сохранить номер {esc(phone)}?",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


# ---------- Касса и инкассация ----------

def handover_summary(p) -> str:
    cash = db.cash_on_hand(p["user_id"])
    name = p.get("requester_name") or db.get_user(p["user_id"])["name"]
    lines = ["💰 <b>Сдача выручки</b>",
             f"✍️ Сотрудник: <b>{esc(name)}</b>",
             f"💵 Сдаёт: <b>{money(p['amount'])}</b>",
             f"🧮 В кассе по данным бота: {money(cash)}"]
    if p["amount"] > cash:
        lines.append(f"⚠️ Сдаёт больше, чем числится в кассе (разница {money(p['amount'] - cash)})")
    elif p["amount"] < cash:
        lines.append(f"📌 После сдачи в кассе останется {money(cash - p['amount'])}")
    lines.append("")
    lines.append("Принять деньги?")
    return "\n".join(lines)


def commit_handover(p):
    u = db.get_user(p["user_id"])
    # Склад операции: чат склада, где написали «сдал» (лента этого склада
    # увидит инкассацию), иначе родной склад сотрудника.
    wh = (db.warehouse_by_id(p["wh_id"]) if p.get("wh_id")
          else db.warehouse_of(p["user_id"]))
    summary = f"Инкассация: {u['name']} сдал {fmt_num(p['amount'])} сом"
    extra = {"amount": p["amount"]}
    if p.get("approver_id"):
        extra["approved_by"] = p["approver_id"]
    op_id, _ = db.commit_operation(
        p["user_id"], "handover", wh["id"] if wh else None, None,
        summary, [], [], extra)
    return op_id, summary


async def start_handover(update, context, actor, data):
    # Сдача выручки доступна, если ЛЮБОЙ доступный сотруднику склад в полном
    # режиме: касса общая, а выручка могла прийти с полного склада (Бека —
    # родной Бишкек на черновиках, но торгует на Кара-Балте; инцидент
    # 27.07.2026 — бот отвечал «переходный период»).
    if transition_blocked(actor) and not any(
            w["full_mode"] for w in db.operable_warehouses(actor)):
        await update.message.reply_text(TRANSITION_HINT)
        return
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        await update.message.reply_text("Не понял сумму. Пример: «сдал 50000».")
        return
    payload = {
        "kind": "handover", "user_id": actor["id"],
        "chat_id": update.effective_chat.id, "amount": amount,
    }
    feed_whs = (db.warehouses_of_feed(update.effective_chat.id)
                if update.effective_chat else [])
    if len(feed_whs) == 1 and db.can_use_warehouse(actor, feed_whs[0]["id"]):
        payload["wh_id"] = feed_whs[0]["id"]
    if is_admin(actor):
        token = new_pending(payload)
        await update.message.reply_text(handover_summary(payload), parse_mode="HTML",
                                        reply_markup=confirm_kb(token))
        return
    payload["approver_id"] = ADMIN_ID
    payload["requester_name"] = actor["name"]
    token = new_pending(payload, ttl=APPROVAL_TTL)
    try:
        await context.bot.send_message(
            ADMIN_ID, handover_summary(payload), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Принял деньги", callback_data=f"ok:{token}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{token}"),
            ]]))
    except Exception:
        log.exception("Не удалось отправить инкассацию админу")
        PENDING.pop(token, None)
        await update.message.reply_text("⚠️ Не удалось отправить админу. Попробуйте позже.")
        return
    await update.message.reply_text(
        f"📨 Сдача выручки {money(amount)} отправлена админу на подтверждение. "
        f"Касса уменьшится, когда он подтвердит приём денег.", parse_mode="HTML")


_AMOUNT_IN_SUMMARY_RE = re.compile(r"\s*(?:[—–-]|\+)?\s*[\d'’]+\s*сом")


def _cash_section(u, limit=15):
    """Секция PDF: движения кассы сотрудника хронологически (последнее внизу).
    Показываются движения ПОСЛЕ последней инкассации «под ноль» (просьба
    владельца 07.08.2026); если такой не было — последние limit движений.
    Возвращает (касса, число движений, секция)."""
    cash = db.cash_on_hand(u["id"])
    moves, since = db.cash_movements_since_zero(u["id"])
    if since is None:
        moves = moves[:limit]
    rows = []
    for op, amt in reversed(moves):
        try:
            d = datetime.fromisoformat(op["ts"]).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            d = op["ts"]
        sign = "+" if amt > 0 else "−"
        # Сумму из описания убираем — она уже показана в колонке «Сумма»
        what = _AMOUNT_IN_SUMMARY_RE.sub("", op["summary"], count=1).strip()
        rows.append([d, f"{sign}{fmt_num(abs(amt))}", what])
    if since:
        try:
            since_str = datetime.fromisoformat(since).strftime("%d.%m.%Y")
        except ValueError:
            since_str = since
        title = f"{u['name']} — движения после инкассации {since_str} (последнее внизу)"
        empty = "движений после инкассации ещё не было"
    else:
        title = f"{u['name']} — движения (последнее внизу)"
        empty = "движений по кассе ещё не было"
    sec = {"title": title,
           "headers": ["Дата", "Сумма", "Операция"],
           "rows": rows or [["—", "—", empty]],
           "widths": [30, 26, 126],
           "footer": f"Сейчас на руках: {money(cash)}"}
    return cash, len(moves), sec


async def _send_cash_pdf(message, target=None):
    """Касса PDF-файлом (просьба владельца 07.08.2026: «не список, а сразу
    PDF»). target — один сотрудник, None — все кассы одним файлом."""
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    if target is not None:
        cash, n_moves, sec = _cash_section(target, limit=20)
        if not n_moves and not cash:
            await message.reply_text(
                f"💰 Касса — {esc(target['name'])}: 0 сом, новых движений нет "
                "(всё сдано либо движений ещё не было).",
                parse_mode="HTML")
            return
        pdf = generate_report_pdf(
            "КАССА СОТРУДНИКА",
            f"ОсОО «ВЕТОП» · {target['name']} · на {date_str}", [sec])
        # safe_filename чистит имя БЕЗ расширения — точку «.pdf» она съедает,
        # и телефон не открывал файл (скриншот владельца 07.08.2026)
        fname = safe_filename(
            f"касса_{target['name']}_{date_str.replace('.', '')}") + ".pdf"
        await message.reply_document(
            document=InputFile(pdf, filename=fname),
            caption=f"💰 Касса — {target['name']}: на руках {money(cash)}")
        return
    summary_rows, sections, total = [], [], 0.0
    for u in db.list_users():
        cash, n_moves, sec = _cash_section(u, limit=10)
        if not n_moves and not cash:
            continue
        mark = " (админ)" if u["role"] == "admin" else ""
        summary_rows.append([u["name"] + mark, money(cash)])
        total += cash
        sections.append(sec)
    if not sections:
        await message.reply_text("💰 Кассы пусты — вся выручка сдана.")
        return
    sections.insert(0, {"title": "Наличные на руках",
                        "headers": ["Сотрудник", "На руках"],
                        "rows": summary_rows, "widths": [120, 62],
                        "footer": f"Всего в кассах: {money(total)}"})
    pdf = generate_report_pdf("КАССЫ СОТРУДНИКОВ",
                              f"ОсОО «ВЕТОП» · на {date_str}", sections)
    await message.reply_document(
        document=InputFile(pdf, filename=f"кассы_{date_str.replace('.', '')}.pdf"),
        caption=f"💰 В кассах всего: {money(total)}")


def _match_employee(arg: str, users):
    """Сотрудник по имени из аргумента (/cash Данияр): регистр не важен,
    небольшие опечатки прощаются; неоднозначность = не найден (кнопки)."""
    a = arg.lower()
    for u in users:
        if u["name"].lower() == a:
            return u
    sub = [u for u in users if a in u["name"].lower()]
    if len(sub) == 1:
        return sub[0]
    names = {u["name"].lower(): u for u in users}
    close = difflib.get_close_matches(a, list(names), n=1, cutoff=0.7)
    return names[close[0]] if close else None


async def cash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    if not await _require_private(update):
        return
    if is_admin(actor):
        users = db.list_users()
        employees = [u for u in users if u["role"] != "admin"]
        # /cash Данияр — сразу PDF этого сотрудника; /cash все — общий PDF
        arg = " ".join(context.args).strip() if context.args else ""
        if arg:
            if arg.lower() in ("все", "всех", "all"):
                await _send_cash_pdf(update.message)
                return
            u = _match_employee(arg, users)
            if u is not None:
                await _send_cash_pdf(update.message, u)
                return
            # имя не распознали — покажем кнопки выбора
        # Кнопки: каждый сотрудник по отдельности + все сразу
        # (просьба владельца 21.07.2026).
        if not employees:
            await _send_cash_pdf(update.message)
            return
        payload = {"kind": "pick_cash", "user_id": actor["id"],
                   "chat_id": update.effective_chat.id}
        token = new_pending(payload)
        kb = [[InlineKeyboardButton(f"👤 {u['name']}",
                                    callback_data=f"pc:{token}:{u['id']}")]
              for u in employees]
        kb.append([InlineKeyboardButton("👥 Все сразу", callback_data=f"pc:{token}:all")])
        kb.append([InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")])
        await update.message.reply_text("Чью кассу показать?",
                                        reply_markup=InlineKeyboardMarkup(kb))
        return
    cash = db.cash_on_hand(actor["id"])
    await update.message.reply_text(
        f"💰 Ваша касса: <b>{money(cash)}</b>\n"
        f"Сдать выручку: напишите «сдал {fmt_num(cash) if cash > 0 else '50000'}»",
        parse_mode="HTML")


# ---------- Возврат товара ----------

def return_summary(p) -> str:
    c = db.client_get(p["client_id"])
    lines = ["🔙 <b>Возврат товара — подтверждение</b>",
             f"🏬 Склад: <b>{esc(p['wh_name'])}</b>",
             f"👤 Клиент: <b>{esc(c['name'])}</b> (текущий долг: {money(c['debt'])})"]
    if p.get("requester_name"):
        lines.append(f"✍️ Заявка от: <b>{esc(p['requester_name'])}</b>")
    lines.append("")
    total = 0
    for i, it in enumerate(p["items"], 1):
        sub = it["qty"] * it["price"]
        total += sub
        box = f"{it['box_qty']} кор / " if it.get("box_qty") else ""
        special = " 💲спеццена" if it.get("special") else ""
        lines.append(f"{i}. {esc(it['name'])} {esc(it['volume'])} — {box}{it['qty']} шт × "
                     f"{fmt_num(it['price'])}{special} = <b>{money(sub)}</b>")
    lines.append("")
    lines.append(f"🔙 Сумма возврата: <b>{money(total)}</b>")
    new_debt = c["debt"] - total
    if new_debt < 0:
        lines.append(f"📊 Долг после возврата: {money(0)} (переплата {money(-new_debt)})")
    else:
        lines.append(f"📊 Долг после возврата: <b>{money(new_debt)}</b>")
    for w in p.get("warnings", []):
        lines.append(f"⚠️ {esc(w)}")
    lines.append("")
    lines.append("Товар вернётся на склад, долг клиента уменьшится. Провести возврат?")
    return "\n".join(lines)


def _arrival_batch_plan(wh_id, items):
    """План партий для прихода (возврат, плюс инвентаризации): явный срок
    позиции, иначе ближайшая существующая датированная партия товара —
    товар не проваливается в партию «без срока» и остаётся виден в /expiry."""
    plan = {}
    for it in items:
        pid, qty = it.get("product_id"), it.get("qty") or 0
        if not pid or qty <= 0:
            continue
        exp = it.get("expiry")
        if not exp:
            dated = [b for b in db.product_batches_of(wh_id, pid)
                     if b["expiry"]]
            exp = dated[0]["expiry"] if dated else ""
        if exp:
            plan.setdefault((wh_id, pid), []).append((exp, qty))
    return plan


def commit_return(p):
    c = db.client_get(p["client_id"])
    if c is None:
        raise ValueError("клиент не найден — возможно, справочник сбрасывали")
    old_debt = c["debt"]
    total = sum(it["qty"] * it["price"] for it in p["items"])
    # Дельты по товару агрегируются (план партий применяется один раз)
    qty_by_pid = {}
    for it in p["items"]:
        if it.get("product_id"):
            qty_by_pid[it["product_id"]] = (qty_by_pid.get(it["product_id"], 0)
                                            + it["qty"])
    stock_deltas = [(p["wh_id"], pid, q) for pid, q in qty_by_pid.items()]
    batch_plan = _arrival_batch_plan(p["wh_id"], p["items"])
    summary = (f"Возврат: {c['name']} — {fmt_num(total)} сом "
               f"(склад {p['wh_name']})" + _debt_suffix(old_debt - total))
    extra = {
        "items": [{**{k: it[k] for k in ("name", "volume", "qty", "price",
                                         "box_qty")},
                   "product_id": it.get("product_id"),
                   "expiry": it.get("expiry")} for it in p["items"]],
        "total": total, "old_debt": old_debt,
    }
    if p.get("approver_id"):
        extra["approved_by"] = p["approver_id"]
    op_id, _ = db.commit_operation(
        p["user_id"], "return", p["wh_id"], p["client_id"], summary,
        stock_deltas, [(p["client_id"], -total)], extra,
        batch_plan=batch_plan or None)
    return op_id, c["name"], old_debt, total, summary


async def send_return_pdf(context, chat_id, client_label, p, old_debt, total):
    pdf = generate_pdf_invoice(
        client_label, p["items"], total, warehouse_name=p["wh_name"],
        doc_title="ВОЗВРАТ ТОВАРА", total_label="Сумма возврата",
        extra_totals=[("Долг до возврата", old_debt),
                      ("Долг после возврата", max(old_debt - total, 0))])
    date_str = datetime.now(BISHKEK).strftime("%d%m%Y")
    filename = f"возврат_{safe_filename(client_label)}_{date_str}.pdf"
    await context.bot.send_document(
        chat_id=chat_id, document=InputFile(pdf, filename=filename),
        caption=f"🔙 Возврат товара от {client_label}")


async def start_return(update, context, actor, data):
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    if transition_blocked(actor) and not wh["full_mode"]:
        await update.message.reply_text(TRANSITION_HINT)
        return
    client_name = str(data.get("client") or "").strip()
    if not client_name:
        await update.message.reply_text("Не понял, от какого клиента возврат.")
        return
    try:
        items, warnings = parse_items(data.get("items") or [])
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    # Срок годности возврата, если указан («срок 11.2028») — как у прихода
    for it, raw in zip(items, data.get("items") or []):
        if isinstance(raw, dict) and raw.get("expiry"):
            exp = _norm_expiry(raw["expiry"])
            if exp:
                it["expiry"] = exp

    payload = {
        "kind": "return", "user_id": actor["id"], "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"],
        "client_name": client_name, "client_id": None,
        "items": items, "warnings": warnings,
    }

    async def _proceed():
        apply_client_prices(payload)
        # Возврат уменьшает долг и увеличивает склад — проводит только админ.
        if not is_admin(actor):
            payload["approver_id"] = ADMIN_ID
            payload["requester_name"] = actor["name"]
            token = new_pending(payload, ttl=APPROVAL_TTL)
            try:
                await context.bot.send_message(
                    ADMIN_ID, return_summary(payload), parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Провести", callback_data=f"ok:{token}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{token}"),
                    ]]))
            except Exception:
                log.exception("Не удалось отправить возврат админу")
                PENDING.pop(token, None)
                await update.message.reply_text("⚠️ Не удалось отправить админу. Попробуйте позже.")
                return
            c = db.client_get(payload["client_id"])
            await update.message.reply_text(
                f"📨 Возврат от «{esc(c['name'])}» отправлен админу на подтверждение. "
                f"Я сообщу результат.", parse_mode="HTML")
        else:
            token = new_pending(payload)
            await update.message.reply_text(return_summary(payload), parse_mode="HTML",
                                            reply_markup=confirm_kb(token))

    exact = db.client_exact(wh["id"], client_name)
    if exact:
        payload["client_id"] = exact["id"]
        await _proceed()
        return
    candidates = db.fuzzy_clients(wh["id"], client_name)
    if not candidates:
        await update.message.reply_text(
            f"❌ Клиент «{esc(client_name)}» не найден на складе «{esc(wh['name'])}». "
            f"Возврат возможен только от существующего клиента.", parse_mode="HTML")
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
    summary = (f"Приход: {c['name']} — {fmt_num(p['amount'])} сом "
               f"(склад {p['wh_name']})"
               + _debt_suffix(old_debt - p["amount"]))
    op_id, _ = db.commit_operation(
        p["user_id"], "payment", p["wh_id"], p["client_id"], summary,
        [], [(p["client_id"], -p["amount"])],
        {"amount": p["amount"], "old_debt": old_debt},
    )
    return op_id, c["name"], old_debt, summary


async def start_payment(update, context, actor, data):
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    if transition_blocked(actor) and not wh["full_mode"]:
        await update.message.reply_text(TRANSITION_HINT)
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

def _norm_expiry(raw) -> str:
    """«11/28», «11.28», «до 12.2027» -> "MM.YYYY"; непонятное -> ""."""
    m = re.search(r"(\d{1,2})\s*[./-]\s*(\d{2,4})", str(raw or ""))
    if not m:
        return ""
    mm, year = int(m.group(1)), int(m.group(2))
    if not 1 <= mm <= 12:
        return ""
    if year < 100:
        year += 2000
    if not 2000 <= year <= 2099:
        return ""
    return f"{mm:02d}.{year}"


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
        exp = f" · срок {it['expiry']}" if it.get("expiry") else ""
        lines.append(f"{i}. {esc(it['name'])} {esc(it['volume'])} — {box}{it['qty']} шт{exp}")
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


async def send_transfer_pdf(context, chat_id, p, op_id, summary, caption=None):
    """PDF прихода/перемещения: позиции, количества, сроки годности."""
    rows, total = [], 0
    for it in p["items"]:
        box = f"{it['box_qty']} кор / " if it.get("box_qty") else ""
        rows.append([it["name"], it["volume"], f"{box}{it['qty']} шт",
                     it.get("expiry") or "—"])
        total += it["qty"]
    if p.get("from_wh_id"):
        title = "ПЕРЕМЕЩЕНИЕ ТОВАРА"
        where = f"{p['from_wh_name']} → {p['wh_name']}"
    else:
        title = "ПРИХОД ТОВАРА"
        where = f"склад «{p['wh_name']}»"
    now = datetime.now(BISHKEK)
    pdf = generate_report_pdf(
        title,
        f"ОсОО «ВЕТОП» · {where} · {now.strftime('%d.%m.%Y %H:%M')} · "
        f"операция №{op_id}",
        [{"headers": ["Товар", "Фасовка", "Количество", "Срок годности"],
          "rows": rows, "widths": [82, 26, 32, 27], "numbered": True,
          "footer": f"Итого: {len(rows)} позиций · {fmt_num(total)} шт"}])
    await context.bot.send_document(
        chat_id=chat_id,
        document=InputFile(pdf, filename=(
            f"приход_{safe_filename(p['wh_name'])}_"
            f"{now.strftime('%d%m%Y_%H%M')}.pdf")),
        caption=caption or f"📦 {summary} (операция №{op_id})",
        parse_mode="HTML" if caption else None)


def commit_transfer(p):
    # Дельты по товару агрегируются (товар мог идти несколькими строками с
    # разными сроками). План партий: у перемещения src_batches (выбор партии
    # или FEFO-раскладка из _finish_transfer) списывает конкретные партии
    # источника И переносит их сроки на склад-получатель; у прихода извне
    # срок позиции ложится партией получателя.
    dest, src, plan = {}, {}, {}
    for it in p["items"]:
        pid = it.get("product_id")
        if not pid:
            continue
        dest[pid] = dest.get(pid, 0) + it["qty"]
        if p["from_wh_id"]:
            src[pid] = src.get(pid, 0) + it["qty"]
        rows = it.get("src_batches")
        if rows is None:
            rows = [(it["expiry"], it["qty"])] if it.get("expiry") else []
        if p["from_wh_id"]:
            for exp, q_ in rows:
                plan.setdefault((p["from_wh_id"], pid), []).append((exp, q_))
        # Получателю срок позиции важнее партий источника: сотрудник мог
        # назвать его в ответ на вопрос бота, а на источнике товар лежал
        # без срока — уезжает он уже со сроком.
        dest_rows = [(it["expiry"], it["qty"])] if it.get("expiry") else rows
        for exp, q_ in dest_rows:
            if exp:
                plan.setdefault((p["wh_id"], pid), []).append((exp, q_))
    stock_deltas = [(p["wh_id"], pid, q) for pid, q in dest.items()]
    stock_deltas += [(p["from_wh_id"], pid, -q) for pid, q in src.items()]
    n = len(p["items"])
    if p["from_wh_id"]:
        summary = f"Перемещение: {p['from_wh_name']} → {p['wh_name']} ({n} поз.)"
    else:
        summary = f"Приход товара на склад {p['wh_name']} ({n} поз.)"
    extra = {"items": [{k: it.get(k) for k in ("name", "volume", "qty",
                                               "box_qty", "expiry",
                                               "product_id")}
                       for it in p["items"]]}
    if p.get("approver_id"):
        extra["approved_by"] = p["approver_id"]
    op_id, _ = db.commit_operation(
        p["user_id"], "transfer", p["wh_id"], None, summary, stock_deltas, [], extra,
        batch_plan=plan or None,
    )
    return op_id, summary


async def start_transfer(update, context, actor, data):
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
    if transition_blocked(actor) and not (
            to_wh["full_mode"] or (from_wh and from_wh["full_mode"])):
        await update.message.reply_text(TRANSITION_HINT)
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
    # Сроки годности прихода: parse_items их не знает — переносим из
    # исходных позиций (порядок совпадает) с нормализацией «11/28» и т.п.
    for it, raw in zip(items, data.get("items") or []):
        if isinstance(raw, dict) and raw.get("expiry"):
            exp = _norm_expiry(raw["expiry"])
            if exp:
                it["expiry"] = exp
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


# ---------- Спеццены ----------

def add_clients_summary(p) -> str:
    entries = p["entries"]
    shown = entries[:30]
    total_debt = sum(e[1] for e in entries)
    lines = [f"👥 <b>Новые клиенты</b> — склад «{esc(p['wh_name'])}» "
             f"({len(entries)} чел.)", ""]
    for i, e in enumerate(shown, 1):
        n, debt = e[0], e[1]
        phone = e[2] if len(e) > 2 else None
        lines.append(f"{i}. {esc(n)}" + (f" — долг {money(debt)}" if debt else "")
                     + (" 📞" if phone else ""))
    if len(entries) > 30:
        lines.append(f"… и ещё {len(entries) - 30}")
    if p["skipped"]:
        lines.append(f"\nУже есть в базе (пропущу, долг им не меняю): {len(p['skipped'])}")
    if total_debt:
        lines.append(f"\n💰 Стартовые долги, итого: <b>{money(total_debt)}</b>")
    lines.append("\nДобавить в справочник?")
    return "\n".join(lines)


async def start_add_clients(update, context, actor, data):
    if not is_admin(actor):
        await update.message.reply_text("⛔ Справочник клиентов пополняет только админ.")
        return
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    seen, entries, skipped = set(), [], []
    for raw in (data.get("clients") or []):
        phone = None
        if isinstance(raw, dict):
            name = str(raw.get("name") or "").strip()
            try:
                debt = float(raw.get("debt") or 0)
            except (TypeError, ValueError):
                debt = 0.0
            if raw.get("phone"):
                phone = _clean_phone(raw.get("phone")) or None
        else:
            name, debt = str(raw or "").strip(), 0.0
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        if db.client_exact(wh["id"], name):
            skipped.append(name)
        else:
            entries.append((name, debt, phone))
    if not entries:
        await update.message.reply_text(
            "Все названные клиенты уже есть в базе — добавлять нечего." if skipped
            else "Не понял имена клиентов — перечислите их через запятую.")
        return
    payload = {"kind": "add_clients", "user_id": actor["id"],
               "chat_id": update.effective_chat.id,
               "wh_id": wh["id"], "wh_name": wh["name"],
               "entries": entries, "skipped": skipped}
    token = new_pending(payload)
    await update.message.reply_text(add_clients_summary(payload), parse_mode="HTML",
                                    reply_markup=confirm_kb(token))


async def start_promise(update, context, actor, data):
    client = str(data.get("client") or "").strip()
    if not client:
        await update.message.reply_text("Не понял имя клиента — скажите ещё раз.")
        return
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    due_raw = str(data.get("date") or "").strip()
    try:
        due = datetime.fromisoformat(due_raw).date()
    except ValueError:
        await update.message.reply_text(
            "Не понял дату. Скажите, например: «Асан обещал 50000 в пятницу».")
        return
    db.promise_add(client, amount, due.isoformat(), actor["id"])
    amt = f"{money(amount)} " if amount else ""
    await update.message.reply_text(
        f"📅 Записал: {esc(client)} обещал {amt}к {due.strftime('%d.%m.%Y')}.\n"
        f"Утром в этот день напомню. Все обещания: /promises", parse_mode="HTML")
    if actor["id"] != ADMIN_ID:
        await notify_admin(context, actor,
                           f"обещание: {client} — {money(amount) if amount else 'сумма не названа'} "
                           f"к {due.strftime('%d.%m.%Y')}")


async def start_promise_done(update, context, actor, data):
    client = str(data.get("client") or "").strip()
    if not client:
        await update.message.reply_text("Не понял имя клиента — скажите ещё раз.")
        return
    # Сотрудник закрывает только СВОИ записи об обещаниях (чужие мог бы
    # закрыть случайной фразой); админ — любые.
    n = db.promises_close(client, None if is_admin(actor) else actor["id"])
    if n:
        await update.message.reply_text(
            f"✅ Отметил: {esc(client)} выполнил обещание. Молодец!", parse_mode="HTML")
        if actor["id"] != ADMIN_ID:
            await notify_admin(context, actor, f"{client} выполнил обещание оплаты")
    else:
        await update.message.reply_text(
            f"У клиента «{esc(client)}» нет открытых обещаний"
            + ("" if is_admin(actor) else ", записанных вами")
            + ". Список: /promises",
            parse_mode="HTML")


def _promise_line(r, today, with_author=False) -> str:
    due = datetime.fromisoformat(r["due_date"]).date()
    amt = money(r["amount"]) if r["amount"] else "сумма не названа"
    if due < today:
        when = f"⚠️ просрочено {(today - due).days} дн."
    elif due == today:
        when = "сегодня"
    else:
        when = due.strftime("%d.%m.%Y")
    line = f"• {esc(r['client'])}: <b>{amt}</b> — {when}"
    if with_author:
        line += f" (записал {esc(r['user_name'])})"
    return line


async def promises_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    admin = is_admin(actor)
    rows = db.promises_open(None if admin else actor["id"])
    if not rows:
        await update.message.reply_text(
            "📅 Открытых обещаний нет.\n"
            "Записать: «Асан обещал 50000 в пятницу»")
        return
    today = datetime.now(BISHKEK).date()
    lines = ["📅 <b>Обещания оплаты</b>", ""]
    lines += [_promise_line(r, today, with_author=admin) for r in rows]
    lines += ["", "Когда клиент заплатит: «Асан выполнил обещание»"]
    await send_long(update.message, "\n".join(lines))


# Час утреннего напоминания об обещаниях (по Бишкеку)
PROMISE_HOUR = int(os.environ.get("PROMISE_HOUR", "9"))


async def promise_reminder_loop(app):
    """Каждое утро: кому сегодня обещали заплатить (и что просрочено)."""
    while True:
        now = datetime.now(BISHKEK)
        target = now.replace(hour=PROMISE_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            today = datetime.now(BISHKEK).date()
            if not db.claim_daily_job(f"promises:{today.isoformat()}"):
                continue  # другой экземпляр бота (перекрытие деплоя) уже шлёт
            rows = db.promises_due(today.isoformat())
            if not rows:
                continue
            header = f"📅 <b>Обещания оплаты на {today.strftime('%d.%m.%Y')}</b>"
            tail = "\nКогда клиент заплатит: «Асан выполнил обещание»"
            # Админу — всё, каждому сотруднику — записанные им
            text = "\n".join([header, ""] +
                             [_promise_line(r, today, with_author=True) for r in rows] + [tail])
            try:
                await app.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
            except Exception:
                # Сбой отправки админу не должен срывать рассылку сотрудникам
                log.warning("Не удалось отправить напоминание об обещаниях админу")
            by_author = {}
            for r in rows:
                if r["user_id"] != ADMIN_ID:
                    by_author.setdefault(r["user_id"], []).append(r)
            for uid, items in by_author.items():
                text = "\n".join([header, ""] +
                                 [_promise_line(r, today) for r in items] + [tail])
                try:
                    await app.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                except Exception:
                    log.warning("Не удалось отправить напоминание об обещаниях %s", uid)
        except Exception:
            log.exception("Ошибка напоминания об обещаниях")


async def start_client_alias(update, context, actor, data):
    """«Вика Уманец — она же Виктория»: дополнительные имена клиента (админ)."""
    if not is_admin(actor):
        await update.message.reply_text("⛔ Псевдонимы клиентов настраивает только админ.")
        return
    name = str(data.get("client") or "").strip()
    aliases = [str(a).strip() for a in (data.get("aliases") or []) if str(a).strip()]
    if not name or not aliases:
        await update.message.reply_text(
            "Пример: «Вика Уманец — она же Виктория, Виктория Уманец»")
        return
    wh_name = str(data.get("warehouse") or "").strip()
    if wh_name:
        wh, err = resolve_warehouse(actor, wh_name)
        if err:
            await update.message.reply_text(err, parse_mode="HTML")
            return
        search_whs = [wh]
    else:
        # Склад не указан — ищем клиента по всем доступным складам:
        # у админа он обычно один и тот же человек только на одном складе.
        search_whs = db.visible_warehouses(actor)
    hits = []
    for w in search_whs:
        c_ = db.client_exact(w["id"], name)
        if c_ is not None:
            hits.append((w, c_))
    if not hits and len(search_whs) == 1:
        cand = db.fuzzy_clients(search_whs[0]["id"], name)
        if len(cand) == 1:
            hits = [(search_whs[0], cand[0])]
    if not hits:
        where = (f"на складе «{esc(search_whs[0]['name'])}»" if len(search_whs) == 1
                 else "ни на одном складе")
        await update.message.reply_text(
            f"Клиент «{esc(name)}» не найден {where}. Проверьте имя: /debts",
            parse_mode="HTML")
        return
    if len(hits) > 1:
        await update.message.reply_text(
            f"Клиент «{esc(name)}» есть на нескольких складах: "
            + ", ".join(f"«{esc(w['name'])}»" for w, _ in hits)
            + ". Укажите склад: «склад Каракол: Барахан — он же …»",
            parse_mode="HTML")
        return
    wh, c = hits[0]
    aliases = [a for a in aliases if a.lower() != c["name"].lower()]
    clash = []
    for a in aliases:
        other = db.client_exact(wh["id"], a)
        if other is not None and other["id"] != c["id"]:
            clash.append(f"«{a}» — уже клиент «{other['name']}»")
    if clash:
        await update.message.reply_text(
            "⚠️ Эти имена уже заняты, псевдонимы не сохранены:\n"
            + "\n".join(clash) + "\nУберите занятые имена и отправьте снова.")
        return
    if not aliases:
        await update.message.reply_text("Новых имён не осталось — нечего сохранять.")
        return
    payload = {"kind": "client_alias", "user_id": actor["id"],
               "chat_id": update.effective_chat.id, "client_id": c["id"],
               "client_name": c["name"], "aliases": aliases, "wh_name": wh["name"]}
    token = new_pending(payload)
    await update.message.reply_text(
        f"🏷 Клиент «{esc(c['name'])}» (склад «{esc(wh['name'])}») получит "
        f"дополнительные имена: {esc(', '.join(aliases))}.\n"
        "Бот будет понимать эти имена в оплатах, накладных и голосовых. Сохранить?",
        parse_mode="HTML", reply_markup=confirm_kb(token))


def change_price_summary(p) -> str:
    lines = [f"🏷 <b>Изменение прайса</b> ({len(p['items'])} поз.)", ""]
    for i, it in enumerate(p["items"], 1):
        lines.append(f"{i}. {esc(it['name'])} {esc(it['volume'])}: "
                     f"{fmt_num(it['old_price'])} → <b>{fmt_num(it['price'])} сом</b>")
    lines += ["", "Новая цена подставится во все будущие накладные. Провести?"]
    return "\n".join(lines)


async def start_change_price(update, context, actor, data):
    if not is_admin(actor):
        await update.message.reply_text("⛔ Цены общего прайса меняет только админ.")
        return
    items, missing = [], []
    for raw in (data.get("items") or []):
        name = str(raw.get("name") or "")
        volume = str(raw.get("volume") or "")
        product = prices.match_product(name, volume)
        if product is None:
            missing.append(f"{name} {volume}".strip())
            continue
        try:
            price = float(raw.get("price"))
        except (TypeError, ValueError):
            missing.append(f"{name} {volume} (не понял цену)")
            continue
        if price <= 0:
            missing.append(f"{name} {volume} (цена должна быть больше нуля)")
            continue
        items.append({"product_id": product["id"], "name": product["name"],
                      "volume": product["volume"], "old_price": product["price"],
                      "price": price})
    if missing:
        await update.message.reply_text(
            "⚠️ Не понял позиции: " + "; ".join(missing) +
            "\nНичего не менял — напишите ещё раз точнее.")
        return
    if not items:
        await update.message.reply_text("Не понял, какие цены менять.")
        return
    payload = {"kind": "change_price", "user_id": actor["id"],
               "chat_id": update.effective_chat.id, "items": items}
    token = new_pending(payload)
    await update.message.reply_text(change_price_summary(payload), parse_mode="HTML",
                                    reply_markup=confirm_kb(token))


async def start_set_buy_price(update, context, actor, data):
    """«Закупочная цена Дексатоп 50мл 0.47» (админ) — в долларах за единицу
    прайса; «... 41 сом» переводится в доллары по текущему курсу."""
    if not is_admin(actor):
        await update.message.reply_text("⛔ Закупочные цены задаёт только админ.")
        return
    # Закуп — секрет от сотрудников: карточку с ценами нельзя публиковать
    # в групповом чате склада (сообщение с суммами осталось бы в истории
    # группы навсегда, даже если кнопку не нажать) — аудит 02.08.2026.
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text(
            "🔒 Закупочные цены — только в личке с ботом, здесь их увидят все.")
        return
    rate = usd_rate()
    cur = str(data.get("currency") or "usd").lower()
    if cur == "som" and rate <= 0:
        await update.message.reply_text(
            "Сначала задайте курс доллара: напишите, например, «курс 87.5».")
        return
    items, missing = [], []
    for raw in (data.get("items") or []):
        name = str(raw.get("name") or "")
        volume = str(raw.get("volume") or "")
        product = prices.match_product(name, volume)
        if product is None:
            missing.append(f"{name} {volume}".strip())
            continue
        try:
            price = float(raw.get("price"))
        except (TypeError, ValueError):
            missing.append(f"{name} {volume} (не понял цену)")
            continue
        if price <= 0:
            missing.append(f"{name} {volume} (цена должна быть больше нуля)")
            continue
        usd = round(price / rate, 4) if cur == "som" else price
        items.append({"product_id": product["id"], "name": product["name"],
                      "volume": product["volume"], "usd": usd})
    if missing:
        await update.message.reply_text(
            "⚠️ Не понял позиции: " + "; ".join(missing) +
            "\nНичего не менял — напишите ещё раз точнее.")
        return
    if not items:
        await update.message.reply_text("Не понял, каким товарам задать закупочную цену.")
        return
    payload = {"kind": "set_buy", "user_id": actor["id"],
               "chat_id": update.effective_chat.id, "items": items}
    token = new_pending(payload)
    lines = ["📥 <b>Закупочные цены</b> (за единицу прайса):", ""]
    for i, it in enumerate(items, 1):
        som = f" ≈ {money(it['usd'] * rate)}" if rate > 0 else ""
        lines.append(f"{i}. {esc(it['name'])} {esc(it['volume'])} — "
                     f"<b>${fmt_usd(it['usd'])}</b>{som}")
    lines += ["", "Сохранить?"]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    reply_markup=confirm_kb(token))


async def start_set_usd_rate(update, context, actor, data):
    if not is_admin(actor):
        await update.message.reply_text("⛔ Курс доллара задаёт только админ.")
        return
    try:
        rate = float(str(data.get("rate") or "").replace(",", "."))
    except ValueError:
        rate = 0
    if not (1 <= rate <= 1000):
        await update.message.reply_text(
            "Не понял курс. Напишите, например: «курс 87.5».")
        return
    old = usd_rate()
    db.set_setting("usd_rate", str(rate))
    note = f" (был {fmt_usd(old)})" if old > 0 else ""
    await update.message.reply_text(
        f"💱 Курс сохранён: {fmt_usd(rate)} сом/${note}.\n"
        f"Маржа в /margin и /stockcost теперь считается по нему.")


async def start_set_buy_markup(update, context, actor, data):
    if not is_admin(actor):
        await update.message.reply_text("⛔ Накладные расходы задаёт только админ.")
        return
    try:
        pct = float(str(data.get("percent") or "").replace(",", ".").replace("%", ""))
    except ValueError:
        pct = -1
    if not (0 <= pct <= 200):
        await update.message.reply_text(
            "Не понял процент. Напишите, например: «накладные расходы 10%».")
        return
    db.set_setting("buy_markup_pct", str(pct))
    await update.message.reply_text(
        f"📦 Накладные расходы на закуп: +{fmt_usd(pct)}%.\n"
        f"Себестоимость в /margin и /stockcost = закуп × курс "
        f"× {fmt_usd(1 + pct / 100)}.")


def set_price_summary(p) -> str:
    c = db.client_get(p["client_id"])
    lines = [f"💲 <b>Спеццены — клиент «{esc(c['name'])}» (склад «{esc(p['wh_name'])}»)</b>", ""]
    for it in p["items"]:
        base = prices.BY_ID[it["product_id"]]["price"]
        if it["price"] > 0:
            lines.append(f"• {esc(it['name'])} {esc(it['volume'])}: <b>{fmt_num(it['price'])} сом</b> "
                         f"(прайс: {fmt_num(base)})")
        else:
            lines.append(f"• {esc(it['name'])} {esc(it['volume'])}: <i>убрать спеццену</i> "
                         f"(вернётся {fmt_num(base)})")
    lines.append("")
    lines.append("Эти цены будут подставляться в накладные клиента автоматически. Сохранить?")
    return "\n".join(lines)


async def start_set_price(update, context, actor, data):
    if not can_transfer(actor):
        await update.message.reply_text("⛔ Спеццены настраивает админ или старший.")
        return
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    client_name = str(data.get("client") or "").strip()
    if not client_name:
        await update.message.reply_text("Не понял имя клиента.")
        return
    items, missing = [], []
    for it in (data.get("items") or []):
        name = str(it.get("name") or "").strip()
        volume = str(it.get("volume") or "").strip()
        try:
            price = float(it.get("price"))
        except (TypeError, ValueError):
            price = -1
        product = prices.match_product(name, volume)
        if product is None or price < 0:
            missing.append(f"{name} {volume}")
            continue
        items.append({"product_id": product["id"], "name": product["name"],
                      "volume": product["volume"], "price": price})
    if not items:
        await update.message.reply_text("⚠️ Ни один товар не распознан по прайсу.")
        return

    payload = {
        "kind": "set_price", "user_id": actor["id"], "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"],
        "client_name": client_name, "client_id": None, "items": items,
    }
    exact = db.client_exact(wh["id"], client_name)
    if exact:
        payload["client_id"] = exact["id"]
        token = new_pending(payload)
        text = set_price_summary(payload)
        if missing:
            text = "⚠️ Не найдены в прайсе: " + ", ".join(esc(x) for x in missing) + "\n\n" + text
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=confirm_kb(token))
        return
    candidates = db.fuzzy_clients(wh["id"], client_name)
    if not candidates:
        await update.message.reply_text(
            f"❌ Клиент «{esc(client_name)}» не найден на складе «{esc(wh['name'])}». "
            f"Спеццену можно задать только существующему клиенту.", parse_mode="HTML")
        return
    token = new_pending(payload)
    rows = [[InlineKeyboardButton(f"👤 {c['name']}", callback_data=f"pk:{token}:{c['id']}")]
            for c in candidates]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")])
    await update.message.reply_text(
        f"Клиент «<b>{esc(client_name)}</b>» не найден на складе «{esc(wh['name'])}».\n"
        f"Возможно, вы имели в виду:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


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
    # Плюсовые корректировки прикладываем к ближайшей датированной партии
    # товара (не в «без срока»); минусовые спишутся FEFO как обычно.
    batch_plan = _arrival_batch_plan(
        p["wh_id"], [{"product_id": pid, "qty": d}
                     for _, pid, d in stock_deltas if d > 0])
    summary = f"Инвентаризация: склад {p['wh_name']} ({len(stock_deltas)} корректир.)"
    extra = {"items": detail}
    if p.get("approver_id"):
        extra["approved_by"] = p["approver_id"]
    op_id, _ = db.commit_operation(
        p["user_id"], "inventory", p["wh_id"], None, summary, stock_deltas, [],
        extra, batch_plan=batch_plan or None)
    return op_id, summary


async def start_inventory(update, context, actor, data):
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    if transition_blocked(actor) and not wh["full_mode"]:
        await update.message.reply_text(TRANSITION_HINT)
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


# ---------- Списание товара со склада ----------

def writeoff_summary(p) -> str:
    lines = ["🗑 <b>Списание товара</b>"]
    if p.get("requester_name"):
        lines.append(f"✍️ Заявка от: <b>{esc(p['requester_name'])}</b>")
    lines.append(f"🏬 Склад: <b>{esc(p['wh_name'])}</b>")
    lines.append(f"📝 Причина: <b>{esc(p['reason'])}</b>")
    lines.append("")
    total = 0.0
    for i, it in enumerate(p["items"], 1):
        box = f"{it['box_qty']} кор / " if it.get("box_qty") else ""
        exp = f" · срок {it['expiry']}" if it.get("expiry") else ""
        lines.append(f"{i}. {esc(it['name'])} {esc(it['volume'])} — "
                     f"{box}{it['qty']} шт{exp}")
        total += it["qty"] * it.get("price", 0)
    lines.append("")
    lines.append(f"💰 Сумма списания: <b>{money(total)}</b>")
    smap = db.stock_map(p["wh_id"])
    for it in p["items"]:
        pid = it.get("product_id")
        if pid and smap.get(pid, 0) < it["qty"]:
            lines.append(f"⚠️ {esc(it['name'])} {esc(it['volume'])}: на складе "
                         f"{smap.get(pid, 0)} шт — остаток уйдёт в минус")
    for w in p.get("warnings", []):
        lines.append(f"⚠️ {esc(w)}")
    lines.append("")
    lines.append("Списать этот товар со склада?")
    return "\n".join(lines)


def commit_writeoff(p):
    """Убирает товар со склада без клиента и без денег (порча, просрочка, бой)."""
    qty_by_pid, plan = {}, {}
    for it in p["items"]:
        pid = it.get("product_id")
        if not pid:
            continue
        qty_by_pid[pid] = qty_by_pid.get(pid, 0) + it["qty"]
        for exp, q_ in (it.get("src_batches") or []):
            plan.setdefault((p["wh_id"], pid), []).append((exp, q_))
    stock_deltas = [(p["wh_id"], pid, -q) for pid, q in qty_by_pid.items()]
    total = sum(it["qty"] * it.get("price", 0) for it in p["items"])
    reason = (p.get("reason") or "").strip()
    # Без причины — просто «Списание:», а не «Списание (не указана):»
    head = f"Списание ({reason})" if reason and reason != "не указана" \
        else "Списание"
    summary = (f"{head}: склад {p['wh_name']}, "
               f"{len(p['items'])} поз. на {fmt_num(total)} сом")
    extra = {"items": [{k: it.get(k) for k in ("name", "volume", "qty", "box_qty",
                                               "price", "expiry", "product_id")}
                       for it in p["items"]],
             "total": total, "reason": p["reason"]}
    if p.get("approver_id"):
        extra["approved_by"] = p["approver_id"]
    op_id, _ = db.commit_operation(
        p["user_id"], "writeoff", p["wh_id"], None, summary, stock_deltas, [],
        extra, batch_plan=plan or None)
    return op_id, summary, total


async def start_writeoff(update, context, actor, data):
    wh, err = resolve_warehouse(actor, str(data.get("warehouse") or "").strip())
    if err:
        await update.message.reply_text(err, parse_mode="HTML")
        return
    if transition_blocked(actor) and not wh["full_mode"]:
        await update.message.reply_text(TRANSITION_HINT)
        return
    try:
        items, warnings = parse_items(data.get("items") or [])
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    for it, raw in zip(items, data.get("items") or []):
        if isinstance(raw, dict) and raw.get("expiry"):
            exp = _norm_expiry(raw["expiry"])
            if exp:
                it["expiry"] = exp
    if not [it for it in items if it.get("product_id")]:
        await update.message.reply_text(
            "⚠️ Ни один товар не распознан по прайсу — списание не создано.")
        return

    payload = {
        "kind": "writeoff", "user_id": actor["id"], "chat_id": update.effective_chat.id,
        "wh_id": wh["id"], "wh_name": wh["name"],
        "reason": str(data.get("reason") or "").strip() or "не указана",
        "items": items, "warnings": warnings,
    }

    # Товар уходит со склада без денег — проводит только админ.
    if not is_admin(actor):
        payload["approver_id"] = ADMIN_ID
        payload["requester_name"] = actor["name"]
        token = new_pending(payload, ttl=APPROVAL_TTL)
        try:
            await context.bot.send_message(
                ADMIN_ID, writeoff_summary(payload), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Провести", callback_data=f"ok:{token}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{token}"),
                ]]))
        except Exception:
            log.exception("Не удалось отправить списание админу")
            PENDING.pop(token, None)
            await update.message.reply_text("⚠️ Не удалось отправить админу. Попробуйте позже.")
            return
        await update.message.reply_text(
            f"📨 Списание со склада «{esc(wh['name'])}» отправлено админу "
            f"на подтверждение. Я сообщу результат.", parse_mode="HTML")
        return

    token = new_pending(payload)
    await update.message.reply_text(writeoff_summary(payload), parse_mode="HTML",
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

def _batch_src_wh(p):
    """Склад, с которого списываются партии: у перемещения — источник.
    None — списывать неоткуда (приход товара извне)."""
    if p.get("kind") == "transfer":
        return p.get("from_wh_id")
    return p.get("wh_id")


def _dated_batches(src_wh, pid):
    """Партии товара с известным сроком годности («без срока» не в счёт)."""
    return [b for b in db.product_batches_of(src_wh, pid) if b["expiry"]]


# Кнопка «партия без срока»: пустой выбор означает FEFO, поэтому партию
# без срока кодируем отдельным знаком (иначе выбор «без срока» списывал
# бы самую старую датированную партию).
BATCH_NO_EXPIRY = "~"


def _choice_batch(choice):
    """Выбор кнопкой -> срок партии для плана списания.
    None — выбора нет («сначала старые», FEFO)."""
    if not choice:
        return None
    return "" if choice == BATCH_NO_EXPIRY else choice


def _batch_options(p, pid, qty):
    """Партии товара для кнопок «какую партию?».

    Продажа — все партии, что реально лежат на складе (в том числе без
    срока): продавец должен сказать, какую коробку взял. Перемещение и
    списание — только датированные и только если их хватает: без срока
    такую операцию всё равно не проводим, спросим дату текстом."""
    src_wh = _batch_src_wh(p)
    if not src_wh:
        return []
    batches = db.product_batches_of(src_wh, pid)
    if p.get("kind") in EXPIRY_REQUIRED_KINDS:
        dated = [b for b in batches if b["expiry"]]
        return dated if sum(b["qty"] for b in dated) >= qty else []
    return batches


def _batch_questions(p):
    """Позиции, для которых нужно спросить партию кнопками: у товара на складе
    несколько партий, срок не указан явно, выбор ещё не сделан."""
    need = []
    choices = p.get("batch_choices") or {}
    # Возврат КЛАДЁТ товар на склад — партии склада тут ни при чём,
    # у него спрашивается срок с упаковки текстом (_expiry_questions).
    if p.get("kind") == "return":
        return need
    if not _batch_src_wh(p):
        return need
    for i, it in enumerate(p["items"]):
        pid = it.get("product_id")
        if not pid or str(i) in choices or it.get("expiry"):
            continue
        batches = _batch_options(p, pid, it["qty"])
        if len(batches) > 1:
            need.append((i, it, batches))
    return need


# Операции, в которых товар уходит со склада и срок годности обязателен
# (просьба владельца 01.08.2026: «без срока не пропускать»).
EXPIRY_REQUIRED_KINDS = ("transfer", "writeoff")

# У кого бот ждёт срок годности текстом: (chat_id, user_id) -> токен заявки
AWAIT_EXPIRY = {}


def _expiry_questions(p):
    """Позиции, по которым срок годности неизвестен: датированных партий на
    складе-источнике не хватает на списываемое количество. Такую операцию
    не проводим, пока сотрудник не назовёт срок с упаковки.

    Возврат (решение владельца 03.08.2026, вариант «как у списания»):
    срок обязателен для КАЖДОЙ позиции без явного срока — упаковка
    физически в руках, раньше возврат молча падал в ближайшую датированную
    партию и /expiry привирал."""
    need = []
    if p.get("kind") == "return":
        for i, it in enumerate(p["items"]):
            if it.get("product_id") and not it.get("expiry"):
                need.append((i, it))
        return need
    if p.get("kind") not in EXPIRY_REQUIRED_KINDS:
        return need
    src_wh = _batch_src_wh(p)
    if not src_wh:
        # ПРИХОД товара извне (перемещение без склада-источника): срок
        # обязателен для каждой позиции — партия ляжет на склад с этим
        # сроком (решение владельца 09.08.2026: «без сроков годности
        # ни перемещения, ни пополнения»).
        for i, it in enumerate(p["items"]):
            if it.get("product_id") and not it.get("expiry"):
                need.append((i, it))
        return need
    choices = p.get("batch_choices") or {}
    for i, it in enumerate(p["items"]):
        pid = it.get("product_id")
        if not pid or it.get("expiry") or _choice_batch(choices.get(str(i))):
            continue
        if sum(b["qty"] for b in _dated_batches(src_wh, pid)) < it["qty"]:
            need.append((i, it))
    return need


async def _ask_expiry(q, context, p, question, chat_id, user_id):
    """Просит написать срок годности текстом и запоминает, кого ждём.

    По заявке сотрудника вопрос уходит и САМОМУ сотруднику — упаковка у
    него на складе (просьба владельца 01.08.2026); админ при этом тоже
    может ответить, принимается первый ответ."""
    i, it = question
    keys = [(chat_id, user_id)]
    requester = None
    if (p.get("approver_id") and p.get("chat_id")
            and p.get("user_id") and p["user_id"] != user_id):
        requester = db.get_user(p["user_id"])
        if requester:
            keys.append((p["chat_id"], p["user_id"]))
    # Один вопрос о сроке за раз: второй вопрос затирал бы первый, и ответ
    # молча уходил бы не в ту операцию (находка ревизии 01.08.2026).
    for k in keys:
        old = AWAIT_EXPIRY.get(k)
        if old and get_pending(old) is not None:
            token = new_pending(p, ttl=p.get("ttl", PENDING_TTL))
            await q.edit_message_text(
                "⏸ Уже висит вопрос о сроке годности по другой операции.\n"
                "Сначала нужно ответить на него (или нажать там «Отмена»), "
                "затем нажмите «Продолжить» здесь.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 Продолжить", callback_data=f"ok:{token}")],
                    [InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")]]))
            return
    p["awaiting_expiry"] = i
    token = new_pending(p, ttl=p.get("ttl", PENDING_TTL))
    src_name = p.get("from_wh_name") or p.get("wh_name")
    what = {"transfer": "перемещение", "writeoff": "списание",
            "return": "возврат"}.get(p["kind"], "операцию")
    arrival = p["kind"] == "transfer" and _batch_src_wh(p) is None
    if arrival:
        what = "приход"
    if p["kind"] == "return":
        why = ("Возврат кладёт товар на склад — посмотрите срок годности "
               "на упаковке возвращаемого товара.")
    elif arrival:
        why = ("Приход товара на склад: партия запишется со сроком "
               "с упаковки.")
    else:
        src_wh = _batch_src_wh(p)
        dated = sum(b["qty"] for b in _dated_batches(src_wh, it.get("product_id") or 0))
        if dated:
            why = (f"Датированных партий на складе «{esc(src_name)}» хватает "
                   f"только на {dated} шт из {it['qty']}.")
        else:
            why = (f"На складе «{esc(src_name)}» у этого товара не записан "
                   f"срок годности.")
    head = (f"📅 <b>{esc(it['name'])} {esc(it['volume'])} — {it['qty']} шт</b>\n"
            f"{why}\n\n")
    ask = (f"Посмотрите на упаковку и напишите срок ответным сообщением — "
           f"например: <b>11.2028</b>\n"
           f"Без срока годности {what} не провожу.")
    sent_to_requester = False
    if requester:
        try:
            await context.bot.send_message(
                p["chat_id"],
                head + f"Это по вашей заявке ({what}). " + ask,
                parse_mode="HTML")
            sent_to_requester = True
        except Exception:
            log.warning("Не удалось отправить вопрос о сроке сотруднику %s — "
                        "спрошу подтверждающего", requester["name"])
    AWAIT_EXPIRY[(chat_id, user_id)] = token
    if sent_to_requester:
        AWAIT_EXPIRY[(p["chat_id"], p["user_id"])] = token
        text = (head + f"📨 Вопрос отправлен сотруднику "
                f"<b>{esc(requester['name'])}</b> — упаковка у него на "
                f"складе. Можете ответить и сами: напишите срок сюда "
                f"(например: <b>11.2028</b>). Принимается первый ответ.")
    else:
        text = head + ask
    # Кнопку «Отмена» показываем только тому, кто вправе её нажать
    # (у заявки сотрудника — только подтверждающий админ).
    kb = None
    if not p.get("approver_id") or user_id == p.get("approver_id"):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")]])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def _ask_batch(q, p, question):
    """Кнопки «какую партию?» для одной позиции накладной/перемещения."""
    i, it, batches = question
    # Сохраняем исходный TTL заявки: перемещение, ждавшее админа сутки, не
    # должно после вопроса о партии жить 15 минут. В кнопке — сам срок,
    # а не порядковый номер (за время вопросов список партий мог измениться).
    token = new_pending(p, ttl=p.get("ttl", PENDING_TTL))
    kb = [[InlineKeyboardButton(
        (f"📅 до {b['expiry']} — {b['qty']} шт" if b["expiry"]
         else f"📦 Без срока — {b['qty']} шт"),
        callback_data=f"pbat:{token}:{i}:{b['expiry'] or BATCH_NO_EXPIRY}")]
        for b in batches]
    fefo = ("🔀 Сначала старые"
            + (f" (до {batches[0]['expiry']})" if batches[0]["expiry"] else ""))
    kb.append([InlineKeyboardButton(fefo, callback_data=f"pbat:{token}:{i}:fefo")])
    kb.append([InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")])
    verb = {"transfer": "Какую партию перемещаете?",
            "writeoff": "Какую партию списываете?"}.get(
                p["kind"], "Из какой партии продаёте?")
    await q.edit_message_text(
        f"📅 <b>{esc(it['name'])} {esc(it['volume'])} — {it['qty']} шт</b>\n"
        f"На складе несколько партий с разными сроками. {verb}",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))


class _MsgResponder:
    """Заглушка callback-кнопки: продолжение диалога обычным сообщением
    (сотрудник ответил сроком годности текстом, кнопки уже нет)."""

    def __init__(self, message):
        self.message = message

    async def edit_message_text(self, text, **kwargs):
        await self.message.reply_text(text, **kwargs)

    async def answer(self, *args, **kwargs):
        return None


async def _continue_op(q, context, actor, p, chat_id, user_id):
    """Дозадаёт вопросы (партия кнопками, срок годности текстом) и проводит
    операцию, когда спрашивать больше нечего."""
    need = _batch_questions(p)
    if need:
        await _ask_batch(q, p, need[0])
        return
    need = _expiry_questions(p)
    if need:
        await _ask_expiry(q, context, p, need[0], chat_id, user_id)
        return
    if p["kind"] == "transfer":
        await _finish_transfer(q, context, actor, p)
    elif p["kind"] == "writeoff":
        await _finish_writeoff(q, context, actor, p)
    elif p["kind"] == "return":
        await _finish_return(q, context, actor, p)
    elif p["kind"] == "amend_invoice":
        await _finish_amend(q, context, actor, p)
    else:
        await _finish_invoice(q, context, actor, p)


async def _finish_amend(q, context, actor, p):
    """Проведение замены/дополнения накладной после выбора партий."""
    try:
        # Сторно старой и проведение новой — одна транзакция в базе.
        op_id, client_label, old_debt, total, summary = commit_invoice(
            p, replace_op_id=p["old_op_id"])
    except ValueError as e:
        await q.edit_message_text(
            f"⚠️ Не удалось заменить накладную №{p['old_op_id']}: {esc(str(e))}",
            parse_mode="HTML")
        return
    # Замена УЖЕ в базе — сбой уведомлений/PDF не должен выглядеть как
    # «не проведено», иначе повтор даст «уже отменена» и панику (тот же
    # класс бага, что чинили в _finish_invoice 27.07; аудит 02.08.2026).
    try:
        await q.edit_message_text(
            f"✅ Накладная №{p['old_op_id']} отменена, вместо неё проведена "
            f"№{op_id}.")
        await send_invoice_pdf(context, p["chat_id"], client_label, p,
                               old_debt, total, op_id=op_id)
        await notify_admin(context, actor,
                           f"замена накладной №{p['old_op_id']} → №{op_id}: {summary}")
        actor_name = db.get_user(p.get("op_user_id") or p["user_id"])["name"]
        await feed_invoice_pdf(
            context, p["wh_id"], client_label, p, old_debt, total,
            caption=(f"🔁 <b>{esc(actor_name)}</b> — {esc(summary)}\n"
                     f"Замена накладной №{p['old_op_id']}"),
            exclude_chat_id=p["chat_id"], op_id=op_id)
        await alert_low_stock(context, [
            (p["wh_id"], it["product_id"], -it["qty"])
            for it in p["items"] if it.get("product_id")])
    except Exception:
        log.exception("Замена №%s→№%s проведена, уведомления не дошли",
                      p["old_op_id"], op_id)
        try:
            await q.edit_message_text(
                f"✅ Замена ПРОВЕДЕНА (№{p['old_op_id']} → №{op_id}), "
                f"но часть уведомлений/PDF не отправилась. Повторно "
                f"проводить НЕ нужно — документ: /op {op_id}")
        except Exception:
            pass


async def _finish_return(q, context, actor, p):
    """Проведение возврата после вопросов о сроке годности."""
    op_id, client_label, old_debt, total, summary = commit_return(p)
    # Возврат УЖЕ в базе — сбой уведомлений/PDF не должен выглядеть как
    # «не проведено» (тот же принцип, что в _finish_invoice).
    try:
        await q.edit_message_text(f"✅ Возврат №{op_id} проведён.")
        await send_return_pdf(context, p["chat_id"], client_label, p, old_debt, total)
        if p.get("approver_id"):
            try:
                await context.bot.send_message(
                    p["chat_id"],
                    f"✅ Админ подтвердил возврат (операция №{op_id}).",
                    parse_mode="HTML")
            except Exception:
                log.warning("Не удалось уведомить заявителя")
        actor_name = db.get_user(p["user_id"])["name"]
        note = "Подтвердил админ" if p.get("approver_id") else ""
        await feed_operation(context, op_id, actor_name, "🔙", note,
                             exclude_chat_id=p["chat_id"])
    except Exception:
        log.exception("Возврат №%s проведён, уведомления не дошли", op_id)
        try:
            await q.edit_message_text(
                f"✅ Возврат №{op_id} ПРОВЕДЁН, но часть уведомлений/PDF не "
                f"отправилась. Повторно проводить НЕ нужно — детали: /op {op_id}")
        except Exception:
            pass


async def _finish_invoice(q, context, actor, p):
    """Проведение накладной после всех проверок и выбора партий."""
    if p.get("client_id") is None:
        # Клиент мог появиться, пока заявка ждала кнопки (вторая
        # накладная, add_clients) — иначе INSERT упадёт по UNIQUE.
        existing = db.client_exact(p["wh_id"], p.get("client_name") or "")
        if existing is not None:
            p["client_id"] = existing["id"]
    op_id, client_label, old_debt, total, summary = commit_invoice(p)
    # Операция УЖЕ в базе — любые сбои дальше (Telegram, PDF) не должны
    # выглядеть как «не проведено», иначе сотрудник повторит накладную
    # и задвоит её (находка аудита 27.07.2026).
    try:
        await q.edit_message_text(f"✅ Накладная №{op_id} проведена.")
        if p.get("approver_id"):
            try:
                await context.bot.send_message(
                    p["chat_id"],
                    f"✅ Админ подтвердил накладную №{op_id} новому клиенту "
                    f"«{esc(client_label)}».", parse_mode="HTML")
            except Exception:
                log.warning("Не удалось уведомить заявителя о накладной")
        await send_invoice_pdf(context, p["chat_id"], client_label, p, old_debt,
                               total, op_id=op_id)
        await notify_admin(context, actor, summary)
        # В ленту склада — только PDF со сводкой в подписи (без
        # отдельного текстового сообщения, просьба владельца 21.07.2026).
        actor_name = db.get_user(p["user_id"])["name"]
        await feed_invoice_pdf(
            context, p["wh_id"], client_label, p, old_debt, total,
            caption=f"🧾 <b>{esc(actor_name)}</b> — {esc(summary)}",
            exclude_chat_id=p["chat_id"], op_id=op_id)
        await alert_low_stock(context, [
            (p["wh_id"], it["product_id"], -it["qty"])
            for it in p["items"] if it.get("product_id")])
    except Exception:
        log.exception("Накладная №%s проведена, но уведомления не дошли", op_id)
        try:
            await q.edit_message_text(
                f"✅ Накладная №{op_id} ПРОВЕДЕНА, но часть уведомлений/PDF "
                "не отправилась. Повторно проводить НЕ нужно — документ: "
                f"/op {op_id}")
        except Exception:
            pass


def _fill_src_batches(p):
    """Раскладка списания по партиям склада-источника: явный срок / выбор
    кнопкой / «сначала старые». Срок, названный в ответ на вопрос бота
    (expiry_asked), к партиям источника не применяется — товар лежит там
    без срока, списываем как есть, а сам срок уезжает с товаром.

    used: снимок партий читается из базы ДО проведения, поэтому две строки
    одного товара без учёта друг друга списывали бы одну и ту же партию
    дважды (ревизия 01.08.2026) — занятое предыдущими строками вычитаем."""
    src_wh = _batch_src_wh(p)
    if not src_wh:
        return
    choices = p.get("batch_choices") or {}
    used = {}

    def take_from(pid, exp, qty):
        used[(pid, exp)] = used.get((pid, exp), 0) + qty
        return (exp, qty)

    for i, it in enumerate(p["items"]):
        pid = it.get("product_id")
        if not pid:
            continue
        exp = "" if it.get("expiry_asked") else (it.get("expiry") or "")
        if not exp:
            picked = _choice_batch(choices.get(str(i)))
            if picked is not None:
                it["src_batches"] = [take_from(pid, picked, it["qty"])]
                continue
        if exp:
            it["src_batches"] = [take_from(pid, exp, it["qty"])]
            continue
        rows_, rest = [], it["qty"]
        for b in db.product_batches_of(src_wh, pid):
            avail = b["qty"] - used.get((pid, b["expiry"]), 0)
            take = min(rest, avail)
            if take > 0:
                rows_.append(take_from(pid, b["expiry"], take))
                rest -= take
            if not rest:
                break
        if rest:
            rows_.append(take_from(pid, "", rest))
        it["src_batches"] = rows_


def _writeoff_pdf(p, op_id, total):
    """Акт списания PDF — состав виден в ленте склада (просьба владельца
    08.08.2026: «в общий чат — PDF-файл с составом списания»)."""
    rows = []
    for it in p["items"]:
        srcs = it.get("src_batches") or []
        if len(srcs) > 1:
            exp_cell = [f"{e or 'без срока'} — {q_} шт" for e, q_ in srcs]
        elif srcs:
            exp_cell = srcs[0][0] or "без срока"
        else:
            exp_cell = it.get("expiry") or "—"
        price = it.get("price") or 0
        rows.append([str(it["name"]).split("(")[0].strip(),
                     str(it.get("volume") or ""), f"{it['qty']} шт",
                     fmt_num(price), fmt_num(price * it["qty"]), exp_cell])
    reason = (p.get("reason") or "").strip()
    sub = (f"ОсОО «ВЕТОП» · склад {p['wh_name']} · "
           + datetime.now(BISHKEK).strftime("%d.%m.%Y"))
    if reason and reason != "не указана":
        sub += f" · причина: {reason}"
    return generate_report_pdf(
        f"АКТ СПИСАНИЯ № {op_id}", sub,
        [{"title": "Списанный товар",
          "headers": ["Товар", "Фасовка", "Кол-во", "Цена", "Сумма", "Срок"],
          "rows": rows, "widths": [46, 20, 18, 16, 21, 36],
          "numbered": True}],
        footer=f"Итого списано: {money(total)} (по ценам прайса)")


async def _finish_writeoff(q, context, actor, p):
    """Проведение списания после выбора партий и уточнения сроков."""
    # Перепроверка остатка на кнопке: заявка сотрудника могла ждать админа
    # до суток, товар за это время могли продать (минус допустим, как у
    # перемещения, но подтверждающий должен его увидеть).
    smap = db.stock_map(p["wh_id"])
    minus = [
        f"{it['name']} {it['volume']}: на складе "
        f"{smap.get(it['product_id'], 0)}, списываем {it['qty']}"
        for it in p["items"]
        if it.get("product_id") and smap.get(it["product_id"], 0) < it["qty"]]
    warn = ""
    if minus:
        warn = ("\n\n⚠️ Остаток изменился со времени заявки — уйдёт в минус:\n• "
                + "\n• ".join(minus) + "\nЕсли это неверно — отмените: /undo")
    _fill_src_batches(p)
    op_id, summary, total = commit_writeoff(p)
    # Операция уже в базе — сбои уведомлений не должны выглядеть как
    # «не проведено» (см. _finish_invoice).
    try:
        await q.edit_message_text(
            f"✅ {esc(summary)} — проведено (операция №{op_id}).\n"
            f"Отменить: /undo {op_id}" + esc(warn), parse_mode="HTML")
        if p.get("approver_id"):
            try:
                await context.bot.send_message(
                    p["chat_id"],
                    f"✅ Админ подтвердил списание (операция №{op_id}): "
                    f"{esc(summary)}", parse_mode="HTML")
            except Exception:
                log.warning("Не удалось уведомить заявителя о списании")
        else:
            await notify_admin(context, actor, summary)
        # Акт списания PDF: нажавшему кнопку и в ленту склада одним
        # сообщением со сводкой в подписи (просьба владельца 08.08.2026)
        fname = f"акт_списания_{op_id}.pdf"
        try:
            await context.bot.send_document(
                chat_id=q.message.chat.id,
                document=InputFile(_writeoff_pdf(p, op_id, total),
                                   filename=fname),
                caption=f"🗑 Акт списания №{op_id}")
        except Exception as e:
            log.warning("Не удалось отправить акт списания: %s", e)
        wh_row = db.warehouse_by_id(p["wh_id"])
        if (wh_row and wh_row["feed_chat_id"]
                and wh_row["feed_chat_id"] not in (p["chat_id"],
                                                   q.message.chat.id)):
            actor_name = db.get_user(p["user_id"])["name"]
            try:
                await context.bot.send_document(
                    chat_id=wh_row["feed_chat_id"],
                    document=InputFile(_writeoff_pdf(p, op_id, total),
                                       filename=fname),
                    caption=f"🗑 <b>{esc(actor_name)}</b> — {esc(summary)}",
                    parse_mode="HTML")
            except Exception as e:
                log.warning("Не удалось отправить акт в ленту склада: %s", e)
        await alert_low_stock(context, [
            (p["wh_id"], it["product_id"], -it["qty"])
            for it in p["items"] if it.get("product_id")])
    except Exception:
        log.exception("Операция №%s проведена, но уведомления не дошли", op_id)
        try:
            await q.edit_message_text(
                f"✅ Списание ПРОВЕДЕНО (операция №{op_id}), но часть "
                "уведомлений не отправилась. Повторно проводить НЕ нужно — "
                f"документ: /op {op_id}")
        except Exception:
            pass


async def _finish_transfer(q, context, actor, p):
    """Проведение перемещения/прихода после проверок и выбора партий."""
    # Перепроверка остатка на кнопке: заявка могла ждать до суток,
    # товар за это время могли продать (минус допустим, но подтверждающий
    # должен его увидеть).
    warn = ""
    if p.get("from_wh_id"):
        smap = db.stock_map(p["from_wh_id"])
        minus = [
            f"{it['name']} {it['volume']}: на складе "
            f"{smap.get(it['product_id'], 0)}, забираем {it['qty']}"
            for it in p["items"]
            if it.get("product_id")
            and smap.get(it["product_id"], 0) < it["qty"]]
        if minus:
            warn = ("\n\n⚠️ Остаток изменился со времени заявки — "
                    "уйдёт в минус:\n• " + "\n• ".join(minus) +
                    "\nЕсли это неверно — отмените: /undo")
    _fill_src_batches(p)
    op_id, summary = commit_transfer(p)
    # Операция уже в базе — сбои уведомлений не должны выглядеть как
    # «не проведено» (см. _finish_invoice).
    try:
        await q.edit_message_text(
            f"✅ {esc(summary)} — проведено (операция №{op_id})." + esc(warn),
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
        # PDF со списком и сроками — в чаты-ленты складов операции
        # (просьба владельца 25.07.2026); нет ленты — подтвердившему.
        actor_name = db.get_user(p["user_id"])["name"]
        cap = f"📦 <b>{esc(actor_name)}</b> — {esc(summary)}"
        if note:
            cap += f"\n{esc(note)}"
        feed_chats = set()
        for wh_id_ in filter(None, (p["wh_id"], p.get("from_wh_id"))):
            wh_ = db.warehouse_by_id(wh_id_)
            if wh_ and wh_["feed_chat_id"]:
                feed_chats.add(wh_["feed_chat_id"])
        try:
            for chat_id_ in (feed_chats or {q.message.chat.id}):
                await send_transfer_pdf(context, chat_id_, p, op_id, summary,
                                        caption=cap)
        except Exception:
            log.exception("Не удалось отправить PDF прихода")
        if p["from_wh_id"]:
            await alert_low_stock(context, [
                (p["from_wh_id"], it["product_id"], -it["qty"])
                for it in p["items"] if it.get("product_id")])
    except Exception:
        log.exception("Операция №%s проведена, но уведомления не дошли", op_id)
        try:
            await q.edit_message_text(
                f"✅ Операция №{op_id} ПРОВЕДЕНА, но часть уведомлений/PDF "
                "не отправилась. Повторно проводить НЕ нужно — документ: "
                f"/op {op_id}")
        except Exception:
            pass


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
        # Снимаем ожидания срока годности, относящиеся К ЭТОЙ заявке (их
        # может быть два: админ + сотрудник-заявитель); чужие вопросы
        # «Отмена» на другой карточке не глушит (ревизия 01.08.2026).
        for k in [k for k, v in AWAIT_EXPIRY.items() if v == token]:
            AWAIT_EXPIRY.pop(k, None)
        await q.answer()
        # Отменённое восстановление базы: подчистить временный файл
        if p.get("kind") in ("restore_db", "restore_db2") and p.get("path"):
            try:
                os.remove(p["path"])
            except OSError:
                pass
        if p.get("approver_id"):
            await q.edit_message_text("❌ Заявка отклонена.")
            if p["kind"] == "invoice":
                what = (f"накладную новому клиенту «{esc(p.get('client_name') or '')}» "
                        f"со стартовым долгом")
            elif p["kind"] == "inventory":
                what = f"инвентаризацию склада «{esc(p['wh_name'])}»"
            elif p["kind"] == "writeoff":
                what = f"списание со склада «{esc(p['wh_name'])}»"
            elif p["kind"] == "return":
                what = f"возврат от «{esc(p.get('client_name') or '')}»"
            elif p["kind"] == "handover":
                what = f"сдачу выручки {money(p['amount'])}"
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

    if kind == "pw":  # выбран склад для операции, где склад не был указан
        PENDING.pop(token, None)
        await q.answer()
        try:
            wh = db.warehouse_by_id(int(parts[2]))
        except (ValueError, IndexError):
            wh = None
        owner_row = db.get_user(p["user_id"])
        if wh is None or owner_row is None or \
                not db.can_use_warehouse(owner_row, wh["id"]):
            await q.edit_message_text("Склад не найден или нет доступа.")
            return
        data = p["action_data"]
        data["warehouse"] = wh["name"]
        try:
            await q.edit_message_text(f"📦 Склад: «{esc(wh['name'])}»",
                                      parse_mode="HTML")
        except Exception:
            pass
        # Продолжаем операцию от имени сотрудника, отправившего сообщение;
        # шим вместо Update — у callback нет своего update.message.
        shim = SimpleNamespace(message=q.message,
                               effective_chat=q.message.chat,
                               effective_user=q.from_user)
        await dispatch_data(shim, context, owner_row, data,
                            draft=p.get("draft", False))
        return

    if kind == "ps":  # выбран склад для отчёта об остатках
        PENDING.pop(token, None)
        await q.answer()
        owner_row = db.get_user(p["user_id"])
        if owner_row is None:
            return
        if len(parts) > 2 and parts[2] == "all":
            whs = db.visible_warehouses(owner_row)
            label = "все склады"
        else:
            try:
                wh = db.warehouse_by_id(int(parts[2]))
            except (ValueError, IndexError):
                wh = None
            if wh is None or not db.can_view_warehouse(owner_row, wh["id"]):
                await q.edit_message_text("Склад не найден или нет доступа.")
                return
            whs = [wh]
            label = f"«{wh['name']}»"
        try:
            await q.edit_message_text(f"📦 Остатки: {esc(label)}", parse_mode="HTML")
        except Exception:
            pass
        shim = SimpleNamespace(message=q.message,
                               effective_chat=q.message.chat,
                               effective_user=q.from_user)
        await _stock_report(shim, context, owner_row, whs,
                            p.get("with_prices", False))
        return

    if kind == "pdbt":  # выбран склад для отчёта о долгах
        PENDING.pop(token, None)
        await q.answer()
        owner_row = db.get_user(p["user_id"])
        if owner_row is None:
            return
        if len(parts) > 2 and parts[2] == "all":
            whs = debt_whs_for(owner_row, db.visible_warehouses(owner_row))
            label = "все склады"
        else:
            try:
                wh = db.warehouse_by_id(int(parts[2]))
            except (ValueError, IndexError):
                wh = None
            if wh is None or not db.can_view_warehouse(owner_row, wh["id"]):
                await q.edit_message_text("Склад не найден или нет доступа.")
                return
            if _debts_hidden_from(owner_row, wh["id"]):
                await q.edit_message_text(HIDDEN_DEBT_MSG)
                return
            whs = [wh]
            label = f"«{wh['name']}»"
        try:
            await q.edit_message_text(f"💰 Долги: {esc(label)}", parse_mode="HTML")
        except Exception:
            pass
        shim = SimpleNamespace(message=q.message,
                               effective_chat=q.message.chat,
                               effective_user=q.from_user)
        await _debts_report(shim, whs)
        return

    if kind == "pc":  # выбор кассы: сотрудник или все сразу (ответ — PDF)
        PENDING.pop(token, None)
        await q.answer()
        target = None
        if not (len(parts) > 2 and parts[2] == "all"):
            try:
                target = db.get_user(int(parts[2]))
            except (ValueError, IndexError):
                target = None
            if target is None:
                await q.edit_message_text("Сотрудник не найден.")
                return
        label = (f"💰 Касса — {esc(target['name'])}" if target
                 else "💰 Кассы всех сотрудников")
        try:
            await q.edit_message_text(label, parse_mode="HTML")
        except Exception:
            pass
        await _send_cash_pdf(q.message, target)
        return

    if kind == "pk":  # выбран существующий клиент
        p["client_id"] = int(parts[2])
        await q.answer()
        if p["kind"] == "set_phone":
            PENDING.pop(token, None)
            c = db.client_get(p["client_id"])
            db.client_set_phone(c["id"], p["phone"])
            await q.edit_message_text(
                f"✅ Телефон клиента «{esc(c['name'])}»: {esc(p['phone'])}", parse_mode="HTML")
            return
        if p["kind"] == "return":
            apply_client_prices(p)
            requester = db.get_user(p["user_id"])
            if requester["role"] != "admin":
                p["approver_id"] = ADMIN_ID
                p["requester_name"] = requester["name"]
                p["ttl"] = APPROVAL_TTL
                try:
                    await context.bot.send_message(
                        ADMIN_ID, return_summary(p), parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Провести", callback_data=f"ok:{token}"),
                            InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{token}"),
                        ]]))
                    await q.edit_message_text(
                        "📨 Возврат отправлен админу на подтверждение. Я сообщу результат.")
                except Exception:
                    log.exception("Не удалось отправить возврат админу")
                    await q.edit_message_text("⚠️ Не удалось отправить админу. Попробуйте позже.")
                    PENDING.pop(token, None)
            else:
                await q.edit_message_text(return_summary(p), parse_mode="HTML",
                                          reply_markup=confirm_kb(token))
            return
        if p["kind"] == "invoice":
            apply_client_prices(p)
            summary = invoice_summary(p)
        elif p["kind"] == "set_price":
            summary = set_price_summary(p)
        else:
            summary = payment_summary(p)
        await q.edit_message_text(summary, parse_mode="HTML", reply_markup=confirm_kb(token))
        return

    if kind == "nw":  # создаём нового клиента (только накладная)
        p["client_id"] = None
        await q.answer()
        requester = db.get_user(p["user_id"])
        if p.get("parsed_debt") and requester["role"] != "admin":
            # Новый клиент со стартовым долгом — только с подтверждения админа
            # (иначе сотрудник мог бы «нарисовать» долг с нуля).
            p["approver_id"] = ADMIN_ID
            p["requester_name"] = requester["name"]
            p["ttl"] = APPROVAL_TTL
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ <b>{esc(requester['name'])}</b> выписывает накладную НОВОМУ "
                    f"клиенту со стартовым долгом {money(p['parsed_debt'])} — нужно "
                    f"ваше подтверждение.\n\n" + invoice_summary(p),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Провести", callback_data=f"ok:{token}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{token}"),
                    ]]))
                await q.edit_message_text(
                    "📨 Новый клиент со стартовым долгом — заявка ушла админу "
                    "на подтверждение. Я сообщу результат.")
            except Exception:
                log.exception("Не удалось отправить заявку о новом клиенте админу")
                await q.edit_message_text("⚠️ Не удалось отправить админу. Попробуйте позже.")
                PENDING.pop(token, None)
            return
        await q.edit_message_text(invoice_summary(p), parse_mode="HTML", reply_markup=confirm_kb(token))
        return

    if kind == "pbat":  # выбор партии (срока годности) для позиции накладной
        PENDING.pop(token, None)
        await q.answer()
        try:
            idx = parts[2]
            item = p["items"][int(idx)]
        except (ValueError, IndexError):
            await q.edit_message_text("⌛ Заявка устарела — отправьте накладную заново.")
            return
        choices = p.setdefault("batch_choices", {})
        choice = parts[3] if len(parts) > 3 else "fefo"
        if choice == "fefo":
            choices[idx] = ""   # пусто = «сначала старые» (FEFO)
        else:
            # В кнопке закодирован сам срок («~» = партия без срока);
            # сверяем с текущими партиями — устаревший выбор уходит в FEFO.
            exp = "" if choice == BATCH_NO_EXPIRY else choice
            current = {b["expiry"] for b in db.product_batches_of(
                _batch_src_wh(p), item.get("product_id") or 0)}
            choices[idx] = (choice if exp in current else "")
        try:
            await _continue_op(q, context, actor, p,
                               q.message.chat.id, q.from_user.id)
        except Exception as e:
            log.exception("Ошибка проведения после выбора партии")
            await q.edit_message_text(f"⚠️ Операция НЕ проведена: {e}")
            # Заявитель (если это была заявка админу) должен узнать об ошибке
            if p.get("chat_id") and p["chat_id"] != q.message.chat.id:
                try:
                    await context.bot.send_message(
                        p["chat_id"],
                        f"⚠️ Ваша заявка не проведена: {e}. Отправьте заново.")
                except Exception:
                    log.warning("Не удалось уведомить заявителя об ошибке")
        return

    if kind == "ok":
        PENDING.pop(token, None)  # сразу, чтобы не провести дважды
        await q.answer()
        try:
            if p["kind"] == "invoice":
                lack = insufficient_stock(p["wh_id"], p["items"])
                if lack:
                    await q.edit_message_text(lack_message(lack), parse_mode="HTML")
                    return
                # У товара несколько партий с разными сроками — спрашиваем,
                # какую продать (просьба владельца 25.07.2026: сотрудник мог
                # физически взять новую партию, FEFO молча списал бы старую).
                await _continue_op(q, context, actor, p,
                                   q.message.chat.id, q.from_user.id)
            elif p["kind"] == "payment":
                op_id, client_label, old_debt, summary = commit_payment(p)
                await q.edit_message_text(
                    f"✅ Оплата проведена (операция №{op_id}).\n\n"
                    + payment_receipt(client_label, old_debt, p["amount"]),
                    parse_mode="HTML")
                await notify_admin(context, actor, summary)
                await feed_operation(context, op_id, db.get_user(p["user_id"])["name"], "💵",
                                     exclude_chat_id=p["chat_id"])
            elif p["kind"] in ("transfer", "writeoff"):
                # Перемещение и списание: спрашиваем, какую партию забираем
                # (выбранный срок переезжает на склад-получатель), а если
                # срок у товара вообще не записан — просим написать его
                # текстом и без него операцию не проводим.
                await _continue_op(q, context, actor, p,
                                   q.message.chat.id, q.from_user.id)
            elif p["kind"] == "handover":
                op_id, summary = commit_handover(p)
                remaining = db.cash_on_hand(p["user_id"])
                await q.edit_message_text(
                    f"✅ {esc(summary)} — принято (операция №{op_id}).\n"
                    f"В его кассе осталось: {money(remaining)}", parse_mode="HTML")
                if p.get("approver_id"):
                    try:
                        await context.bot.send_message(
                            p["chat_id"],
                            f"✅ Админ принял выручку {money(p['amount'])} (операция №{op_id}).\n"
                            f"В вашей кассе осталось: {money(remaining)}", parse_mode="HTML")
                    except Exception:
                        log.warning("Не удалось уведомить заявителя")
                actor_name = db.get_user(p["user_id"])["name"]
                await feed_operation(context, op_id, actor_name, "💰",
                                     exclude_chat_id=p["chat_id"])
            elif p["kind"] == "return":
                # Срок годности возврата обязателен (решение владельца
                # 03.08.2026): позиции без срока — вопрос текстом, потом
                # проведение (_finish_return).
                await _continue_op(q, context, actor, p,
                                   q.message.chat.id, q.from_user.id)
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
                    await feed_operation(context, op_id, actor_name, "📋", note,
                                         exclude_chat_id=p["chat_id"])
            elif p["kind"] == "set_price":
                c = db.client_get(p["client_id"])
                for it in p["items"]:
                    db.set_client_price(p["client_id"], it["product_id"], it["price"])
                await q.edit_message_text(
                    f"✅ Спеццены клиента «{esc(c['name'])}» сохранены ({len(p['items'])} поз.). "
                    f"Посмотреть: /client {esc(c['name'])}", parse_mode="HTML")
                await notify_admin(context, actor,
                                   f"спеццены для {c['name']}: {len(p['items'])} поз.")
            elif p["kind"] == "set_buy":
                saved, failed = 0, []
                for it in p["items"]:
                    try:
                        db.product_set_buy(it["product_id"], it["usd"])
                        saved += 1
                    except ValueError:
                        # товар исчез, пока карточка ждала — не бросаем
                        # середину списка, честно перечисляем несохранённое
                        failed.append(f"{it['name']} {it['volume']}")
                msg = (f"✅ Закупочные цены сохранены ({saved} поз.). "
                       f"Отчёты: /margin, /stockcost, настройки: /rate")
                if failed:
                    msg += "\n⚠️ Не найдены: " + ", ".join(failed)
                await q.edit_message_text(msg)
            elif p["kind"] == "set_min":
                for it in p["items"]:
                    db.set_min_stock(p["wh_id"], it["product_id"], it["qty"])
                await q.edit_message_text(
                    f"✅ Минимальные остатки для склада «{esc(p['wh_name'])}» сохранены "
                    f"({len(p['items'])} поз.). Посмотреть: /minstock", parse_mode="HTML")
            elif p["kind"] == "amend_invoice":
                # Часовое окно сотрудника перепроверяется на кнопке: карточка
                # живёт 15 минут, и нажатие на исходе TTL растягивало окно
                # (аудит 02.08.2026). Админа проверка пропускает.
                old_op = db.get_operation(p["old_op_id"])
                err = (_replace_rights_error(actor, old_op)
                       if old_op else "накладная не найдена")
                if err:
                    await q.edit_message_text(f"⌛ Заявка устарела: {err}")
                    return
                # Запас, который вернёт сторно старой накладной: при полной
                # замене он посчитан заранее (returned_qty), при дополнении —
                # это старые строки (первые old_count позиций).
                back = dict(p.get("returned_qty") or {})
                for it in p["items"][:p["old_count"]]:
                    if it.get("product_id"):
                        back[it["product_id"]] = back.get(it["product_id"], 0) + it["qty"]
                lack = insufficient_stock(p["wh_id"], p["items"], extra_available=back)
                if lack:
                    await q.edit_message_text(lack_message(lack), parse_mode="HTML")
                    return
                # Партии: если у товара несколько сроков, спрашиваем кнопками,
                # из какой партии продано — как у обычной накладной (раньше
                # замена молча списывала FEFO; просьба владельца 03.08.2026).
                await _continue_op(q, context, actor, p,
                                   q.message.chat.id, q.from_user.id)
            elif p["kind"] == "load_wh":
                data, _src = _stock_load_data(p["wh_name"])
                if data is None:
                    await q.edit_message_text("⚠️ Таблица загрузки не найдена.")
                    return
                # Перепроверка на кнопке: остатки должны быть ТЕМИ ЖЕ, что в
                # момент заявки (снимок stock_sig) — иначе прошла другая
                # загрузка/операция, и проведение задвоило бы товар.
                sig_now = sorted([pid, q_] for pid, q_
                                 in db.stock_map(p["wh_id"]).items() if q_)
                if sig_now != [list(x) for x in (p.get("stock_sig") or [])]:
                    await q.edit_message_text(
                        "⚠️ Остатки склада изменились, пока заявка ждала "
                        "кнопки, — загрузка отменена, ничего не изменено. "
                        "Отправьте /loadwh заново.")
                    return
                # Строки одного товара = партии: складываем в общую дельту,
                # а раскладку по срокам передаём планом партий.
                totals, plan = {}, {}
                for pid, qty, exp in data:
                    if qty <= 0:
                        continue
                    totals[pid] = totals.get(pid, 0) + qty
                    plan.setdefault((p["wh_id"], pid), []).append((exp or "", qty))
                deltas = [(p["wh_id"], pid, q) for pid, q in totals.items()]
                total = sum(totals.values())
                op_id, _ = db.commit_operation(
                    p["user_id"], "inventory", p["wh_id"], None,
                    f"Стартовая загрузка остатков склада {p['wh_name']}: "
                    f"{len(deltas)} поз., {total} шт",
                    deltas, [], {"load": p["wh_name"]}, batch_plan=plan)
                # Старый справочник «один срок на товар» — ближайший срок.
                for (wh_, pid), rows_ in plan.items():
                    dated = sorted((e for e, _q in rows_ if e),
                                   key=lambda e: e[3:] + e[:2])
                    if dated:
                        db.set_product_expiry(wh_, pid, dated[0])
                await q.edit_message_text(
                    f"✅ Остатки склада «{esc(p['wh_name'])}» загружены "
                    f"(операция №{op_id}): {len(deltas)} позиций, {total} шт "
                    f"({sum(len(r) for r in plan.values())} партий).\n"
                    f"Сроки годности — /expiry {esc(p['wh_name'])}.\n"
                    f"Проверьте: /stock {esc(p['wh_name'])}.", parse_mode="HTML")
            elif p["kind"] == "reset_wh":
                # Первая кнопка — не стираем, а показываем ВТОРОЕ подтверждение
                # с отдельной кнопкой (защита от «одним нажатием», просьба
                # владельца 22.07.2026).
                token2 = new_pending({**{k: p[k] for k in
                                         ("kind", "user_id", "chat_id",
                                          "wh_id", "wh_name")},
                                      "kind": "reset_wh2"})
                kb2 = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 Да, стереть безвозвратно",
                                          callback_data=f"ok:{token2}")],
                    [InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token2}")],
                ])
                await q.edit_message_text(
                    f"⚠️ <b>Последняя проверка</b>\n\n"
                    f"Склад «{esc(p['wh_name'])}» будет стёрт БЕЗВОЗВРАТНО "
                    "(операции, остатки, клиенты с долгами, сроки).\n"
                    "Перед стиранием пришлю вам свежую копию базы — по ней "
                    "всё можно восстановить.",
                    parse_mode="HTML", reply_markup=kb2)
            elif p["kind"] == "restore_db":
                # Второе подтверждение отдельной кнопкой — как у /resetwh.
                token2 = new_pending({**{k: p[k] for k in
                                         ("kind", "user_id", "chat_id",
                                          "path", "fname")},
                                      "kind": "restore_db2"})
                kb2 = InlineKeyboardMarkup([
                    [InlineKeyboardButton("💾 Да, заменить базу",
                                          callback_data=f"ok:{token2}")],
                    [InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token2}")],
                ])
                await q.edit_message_text(
                    "⚠️ <b>Последняя проверка</b>\n\n"
                    "Рабочая база будет заменена файлом "
                    f"{esc(p['fname'])} безвозвратно. Сначала пришлю копию "
                    "текущей базы, затем заменю и перезапущусь.",
                    parse_mode="HTML", reply_markup=kb2)
            elif p["kind"] == "restore_db2":
                if not os.path.exists(p["path"]):
                    await q.edit_message_text(
                        "⌛ Файл бэкапа уже удалён с сервера (перезапуск?). "
                        "Отправьте файл ещё раз.")
                    return
                try:
                    await send_backup(context.bot)
                except Exception as e:
                    log.exception("Бэкап перед восстановлением не удался")
                    await q.edit_message_text(
                        f"⛔ Восстановление НЕ выполнено: не удалось снять "
                        f"копию текущей базы ({esc(str(e))}). Попробуйте ещё раз.",
                        parse_mode="HTML")
                    return
                db.restore_from(p["path"])
                try:
                    os.remove(p["path"])
                except OSError:
                    pass
                await q.edit_message_text(
                    "✅ База заменена файлом бэкапа.\n"
                    "🔄 Перезапускаюсь, чтобы всё перечиталось начисто — "
                    "буду в строю примерно через полминуты. Потом проверьте: "
                    "/dbinfo и /stock.")
                log.warning("База восстановлена из бэкапа %s — перезапуск", p["fname"])
                asyncio.get_running_loop().call_later(1.5, os._exit, 1)
            elif p["kind"] == "reset_wh2":
                # Автобэкап перед необратимым удалением: если копия не
                # отправилась — сброс не выполняем.
                try:
                    await send_backup(context.bot)
                except Exception as e:
                    log.exception("Бэкап перед сбросом склада не удался")
                    await q.edit_message_text(
                        f"⛔ Сброс НЕ выполнен: не удалось снять резервную "
                        f"копию базы ({esc(str(e))}). Попробуйте ещё раз: "
                        f"/resetwh {esc(p['wh_name'])}", parse_mode="HTML")
                    return
                stats = db.reset_warehouse(p["wh_id"])
                _KNOWN_CLIENTS_CACHE["ts"] = 0.0  # имена стёртых клиентов — вон из подсказок
                await q.edit_message_text(
                    f"🗑 Склад «{esc(p['wh_name'])}» очищен полностью:\n"
                    f"• операций удалено: {stats['ops']}\n"
                    f"• остатков списано: {stats['stock_positions']} поз., "
                    f"{fmt_num(stats['stock_qty'])} шт\n"
                    f"• клиентов удалено: {stats['clients']} "
                    f"(долгов было {money(stats['debt'])})\n\n"
                    "Загрузка заново:\n"
                    "1. Остатки: /loadwh Склад (если есть таблица) или инвентаризацией\n"
                    "2. Клиенты: «добавь клиентов на "
                    f"{esc(p['wh_name'])}: Имя — долг, Имя — долг, ...»",
                    parse_mode="HTML")
            elif p["kind"] == "add_clients":
                added, skipped = db.clients_add_bulk(p["wh_id"], p["entries"])
                total_debt = sum(e[1] for e in p["entries"])
                msg = (f"✅ Добавлено клиентов на склад «{esc(p['wh_name'])}»: "
                       f"{len(added)}.")
                if total_debt:
                    msg += f" Стартовые долги: {money(total_debt)}."
                if skipped:
                    msg += f" Пропущено (уже были): {len(skipped)}."
                msg += "\nТеперь голосовые будут распознавать эти имена точнее."
                await q.edit_message_text(msg, parse_mode="HTML")
            elif p["kind"] == "client_alias":
                for a in p["aliases"]:
                    db.add_client_alias(p["client_id"], a)
                _KNOWN_CLIENTS_CACHE["ts"] = 0.0  # чтобы имена сразу попали в подсказки
                await q.edit_message_text(
                    f"✅ Запомнил: {esc(', '.join(p['aliases']))} — это "
                    f"«{esc(p['client_name'])}» (склад «{esc(p['wh_name'])}»).",
                    parse_mode="HTML")
            elif p["kind"] == "undo_op":
                ok, msg = db.cancel_operation(p["op_id"])
                if not ok:
                    await q.edit_message_text(f"⚠️ {esc(msg)}", parse_mode="HTML")
                else:
                    await q.edit_message_text(
                        f"↩️ Операция №{p['op_id']} отменена: {esc(msg)}\n"
                        f"Остатки и долги возвращены как было.", parse_mode="HTML")
                    await notify_admin(context, actor,
                                       f"отменил операцию №{p['op_id']}: {msg}")
                    cancelled = db.get_operation(p["op_id"])
                    if cancelled:
                        await post_feed(
                            context, db.operation_warehouses(cancelled),
                            f"↩️ <b>{esc(actor['name'])}</b> отменил операцию "
                            f"№{p['op_id']}: {esc(msg)}",
                            exclude_chat_id=p.get("chat_id"))
            elif p["kind"] == "change_price":
                for it in p["items"]:
                    db.product_set_price(it["product_id"], it["price"], p["user_id"])
                prices.set_data(db.products_active())
                _refresh_price_dependents()
                lines = [f"✅ Прайс обновлён ({len(p['items'])} поз.):"]
                for it in p["items"]:
                    lines.append(f"• {esc(it['name'])} {esc(it['volume'])}: "
                                 f"{fmt_num(it['old_price'])} → <b>{fmt_num(it['price'])} сом</b>")
                lines.append("История: /pricelog")
                await q.edit_message_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            log.exception("Ошибка проведения операции")
            # Сообщаем и нажавшему кнопку (иначе админ-подтверждающий не видит
            # сбой заявки), и заявителю в его чат.
            try:
                await q.edit_message_text(f"⚠️ Ошибка при проведении: {e}")
            except Exception:
                pass
            try:
                if q.message is None or p.get("chat_id") != q.message.chat.id:
                    await context.bot.send_message(
                        p["chat_id"], f"⚠️ Ошибка при проведении: {e}")
            except Exception:
                log.warning("Не удалось сообщить заявителю об ошибке")
        return

    await q.answer()


# ---------- Сообщения ----------

# Ответ на вопрос о сроке — практически только дата («11.2028», «до 12.27»,
# «срок 06.2029»). Либеральный _norm_expiry здесь опасен: «в 10.30 приеду»
# или «оплатил 5.000» превращались бы в срок годности (ревизия 01.08.2026).
EXPIRY_REPLY_RE = re.compile(
    r"^(?:срок(?:\s+годности)?|до|годен\s+до)?[:\s]*"
    r"(\d{1,2}\s*[./-]\s*\d{2,4})\s*(?:г(?:\.|ода)?)?\.?$",
    re.IGNORECASE)


async def expiry_reply(update, context, text: str) -> bool:
    """Сообщение — ответ на вопрос бота «какой срок годности?».

    True — сообщение обработано (в ИИ не идёт: и быстрее, и бесплатно).
    В группе непонятный ответ пропускаем дальше как обычное сообщение,
    чтобы не мешать разговору; вопрос остаётся висеть."""
    key = (update.effective_chat.id, update.effective_user.id)
    token = AWAIT_EXPIRY.get(key)
    if not token:
        return False
    private = update.effective_chat.type == "private"
    p = get_pending(token)
    if p is None:
        # Вопрос протух — не съедаем сообщение: пусть обработается как
        # обычно, а про устаревший вопрос скажем отдельно.
        AWAIT_EXPIRY.pop(key, None)
        if private:
            await update.message.reply_text(
                "⌛ Вопрос о сроке годности устарел — ту операцию нужно "
                "отправить заново. Это сообщение обрабатываю как обычно.")
        return False
    m = EXPIRY_REPLY_RE.match(text.strip())
    exp = _norm_expiry(m.group(1)) if m else ""
    if not exp:
        if private:
            await update.message.reply_text(
                "Не понял срок годности. Напишите месяц и год — например: "
                "<b>11.2028</b>\nИли нажмите «Отмена» под вопросом.",
                parse_mode="HTML")
        return private
    actor = db.get_user(update.effective_user.id)
    if actor is None or not actor["active"]:
        AWAIT_EXPIRY.pop(key, None)
        PENDING.pop(token, None)
        return private
    # Вопрос мог ждать ответа и у админа, и у сотрудника-заявителя —
    # принимается первый ответ, оба ожидания снимаются.
    for k in [k for k, v in AWAIT_EXPIRY.items() if v == token]:
        AWAIT_EXPIRY.pop(k, None)
    PENDING.pop(token, None)
    idx = p.pop("awaiting_expiry", None)
    try:
        it = p["items"][int(idx)]
    except (TypeError, ValueError, IndexError, KeyError):
        await update.message.reply_text(
            "⌛ Заявка устарела — отправьте операцию заново.")
        return True
    it["expiry"] = exp
    it["expiry_asked"] = True      # на складе товар лежит без срока
    # Ответил сотрудник — админ (подтверждающий) должен видеть, что заявка
    # поехала дальше; ответил админ — сотруднику скажет _finish_*.
    if p.get("approver_id") and update.effective_user.id != p["approver_id"]:
        try:
            await context.bot.send_message(
                p["approver_id"],
                f"📅 {esc(actor['name'])} назвал срок {exp} для "
                f"{esc(it['name'])} {esc(it['volume'])} — провожу операцию.",
                parse_mode="HTML")
        except Exception:
            log.warning("Не удалось уведомить админа об ответе о сроке")
    responder = _MsgResponder(update.message)
    await update.message.reply_text(
        f"📅 Записал срок {exp}: {esc(it['name'])} {esc(it['volume'])}.",
        parse_mode="HTML")
    try:
        await _continue_op(responder, context, actor, p, key[0], key[1])
    except Exception as e:
        log.exception("Ошибка проведения после ответа о сроке годности")
        await update.message.reply_text(f"⚠️ Операция НЕ проведена: {e}")
    return True


async def process_text(update, context, actor, text, draft=False, quiet=False):
    chat_id = update.effective_chat.id
    history = chat_histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})
    chat_histories[chat_id] = history[-HISTORY_LIMIT:]

    if not quiet:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        reply = await ask_claude(_request_history(chat_id), actor)
    except Exception as e:
        log.exception("Claude API error")
        # Убираем добавленное user-сообщение — иначе парность истории
        # ломается и мусор ездит в контексте следующих запросов.
        h = chat_histories.get(chat_id, [])
        if h and h[-1]["role"] == "user":
            h.pop()
        # Отвечаем и в чате склада: сюда попадает только похожее на операцию
        # (фильтр), молчание = сотрудник уверен, что оплата записана.
        await update.message.reply_text(
            f"⚠️ Не получилось обработать: {e}\nПопробуйте ещё раз или напишите боту в личку.")
        return
    chat_histories[chat_id].append({"role": "assistant", "content": reply})
    chat_histories[chat_id] = chat_histories[chat_id][-HISTORY_LIMIT:]

    await dispatch_action(update, context, actor, reply, draft, quiet=quiet)


# Действия, где склад берётся «свой по умолчанию», если не указан в сообщении.
WAREHOUSE_ACTIONS = {"invoice", "payment", "return", "inventory", "writeoff",
                     "amend_invoice", "replace_invoice", "set_min", "set_phone",
                     "set_price", "client_alias"}


async def dispatch_action(update, context, actor, reply, draft=False, quiet=False):
    data = extract_action(reply)
    if data is not None:
        # «Проведи за Данияра: Валя приход 1490» — операция от имени сотрудника:
        # запишется на него в журнал, деньги лягут в ЕГО кассу. Только админ.
        as_emp = str(data.pop("as_employee", "") or "").strip()
        if as_emp and is_admin(actor):
            emp = db.user_by_ref(as_emp)
            if emp is None or not emp["active"]:
                await update.message.reply_text(
                    f"Сотрудник «{esc(as_emp)}» не найден. Сотрудники: "
                    + ", ".join(u["name"] for u in db.list_users()),
                    parse_mode="HTML")
                return
            actor = emp
    if data is None:
        if "{" in reply and '"action"' in reply:
            # Модель начала выдавать JSON операции, но он не разобрался —
            # обычно ответ обрезан по лимиту токенов (очень длинный список).
            # Сырой JSON пользователю не показываем.
            log.warning("JSON операции не разобрался (обрезан?): %.500s", reply)
            await update.message.reply_text(
                "⚠️ Список получился слишком длинным — ответ оборвался, и я "
                "не смог его разобрать. Разбейте список на 2–3 части и "
                "отправьте по очереди, пожалуйста.")
            return
        # Отвечаем и в чате склада: сюда доходят только сообщения, похожие
        # на операцию (бесплатный фильтр), и ответ модели — обычно уточняющий
        # вопрос («какой Асан?»). Молчать нельзя — сотрудник решит, что
        # операция записана.
        await update.message.reply_text(reply)
        return
    # Сообщение в чате-ленте склада без указания склада: операция идёт
    # на склад этого чата (если чат привязан ровно к одному складу).
    # Замена/дополнение накладной по номеру: склад определяется самой
    # накладной, подстановка склада чата и вопрос «на каком складе?» не
    # нужны. Только для этих действий: модель может протащить op_id в
    # другие действия мусором из истории — там номер игнорируется.
    op_id_given = (data.get("action") in ("replace_invoice", "amend_invoice")
                   and bool(data.get("op_id")))
    if (data.get("action") in WAREHOUSE_ACTIONS and not op_id_given
            and not str(data.get("warehouse") or "").strip()
            and update.effective_chat is not None):
        feed_whs = db.warehouses_of_feed(update.effective_chat.id)
        if len(feed_whs) == 1:
            data["warehouse"] = feed_whs[0]["name"]
    # Сотрудник с доступом к нескольким складам не указал склад — уточняем
    # кнопками, чтобы операция случайно не ушла на «родной» склад
    # (просьба владельца 21.07.2026). Админа не трогаем: у него все склады,
    # и он работает со своего Бишкека.
    if (data.get("action") in WAREHOUSE_ACTIONS and not is_admin(actor)
            and not op_id_given
            and not str(data.get("warehouse") or "").strip()):
        whs = db.operable_warehouses(actor)
        if len(whs) > 1:
            payload = {"kind": "pick_wh", "user_id": actor["id"],
                       "chat_id": update.effective_chat.id,
                       "action_data": data, "draft": draft}
            token = new_pending(payload)
            kb = [[InlineKeyboardButton(f"📦 {w['name']}",
                                        callback_data=f"pw:{token}:{w['id']}")]
                  for w in whs]
            kb.append([InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")])
            await update.message.reply_text(
                "У вас доступ к нескольким складам — уточните, на каком провести:",
                reply_markup=InlineKeyboardMarkup(kb))
            return
    await dispatch_data(update, context, actor, data, reply, draft)


async def dispatch_data(update, context, actor, data, reply="", draft=False):
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
        elif action == "writeoff":
            await start_writeoff(update, context, actor, data)
        elif action == "set_price":
            await start_set_price(update, context, actor, data)
        elif action == "change_price":
            await start_change_price(update, context, actor, data)
        elif action == "set_buy_price":
            await start_set_buy_price(update, context, actor, data)
        elif action == "set_usd_rate":
            await start_set_usd_rate(update, context, actor, data)
        elif action == "set_buy_markup":
            await start_set_buy_markup(update, context, actor, data)
        elif action == "add_clients":
            await start_add_clients(update, context, actor, data)
        elif action == "client_alias":
            await start_client_alias(update, context, actor, data)
        elif action == "amend_invoice":
            await start_amend_invoice(update, context, actor, data)
        elif action == "replace_invoice":
            await start_replace_invoice(update, context, actor, data)
        elif action == "promise":
            await start_promise(update, context, actor, data)
        elif action == "promise_done":
            await start_promise_done(update, context, actor, data)
        elif action == "return":
            await start_return(update, context, actor, data)
        elif action == "handover":
            await start_handover(update, context, actor, data)
        elif action == "set_phone":
            await start_set_phone(update, context, actor, data)
        else:
            await update.message.reply_text(reply or "⚠️ Не понял действие — напишите ещё раз.")
    except Exception as e:
        log.exception("Ошибка обработки действия %s", action)
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


DRAFT_RE = re.compile(r"^черновик[:,\s]*", re.IGNORECASE)

MAX_PHOTO_BYTES = 4_500_000  # лимит Claude на изображение


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото рукописного/печатного списка -> накладная (или другое действие)."""
    import base64
    if update.message is None:
        return
    if update.effective_chat.type != "private":
        return
    actor = await get_actor(update)
    if actor is None:
        return
    caption = (update.message.caption or "").strip()
    if CERT_CAPTION_RE.match(caption):
        # Фото сертификата от админа — сохранить, в ИИ не отправлять
        if update.message.document is not None:
            await _handle_cert_upload(update, update.message.document.file_id,
                                      "document",
                                      update.message.document.file_name,
                                      caption)
        elif update.message.photo:
            await _handle_cert_upload(update, update.message.photo[-1].file_id,
                                      "photo", None, caption)
        return
    if is_admin(actor):
        # Запомним фото на случай, если подпись «сертификат …» придёт
        # следующим сообщением (обычная обработка продолжается)
        if update.message.document is not None:
            _remember_cert_doc(update, update.message.document.file_id,
                               "document", update.message.document.file_name)
        elif update.message.photo:
            _remember_cert_doc(update, update.message.photo[-1].file_id,
                               "photo", None)
    draft = False
    m = DRAFT_RE.match(caption)
    if m:
        draft = True
        caption = caption[m.end():].strip()

    if update.message.photo:
        tg_obj = update.message.photo[-1]
        media_type = "image/jpeg"
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        tg_obj = update.message.document
        media_type = update.message.document.mime_type
    else:
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        tg_file = await tg_obj.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        log.exception("Не удалось скачать фото")
        await update.message.reply_text(f"⚠️ Не удалось загрузить фото: {e}")
        return
    if len(raw) > MAX_PHOTO_BYTES:
        await update.message.reply_text(
            "⚠️ Фото слишком большое. Отправьте его как обычное фото (со сжатием), "
            "а не как файл.")
        return

    instruction = (
        "На фото — рукописный или печатный список. Внимательно прочитай его "
        "и разбери по правилам выше: накладная -> JSON invoice, перемещение -> transfer, "
        "инвентаризация -> inventory; если по подписи это список клиентов (без товаров) "
        "-> add_clients. Названия товаров сопоставь с прайсом. "
        "Если какая-то строка неразборчива — не выдумывай, спроси текстом.")
    if caption:
        instruction += f"\nПодпись сотрудника к фото: {caption}"
    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": media_type,
                    "data": base64.b64encode(raw).decode()}},
        {"type": "text", "text": instruction},
    ]

    history = chat_histories.setdefault(chat_id, [])
    # История с обрезкой старых сообщений и гарантией «первым — user»
    messages = _request_history(chat_id) + [{"role": "user", "content": content}]
    try:
        resp = await anthropic_client.messages.create(
            model=CLAUDE_MODEL, max_tokens=CLAUDE_MAX_TOKENS,
            system=system_blocks(actor), messages=messages)
        track_usage(resp)
        reply = _response_text(resp)
    except Exception as e:
        log.exception("Claude API error (photo)")
        await update.message.reply_text(f"⚠️ Ошибка при распознавании фото: {e}")
        return
    # в историю кладём текстовый след вместо картинки, чтобы не раздувать контекст
    trace = "[Отправил фото со списком товаров]"
    if caption:
        trace += f" Подпись: {caption}"
    history.append({"role": "user", "content": trace})
    history.append({"role": "assistant", "content": reply})
    chat_histories[chat_id] = history[-HISTORY_LIMIT:]

    await dispatch_action(update, context, actor, reply, draft=draft)


def _build_stt_prompt() -> str:
    """Подсказка Whisper, чтобы «Албенивер» не превращался в «Албанию».
    У Whisper лимит подсказки 224 токена, кириллица «тяжёлая» (~2 символа
    на токен), поэтому препараты держим в ~260 символах — остаток бюджета
    отдаём именам клиентов (см. _stt_prompt_full)."""
    seen, names = set(), []
    for it in prices.PRICE_LIST_DATA:
        word = it["name"].split("(")[0].strip().split()[0].capitalize()
        if word not in seen:
            seen.add(word)
            names.append(word)
    head = "Джарвис! Накладная ветаптеки ВЕТОП: "
    tail = "; черновик, коробка, штук, долг, приход, сом."
    body = ""
    for n in names:
        if len(head) + len(body) + len(n) + len(tail) + 2 > 260:
            break
        body += (", " if body else "") + n
    return head + body + tail


# Лимит подсказки Whisper — 224 токена. Кириллические имена собственные могут
# токенизироваться тяжелее 2 симв/токен, поэтому бюджет консервативный:
# 380 символов ≈ 190–220 токенов. Превышение = 400 от Groq и повтор без
# подсказки вовсе (тогда препараты снова коверкаются).
STT_PROMPT_BUDGET = 380


def _stt_prompt_full() -> str:
    """Подсказка Whisper: препараты + имена известных клиентов из базы."""
    base = STT_PROMPT
    try:
        names = known_clients_cached(30)
    except Exception:
        log.exception("Не удалось получить имена клиентов для подсказки STT")
        names = []
    if not names:
        return base
    extra = ""
    for n in names:
        add = (", " if extra else " Клиенты: ") + n
        if len(base) + len(extra) + len(add) + 1 > STT_PROMPT_BUDGET:
            break
        extra += add
    return base + (extra + "." if extra else "")


STT_PROMPT = _build_stt_prompt()


def _stt_error(resp) -> RuntimeError:
    log.error("STT %s: %s", resp.status_code, resp.text[:500])
    try:
        j = resp.json()
        detail = j.get("error", {}).get("message") or j.get("detail")
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
        detail = str(detail or resp.text[:200])
    except Exception:
        detail = resp.text[:200]
    return RuntimeError(f"сервис распознавания ответил {resp.status_code} ({detail})")


async def _transcribe_elevenlabs(raw: bytes, filename: str, mime: str) -> str:
    """ElevenLabs Scribe: поддерживает кыргызский, язык определяет сам."""
    import httpx
    async with httpx.AsyncClient(timeout=120) as cl:
        resp = await cl.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            data={"model_id": ELEVENLABS_STT_MODEL},
            files={"file": (filename, raw, mime)},
        )
        if resp.status_code >= 400:
            raise _stt_error(resp)
        return (resp.json().get("text") or "").strip()


async def _transcribe_whisper(raw: bytes, filename: str, mime: str) -> str:
    """OpenAI-совместимый Whisper API (Groq/OpenAI)."""
    import httpx
    url = f"{STT_BASE_URL}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {STT_API_KEY}"}
    data = {"model": STT_MODEL, "temperature": "0", "prompt": _stt_prompt_full()}
    if STT_LANGUAGE and STT_LANGUAGE != "auto":
        data["language"] = STT_LANGUAGE
    async with httpx.AsyncClient(timeout=120) as cl:
        resp = await cl.post(url, headers=headers, data=data,
                             files={"file": (filename, raw, mime)})
        if resp.status_code == 400:
            # Скорее всего сервису не понравилась подсказка — пробуем без неё.
            log.warning("STT 400, повтор без подсказки: %s", resp.text[:500])
            data.pop("prompt", None)
            resp = await cl.post(url, headers=headers, data=data,
                                 files={"file": (filename, raw, mime)})
        elif resp.status_code == 429:
            # Лимит запросов Groq — одна повторная попытка после паузы.
            log.warning("STT 429, повтор через 3 сек")
            await asyncio.sleep(3)
            resp = await cl.post(url, headers=headers, data=data,
                                 files={"file": (filename, raw, mime)})
        if resp.status_code >= 400:
            raise _stt_error(resp)
        return (resp.json().get("text") or "").strip()


async def transcribe_audio(raw: bytes, filename: str, mime: str) -> str:
    """Речь -> текст. ElevenLabs (если задан ключ, умеет кыргызский), иначе Whisper."""
    if ELEVENLABS_API_KEY:
        return await _transcribe_elevenlabs(raw, filename, mime)
    return await _transcribe_whisper(raw, filename, mime)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Голосовое сообщение -> текст (Whisper) -> обычная обработка."""
    if update.message is None:
        return
    quiet = False
    if update.effective_chat.type != "private":
        # В чате-ленте склада голосовые сотрудников тоже обрабатываем,
        # но молча: бот вмешается, только если услышит операцию.
        actor = _feed_chat_actor(update)
        if actor is None:
            return
        quiet = True
    else:
        actor = await get_actor(update)
        if actor is None:
            return
    if quiet and not STT_API_KEY:
        # В чате склада обращение «Джарвис» ищем бесплатным Groq — без него
        # голосовые в группах не слушаем вовсе (платный ElevenLabs не должен
        # расшифровывать каждый разговор ради поиска обращения).
        return
    if not STT_API_KEY and not ELEVENLABS_API_KEY:
        if is_admin(actor):
            await update.message.reply_text(
                "🎤 Распознавание голосовых пока не настроено.\n\n"
                "1. Зарегистрируйтесь на console.groq.com (бесплатно)\n"
                "2. Создайте API-ключ (кнопка API Keys)\n"
                "3. На Railway добавьте переменную GROQ_API_KEY с этим ключом\n"
                "После перезапуска бот начнёт понимать голосовые.")
        else:
            await update.message.reply_text(
                "🎤 Голосовые пока не подключены — напишите текстом, пожалуйста.")
        return
    voice = update.message.voice
    audio = update.message.audio
    tg_obj = voice or audio
    if tg_obj is None:
        return
    duration = tg_obj.duration or 0
    if duration > MAX_VOICE_SECONDS:
        if not quiet:
            await update.message.reply_text(
                f"⚠️ Голосовое слишком длинное ({duration // 60} мин {duration % 60} сек). "
                f"Максимум — {MAX_VOICE_SECONDS // 60} мин.")
        return
    chat_id = update.effective_chat.id
    if not quiet:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        tg_file = await tg_obj.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        log.exception("Не удалось скачать голосовое")
        if not quiet:
            await update.message.reply_text(f"⚠️ Не удалось загрузить голосовое: {e}")
        return
    if voice is not None:
        filename, mime = "voice.ogg", "audio/ogg"
    else:
        filename = audio.file_name or "audio.mp3"
        mime = audio.mime_type or "audio/mpeg"
    try:
        if quiet:
            # Чат склада: обращение ищем бесплатным Groq/Whisper (русский).
            text = await _transcribe_whisper(raw, filename, mime)
        else:
            text = await transcribe_audio(raw, filename, mime)
    except Exception as e:
        log.exception("Ошибка распознавания голосового")
        if not quiet:
            await update.message.reply_text(f"⚠️ Не удалось распознать голосовое: {e}")
        return
    if not text:
        if not quiet:
            await update.message.reply_text(
                "⚠️ Не разобрал речь — попробуйте ещё раз или напишите текстом.")
        return
    if quiet:
        # В чате склада бот слушает только голосовые, начинающиеся с
        # обращения «Джарвис» — остальные разговоры игнорирует молча.
        rest = _strip_wake_word(text)
        if rest is None:
            return
        if ELEVENLABS_API_KEY:
            # К боту обратились явно — можно потратить платный ElevenLabs,
            # чтобы правильно разобрать кыргызскую или смешанную речь.
            try:
                text2 = await _transcribe_elevenlabs(raw, filename, mime)
                if text2:
                    rest2 = _strip_wake_word(text2)
                    rest = rest2 if rest2 is not None else text2
            except Exception:
                log.exception("ElevenLabs в чате склада не сработал — "
                              "остаёмся на распознавании Whisper")
        text = rest
        if not text:
            await update.message.reply_text(
                "🎤 Слушаю! После «Джарвис» продиктуйте операцию, например: "
                "«Джарвис, Асель приход 5000».")
            return
        await update.message.reply_text(f"🎤 Распознал: «{text}»")
    else:
        # Эхо «Распознал» — в личке всегда; в чате склада — выше, только
        # после обращения «Джарвис».
        await update.message.reply_text(f"🎤 Распознал: «{text}»")
    # Срок годности можно и продиктовать: «одиннадцать двадцать восемь»
    # распознаётся как «11.28» — этого достаточно.
    if await expiry_reply(update, context, text):
        return
    draft = False
    m = DRAFT_RE.match(text)
    if m:
        draft = True
        text = text[m.end():].strip()
        if not text:
            if not quiet:
                await update.message.reply_text(
                    "После слова «черновик» продиктуйте накладную.")
            return
    await process_text(update, context, actor, text, draft=draft, quiet=quiet)


def _feed_chat_actor(update):
    """Сотрудник в чате-ленте склада: обрабатываем его сообщения как операции.

    Возвращает строку users или None (чужой чат / не сотрудник) — без ответов,
    чтобы бот не встревал в разговоры."""
    if not db.warehouses_of_feed(update.effective_chat.id):
        return None
    user = update.effective_user
    if user is None:
        return None
    row = db.get_user(user.id)
    if row is None or not row["active"]:
        return None
    return row


# Обращение «Джарвис» в начале сообщения в чате склада: голосовые без него
# игнорируются, с ним — обрабатываются как явная команда боту (просьба
# владельца 21.07.2026). Распознавание может исказить имя, поэтому кроме
# точных вариантов допускаем нечёткое совпадение первого слова.
_WAKE_WORDS = ("джарвис", "жарвис", "jarvis")
_WAKE_RE = re.compile(
    r"^\W*(джарвис|жарвис|джервис|жервис|джарбис|jarvis|jarvis'?s)\b[\s,.!:—–-]*",
    re.IGNORECASE)
_FIRST_WORD_RE = re.compile(r"^\W*([а-яёa-z]+)[\s,.!:—–-]*", re.IGNORECASE)


def _strip_wake_word(text: str):
    """«Джарвис, Асель приход 5000» -> «Асель приход 5000»; None — обращения нет."""
    m = _WAKE_RE.match(text)
    if m is None:
        m = _FIRST_WORD_RE.match(text)
        if m is None:
            return None
        w = m.group(1).lower()
        # Нечёткое совпадение — только для слов, начинающихся как «Джарвис»
        # (дж/ж/j): порог 0.7 без этого ловил «барис», «завис», «давис»
        # (аудит 27.07.2026).
        if not w.startswith(("дж", "ж", "j")):
            return None
        if max(difflib.SequenceMatcher(None, w, wk).ratio()
               for wk in _WAKE_WORDS) < 0.7:
            return None
    return text[m.end():].strip()


# Слова-приметы операций: если ни одной приметы нет, сообщение в чате склада
# в ИИ не отправляется (экономия API — болтовня до Claude не доходит).
# «Сильные» слова срабатывают сами по себе; «слабые» — только вместе с
# цифрами (аудит 27.07.2026: «Долго ждать» ловилось на «долг», «Телефон
# разрядился» — на «телефон»).
OP_STRONG = ("приход", "наклад", "черновик", "возврат", "перемещ",
             "инвентариз", "сдал", "сдаю", "обещал", "обеща", "спеццен",
             "спиши", "списан", "списать", "просрочк", "утилиз")
OP_WEAK = ("оплат", "заплат", "минимал", "минимум", "телефон", "добавь",
           "долг")


def _looks_like_operation(text: str) -> bool:
    """Бесплатная проверка «похоже на операцию» для чатов складов."""
    t = text.lower()
    has_digit = any(ch.isdigit() for ch in t)
    if any(k in t for k in OP_STRONG):
        return True
    if not has_digit:
        return False
    if any(k in t for k in OP_WEAK):
        return True
    # Товар из прайса + числа — похоже на накладную/оплату. Короткие
    # приметы («топ», «ивер») — только целым словом, иначе ловится «стоп»,
    # «универ» и т.п.
    words = set(re.findall(r"[а-яёa-z]+", t))
    for p in prices.PRICE_LIST_DATA:
        w = p["name"].split()[0].split("-")[0].lower()
        if (w in t if len(w) >= 5 else w in words):
            return True
    for name in known_clients_cached(300):
        w = name.split()[0].lower()
        if len(w) >= 3 and w in words:
            return True
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not update.message.text:
        return
    # Ответ на вопрос о сроке годности ловим до всех фильтров: голое
    # «11.2028» в чате склада на операцию не похоже и иначе потерялось бы.
    if await expiry_reply(update, context, update.message.text.strip()):
        return
    quiet = False
    if update.effective_chat.type != "private":
        # В группах работаем только в чатах-лентах складов и только с
        # сотрудниками; отвечаем только на операции (просьба владельца
        # 21.07.2026: «Асан приход 5000» можно писать прямо в чат склада).
        actor = _feed_chat_actor(update)
        if actor is None:
            return
        # «Джарвис, …» — явное обращение, обрабатываем без фильтра примет.
        rest = _strip_wake_word(update.message.text)
        if rest is None and not _looks_like_operation(update.message.text):
            return
        quiet = True
    else:
        actor = await get_actor(update)
        if actor is None:
            return
    text = update.message.text.strip()
    if quiet and rest is not None:
        text = rest
        if not text:
            await update.message.reply_text(
                "Слушаю! После «Джарвис» напишите операцию, например: "
                "«Джарвис, Асель приход 5000».")
            return
    # «сертификат …» текстом в личке — не для ИИ: либо подпись к только что
    # присланному файлу (владелец шлёт файл и подпись отдельно, 08.08.2026),
    # либо подсказка про /cert. Бесплатно и без фантазий ИИ.
    if not quiet and CERT_CAPTION_RE.match(text) and len(text.split()) <= 25:
        if await cert_text_reply(update, actor, text):
            return
    draft = False
    m = DRAFT_RE.match(text)
    if m:
        draft = True
        text = text[m.end():].strip()
        if not text:
            if not quiet:
                await update.message.reply_text(
                    "После слова «черновик» напишите накладную.\n"
                    "Пример: черновик: Асан, Албенивер 200мл 1к")
            return
    await process_text(update, context, actor, text, draft=draft, quiet=quiet)


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
            "⏳ <b>Сейчас переходный период</b> — накладные выдаются черновиками.",
            "Просто напишите как обычно:",
            "<i>Асан, Албенивер 200мл 1к, долг 31470</i>",
            "Придёт PDF-накладная для клиента, база не меняется.",
            "Слово «черновик» писать не нужно (но можно).",
            "Ещё можно спрашивать цены: <i>сколько стоит Альтопен 1л?</i> и /price",
            "",
            "Остальные функции откроются после настройки базы:",
        ]
    lines += [
        "📋 <b>Накладная</b> — просто напишите:",
        "<i>Асан, Албенивер 200мл 1к, Дексатоп 50мл 5 шт</i>",
        "С другого склада: <i>со склада Ош: Асан, ...</i>",
        "📝 <b>Черновик</b> (без проведения): <i>черновик: Асан, ...</i>",
        "📷 <b>Фото списка</b> — сфотографируйте рукописный список, бот сам разберёт "
        "(в подписи можно указать клиента и «черновик»)",
        "🎤 <b>Голосовое</b> — продиктуйте накладную или вопрос, бот распознает речь "
        "(можно начать со слова «черновик»)",
        "💵 <b>Оплата</b>: <i>Асан приход 5000</i>",
        "",
        "📌 <b>Команды:</b>",
        "/stock — остатки склада",
        "/stockprice — остатки с ценами и суммой по прайсу",
        "/debts — долги клиентов",
        "/report — отчёт (можно: /report неделя, /report Бишкек месяц)",
        "/olddebts — кто давно не платил (можно: /olddebts 45)",
        "/deadstock — залежавшийся товар · /forecast — что скоро закончится",
        "/client Имя — карточка клиента (долг, история)",
        "/sales Препарат — история продаж препарата (можно: /sales Дексатоп 50мл Мустанг)",
        "/act Имя — акт сверки в PDF",
        "/price — прайс-лист",
        "/pricepdf — красивый PDF-прайс для отправки клиентам",
        "/cert Название — сертификат соответствия препарата (для клиентов)",
        "/log — журнал последних операций PDF-файлом (/log 50 — больше)",
        "/undo — отменить свою последнюю операцию (в течение часа)",
        "➕ <b>Дополнить накладную</b>: <i>добавь в последнюю накладную Мустанга: "
        "Дексатоп 50мл 5 шт</i>",
        "/clear — очистить историю разговора",
    ]
    lines += [
        "",
        "📦 <b>Перемещение товара</b> (заявка, подтверждает админ):",
        "<i>с Бишкека на Каракол: Альтопен 100мл 2к</i>",
        "📋 <b>Инвентаризация</b> (корректировку подтверждает админ):",
        "<i>инвентаризация: Альтопен 100мл 18, Дексатоп 50мл 9</i>",
        "🔙 <b>Возврат товара</b> (подтверждает админ):",
        "<i>возврат от Асана: Альтопен 100мл 5 шт</i>",
        "🗑 <b>Списание</b> — порча, бой, просрочка (подтверждает админ):",
        "<i>спиши Альтопен 100мл 5 шт — просрочка</i>",
        "📅 При перемещении и списании бот спросит срок годности, если он "
        "у товара не записан — посмотрите на упаковку и напишите его "
        "(например <i>11.2028</i>).",
        "💰 <b>Сдать выручку</b>: <i>сдал 50000</i> · /cash — ваша касса",
        "📞 <b>Телефон клиента</b>: <i>телефон Асана: 0700 12 34 56</i>",
        "📅 <b>Обещание оплаты</b>: <i>Асан обещал 50000 в пятницу</i> — утром "
        "напомню · /promises — список (заплатил: <i>Асан выполнил обещание</i>)",
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
            "<i>проведи за Данияра: Валя приход 1490</i> — операция от имени "
            "сотрудника (деньги в его кассу)",
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
            "/export — Excel: операции, долги, остатки, кассы "
            "(можно: /export неделя, /export все)",
            "/api — расходы на ИИ и остаток на счёте (задать: /apibalance 50)",
            "/drafts — черновики за сегодня по сотрудникам "
            "(сводка сама приходит в 19:00)",
            "/minstock — пороги «заканчивается товар» "
            "(задать: «минимум для Каракола: Альтопен 100мл 20 шт»)",
            "Спеццены: «цена для Асана: Альтопен 100мл 85» (убрать: цена 0)",
            "Общий прайс: «новая цена Альтопен 100мл 95» · /pricelog — история",
            "/abc — АВС-анализ: топ товаров и клиентов (можно: /abc неделя)",
            "Справочник клиентов: «добавь клиентов на Каракол: Асан, Болот...» "
            "(можно голосом или фото списка; с долгами: «Асан — 31470»)",
            "/fullmode — какие склады в полном учёте (включить: /fullmode Каракол)",
            "/expiry Склад — сроки годности товара",
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


async def pricepdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                       action="upload_document")
    pdf = generate_price_pdf(prices.PRICE_LIST_DATA)
    filename = f"прайс_ВЕТОП_{datetime.now(BISHKEK).strftime('%d%m%Y')}.pdf"
    await update.message.reply_document(
        document=InputFile(pdf, filename=filename),
        caption="📋 Прайс-лист ВЕТОП — можно переслать клиенту")


async def _stock_report(update, context, actor, whs, with_prices=False):
    """Собирает и шлёт PDF остатков по списку складов.

    Весь прайс по порядку: нулевые остатки тоже видны (прочерком)."""
    sections, summary = [], []
    for wh in whs:
        smap = db.stock_map(wh["id"])
        rows, total_qty, total_sum, in_stock = [], 0, 0.0, 0
        for p in prices.PRICE_LIST_DATA:
            qty = smap.get(p["id"], 0)
            if with_prices:
                sub = qty * p["price"]
                total_sum += sub
                rows.append([p["id"], p["name"], p["volume"],
                             f"{qty} шт" if qty else "—",
                             fmt_num(p["price"]),
                             fmt_num(sub) if qty else "—"])
            else:
                rows.append([p["id"], p["name"], p["volume"],
                             f"{qty} шт" if qty else "—"])
            if qty:
                total_qty += qty
                in_stock += 1
        if not in_stock:
            summary.append(f"📦 «{wh['name']}»: пусто")
            continue
        if with_prices:
            sections.append({
                "title": f"Склад «{wh['name']}»",
                "headers": ["№", "Товар", "Фасовка", "Остаток", "Цена", "Сумма"],
                "rows": rows, "widths": [10, 76, 24, 19, 17, 21],
                "footer": (f"В наличии: {in_stock} из {len(rows)} позиций прайса · "
                           f"{fmt_num(total_qty)} шт · на {money(total_sum)}"),
            })
            summary.append(f"💰 «{wh['name']}»: {fmt_num(total_qty)} шт "
                           f"на {money(total_sum)} по прайсу")
        else:
            sections.append({
                "title": f"Склад «{wh['name']}»",
                "headers": ["№", "Товар", "Фасовка", "Остаток"],
                "rows": rows, "widths": [10, 105, 28, 24],
                "footer": (f"В наличии: {in_stock} из {len(rows)} позиций прайса · "
                           f"всего {fmt_num(total_qty)} шт"),
            })
            summary.append(f"📦 «{wh['name']}»: в наличии {in_stock} из "
                           f"{len(rows)} поз., {fmt_num(total_qty)} шт")
    if not sections:
        await update.message.reply_text("\n".join(summary) or "Складов нет.")
        return
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    if with_prices:
        pdf = generate_report_pdf(
            "ОСТАТКИ С ЦЕНАМИ",
            f"ОсОО «ВЕТОП» · продажные цены · на {date_str}", sections)
        filename = f"остатки_цены_{date_str.replace('.', '')}.pdf"
    else:
        pdf = generate_report_pdf(
            "ОСТАТКИ СКЛАДОВ", f"ОсОО «ВЕТОП» · на {date_str}", sections)
        filename = f"остатки_{date_str.replace('.', '')}.pdf"
    await update.message.reply_document(
        document=InputFile(pdf, filename=filename), caption="\n".join(summary))


_DATE_WORD = {"вчера": -1, "сегодня": 0, "позавчера": -2}


def _parse_moment(tokens):
    """['вчера'] / ['22.07'] / ['22.07.2026', '14:30'] -> (datetime, желаемая
    подпись) или (None, None). Без времени — конец дня 23:59."""
    if not tokens:
        return None, None
    now = datetime.now(BISHKEK)
    d = None
    rest = []
    t0 = tokens[0].lower()
    if t0 in _DATE_WORD:
        d = (now + timedelta(days=_DATE_WORD[t0])).replace(
            hour=0, minute=0, second=0, microsecond=0)
        rest = tokens[1:]
    else:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", t0)
        if m:
            try:
                d = datetime(int(m.group(3) or now.year), int(m.group(2)),
                             int(m.group(1)), tzinfo=BISHKEK)
            except ValueError:
                return None, None
            rest = tokens[1:]
    if d is None:
        return None, None
    hh, mm = 23, 59
    if rest:
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", rest[0])
        if m and 0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59:
            hh, mm = int(m.group(1)), int(m.group(2))
    moment = d.replace(hour=hh, minute=mm, second=59)
    return moment, moment.strftime("%d.%m.%Y %H:%M")


async def stockat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Остатки склада на дату/время в прошлом (по журналу операций)."""
    actor = await get_actor(update)
    if actor is None:
        return
    if not await _require_private(update):
        return
    usage = ("Использование: /stockat Склад Дата [Время]\n"
             "Примеры:\n"
             "/stockat Каракол вчера — конец вчерашнего дня\n"
             "/stockat Каракол 22.07 — конец дня 22.07\n"
             "/stockat Каракол 22.07 14:30 — на момент времени\n"
             "Время операций смотрите в /log")
    args = list(context.args or [])
    # Склад — все слова до первого похожего на дату
    wh_tokens = []
    while args and not (args[0].lower() in _DATE_WORD
                        or re.fullmatch(r"\d{1,2}\.\d{1,2}(\.\d{4})?", args[0])):
        wh_tokens.append(args.pop(0))
    wh_name = " ".join(wh_tokens).strip()
    moment, label = _parse_moment(args)
    if not wh_name or moment is None:
        await update.message.reply_text(usage)
        return
    wh = db.warehouse_by_name(wh_name)
    if wh is None or not db.can_view_warehouse(actor, wh["id"]):
        await update.message.reply_text(
            f"Склад «{esc(wh_name)}» не найден или нет доступа.", parse_mode="HTML")
        return
    now = datetime.now(BISHKEK)
    if moment > now:
        moment, label = now, now.strftime("%d.%m.%Y %H:%M")
    smap = db.stock_at(wh["id"], moment.isoformat(timespec="seconds"))
    rows, total_qty, in_stock = [], 0, 0
    for p in prices.PRICE_LIST_DATA:
        qty = smap.get(p["id"], 0)
        rows.append([p["id"], p["name"], p["volume"],
                     f"{qty} шт" if qty else "—"])
        if qty:
            total_qty += qty
            in_stock += 1
    pdf = generate_report_pdf(
        "ОСТАТКИ НА ДАТУ",
        f"ОсОО «ВЕТОП» · склад «{wh['name']}» · на {label}",
        [{"title": f"Склад «{wh['name']}» на {label}",
          "headers": ["№", "Товар", "Фасовка", "Остаток"],
          "rows": rows, "widths": [10, 105, 28, 24],
          "footer": (f"В наличии было: {in_stock} из {len(rows)} позиций · "
                     f"всего {fmt_num(total_qty)} шт")}])
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"остатки_{wh['name']}_"
                           f"{moment.strftime('%d%m%Y_%H%M')}.pdf"),
        caption=(f"🕰 «{wh['name']}» на {label}: {in_stock} поз., "
                 f"{fmt_num(total_qty)} шт\n"
                 "Восстановлено по журналу операций; отменённые операции "
                 "не учитываются."))


async def _stock_cmd(update, context, with_prices):
    actor = await get_actor(update)
    if actor is None:
        return
    # В чате склада — только склад этого чата, без кнопок выбора.
    whs_group, in_group = await _group_only_feed_whs(update, actor)
    if in_group:
        if whs_group:
            await _stock_report(update, context, actor, whs_group, with_prices)
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if arg and arg.lower() != "all":
        wh = db.warehouse_by_name(arg)
        if wh is None or not db.can_view_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(arg)}» не найден или нет доступа.", parse_mode="HTML")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
        if not whs:
            await update.message.reply_text("У вас нет склада — обратитесь к администратору.")
            return
        if not arg and len(whs) > 1:
            # Несколько складов — спрашиваем кнопками, какой показать
            # (просьба владельца 21.07.2026). «Все сразу» — кнопкой или /stock all.
            payload = {"kind": "pick_stock", "user_id": actor["id"],
                       "chat_id": update.effective_chat.id,
                       "with_prices": with_prices}
            token = new_pending(payload)
            kb = [[InlineKeyboardButton(f"📦 {w['name']}",
                                        callback_data=f"ps:{token}:{w['id']}")]
                  for w in whs]
            kb.append([InlineKeyboardButton("🗂 Все склады сразу",
                                            callback_data=f"ps:{token}:all")])
            kb.append([InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")])
            await update.message.reply_text("Остатки какого склада показать?",
                                            reply_markup=InlineKeyboardMarkup(kb))
            return
    await _stock_report(update, context, actor, whs, with_prices)


async def show_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _stock_cmd(update, context, with_prices=False)


async def show_stock_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Остатки склада с продажными ценами и суммой по прайсу (/stockprice)."""
    await _stock_cmd(update, context, with_prices=True)


# Долги отдельных складов можно скрыть от сотрудников (/hidedebts —
# просьба владельца 13.08.2026 при загрузке должников Бишкека:
# «никому, кроме меня, не показывай должников»). Хранится в settings.
HIDDEN_DEBT_MSG = "Долги этого склада пока показывает только администратор."


def hidden_debt_wh_ids() -> set:
    raw = db.get_setting("hidden_debt_whs")
    try:
        return {int(x) for x in json.loads(raw)} if raw else set()
    except (ValueError, TypeError):
        return set()


def debt_whs_for(actor, whs) -> list:
    """Склады без тех, чьи долги скрыты от сотрудников. Админ видит всё."""
    if is_admin(actor):
        return list(whs)
    hid = hidden_debt_wh_ids()
    return [w for w in whs if w["id"] not in hid]


def _debts_hidden_from(actor, wh_id: int) -> bool:
    return not is_admin(actor) and wh_id in hidden_debt_wh_ids()


async def hidedebts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрыть/открыть долги склада для сотрудников (админ)."""
    if await _require_admin(update) is None:
        return
    hid = hidden_debt_wh_ids()
    args = list(context.args or [])
    if not args:
        if hid:
            names = [w["name"] for w in db.all_warehouses() if w["id"] in hid]
            txt = ("🙈 Долги скрыты от сотрудников: " + ", ".join(names)
                   + "\n(отчёты о долгах, справочник, карточки клиентов и "
                     "понедельничные напоминания по этим складам видите "
                     "только вы)")
        else:
            txt = "Скрытых долгов нет — все склады видны сотрудникам как обычно."
        txt += "\n\nСкрыть: /hidedebts Бишкек\nОткрыть: /hidedebts выкл Бишкек"
        await update.message.reply_text(txt)
        return
    off = args[0].lower() in ("выкл", "выкл.", "off", "открыть")
    name = " ".join(args[1:] if off else args).strip()
    wh = db.warehouse_by_name(name)
    if wh is None:
        await update.message.reply_text(
            f"Склад «{esc(name)}» не найден.", parse_mode="HTML")
        return
    if off:
        hid.discard(wh["id"])
        msg = (f"👁 Долги склада «{esc(wh['name'])}» снова видны сотрудникам "
               "с доступом.")
    else:
        hid.add(wh["id"])
        msg = (f"🙈 Долги склада «{esc(wh['name'])}» теперь видите только вы: "
               "/debts, /olddebts, /clients, /client, /act и понедельничные "
               "напоминания сотрудникам их не покажут.")
    db.set_setting("hidden_debt_whs", json.dumps(sorted(hid)))
    await update.message.reply_text(msg, parse_mode="HTML")


async def show_debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    # В чате-ленте склада показываем ТОЛЬКО склад этого чата (просьба
    # владельца 03.08.2026) — команда команды склада, чужие склады и их
    # телефоны в группу не попадают. В прочих группах — отказ (аудит 27.07).
    whs_group, in_group = await _group_only_feed_whs(update, actor)
    if in_group and not whs_group:
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if in_group:
        whs = debt_whs_for(actor, whs_group)
        if not whs:
            await update.message.reply_text(HIDDEN_DEBT_MSG)
            return
    elif arg and arg.lower() != "all":
        wh = db.warehouse_by_name(arg)
        if wh is None or not db.can_view_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(arg)}» не найден или нет доступа.", parse_mode="HTML")
            return
        if _debts_hidden_from(actor, wh["id"]):
            await update.message.reply_text(HIDDEN_DEBT_MSG)
            return
        whs = [wh]
    else:
        whs = debt_whs_for(actor, db.visible_warehouses(actor))
        if not whs:
            await update.message.reply_text(HIDDEN_DEBT_MSG)
            return
        if not arg and len(whs) > 1:
            # Несколько складов — сначала спрашиваем кнопками (просьба
            # владельца 10.08.2026: «не отправляй сразу все долги»).
            # «Все сразу» — кнопкой или /debts all.
            payload = {"kind": "pick_debts", "user_id": actor["id"],
                       "chat_id": update.effective_chat.id}
            token = new_pending(payload)
            kb = [[InlineKeyboardButton(f"💰 {w['name']}",
                                        callback_data=f"pdbt:{token}:{w['id']}")]
                  for w in whs]
            kb.append([InlineKeyboardButton("🗂 Все склады сразу",
                                            callback_data=f"pdbt:{token}:all")])
            kb.append([InlineKeyboardButton("❌ Отмена", callback_data=f"no:{token}")])
            await update.message.reply_text("Долги какого склада показать?",
                                            reply_markup=InlineKeyboardMarkup(kb))
            return
    await _debts_report(update, whs)


async def _debts_report(update, whs):
    sections, summary = [], []
    grand_total = 0
    for wh in whs:
        clients = [c for c in db.clients_of(wh["id"]) if c["debt"] != 0]
        if not clients:
            summary.append(f"🏬 «{wh['name']}»: долгов нет ✅")
            continue
        rows, total = [], 0
        for c in sorted(clients, key=lambda r: -r["debt"]):
            if c["debt"] > 0:
                rows.append([c["name"], money(c["debt"]), c["phone"] or ""])
                total += c["debt"]
            else:
                rows.append([c["name"], f"переплата {money(-c['debt'])}", c["phone"] or ""])
        sections.append({
            "title": f"Склад «{wh['name']}»",
            "headers": ["Клиент", "Долг", "Телефон"],
            "rows": rows, "widths": [85, 45, 37], "numbered": True,
            "footer": f"Итого долгов: {money(total)} · должников: "
                      f"{sum(1 for c in clients if c['debt'] > 0)}",
        })
        summary.append(f"🏬 «{wh['name']}»: {money(total)} "
                       f"({sum(1 for c in clients if c['debt'] > 0)} должн.)")
        grand_total += total
    if not sections:
        await update.message.reply_text("\n".join(summary) or "✅ Долгов нет")
        return
    footer = f"ВСЕГО ПО СКЛАДАМ: {money(grand_total)}" if len(sections) > 1 else ""
    if len(whs) > 1:
        summary.append(f"💰 Всего: {money(grand_total)}")
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    pdf = generate_report_pdf("ДОЛГИ КЛИЕНТОВ", f"ОсОО «ВЕТОП» · на {date_str}",
                              sections, footer=footer)
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"долги_{date_str.replace('.', '')}.pdf"),
        caption="\n".join(summary))


async def invoices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Накладные за период с составом: /invoices [Склад] [день|неделя|месяц]."""
    actor = await get_actor(update)
    if actor is None:
        return
    if not await _require_private(update):
        return
    args = list(context.args or [])
    days_back, label = 0, "за сегодня"
    if args and args[-1].lower() in PERIODS:
        days_back, label = PERIODS[args[-1].lower()]
        args = args[:-1]
    wh_name = " ".join(args).strip()
    if wh_name and wh_name.lower() != "all":
        wh = db.warehouse_by_name(wh_name)
        if wh is None or not db.can_view_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(wh_name)}» не найден или нет доступа.", parse_mode="HTML")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
    now = datetime.now(BISHKEK)
    start = (now - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    ops = [op for op in db.operations_since(start.isoformat(timespec="seconds"))
           if op["type"] == "invoice" and op["status"] == "done"]
    sections, summary = [], []
    for wh in whs:
        rows, total_sum, total_paid = [], 0.0, 0.0
        for op in ops:
            if op["warehouse_id"] != wh["id"]:
                continue
            try:
                data = json.loads(op["data"])
            except (ValueError, TypeError):
                data = {}
            client = db.client_get(op["client_id"]) if op["client_id"] else None
            # Короткое имя (без состава в скобках) + неразрывные пробелы,
            # чтобы «— 5 шт» не отрывалось на следующую строку.
            goods = [
                f"{j}. {str(it.get('name') or '').split('(')[0].strip()} "
                f"{it.get('volume')} — {it.get('qty')} шт"
                for j, it in enumerate(data.get("items", []), 1)]
            amount = data.get("total") or 0
            paid = data.get("payment") or 0
            total_sum += amount
            total_paid += paid
            try:
                ts = datetime.fromisoformat(op["ts"]).strftime("%d.%m %H:%M")
            except ValueError:
                ts = op["ts"]
            money_cell = fmt_num(amount)
            if paid:
                money_cell += f" (опл. {fmt_num(paid)})"
            rows.append([f"№{op['id']} · {ts}",
                         client["name"] if client else "—", goods, money_cell])
        if not rows:
            summary.append(f"🧾 «{wh['name']}»: накладных {label} нет")
            continue
        sections.append({
            "title": f"Склад «{wh['name']}»",
            "headers": ["Накладная", "Клиент", "Товары", "Сумма"],
            "rows": rows, "widths": [26, 32, 80, 29], "numbered": True,
            "footer": (f"Накладных: {len(rows)} · на {money(total_sum)}"
                       + (f" · оплачено сразу {money(total_paid)}"
                          if total_paid else "")),
        })
        summary.append(f"🧾 «{wh['name']}»: {len(rows)} накладных "
                       f"на {money(total_sum)}")
    if not sections:
        await update.message.reply_text("\n".join(summary) or "Накладных нет.")
        return
    pdf = generate_report_pdf(
        "НАКЛАДНЫЕ", f"ОсОО «ВЕТОП» · {label} · на {now.strftime('%d.%m.%Y %H:%M')}",
        sections)
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"накладные_{now.strftime('%d%m%Y')}.pdf"),
        caption="\n".join(summary) + "\nОтменить накладную: /undo Номер (админ)")


async def clients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полный справочник клиентов склада (включая нулевые долги) — PDF."""
    actor = await get_actor(update)
    if actor is None:
        return
    if not await _require_private(update):
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if arg and arg.lower() != "all":
        wh = db.warehouse_by_name(arg)
        if wh is None or not db.can_view_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(arg)}» не найден или нет доступа.", parse_mode="HTML")
            return
        if _debts_hidden_from(actor, wh["id"]):
            await update.message.reply_text(HIDDEN_DEBT_MSG)
            return
        whs = [wh]
    else:
        whs = debt_whs_for(actor, db.visible_warehouses(actor))
        if not whs:
            await update.message.reply_text(HIDDEN_DEBT_MSG)
            return
    sections, summary = [], []
    for wh in whs:
        clients = db.clients_of(wh["id"])
        if not clients:
            summary.append(f"🏬 «{wh['name']}»: клиентов пока нет")
            continue
        rows, total_debt, debtors = [], 0.0, 0
        for c in sorted(clients, key=lambda r: r["name"].lower()):
            aliases = db.client_aliases_list(c["id"])
            name = c["name"] + (f" ({', '.join(aliases)})" if aliases else "")
            if c["debt"] > 0:
                debt_cell = money(c["debt"])
                total_debt += c["debt"]
                debtors += 1
            elif c["debt"] < 0:
                debt_cell = f"переплата {money(-c['debt'])}"
            else:
                debt_cell = "—"
            rows.append([name, debt_cell, c["phone"] or ""])
        sections.append({
            "title": f"Склад «{wh['name']}»",
            "headers": ["Клиент", "Долг", "Телефон"],
            "rows": rows, "widths": [85, 45, 37], "numbered": True,
            "footer": f"Клиентов: {len(rows)} · должников: {debtors} · "
                      f"долгов: {money(total_debt)}",
        })
        summary.append(f"🏬 «{wh['name']}»: {len(rows)} клиентов, "
                       f"долгов {money(total_debt)}")
    if not sections:
        await update.message.reply_text("\n".join(summary) or "Клиентов пока нет.")
        return
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    pdf = generate_report_pdf("СПРАВОЧНИК КЛИЕНТОВ",
                              f"ОсОО «ВЕТОП» · на {date_str}", sections)
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"клиенты_{date_str.replace('.', '')}.pdf"),
        caption="\n".join(summary) + "\nКарточка клиента: /client Имя")


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


def report_data(warehouses, days_back: int, last_hours: int = None,
                start_dt=None, end_dt=None):
    """Цифры отчёта по складам за период (для текста и PDF).

    last_hours — скользящее окно «последние N часов»;
    start_dt/end_dt — явные границы (для «хвоста» вчерашнего вечера
    в календарной сводке дня, решение владельца 08.08.2026)."""
    if start_dt is not None:
        start = start_dt
    elif last_hours:
        start = datetime.now(BISHKEK) - timedelta(hours=last_hours)
    else:
        start = (datetime.now(BISHKEK) - timedelta(days=days_back)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    ops = db.operations_since(
        start.isoformat(timespec="seconds"),
        end_dt.isoformat(timespec="seconds") if end_dt else None)
    out = []
    for wh in warehouses:
        inv_data, pay_sum, transfers = [], 0.0, 0
        ret_sum, ret_count = 0.0, 0
        wo_sum, wo_count = 0.0, 0
        hand_sum, hand_by = 0.0, {}
        for op in ops:
            try:
                data = json.loads(op["data"])
            except (ValueError, TypeError):
                continue
            if op["type"] == "invoice" and op["warehouse_id"] == wh["id"]:
                inv_data.append(data)
            elif op["type"] == "payment" and op["warehouse_id"] == wh["id"]:
                pay_sum += data.get("amount", 0)
            elif op["type"] == "return" and op["warehouse_id"] == wh["id"]:
                ret_sum += data.get("total", 0)
                ret_count += 1
            elif op["type"] == "writeoff" and op["warehouse_id"] == wh["id"]:
                wo_sum += data.get("total", 0)
                wo_count += 1
            elif op["type"] == "handover" and op["warehouse_id"] == wh["id"]:
                amt = data.get("amount", 0)
                hand_sum += amt
                hand_by[op["user_id"]] = hand_by.get(op["user_id"], 0) + amt
            elif op["type"] == "transfer" and wh["id"] in db.operation_warehouses(op):
                transfers += 1
        sales = sum(d.get("total", 0) for d in inv_data)
        inv_payments = sum(d.get("payment", 0) for d in inv_data)
        top = {}
        for d in inv_data:
            for it in d.get("items", []):
                key = f"{it.get('name', '')} {it.get('volume', '')}".strip()
                top[key] = top.get(key, 0) + (it.get("qty") or 0)
        hand_list = []
        for uid, amt in hand_by.items():
            u = db.get_user(uid)
            hand_list.append((u["name"] if u else str(uid), amt))
        out.append({
            "wh": wh, "n_inv": len(inv_data), "sales": sales,
            "money": inv_payments + pay_sum, "debt_added": sales - inv_payments,
            "ret_count": ret_count, "ret_sum": ret_sum, "transfers": transfers,
            "wo_count": wo_count, "wo_sum": wo_sum,
            "hand_sum": hand_sum, "hand_list": hand_list,
            "top": sorted(top.items(), key=lambda x: -x[1]),
            "empty": (not inv_data and not pay_sum and not transfers
                      and not ret_count and not wo_count and not hand_sum),
        })
    return out


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
    # В чате склада — только склад этого чата (период из аргументов работает).
    whs_group, in_group = await _group_only_feed_whs(update, actor)
    if in_group:
        if not whs_group:
            return
        whs = whs_group
    elif wh_words:
        wh = db.warehouse_by_name(" ".join(wh_words))
        if wh is None or not db.can_view_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(' '.join(wh_words))}» не найден или нет доступа.\n"
                "Использование: /report [Склад] [день|неделя|месяц]",
                parse_mode="HTML")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
    pdf, caption = build_report_pdf(whs, days_back, label)
    if pdf is None:
        await update.message.reply_text(caption)
        return
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"отчёт_{date_str.replace('.', '')}.pdf"),
        caption=caption)


def build_report_pdf(whs, days_back: int, label: str, last_hours: int = None):
    """PDF-отчёт по складам. Возвращает (pdf, подпись) или (None, текст-пусто)."""
    data = report_data(whs, days_back, last_hours=last_hours)
    if all(d["empty"] for d in data):
        return None, f"📊 Операций {label} не было."
    sections, summary = [], []
    grand_sales = grand_money = 0
    for d in data:
        wh = d["wh"]
        if d["empty"]:
            summary.append(f"📊 «{wh['name']}»: операций не было")
            continue
        rows = [["Накладных", f"{d['n_inv']} шт на {money(d['sales'])}"],
                ["Принято денег", money(d["money"])]]
        if d["debt_added"] > 0:
            rows.append(["Выдано в долг", money(d["debt_added"])])
        if d["ret_count"]:
            rows.append(["Возвраты", f"{d['ret_count']} на {money(d['ret_sum'])}"])
        if d["wo_count"]:
            rows.append(["Списания", f"{d['wo_count']} на {money(d['wo_sum'])}"])
        if d["transfers"]:
            rows.append(["Приходы/перемещения", str(d["transfers"])])
        if d["hand_sum"]:
            who = ", ".join(f"{money(a)} ({n})" for n, a in d["hand_list"])
            rows.append(["Сдано выручки", who])
        sections.append({"title": f"Склад «{wh['name']}»",
                         "headers": ["Показатель", "Значение"],
                         "rows": rows, "widths": [80, 87]})
        if d["top"]:
            # Отчёт по одному складу показывает ВСЕ проданные препараты
            # (просьба владельца 01.08.2026), общий по нескольким — топ-10,
            # иначе PDF разрастается на все склады сразу.
            one_wh = len(data) == 1
            shown = d["top"] if one_wh else d["top"][:10]
            note = f"Позиций продано: {len(d['top'])}"
            if not one_wh and len(d["top"]) > len(shown):
                note += f" · в списке первые {len(shown)}"
            sections.append({"title": ("Проданные товары" if one_wh
                                       else "Топ товаров") + f" — «{wh['name']}»",
                             "headers": ["Товар", "Продано"],
                             "rows": [[n, f"{q} шт"] for n, q in shown],
                             "widths": [130, 37],
                             "numbered": True, "footer": note})
        line = (f"📊 «{wh['name']}»: продажи {money(d['sales'])}, "
                f"деньги {money(d['money'])}")
        if d["hand_sum"]:
            line += "\n💵 Сдано выручки: " + ", ".join(
                f"{money(a)} ({n})" for n, a in d["hand_list"])
        summary.append(line)
        if not is_training_wh(wh):
            grand_sales += d["sales"]
            grand_money += d["money"]
    footer = ""
    has_training = any(is_training_wh(w) for w in whs)
    if len(whs) > 1:
        mark = " (без учебного склада)" if has_training else ""
        footer = f"ИТОГО{mark}: продажи {money(grand_sales)} · деньги {money(grand_money)}"
        summary.append(f"💰 Итого{mark}: продажи {money(grand_sales)}, "
                       f"деньги {money(grand_money)}")
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    pdf = generate_report_pdf("ОТЧЁТ ПО СКЛАДАМ",
                              f"ОсОО «ВЕТОП» · {label} · на {date_str}",
                              sections, footer=footer)
    return pdf, "\n".join(summary)[:1000]


async def send_evening_summaries(bot):
    """Вечерняя сводка дня в каждый чат-ленту (если были операции) — PDF."""
    by_chat = {}
    for wh in db.all_warehouses():
        if wh["feed_chat_id"]:
            by_chat.setdefault(wh["feed_chat_id"], []).append(wh)
    now = datetime.now(BISHKEK)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_cut = midnight - timedelta(hours=24 - SUMMARY_HOUR)
    for chat_id, whs in by_chat.items():
        # КАЛЕНДАРНЫЙ день с полуночи (решение владельца 08.08.2026:
        # скользящие сутки показывали вчерашние вечерние деньги как
        # сегодняшние). Операции «вчера после сводки» (20:00–полночь)
        # не теряются — идут отдельной строкой в подписи.
        pdf, caption = build_report_pdf(whs, 0, "за день")
        tail = report_data(whs, 0, start_dt=yesterday_cut, end_dt=midnight)
        tail_lines = []
        for d in tail:
            if d.get("empty"):
                continue
            bits = []
            if d["sales"]:
                bits.append(f"продажи {money(d['sales'])}")
            if d["money"]:
                bits.append(f"деньги {money(d['money'])}")
            if d.get("hand_sum"):
                bits.append(f"сдано выручки {money(d['hand_sum'])}")
            if bits:
                tail_lines.append(
                    f"🌙 «{d['wh']['name']}» вчера после сводки: "
                    + ", ".join(bits))
        if pdf is None:
            if tail_lines:
                # Сегодня пусто, но вчерашний вечер не должен пропасть
                try:
                    await bot.send_message(
                        chat_id, "🌆 Итоги дня: операций сегодня не было.\n"
                        + "\n".join(tail_lines))
                except Exception as e:
                    log.warning("Не удалось отправить сводку в %s: %s",
                                chat_id, e)
            continue  # день без операций — не шумим
        if tail_lines:
            caption += "\n" + "\n".join(tail_lines)
        wh_ids = {w["id"] for w in whs}
        cash_lines = []
        for u in db.list_users():
            own = db.warehouse_of(u["id"])
            if own is None or own["id"] not in wh_ids or u["role"] == "admin":
                continue
            cash = db.cash_on_hand(u["id"])
            if cash > 0:
                cash_lines.append(f"💰 В кассе у {u['name']}: {money(cash)}")
        caption = "🌆 Итоги дня\n" + caption
        if cash_lines:
            caption += "\n" + "\n".join(cash_lines)
        date_str = datetime.now(BISHKEK).strftime("%d%m%Y")
        try:
            await bot.send_document(
                chat_id,
                document=InputFile(pdf, filename=f"итоги_дня_{date_str}.pdf"),
                caption=caption[:1000])
        except Exception as e:
            log.warning("Не удалось отправить сводку в %s: %s", chat_id, e)


def build_draft_summary(last_hours: int = None) -> str | None:
    """Сводка по черновикам; None, если черновиков не было.

    last_hours — скользящее окно для вечерней рассылки (окно «с полуночи»
    навсегда теряло черновики, выписанные после часа отправки)."""
    now = datetime.now(BISHKEK)
    if last_hours:
        since = now - timedelta(hours=last_hours)
        title = f"📝 <b>Черновики за сутки (к {now.strftime('%H:%M %d.%m.%Y')})</b>"
    else:
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = f"📝 <b>Черновики за {since.strftime('%d.%m.%Y')}</b>"
    rows = db.drafts_since(since.isoformat(timespec="seconds"))
    if not rows:
        return None
    lines = [title, ""]
    total_n, total_sum = 0, 0.0
    for r in rows:
        lines.append(f"• {esc(r['name'])}: {r['n']} шт. — <b>{money(r['total'])}</b>")
        total_n += r["n"]
        total_sum += r["total"]
    lines += ["", f"Итого: {total_n} шт. на <b>{money(total_sum)}</b>"]
    return "\n".join(lines)


async def draft_summary_loop(app):
    """Каждый вечер в DRAFT_SUMMARY_HOUR — сводка по черновикам админу в личку."""
    while True:
        now = datetime.now(BISHKEK)
        target = now.replace(hour=DRAFT_SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            if not db.claim_daily_job(
                    f"drafts:{datetime.now(BISHKEK).date().isoformat()}"):
                continue
            text = build_draft_summary(last_hours=24)
            if text:
                await app.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
        except Exception:
            log.exception("Ошибка сводки по черновикам")


async def pricelog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    rows = db.price_history_recent(15)
    if not rows:
        await update.message.reply_text(
            "🏷 Цены прайса ещё не менялись.\n"
            "Изменить: «новая цена Альтопен 100мл 95»")
        return
    lines = ["🏷 <b>Последние изменения прайса</b>", ""]
    for r in rows:
        try:
            d = datetime.fromisoformat(r["ts"]).strftime("%d.%m.%Y")
        except ValueError:
            d = r["ts"]
        lines.append(f"{d} — {esc(r['name'])} {esc(r['volume'])}: "
                     f"{fmt_num(r['old_price'])} → <b>{fmt_num(r['new_price'])} сом</b> "
                     f"({esc(r['user_name'])})")
    await send_long(update.message, "\n".join(lines))


async def drafts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    text = build_draft_summary()
    await update.message.reply_text(
        text or "📝 Сегодня черновиков ещё не было.", parse_mode="HTML")


async def send_cash_alert(bot):
    """Ежедневно за 10 минут до вечерней сводки — админу в личку список
    ВСЕХ несданных касс, любая сумма (решение владельца 09.08.2026:
    «любая сумма, даже пять сом, каждый день, в 19:50»)."""
    lines = []
    for u in db.list_users():
        if u["role"] == "admin":
            continue
        cash = db.cash_on_hand(u["id"])
        if cash <= 0:
            continue
        last = db.last_handover_ts(u["id"])
        since = "сдач ещё не было"
        if last:
            try:
                days = (datetime.now(BISHKEK)
                        - datetime.fromisoformat(last)).days
                since = ("сдавал сегодня" if days == 0
                         else f"последняя сдача {days} дн. назад")
            except ValueError:
                since = ""
        lines.append(f"👤 {esc(u['name'])}: <b>{money(cash)}</b>"
                     + (f" — {since}" if since else ""))
    if not lines:
        return  # все кассы по нулям — не шумим
    text = ("💰 <b>Наличные на руках у сотрудников</b>\n"
            + "\n".join(lines)
            + "\n\nСдача выручки: сотрудник пишет «сдал сумма», "
              "вы подтверждаете кнопкой. Детали: /cash")
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        log.warning("Не удалось отправить напоминание о кассах: %s", e)


async def cash_alert_loop(app):
    while True:
        now = datetime.now(BISHKEK)
        target = (now.replace(hour=SUMMARY_HOUR, minute=0, second=0,
                              microsecond=0) - timedelta(minutes=10))
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            if not db.claim_daily_job(
                    f"cash_alert:{datetime.now(BISHKEK).date().isoformat()}"):
                continue
            await send_cash_alert(app.bot)
        except Exception:
            log.exception("Ошибка напоминания о кассах")


async def evening_summary_loop(app):
    while True:
        now = datetime.now(BISHKEK)
        target = now.replace(hour=SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            if not db.claim_daily_job(
                    f"evening:{datetime.now(BISHKEK).date().isoformat()}"):
                continue
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
        elif op["type"] == "return":
            plus = 0
            minus = data.get("total", 0)
            doc = f"Возврат товара №{op['id']}"
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
    if c is not None and _debts_hidden_from(actor, wh["id"]):
        # Карточка и акт сверки показывают долг — для скрытого склада
        # сотруднику не отдаём (/hidedebts).
        await update.message.reply_text(HIDDEN_DEBT_MSG)
        return None, None
    if c is None:
        hints = []
        for w in debt_whs_for(actor, db.visible_warehouses(actor)):
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
    aliases = db.client_aliases_list(c["id"])
    if aliases:
        lines.append(f"🏷 Также известен как: {esc(', '.join(aliases))}")
    if c["phone"]:
        lines.append(f"📞 {esc(c['phone'])}")
    else:
        lines.append(f"📞 нет номера — сохранить: «телефон {esc(c['name'])}: 0700...»")
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
    specials = db.client_prices_map(c["id"])
    if specials:
        lines.append("")
        lines.append("💲 <b>Спеццены:</b>")
        for pid, price in specials.items():
            p_row = prices.BY_ID.get(pid)
            if p_row:
                lines.append(f"• {esc(p_row['name'])} {esc(p_row['volume'])}: "
                             f"{fmt_num(price)} сом (прайс {fmt_num(p_row['price'])})")
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
    pdf = generate_act_pdf(c["name"], wh["name"], rows, start_debt, c["debt"], period,
                           client_phone=c["phone"])
    filename = f"акт_сверки_{safe_filename(c['name'])}_{datetime.now(BISHKEK).strftime('%d%m%Y')}.pdf"
    await update.message.reply_document(
        document=InputFile(pdf, filename=filename),
        caption=f"📄 Акт сверки: {c['name']} — долг {fmt_num(c['debt'])} сом")


def _cutoff_iso(days: int) -> str:
    return (datetime.now(BISHKEK) - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def sales_by_warehouse(days: int) -> dict:
    """Продажи за период по журналу: {wh_id: {product_id: продано_шт}}."""
    sold = {}
    for op in db.operations_since(_cutoff_iso(days)):
        if op["type"] != "invoice":
            continue
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        for wh_id, pid, delta in data.get("stock_deltas", []):
            if delta < 0:
                sold.setdefault(wh_id, {})[pid] = sold.get(wh_id, {}).get(pid, 0) - delta
    return sold


def arrivals_by_warehouse(days: int) -> dict:
    """Недавние поступления (приход/перемещение/инвентаризация +): {wh_id: {pid}}."""
    arrived = {}
    for op in db.operations_since(_cutoff_iso(days)):
        if op["type"] not in ("transfer", "inventory"):
            continue
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        for wh_id, pid, delta in data.get("stock_deltas", []):
            if delta > 0:
                arrived.setdefault(wh_id, set()).add(pid)
    return arrived


def build_deadstock(warehouses, days: int) -> str:
    """Товар с остатком, который не продавался days+ дней."""
    sold_all = sales_by_warehouse(days)
    recent_arrivals = arrivals_by_warehouse(21)  # свежий приход — рано судить
    lines = [f"🧊 <b>Мёртвый товар — без продаж {days}+ дней</b>"]
    grand_frozen = 0.0
    found = False
    for wh in warehouses:
        smap = db.stock_map(wh["id"])
        sold = sold_all.get(wh["id"], {})
        arrived = recent_arrivals.get(wh["id"], set())
        rows = []
        for p in prices.PRICE_LIST_DATA:
            qty = smap.get(p["id"], 0)
            if qty <= 0 or sold.get(p["id"], 0) > 0 or p["id"] in arrived:
                continue
            value = qty * p["price"]
            sellers = [w for w in db.all_warehouses()
                       if w["id"] != wh["id"] and sold_all.get(w["id"], {}).get(p["id"], 0) > 0]
            rows.append((value, p, qty, sellers))
        if not rows:
            continue
        found = True
        lines.append("")
        lines.append(f"🏬 <b>Склад «{esc(wh['name'])}»</b>")
        subtotal = 0.0
        for value, p, qty, sellers in sorted(rows, key=lambda r: -r[0]):
            hint = ""
            if sellers:
                hint = " · продаётся в " + ", ".join(f"«{esc(w['name'])}»" for w in sellers[:2])
            lines.append(f"• {esc(p['name'])} {esc(p['volume'])} — {qty} шт "
                         f"(<b>{money(value)}</b>){hint}")
            subtotal += value
        lines.append(f"💰 Заморожено: <b>{money(subtotal)}</b>")
        grand_frozen += subtotal
    if not found:
        return ""
    if len(warehouses) > 1:
        lines.append("")
        lines.append(f"🧊 Всего заморожено: <b>{money(grand_frozen)}</b>")
    lines.append("")
    lines.append("💡 Залежавшееся можно перекинуть туда, где оно продаётся: "
                 "«с Манаса на Каракол: ...»")
    return "\n".join(lines)


FORECAST_WINDOW = 14   # скорость продаж считаем за 2 недели
FORECAST_HORIZON = 14  # показываем то, чего хватит менее чем на 2 недели


def build_forecast(warehouses) -> str:
    """Прогноз: чего хватит менее чем на FORECAST_HORIZON дней."""
    sold_all = sales_by_warehouse(FORECAST_WINDOW)
    lines = [f"⏳ <b>Скоро закончится (при продажах последних {FORECAST_WINDOW} дней)</b>"]
    found = False
    for wh in warehouses:
        sold = sold_all.get(wh["id"], {})
        if not sold:
            continue
        smap = db.stock_map(wh["id"])
        rows = []
        for pid, sold_qty in sold.items():
            qty = smap.get(pid, 0)
            p = prices.BY_ID.get(pid)
            if p is None or qty < 0:
                continue
            days_left = qty / (sold_qty / FORECAST_WINDOW)
            if days_left <= FORECAST_HORIZON:
                rows.append((days_left, p, qty, sold_qty))
        if not rows:
            continue
        found = True
        lines.append("")
        lines.append(f"🏬 <b>Склад «{esc(wh['name'])}»</b>")
        for days_left, p, qty, sold_qty in sorted(rows, key=lambda r: r[0]):
            when = "уже закончился!" if qty <= 0 else f"хватит на ≈{max(int(days_left), 0)} дн."
            lines.append(f"• {esc(p['name'])} {esc(p['volume'])} — {qty} шт, {when} "
                         f"(продано {sold_qty} шт за {FORECAST_WINDOW} дн.)")
    if not found:
        return ""
    return "\n".join(lines)


async def deadstock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    days = DEADSTOCK_DAYS
    wh_words = []
    for word in (context.args or []):
        if word.isdigit():
            days = max(7, int(word))
        else:
            wh_words.append(word)
    if wh_words:
        wh = db.warehouse_by_name(" ".join(wh_words))
        if wh is None or not db.can_use_warehouse(actor, wh["id"]):
            await update.message.reply_text("Склад не найден или нет доступа.")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
    text = build_deadstock(whs, days)
    if not text:
        await update.message.reply_text(
            f"🎉 Мёртвого товара нет — всё с остатком продавалось за последние {days} дней "
            f"(свежие поступления не считаются).")
        return
    await send_long(update.message, text)


async def forecast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if arg:
        wh = db.warehouse_by_name(arg)
        if wh is None or not db.can_use_warehouse(actor, wh["id"]):
            await update.message.reply_text("Склад не найден или нет доступа.")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
    text = build_forecast(whs)
    if not text:
        await update.message.reply_text(
            f"✅ Ничего не заканчивается: всех продаваемых товаров хватит более чем на "
            f"{FORECAST_HORIZON} дней.")
        return
    await send_long(update.message, text)


async def monthly_deadstock_loop(app):
    """Первого числа каждого месяца в 09:00 — отчёт админу."""
    while True:
        now = datetime.now(BISHKEK)
        if now.month == 12:
            target = now.replace(year=now.year + 1, month=1, day=1,
                                 hour=9, minute=0, second=0, microsecond=0)
        else:
            target = now.replace(month=now.month + 1, day=1,
                                 hour=9, minute=0, second=0, microsecond=0)
        first_this = now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
        if first_this > now:
            target = first_this
        await asyncio.sleep((target - now).total_seconds())
        stamp = datetime.now(BISHKEK).strftime("%Y-%m")
        try:
            if db.claim_daily_job(f"deadstock:{stamp}"):
                text = build_deadstock(db.all_warehouses(), DEADSTOCK_DAYS)
                if text:
                    await send_long_bot(app.bot, ADMIN_ID, text)
        except Exception:
            log.exception("Ошибка отчёта о мёртвом товаре")
        try:
            # Итог прибыли за прошедший календарный месяц (решение
            # владельца 03.08.2026) — без напоминаний, 1-го числа.
            if db.claim_daily_job(f"margin_month:{stamp}"):
                now2 = datetime.now(BISHKEK)
                first_this = now2.replace(day=1, hour=0, minute=0,
                                          second=0, microsecond=0)
                prev_last = first_this - timedelta(days=1)
                first_prev = prev_last.replace(day=1)
                pdf, caption, filename = build_margin_report(
                    first_prev.isoformat(timespec="seconds"),
                    first_this.isoformat(timespec="seconds"),
                    f"за {prev_last.strftime('%m.%Y')}")
                if pdf is not None:
                    await app.bot.send_document(
                        ADMIN_ID, document=InputFile(pdf, filename=filename),
                        caption="🗓 Итог месяца\n" + caption)
        except Exception:
            log.exception("Ошибка месячного отчёта прибыли")


def overdue_rows(warehouses, min_days: int):
    """Должники min_days+ дней: [(склад, [(дней_или_None, клиент, платил_ли)])]."""
    now = datetime.now(BISHKEK)
    out = []
    for wh in warehouses:
        rows = []
        for c, ref_ts, has_paid in db.debtors_with_age(wh["id"]):
            if ref_ts is None:
                days = None  # операций нет вовсе — долг занесён вручную давно
            else:
                try:
                    ts = datetime.fromisoformat(ref_ts)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=BISHKEK)
                    days = (now - ts).days
                except ValueError:
                    days = None
            if days is not None and days < min_days:
                continue
            rows.append((days, c, has_paid))
        if rows:
            rows.sort(key=lambda r: -(r[0] if r[0] is not None else 10**6))
            out.append((wh, rows))
    return out


def growing_debt_rows(warehouses, days: int):
    """Список риска: клиенты, чей долг за последние days дней ВЫРОС — набрали
    товара в долг больше, чем погасили (берут новое, не гася старое).
    Считается по дельтам журнала операций; стартовые загрузки справочника
    ростом не считаются (мимо журнала). Учебные склады пропускаются.
    Возвращает [(wh, [(рост, клиент, набрал, погасил), ...])]."""
    since = (datetime.now(BISHKEK)
             - timedelta(days=days)).isoformat(timespec="seconds")
    growth = db.debt_growth(since)
    out = []
    for wh in warehouses:
        if is_training_wh(wh):
            continue
        rows = []
        for c in db.clients_of(wh["id"]):
            took, paid = growth.get(c["id"], (0.0, 0.0))
            delta = took - paid
            # Рост меньше сома — шум округления, клиент без долга — не риск
            if delta < 1 or c["debt"] <= 0:
                continue
            rows.append((delta, c, took, paid))
        if rows:
            rows.sort(key=lambda r: -r[0])
            out.append((wh, rows))
    return out


def overdue_report(warehouses, min_days: int):
    """PDF «Давно не платили» + раздел риска «Растущие долги».
    Возвращает (pdf, found, total, grew_n, grew_total) или None, если ни
    зависших, ни растущих долгов нет."""
    data = overdue_rows(warehouses, min_days)
    grow = growing_debt_rows(warehouses, DEBT_GROWTH_DAYS)
    if not data and not grow:
        return None
    sections, found, total = [], 0, 0.0
    for wh, rows in data:
        sec_rows, sec_total = [], 0.0
        for days, c, has_paid in rows:
            age = f"{days} дн." if days is not None else "давно"
            if not has_paid:
                age += " (ни одной оплаты)"
            sec_rows.append([c["name"], money(c["debt"]), age, c["phone"] or ""])
            sec_total += c["debt"]
        found += len(sec_rows)
        total += sec_total
        sections.append({"title": f"Склад «{wh['name']}»",
                         "headers": ["Клиент", "Долг", "Без оплат", "Телефон"],
                         "rows": sec_rows, "widths": [70, 38, 34, 25],
                         "numbered": True,
                         "footer": f"Итого: {money(sec_total)}"})
    grew_n, grew_total = 0, 0.0
    for wh, rows in grow:
        sec_rows, sec_total = [], 0.0
        for delta, c, took, paid in rows:
            sec_rows.append([c["name"], money(c["debt"]), fmt_num(took),
                             fmt_num(paid), f"+{fmt_num(delta)}"])
            sec_total += delta
        grew_n += len(sec_rows)
        grew_total += sec_total
        sections.append({"title": f"РАСТУЩИЕ ДОЛГИ за {DEBT_GROWTH_DAYS} дн. — "
                                  f"склад «{wh['name']}»",
                         "headers": ["Клиент", "Долг сейчас", "Набрал",
                                     "Погасил", "Рост"],
                         "rows": sec_rows, "widths": [51, 32, 28, 28, 28],
                         "numbered": True,
                         "footer": f"Итого рост: +{money(sec_total)}"})
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    if data:
        title = "ДАВНО НЕ ПЛАТИЛИ"
        sub = f"ОсОО «ВЕТОП» · {min_days}+ дней без оплат · на {date_str}"
    else:  # только растущие долги — заголовок «давно не платили» сбивал бы
        title = "РАСТУЩИЕ ДОЛГИ"
        sub = f"ОсОО «ВЕТОП» · рост за {DEBT_GROWTH_DAYS} дн. · на {date_str}"
    pdf = generate_report_pdf(
        title, sub, sections,
        footer=f"ИТОГО ЗАВИСШИХ ДОЛГОВ: {money(total)}" if len(data) > 1 else "")
    return pdf, found, total, grew_n, grew_total


def overdue_filename() -> str:
    date_str = datetime.now(BISHKEK).strftime("%d%m%Y")
    return f"старые_долги_{date_str}.pdf"


async def olddebts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    # Долги и телефоны — только в личке (см. show_debts).
    if not await _require_private(update):
        return
    min_days = DEBT_ALERT_DAYS
    if context.args:
        try:
            min_days = max(1, int(context.args[0]))
        except ValueError:
            await update.message.reply_text(
                "Использование: /olddebts [дней]\nПример: /olddebts 45")
            return
    report = overdue_report(
        debt_whs_for(actor, db.visible_warehouses(actor)), min_days)
    if report is None:
        await update.message.reply_text(
            f"✅ Нет клиентов без оплат дольше {min_days} дней, "
            f"растущих долгов тоже нет.")
        return
    pdf, found, total, grew_n, grew_total = report
    await update.message.reply_document(
        document=InputFile(pdf, filename=overdue_filename()),
        caption=_overdue_caption(min_days, found, total, grew_n, grew_total))


# ---------- /sales — история продаж препарата (как поиск в 1С) ----------

def _pnorm(s: str) -> str:
    """Нормализация названий для сравнения: нижний регистр, без пробелов,
    дефисов и точек («50 мл» == «50мл»), ё=е."""
    return re.sub(r"[\s\-–—.]+", "", str(s).lower()).replace("ё", "е")


def _base_name(pr) -> str:
    """Название препарата без расшифровки в скобках."""
    return pr["name"].split("(")[0].strip()


def _sales_pids(q: str):
    """Точное совпадение запроса с препаратом прайса: «дексатоп» → все
    фасовки, «дексатоп 50мл» → одна. Возвращает (подпись, [pids]) или None."""
    qn = _pnorm(q)
    if not qn:
        return None
    sku = [pr for pr in prices.PRICE_LIST_DATA
           if _pnorm(_base_name(pr) + pr["volume"]) == qn]
    if sku:
        return f"{_base_name(sku[0])} {sku[0]['volume']}", [p["id"] for p in sku]
    same = [pr for pr in prices.PRICE_LIST_DATA if _pnorm(_base_name(pr)) == qn]
    if same:
        return _base_name(same[0]), [p["id"] for p in same]
    return None


def _parse_sales_query(text: str):
    """«дексатоп 50мл мустанг» → (подпись, pids, клиент|None, []).

    Длиннейший префикс слов — препарат (можно с фасовкой), остаток — клиент;
    клиента можно отделить и запятой. Точного совпадения нет — пробуем с
    опечатками. Совсем не поняли — (None, None, None, кандидаты-подсказки)."""
    if "," in text:
        prod_part, cli_part = [x.strip() for x in text.split(",", 1)]
    else:
        prod_part, cli_part = text.strip(), ""
    words = prod_part.split()
    for cut in range(len(words), 0, -1):
        got = _sales_pids(" ".join(words[:cut]))
        if not got:
            continue
        label, pids = got
        rest = words[cut:]
        # Хвост может уточнять фасовку: «дексатоп 50» и «дексатоп 50 мл»
        if rest and len(pids) > 1:
            for vcut in (min(2, len(rest)), 1):
                vq = _pnorm(" ".join(rest[:vcut]))
                sub = [pid for pid in pids
                       if vq and vq in _pnorm(prices.BY_ID[pid]["volume"])]
                if sub and len(sub) < len(pids):
                    pids, rest = sub, rest[vcut:]
                    if len(sub) == 1:
                        label += f" {prices.BY_ID[sub[0]]['volume']}"
                    break
        cli = f"{' '.join(rest)} {cli_part}".strip()
        return label, pids, cli or None, []
    # Опечатки: тот же перебор префиксов, но через difflib (как у /cert)
    cands = []
    for cut in range(len(words), 0, -1):
        name, cands = _cert_product_match(" ".join(words[:cut]))
        if not name:
            continue
        pids = [pr["id"] for pr in prices.PRICE_LIST_DATA
                if _base_name(pr) == name]
        if not pids:  # имя есть только у сертификатов — не товар прайса
            continue
        cli = f"{' '.join(words[cut:])} {cli_part}".strip()
        return name, pids, cli or None, []
    return None, None, None, cands


def sales_history_report(whs, label, pids, cli_ids=None, cli_label=None):
    """PDF истории продаж препарата по журналу: секция на фасовку, новые
    сверху; черновики (не учёт) — отдельной секцией. None — данных нет.
    Возвращает (pdf, подпись-итог)."""
    wh_names = {w["id"]: w["name"] for w in whs}
    per_pid = {}
    sold_qty = sold_sum = ret_qty = ret_sum = 0.0
    ops, newest, oldest = set(), None, None
    for op, it in db.product_sales(pids, set(wh_names), cli_ids):
        qty, price = float(it.get("qty") or 0), float(it.get("price") or 0)
        try:
            dstr = datetime.fromisoformat(op["ts"]).strftime("%d.%m.%Y")
        except ValueError:
            dstr = op["ts"][:10]
        if newest is None:
            newest = dstr
        oldest = dstr
        ret = op["type"] == "return"
        if ret:
            ret_qty += qty
            ret_sum += qty * price
        else:
            sold_qty += qty
            sold_sum += qty * price
        ops.add(op["id"])
        cli = op["client_name"] or "—"
        if len(whs) > 1:
            cli += f" ({wh_names.get(op['warehouse_id'], '?')})"
        sign = "-" if ret else ""
        per_pid.setdefault(it.get("product_id"), []).append(
            [dstr, cli, sign + fmt_num(qty),
             fmt_num(price), sign + fmt_num(qty * price),
             f"№{op['id']}" + (" возврат" if ret else "")])
    sections = []
    for pid in pids:
        rows = per_pid.get(pid)
        if not rows:
            continue
        pr = prices.BY_ID.get(pid)
        title = f"{_base_name(pr)} {pr['volume']}" if pr else f"Товар №{pid}"
        note = ""
        if len(rows) > 80:
            note = f" (показаны последние 80 из {len(rows)})"
            rows = rows[:80]
        sections.append({"title": title + note,
                         "headers": ["Дата", "Клиент", "Кол-во", "Цена",
                                     "Сумма", "№ оп."],
                         "rows": rows, "widths": [24, 55, 20, 22, 26, 20]})
    # Черновики переходного периода — отдельно, в учёт не входят
    want = [(_pnorm(pr["name"]), _pnorm(_base_name(pr)), _pnorm(pr["volume"]))
            for pr in (prices.BY_ID[pid] for pid in pids if pid in prices.BY_ID)]
    draft_rows, draft_qty, draft_sum = [], 0.0, 0.0
    for r in db.draft_items_all():
        if cli_label:
            a, b = _pnorm(r["client"]), _pnorm(cli_label)
            if a != b and b not in a and a not in b:
                continue
        try:
            its = json.loads(r["items"])
        except (ValueError, TypeError):
            continue
        for it in its:
            n, v = _pnorm(it.get("name") or ""), _pnorm(it.get("volume") or "")
            if not any(v == vn and n in (nn, bn) for nn, bn, vn in want):
                continue
            qty, s = float(it.get("qty") or 0), float(it.get("sum") or 0)
            try:
                dstr = datetime.fromisoformat(r["ts"]).strftime("%d.%m.%Y")
            except ValueError:
                dstr = r["ts"][:10]
            draft_qty += qty
            draft_sum += s
            if len(draft_rows) < 60:
                draft_rows.append([dstr, r["client"], fmt_num(qty),
                                   fmt_num(s / qty if qty else 0), fmt_num(s)])
    if draft_rows:
        sections.append({"title": "ЧЕРНОВИКИ (не учёт)",
                         "headers": ["Дата", "Клиент", "Кол-во", "Цена", "Сумма"],
                         "rows": draft_rows, "widths": [26, 61, 22, 26, 32]})
    if not sections:
        return None
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    sub = f"ОсОО «ВЕТОП» · {label}"
    if cli_label:
        sub += f" · клиент: {cli_label}"
    pdf = generate_report_pdf(
        "ИСТОРИЯ ПРОДАЖ", sub + f" · на {date_str}", sections,
        footer=f"ПРОДАНО: {fmt_num(sold_qty)} шт на {money(sold_sum)}"
               + (f" · возвраты: {fmt_num(ret_qty)} шт" if ret_qty else ""))
    cap = f"🔎 {label}"
    if cli_label:
        cap += f" → {cli_label}"
    if ops:
        cap += (f": продано {fmt_num(sold_qty)} шт на {money(sold_sum)} "
                f"({len(ops)} опер., {oldest} — {newest})")
    else:
        cap += ": реальных продаж нет"
    if ret_qty:
        cap += f"\n↩️ Возвраты: {fmt_num(ret_qty)} шт на {money(ret_sum)}"
    if draft_rows:
        cap += f"\n📝 Черновики (не учёт): {fmt_num(draft_qty)} шт на {money(draft_sum)}"
    return pdf, cap


async def sales_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    # Продажи и клиенты по всем складам сотрудника — только личка
    if not await _require_private(update):
        return
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "🔎 История продаж препарата (все накладные по нему).\n\n"
            "Примеры:\n"
            "/sales Дексатоп — все фасовки\n"
            "/sales Дексатоп 50мл — одна фасовка\n"
            "/sales Дексатоп 50мл Мустанг — только этому клиенту")
        return
    label, pids, cli_q, cands = _parse_sales_query(query)
    if not pids:
        msg = "Не понял, какой это препарат."
        if cands:
            msg += " Возможно: " + ", ".join(cands)
        msg += "\nПример: /sales Дексатоп 50мл"
        await update.message.reply_text(msg)
        return
    whs = db.visible_warehouses(actor)
    cli_ids = cli_label = None
    if cli_q:
        matches = {}
        for wh in whs:
            c = db.client_exact(wh["id"], cli_q)
            if c:
                matches[c["id"]] = c
        if not matches:
            for wh in whs:
                for c in db.fuzzy_clients(wh["id"], cli_q):
                    matches[c["id"]] = c
        names = {c["name"] for c in matches.values()}
        if not matches:
            await update.message.reply_text(
                f"Клиент «{cli_q}» не найден. Продажи по всем клиентам: "
                f"/sales {label}")
            return
        if len(names) > 1:
            await update.message.reply_text(
                "Уточните клиента, похожих несколько: "
                + ", ".join(sorted(names)[:5]))
            return
        cli_ids, cli_label = set(matches), next(iter(names))
    report = sales_history_report(whs, label, pids, cli_ids, cli_label)
    if report is None:
        await update.message.reply_text(
            f"По препарату «{label}»"
            + (f" клиенту «{cli_label}»" if cli_label else "")
            + " продаж не найдено — ни накладных, ни черновиков.")
        return
    pdf, cap = report
    fname = safe_filename(f"продажи_{label}") + ".pdf"
    await update.message.reply_document(
        document=InputFile(pdf, filename=fname), caption=cap[:1000])


def _overdue_caption(min_days, found, total, grew_n, grew_total) -> str:
    parts = []
    if found:
        parts.append(f"⏰ Не платили {min_days}+ дней: {found} клиентов, "
                     f"зависло {money(total)}")
    if grew_n:
        parts.append(f"📈 Растущие долги: {grew_n} клиентов, "
                     f"+{money(grew_total)} за {DEBT_GROWTH_DAYS} дн.")
    return "\n".join(parts)


async def send_debt_alerts(bot):
    """Еженедельное напоминание о старых долгах PDF-файлом:
    админу — все склады, сотруднику — свои."""
    report = overdue_report(db.all_warehouses(), DEBT_ALERT_DAYS)
    if report:
        pdf, found, total, grew_n, grew_total = report
        try:
            await bot.send_document(
                ADMIN_ID, document=InputFile(pdf, filename=overdue_filename()),
                caption=_overdue_caption(DEBT_ALERT_DAYS, found, total,
                                         grew_n, grew_total))
        except Exception as e:
            log.warning("Не удалось отправить напоминание админу: %s", e)
    for u in db.list_users():
        if u["id"] == ADMIN_ID:
            continue
        report = overdue_report(debt_whs_for(u, db.visible_warehouses(u)),
                                DEBT_ALERT_DAYS)
        if not report:
            continue
        pdf, found, total, grew_n, grew_total = report
        try:
            await bot.send_document(
                u["id"], document=InputFile(pdf, filename=overdue_filename()),
                caption=_overdue_caption(DEBT_ALERT_DAYS, found, total,
                                         grew_n, grew_total)
                        + "\nПора напомнить клиентам об оплате 📞")
        except Exception as e:
            log.warning("Не удалось отправить напоминание %s: %s", u["name"], e)


def expiry_alert_report(warehouses):
    """PDF «Сроки на исходе»: просроченные партии (кандидаты на списание)
    и партии, истекающие в ближайшие 3 месяца (продавать первыми).
    Учебные склады пропускаются. None — тревожиться не о чем."""
    now = datetime.now(BISHKEK)
    soon = now + timedelta(days=92)
    sections = []
    tot_expired = tot_soon = 0
    for wh in warehouses:
        if is_training_wh(wh):
            continue
        rows, n_expired, n_soon = [], 0, 0
        for r in db.expiry_list(wh["id"]):
            if r["qty"] <= 0 or not r["expiry"]:
                continue
            d = _expiry_key_to_date(r["expiry"])
            if d is None or d > soon:
                continue
            p = prices.BY_ID.get(r["product_id"])
            label = (p["name"].split("(")[0].strip() if p
                     else f"товар №{r['product_id']}")
            volume = p["volume"] if p else ""
            if d <= now:
                status = "ПРОСРОЧЕНО — списать"
                n_expired += 1
            else:
                status = "продавать первым"
                n_soon += 1
            rows.append([r["expiry"], label, volume, f"{r['qty']} шт", status])
        if rows:
            sections.append({
                "title": f"Склад «{wh['name']}»",
                "headers": ["Срок", "Товар", "Фасовка", "Кол-во", "Что делать"],
                "rows": rows, "widths": [22, 62, 24, 20, 39], "numbered": True,
                "footer": f"Просрочено: {n_expired} · истекает скоро: {n_soon}",
            })
            tot_expired += n_expired
            tot_soon += n_soon
    if not sections:
        return None
    date_str = now.strftime("%d.%m.%Y")
    pdf = generate_report_pdf(
        "СРОКИ НА ИСХОДЕ",
        f"ОсОО «ВЕТОП» · просрочка и ближайшие 3 месяца · на {date_str}",
        sections,
        footer=f"ИТОГО: просрочено {tot_expired} партий · на исходе {tot_soon}")
    return pdf, tot_expired, tot_soon


async def send_expiry_alerts(bot):
    """Еженедельно админу: что просрочено и что продавать первым."""
    report = expiry_alert_report(db.all_warehouses())
    if not report:
        return
    pdf, n_expired, n_soon = report
    date_str = datetime.now(BISHKEK).strftime("%d%m%Y")
    caption = "📅 Сроки годности на исходе"
    if n_expired:
        caption += f"\n‼️ Просрочено: {n_expired} партий — спишите («спиши …»)"
    if n_soon:
        caption += f"\n⚠️ Истекают в 3 месяца: {n_soon} партий — продавать первыми"
    try:
        await bot.send_document(
            ADMIN_ID, document=InputFile(pdf, filename=f"сроки_тревога_{date_str}.pdf"),
            caption=caption)
    except Exception as e:
        log.warning("Не удалось отправить тревогу о сроках: %s", e)


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
            if not db.claim_daily_job(
                    f"backup:{datetime.now(BISHKEK).date().isoformat()}"):
                continue
            await send_backup(app.bot)
        except Exception:
            log.exception("Ошибка ежедневного бэкапа")


EXPORT_PERIODS = {
    "день": (0, "за сегодня"), "сегодня": (0, "за сегодня"),
    "неделя": (6, "за 7 дней"), "неделю": (6, "за 7 дней"),
    "месяц": (29, "за 30 дней"),
    "все": (3650, "за всё время"), "всё": (3650, "за всё время"),
}


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    days_back, label = EXPORT_PERIODS["месяц"]
    if context.args:
        key = context.args[0].lower()
        if key in EXPORT_PERIODS:
            days_back, label = EXPORT_PERIODS[key]
        else:
            await update.message.reply_text(
                "Использование: /export [день|неделя|месяц|все]")
            return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                       action="upload_document")
    import export_xlsx
    start = (datetime.now(BISHKEK) - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    xlsx = export_xlsx.build_export(start.isoformat(timespec="seconds"), label)
    filename = f"экспорт_ВЕТОП_{datetime.now(BISHKEK).strftime('%d%m%Y')}.xlsx"
    await update.message.reply_document(
        document=InputFile(xlsx, filename=filename),
        caption=f"📊 Экспорт {label}: операции, долги, остатки, кассы")


async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки закупа: курс, накладные расходы, сколько цен задано."""
    if await _require_admin(update) is None:
        return
    if not await _require_private(update):
        return
    rate, mk = usd_rate(), buy_markup_pct()
    n = len(db.products_buy_map())
    lines = [
        f"💱 Курс: <b>{fmt_usd(rate) if rate else 'не задан'}</b> сом/$",
        f"📦 Накладные расходы на закуп: <b>+{fmt_usd(mk)}%</b>",
        f"📥 Закупочные цены заданы: <b>{n}</b> из {len(prices.PRICE_LIST_DATA)} товаров",
        "",
        "Изменить фразой: «курс 88», «накладные расходы 10%»,",
        "«закупочная цена Дексатоп 50мл 0.47». Отчёты: /margin, /stockcost",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def stockcost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Остатки складов в закупочных ценах (только админ): /stockcost [Склад]."""
    actor = await _require_admin(update)
    if actor is None:
        return
    if not await _require_private(update):
        return
    bmap = buy_som_map()
    if not bmap:
        await update.message.reply_text(
            "Курс доллара не задан — напишите «курс 87.5» (см. /rate).")
        return
    arg = " ".join(context.args).strip() if context.args else ""
    if arg and arg.lower() != "all":
        wh = db.warehouse_by_name(arg)
        if wh is None:
            await update.message.reply_text(f"Склад «{esc(arg)}» не найден.",
                                            parse_mode="HTML")
            return
        whs = [wh]
    else:
        # Учебный полигон — не настоящие деньги: в сводный отчёт не входит
        # (посмотреть его можно явно: /stockcost Учебный).
        whs = [w for w in db.visible_warehouses(actor) if not is_training_wh(w)]
    sections, summary = [], []
    g_buy = g_sale = 0.0
    for wh in whs:
        stock = db.stock_map(wh["id"])
        rows, t_buy, t_sale, unknown = [], 0.0, 0.0, 0

        def add_row(pid, name, volume, price, qty):
            nonlocal t_buy, t_sale, unknown
            sale = qty * price
            t_sale += sale
            b = bmap.get(pid)
            short = f"{pid}. {name.split('(')[0].strip()}"
            if b is None:
                unknown += 1
                rows.append([short, volume, qty, "—", "—", money(sale)])
                return
            t_buy += qty * b
            rows.append([short, volume, qty, fmt_num(b), money(qty * b),
                         money(sale)])

        seen = set()
        for p in prices.PRICE_LIST_DATA:
            # Минусовые остатки тоже показываем: они уменьшают реальную
            # стоимость склада, скрывать их — завышать итог (аудит 02.08.2026).
            qty = stock.get(p["id"], 0)
            seen.add(p["id"])
            if qty != 0:
                add_row(p["id"], p["name"], p["volume"], p["price"], qty)
        for pid, qty in sorted(stock.items()):
            # Товар, выведенный из прайса, с остатком — как в Excel-экспорте.
            if qty != 0 and pid not in seen:
                row = db.connect().execute(
                    "SELECT name, volume, price FROM products WHERE id=?",
                    (pid,)).fetchone()
                add_row(pid, (row["name"] + " (вне прайса)") if row
                        else f"товар (вне прайса)", row["volume"] if row else "",
                        row["price"] if row else 0, qty)
        if not rows:
            summary.append(f"🏬 «{wh['name']}»: пусто")
            continue
        foot = (f"Закуп: {money(t_buy)} · Продажа: {money(t_sale)} · "
                f"Потенциальная наценка: {money(t_sale - t_buy)}")
        if unknown:
            foot += f" · без закупа: {unknown} поз. (не в сумме закупа)"
        sections.append({
            "title": f"Склад «{wh['name']}»",
            "headers": ["Товар", "Фасовка", "Остаток", "Закуп/шт",
                        "Сумма закуп", "Сумма продажа"],
            "rows": rows, "widths": [58, 22, 17, 19, 28, 28],
            "footer": foot,
        })
        summary.append(f"🏬 «{wh['name']}»: закуп {money(t_buy)}, "
                       f"продажа {money(t_sale)}")
        g_buy += t_buy
        g_sale += t_sale
    if not sections:
        await update.message.reply_text("\n".join(summary) or "Склады пусты.")
        return
    footer = (f"ВСЕГО: закуп {money(g_buy)} · продажа {money(g_sale)} · "
              f"наценка {money(g_sale - g_buy)}") if len(sections) > 1 else ""
    if len(whs) > 1:
        summary.append(f"💰 Всего по закупу: {money(g_buy)}")
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    pdf = generate_report_pdf(
        "ОСТАТКИ ПО ЗАКУПОЧНЫМ ЦЕНАМ",
        f"ОсОО «ВЕТОП» · на {date_str} · курс {fmt_usd(usd_rate())} сом/$"
        + (f" · накладные +{fmt_usd(buy_markup_pct())}%" if buy_markup_pct() else ""),
        sections, footer=footer)
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"закуп_остатки_{date_str.replace('.', '')}.pdf"),
        caption="\n".join(summary))


async def margin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прибыль по проданному (только админ): /margin [день|неделя|месяц|все].
    Выручка по фактическим ценам накладных минус себестоимость по закупу;
    возвраты вычитаются, списания — отдельной строкой потерь."""
    actor = await _require_admin(update)
    if actor is None:
        return
    if not await _require_private(update):
        return
    bmap = buy_som_map()
    if not bmap:
        await update.message.reply_text(
            "Курс доллара не задан — напишите «курс 87.5» (см. /rate).")
        return
    days_back, label = EXPORT_PERIODS["месяц"]
    if context.args:
        key = context.args[0].lower()
        if key in EXPORT_PERIODS:
            days_back, label = EXPORT_PERIODS[key]
        else:
            await update.message.reply_text(
                "Использование: /margin [день|неделя|месяц|все]")
            return
    start = (datetime.now(BISHKEK) - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    pdf, caption, filename = build_margin_report(
        start.isoformat(timespec="seconds"), None, label)
    if pdf is None:
        await update.message.reply_text(caption)
        return
    await update.message.reply_document(
        document=InputFile(pdf, filename=filename), caption=caption)


def build_margin_report(start_iso: str, end_iso, label: str):
    """PDF прибыли за период [start_iso, end_iso). Возвращает
    (pdf, подпись, имя_файла) или (None, текст-почему, None).
    end_iso=None — по сейчас. Общая сборка для /margin и месячного
    автоотчёта."""
    bmap = buy_som_map()
    if not bmap:
        return None, "Курс доллара не задан — напишите «курс 87.5» (см. /rate).", None
    rev = cost = unknown_rev = wo_cost = 0.0
    wo_unknown = 0
    unknown_names = set()
    by_wh = {}    # wh_id -> [выручка, прибыль]
    by_prod = {}  # pid -> [шт, выручка, прибыль]
    n_inv = 0
    skip_whs = training_wh_ids()  # учебный полигон — не настоящие деньги
    for op in db.operations_since(start_iso):
        if op["type"] not in ("invoice", "return", "writeoff"):
            continue
        if end_iso and op["ts"] >= end_iso:
            continue
        if op["warehouse_id"] in skip_whs:
            continue
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        sign = -1 if op["type"] == "return" else 1
        if op["type"] == "invoice":
            n_inv += 1
        for it in data.get("items", []):
            pid = it.get("product_id")
            qty = float(it.get("qty") or 0)
            b = bmap.get(pid) if pid else None
            if op["type"] == "writeoff":
                if b is not None:
                    wo_cost += qty * b
                else:
                    wo_unknown += 1  # потери без закупа — честно скажем в PDF
                continue
            amount = qty * float(it.get("price") or 0)
            if b is None:
                unknown_rev += sign * amount
                unknown_names.add(
                    f"{str(it.get('name') or '').split('(')[0].strip()} "
                    f"{it.get('volume') or ''}".strip())
                continue
            c = sign * qty * b
            rev += sign * amount
            cost += c
            w = by_wh.setdefault(op["warehouse_id"], [0.0, 0.0])
            w[0] += sign * amount
            w[1] += sign * amount - c
            pr = by_prod.setdefault(pid, [0.0, 0.0, 0.0])
            pr[0] += sign * qty
            pr[1] += sign * amount
            pr[2] += sign * amount - c
    if (not n_inv and not by_prod and not unknown_rev
            and not wo_cost and not wo_unknown):
        return (None, f"Реальных накладных {label} не было — прибыль считать "
                      f"не по чему. (Черновики в маржу не входят.)", None)
    profit = rev - cost
    sections = [{
        "title": "Итог",
        "headers": ["Показатель", "Сумма"],
        "rows": [["Выручка (по накладным, минус возвраты)", money(rev)],
                 ["Себестоимость по закупу", money(cost)],
                 ["ПРИБЫЛЬ", money(profit)]]
        + ([["Наценка к закупу", f"{profit / cost * 100:.0f}%"]] if cost > 0 else [])
        + ([["Списано товара (по закупу)", money(wo_cost)
             + (f" + {wo_unknown} поз. без закупа" if wo_unknown else "")]]
           if wo_cost or wo_unknown else [])
        + ([["Продажи без закупочной цены (не в прибыли)", money(unknown_rev)]]
           if unknown_rev else []),
        "widths": [110, 45],
    }]
    if len(by_wh) > 1:
        rows = []
        for wh_id, (w_rev, w_profit) in sorted(by_wh.items(),
                                               key=lambda kv: -kv[1][1]):
            wh = db.warehouse_by_id(wh_id)
            rows.append([wh["name"] if wh else f"склад №{wh_id}",
                         money(w_rev), money(w_profit)])
        sections.append({"title": "По складам",
                         "headers": ["Склад", "Выручка", "Прибыль"],
                         "rows": rows, "widths": [70, 42, 42]})
    top = sorted(by_prod.items(), key=lambda kv: -kv[1][2])[:15]
    if top:
        rows = []
        for pid, (q, a, pf) in top:
            p = prices.BY_ID.get(pid)
            nm = (f"{pid}. {p['name'].split('(')[0].strip()} {p['volume']}"
                  if p else f"товар №{pid}")
            rows.append([nm, fmt_num(q), money(a), money(pf)])
        sections.append({"title": "Топ товаров по прибыли",
                         "headers": ["Товар", "Продано", "Выручка", "Прибыль"],
                         "rows": rows, "widths": [72, 22, 30, 30],
                         "numbered": True})
    foot_notes = []
    if skip_whs:
        foot_notes.append("Учебный склад в прибыль не входит.")
    if unknown_names:
        shown = ", ".join(sorted(unknown_names)[:6])
        if len(unknown_names) > 6:
            shown += f" и ещё {len(unknown_names) - 6}"
        foot_notes.append(f"Без закупочной цены: {shown} — задайте фразой "
                          f"«закупочная цена ...».")
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    pdf = generate_report_pdf(
        "ПРИБЫЛЬ ПО ЗАКУПОЧНЫМ ЦЕНАМ",
        f"ОсОО «ВЕТОП» · {label} · на {date_str} · курс {fmt_usd(usd_rate())} "
        f"сом/$" + (f" · накладные +{fmt_usd(buy_markup_pct())}%"
                    if buy_markup_pct() else ""),
        sections, footer=" ".join(foot_notes))
    caption = (f"💰 Прибыль {label}: {money(profit)} "
               f"(выручка {money(rev)}, закуп {money(cost)})")
    return pdf, caption, f"прибыль_{date_str.replace('.', '')}.pdf"


async def api_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Расходы на API Anthropic и остаток от заданного баланса."""
    if await _require_admin(update) is None:
        return
    now = datetime.now(BISHKEK)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n_today, spent_today = db.api_usage_since(today.isoformat(timespec="seconds"))
    n_month, spent_month = db.api_usage_since(
        (today - timedelta(days=29)).isoformat(timespec="seconds"))
    lines = [
        "🤖 <b>Расходы на API Anthropic</b>",
        f"Сегодня: <b>${spent_today:.2f}</b> ({n_today} запросов)",
        f"За 30 дней: <b>${spent_month:.2f}</b> ({n_month} запросов)",
        f"Модель: {esc(CLAUDE_MODEL)}",
    ]
    # Здоровье кэша промпта: правила и прайс должны идти по скидке 90%.
    # Если доля кэша упала — какое-то обновление сломало кэширование, и
    # каждый запрос платит полную цену; это видно здесь без разработчика.
    inp, cread, cwrite = db.api_cache_stats(
        (today - timedelta(days=29)).isoformat(timespec="seconds"))
    total_in = inp + cread + cwrite
    if n_month >= 20 and total_in:
        share = cread * 100 // total_in
        if share >= 40:
            lines.append(f"♻️ Кэш промпта работает: {share}% входных токенов "
                         f"по скидке 90% (за 30 дней)")
        else:
            lines.append(f"⚠️ Кэш промпта работает слабо: только {share}% "
                         f"входных токенов из кэша — запросы дороже обычного. "
                         f"Покажите это сообщение Джарвису.")
    balance = db.get_setting("api_balance")
    balance_ts = db.get_setting("api_balance_ts")
    if balance and balance_ts:
        _, spent_since = db.api_usage_since(balance_ts)
        remaining = float(balance) - spent_since
        try:
            since_str = datetime.fromisoformat(balance_ts).strftime("%d.%m.%Y")
        except ValueError:
            since_str = balance_ts
        lines.append("")
        lines.append(f"💳 Баланс ${float(balance):.2f} задан {since_str}, "
                     f"потрачено с тех пор ${spent_since:.2f}")
        icon = "⚠️" if remaining < 5 else "✅"
        lines.append(f"{icon} <b>Осталось примерно: ${remaining:.2f}</b>")
    else:
        lines.append("")
        lines.append("💳 Чтобы бот показывал остаток: пополните счёт в консоли Anthropic "
                     "и напишите /apibalance 50 (сумма на счёте в долларах)")
    lines.append("")
    lines.append("ℹ️ Считается по токенам каждого запроса; точный остаток — "
                 "в консоли console.anthropic.com")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def apibalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    if not context.args:
        await update.message.reply_text(
            "Использование: /apibalance 50\n"
            "Укажите, сколько долларов сейчас на счёте Anthropic — "
            "бот будет вычитать расходы и показывать остаток в /api")
        return
    try:
        balance = float(context.args[0].replace("$", "").replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Не понял сумму. Пример: /apibalance 50")
        return
    db.set_setting("api_balance", str(balance))
    db.set_setting("api_balance_ts",
                   datetime.now(BISHKEK).isoformat(timespec="seconds"))
    await update.message.reply_text(
        f"✅ Баланс ${balance:.2f} записан. Остаток смотрите командой /api")


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_admin(update) is None:
        return
    await send_backup(context.bot)
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("💾 Копия отправлена админу.")


def db_storage_persistent() -> bool:
    """Постоянное ли хранилище: Volume в /data или явный DB_PATH."""
    return bool(os.environ.get("DB_PATH")) or os.path.isdir("/data")


async def dbinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Где живёт база и что в ней есть — для проверки Volume на Railway."""
    if await _require_admin(update) is None:
        return
    path = db._db_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    conn = db.connect()
    n_cli = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    n_ops = conn.execute(
        "SELECT COUNT(*) FROM operations WHERE status='done'").fetchone()[0]
    n_stock = conn.execute(
        "SELECT COALESCE(SUM(qty),0) FROM stock WHERE qty>0").fetchone()[0]
    if db_storage_persistent():
        store = "🟢 ПОСТОЯННОЕ (Volume подключён — данные переживают обновления)"
    else:
        store = ("🔴 ВРЕМЕННОЕ — данные СТИРАЮТСЯ при каждом обновлении бота!\n"
                 "Нужно подключить Volume к сервису на Railway (папка /data).")
    await update.message.reply_text(
        f"🗄 <b>База данных</b>\n"
        f"Файл: <code>{path}</code>\n"
        f"Размер: {size / 1024:.0f} КБ\n"
        f"Клиентов: {n_cli} · Операций: {n_ops} · Остатков: {fmt_num(n_stock)} шт\n\n"
        f"Хранилище: {store}",
        parse_mode="HTML")


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
        today = datetime.now(BISHKEK).date().isoformat()
        try:
            if db.claim_daily_job(f"debts:{today}"):
                await send_debt_alerts(app.bot)
        except Exception:
            log.exception("Ошибка напоминания о долгах")
        try:
            # Понедельничная тревога о сроках годности (решение владельца
            # 03.08.2026): просрочка и партии ближайших 3 месяцев.
            if db.claim_daily_job(f"expiry_alert:{today}"):
                await send_expiry_alerts(app.bot)
        except Exception:
            log.exception("Ошибка тревоги о сроках годности")


LOG_TYPE_ICONS = {"invoice": "🧾", "payment": "💵", "transfer": "📦",
                  "inventory": "📋", "return": "🔙", "handover": "💰",
                  "writeoff": "🗑"}

# Названия типов операций для таблиц PDF (эмодзи в шрифте документа нет)
LOG_TYPE_NAMES = {"invoice": "Накладная", "payment": "Оплата",
                  "transfer": "Перемещение", "inventory": "Инвентаризация",
                  "return": "Возврат", "handover": "Сдача выручки",
                  "writeoff": "Списание"}

# Слова-фильтры /log по типу операции: /log Азамат накладные 50
LOG_TYPE_FILTERS = {
    "накладные": "invoice", "накладная": "invoice",
    "приходы": "payment", "приход": "payment",
    "оплаты": "payment", "оплата": "payment",
    "возвраты": "return", "возврат": "return",
    "списания": "writeoff", "списание": "writeoff",
    "перемещения": "transfer", "перемещение": "transfer",
    "инкассации": "handover", "инкассация": "handover", "сдачи": "handover",
    "инвентаризации": "inventory", "инвентаризация": "inventory",
}
LOG_FILTER_LABELS = {
    "invoice": "только накладные",
    "payment": "приходы денег (оплаты и накладные с приходом)",
    "return": "только возвраты", "writeoff": "только списания",
    "transfer": "только перемещения", "handover": "только сдачи выручки",
    "inventory": "только инвентаризации",
}


async def show_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    if not await _require_private(update):
        return
    # Аргументы в любом порядке: число (сколько), тип операции, имя
    # сотрудника (только админ): /log Азамат накладные 50
    n, op_type, name_words = 20, None, []
    for a in (context.args or []):
        try:
            # Нижняя граница обязательна: отрицательный LIMIT в SQLite
            # означает «без лимита» — /log -5 выгружал бы весь журнал.
            n = max(1, min(int(a), 100))
            continue
        except ValueError:
            pass
        t = LOG_TYPE_FILTERS.get(a.lower())
        if t:
            op_type = t
        else:
            name_words.append(a)
    target_id = None if is_admin(actor) else actor["id"]
    who_label = "все сотрудники" if is_admin(actor) else actor["name"]
    wh_filter = None
    if name_words:
        joined = " ".join(name_words)
        # Слово может быть складом («/log Бишкек 30», просьба владельца
        # 13.08.2026) — сначала пробуем склад, потом сотрудника.
        wh_try = db.warehouse_by_name(joined)
        if wh_try is not None:
            if not any(w["id"] == wh_try["id"]
                       for w in db.visible_warehouses(actor)):
                await update.message.reply_text(
                    f"Склад «{esc(wh_try['name'])}» вам недоступен.",
                    parse_mode="HTML")
                return
            wh_filter = wh_try
        else:
            u = _match_employee(joined, db.list_users())
            if u is None:
                await update.message.reply_text(
                    f"Сотрудник или склад «{esc(joined)}» не найден.\n"
                    "Примеры: /log Азамат накладные 50, /log Бишкек 30",
                    parse_mode="HTML")
                return
            if not is_admin(actor) and u["id"] != actor["id"]:
                await update.message.reply_text(
                    "Чужие операции видит только администратор — /log без "
                    "имени покажет ваши.")
                return
            target_id, who_label = u["id"], u["name"]
    ops = db.recent_operations(n, target_id, op_type,
                               wh_filter["id"] if wh_filter else None)
    if not ops:
        if op_type or name_words:
            await update.message.reply_text(
                f"Операций не найдено ({esc(who_label)}"
                + (f", склад «{esc(wh_filter['name'])}»" if wh_filter else "")
                + (f", {LOG_FILTER_LABELS[op_type]}" if op_type else "")
                + ").", parse_mode="HTML")
        else:
            await update.message.reply_text("Журнал пуст.")
        return
    # Хронологический порядок: старые сверху, новые снизу — как в чате
    # (просьба владельца 25.07.2026).
    ops = list(reversed(ops))
    sections, cancelled, money_sum = [], 0, 0.0
    rows, last_day = [], None

    def flush(day):
        if rows:
            sections.append({"title": day,
                             "headers": ["№ оп.", "Время", "Сотрудник",
                                         "Тип", "Операция"],
                             "rows": list(rows), "widths": [16, 16, 28, 33, 89]})
            rows.clear()

    for op in ops:
        try:
            dt = datetime.fromisoformat(op["ts"])
            day, tm = dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M")
        except ValueError:
            day, tm = "—", op["ts"]
        if day != last_day:
            flush(last_day)
            last_day = day
        u = db.get_user(op["user_id"])
        who = u["name"] if u else str(op["user_id"])
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            data = {}
        summary = op["summary"]
        type_label = LOG_TYPE_NAMES.get(op["type"], op["type"])
        if op["type"] == "invoice" and data.get("payment"):
            # Накладная с деньгами сразу — честный тип (замечание
            # владельца 08.08.2026: «написано только накладная, но там
            # ещё же есть приход»)
            type_label = "Накладная + приход"
        if op_type == "payment":
            # В фильтре «приходы» показываем ДЕНЬГИ, а не накладную
            # (замечание владельца 08.08.2026: «зачем мне тут накладная?»)
            amt = (data.get("amount") if op["type"] == "payment"
                   else data.get("payment")) or 0
            if op["type"] == "invoice":
                c = db.client_get(op["client_id"]) if op["client_id"] else None
                if c is not None:
                    type_label = "Приход при накладной"
                    wh_ = (db.warehouse_by_id(op["warehouse_id"])
                           if op["warehouse_id"] else None)
                    summary = (f"Приход: {c['name']} — {fmt_num(amt)} сом "
                               f"при накладной №{op['id']}"
                               + (f" (склад {wh_['name']})" if wh_ else ""))
                    if (data.get("old_debt") is not None
                            and data.get("total") is not None):
                        summary += _debt_suffix(
                            data["old_debt"] + data["total"] - amt)
            if op["status"] != "cancelled":
                money_sum += amt
        if op["status"] == "cancelled":
            cancelled += 1
            summary = f"ОТМЕНЕНА · {summary}"
        rows.append([str(op["id"]), tm, who, type_label, summary])
    flush(last_day)
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    sub = f"ОсОО «ВЕТОП» · последние {len(ops)} · {who_label}"
    if wh_filter:
        sub += f" · склад «{wh_filter['name']}»"
    if op_type:
        sub += f" · {LOG_FILTER_LABELS[op_type]}"
    footer = f"Операций в журнале: {len(ops)}"
    if op_type == "payment":
        footer += f" · принято денег: {money(money_sum)}"
    if cancelled:
        footer += f" · из них отменено: {cancelled}"
    pdf = generate_report_pdf(
        "ЖУРНАЛ ОПЕРАЦИЙ", sub + f" · на {date_str}",
        sections, footer=footer)
    caption = f"🗒 Последние операции: {len(ops)} (новые внизу)"
    if op_type or target_id is not None and is_admin(actor):
        caption = (f"🗒 {who_label}"
                   + (f", {LOG_FILTER_LABELS[op_type]}" if op_type else "")
                   + f": {len(ops)} операций (новые внизу)")
    if is_admin(actor):
        caption += "\nОтменить: /undo Номер"
        caption += "\nФильтры: /log Азамат накладные 50"
    else:
        caption += "\nБольше: /log 50"
    caption += " · подробности: /op Номер"
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"журнал_{date_str.replace('.', '')}.pdf"),
        caption=caption)


async def op_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Детали операции по номеру: /op 26 (сотрудник — только свои)."""
    actor = await get_actor(update)
    if actor is None:
        return
    if not await _require_private(update):
        return
    try:
        op_id = int(context.args[0])
    except (IndexError, ValueError, TypeError):
        await update.message.reply_text(
            "Укажите номер операции: /op 26\nНомера смотрите в /log или /invoices")
        return
    op = db.get_operation(op_id)
    if op is None or (not is_admin(actor) and op["user_id"] != actor["id"]):
        await update.message.reply_text(
            f"Операция №{op_id} не найдена"
            + ("" if is_admin(actor) else " среди ваших операций") + ".")
        return
    try:
        data = json.loads(op["data"])
    except (ValueError, TypeError):
        data = {}
    u = db.get_user(op["user_id"])
    wh = db.warehouse_by_id(op["warehouse_id"]) if op["warehouse_id"] else None
    client = db.client_get(op["client_id"]) if op["client_id"] else None
    try:
        ts = datetime.fromisoformat(op["ts"]).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        ts = op["ts"]
    type_names = {"invoice": "Накладная", "payment": "Приход денег",
                  "transfer": "Перемещение / приход товара",
                  "inventory": "Инвентаризация", "return": "Возврат",
                  "handover": "Сдача выручки", "writeoff": "Списание"}
    type_label = type_names.get(op["type"], op["type"])
    if op["type"] == "invoice" and data.get("payment"):
        type_label = "Накладная + приход"
    info = [["Тип", type_label],
            ["Статус", "❌ ОТМЕНЕНА" if op["status"] == "cancelled"
             else "проведена"],
            ["Дата", ts],
            ["Исполнитель", u["name"] if u else str(op["user_id"])]]
    if wh:
        info.append(["Склад", wh["name"]])
    if client:
        info.append(["Клиент", client["name"]])
    # «Сводку» не показываем — она дублирует склад/клиента/сумму, которые
    # уже разложены по своим строкам (замечание владельца 26.07.2026).
    bd = data.get("batch_deltas") or []
    # Партии показываем в строке товара (колонка «Срок»), а не отдельной
    # кучей (просьба владельца 26.07.2026). Списания (продажа/перемещение)
    # приоритетнее приходов; товар находим по product_id, для старых
    # записей — по названию и фасовке из прайса.
    bd_by_pid = {}
    for _, pid_, exp_, chg_ in bd:
        if chg_ < 0:
            bd_by_pid.setdefault(pid_, []).append((exp_, -chg_))
    if not bd_by_pid:
        for _, pid_, exp_, chg_ in bd:
            if chg_ > 0 and exp_:
                bd_by_pid.setdefault(pid_, []).append((exp_, chg_))
    name_to_pid = {(pr["name"], pr["volume"]): pr["id"]
                   for pr in prices.PRICE_LIST_DATA}
    embedded = set()

    def _item_expiry_cell(it):
        pid_ = it.get("product_id") or name_to_pid.get(
            (str(it.get("name") or ""), str(it.get("volume") or "")))
        rows_ = bd_by_pid.get(pid_)
        if rows_:
            embedded.add(pid_)
            if len(rows_) == 1:
                return rows_[0][0] or "без срока"
            # Каждая партия своей строкой, «05.2028 — 15 шт» неразрывно
            return [f"{e or 'без срока'} — {q_} шт"
                    for e, q_ in rows_]
        return it.get("expiry") or "—"

    sections = [{"headers": ["Параметр", "Значение"],
                 "rows": info, "widths": [38, 139]}]
    items = data.get("items") or []
    if items:
        rows = []
        for it in items:
            name = str(it.get("name") or "").split("(")[0].strip()
            price = it.get("price")
            rows.append([
                name, str(it.get("volume") or ""), f"{it.get('qty')} шт",
                fmt_num(price) if price is not None else "—",
                fmt_num((it.get("qty") or 0) * price) if price is not None else "—",
                _item_expiry_cell(it)])
        if set(bd_by_pid) - embedded:
            # Партии, не привязавшиеся к строкам (старые записи), — в шапку
            info.append(["Партии", ", ".join(
                f"{e or 'без срока'}: {q_} шт"
                for pid_ in set(bd_by_pid) - embedded
                for e, q_ in bd_by_pid[pid_])])
        footer = ""
        if data.get("total") is not None:
            footer = f"Итого: {money(data['total'])}"
            if data.get("payment"):
                footer += f" · оплачено сразу: {money(data['payment'])}"
        sections.append({"title": "Состав",
                         "headers": ["Товар", "Фасовка", "Кол-во", "Цена",
                                     "Сумма", "Срок"],
                         "rows": rows, "widths": [46, 20, 18, 16, 21, 36],
                         "numbered": True, "footer": footer})
    elif data.get("amount") is not None:
        sections[0]["rows"].append(["Сумма", money(data["amount"])])
    status_mark = " (ОТМЕНЕНА)" if op["status"] == "cancelled" else ""
    pdf = generate_report_pdf(
        f"ОПЕРАЦИЯ №{op['id']}{status_mark}",
        f"ОсОО «ВЕТОП» · {type_label} · {ts}",
        sections)
    icon = LOG_TYPE_ICONS.get(op["type"], "▫️")
    caption = f"{icon} №{op['id']} · {op['summary']}"
    if op["status"] != "cancelled" and is_admin(actor):
        caption += f"\nОтменить: /undo {op['id']}"
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"операция_{op['id']}.pdf"),
        caption=caption)


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actor = await get_actor(update)
    if actor is None:
        return
    if context.args and not is_admin(actor):
        await update.message.reply_text(
            "⛔ Отменять операцию по номеру может только админ.\n"
            "Просто /undo (без номера) отменяет вашу последнюю операцию.")
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
                    "⛔ Отменить операцию можно только в течение часа. "
                    "Обратитесь к администратору.")
                return
    if op["status"] != "done":
        await update.message.reply_text(f"⚠️ Операция №{op['id']} уже отменена.")
        return
    # Сначала показываем, ЧТО будет отменено, и просим подтвердить кнопкой —
    # защита от случайного /undo (случай 21.07.2026: админ голым /undo
    # откатил стартовую загрузку Каракола).
    try:
        ts = datetime.fromisoformat(op["ts"]).strftime("%d.%m %H:%M")
    except ValueError:
        ts = op["ts"]
    u = db.get_user(op["user_id"])
    who = u["name"] if u else str(op["user_id"])
    # Если сторно уведёт остатки в минус (после отмены успели продать) —
    # предупреждаем прямо в карточке подтверждения.
    minus = []
    try:
        deltas = json.loads(op["data"]).get("stock_deltas", [])
    except (ValueError, TypeError):
        deltas = []
    for wh_id2, pid, d in deltas:
        if d > 0:
            left = db.stock_qty(wh_id2, pid) - d
            if left < 0:
                pr = prices.BY_ID.get(pid)
                label = pr["name"].split("(")[0].strip() if pr else f"товар №{pid}"
                minus.append(f"{label}: станет {left} шт")
    warn = ""
    if minus:
        shown = "\n".join(f"• {esc(m)}" for m in minus[:8])
        if len(minus) > 8:
            shown += f"\n… и ещё {len(minus) - 8} поз."
        warn = f"\n\n⚠️ <b>После отмены остатки уйдут в минус:</b>\n{shown}"
    payload = {"kind": "undo_op", "user_id": actor["id"],
               "chat_id": update.effective_chat.id, "op_id": op["id"]}
    token = new_pending(payload)
    await update.message.reply_text(
        "↩️ <b>Отмена операции — проверьте:</b>\n\n"
        f"№{op['id']} · {ts} · {esc(who)}:\n{esc(op['summary'])}\n\n"
        "Остатки и долги вернутся как до этой операции." + warn,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Да, отменить операцию", callback_data=f"ok:{token}"),
            InlineKeyboardButton("❌ Нет", callback_data=f"no:{token}"),
        ]]))


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
    lines = ["👥 <b>Команда:</b>"]
    role_icons = {"admin": "👑", "senior": "⭐", "employee": "👤"}
    role_names = {"admin": "админ", "senior": "старший", "employee": "сотрудник"}
    for u in db.list_users():
        wh = db.warehouse_of(u["id"])
        wh_label = esc(wh["name"]) if wh else "⚠️ не назначен"
        if u["role"] == "admin":
            line = (f"{role_icons['admin']} <b>{esc(u['name'])}</b> "
                    f"(<code>{u['id']}</code>) — админ, доступ ко ВСЕМ складам "
                    f"(родной: «{wh_label}»)")
        else:
            extra_access = db.access_warehouses(u["id"])
            full = [w for w in extra_access if not w["view_only"]]
            watch = [w for w in extra_access if w["view_only"]]
            line = (f"{role_icons.get(u['role'], '👤')} <b>{esc(u['name'])}</b> "
                    f"(<code>{u['id']}</code>) — {role_names.get(u['role'], 'сотрудник')}, "
                    f"склад «{wh_label}»")
            if full:
                line += " + доступ: " + ", ".join(f"«{esc(w['name'])}»" for w in full)
            if watch:
                line += " + просмотр: " + ", ".join(f"«{esc(w['name'])}»" for w in watch)
        lines.append(line)
    lines.append("")
    lines.append("Родной склад — куда идут операции, если склад не указан "
                 "в сообщении. «Просмотр» — видит отчёты склада, но операции "
                 "там не проводит (/watch Имя Склад).")
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
    own = db.warehouse_of(u["id"])
    if own and own["id"] == wh["id"]:
        await update.message.reply_text("Это его собственный склад — доступ уже есть.")
        return
    db.grant_access(u["id"], wh["id"])
    await update.message.reply_text(
        f"✅ <b>{esc(u['name'])}</b> получил доступ к складу «{esc(wh['name'])}».", parse_mode="HTML")


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Доступ-просмотр: сотрудник видит отчёты склада (/stock, /debts,
    /report, /expiry и т.д.), но операции проводить не может. Сделан для
    Беки-снабженца (03.08.2026). Забрать — /noaccess Имя Склад."""
    if await _require_admin(update) is None:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /watch Имя Склад — доступ только на просмотр\n"
            "Пример: /watch Бека Манас. Забрать: /noaccess Бека Манас")
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
    own = db.warehouse_of(u["id"])
    if own and own["id"] == wh["id"]:
        await update.message.reply_text("Это его собственный склад — там у него полный доступ.")
        return
    db.grant_access(u["id"], wh["id"], view_only=True)
    await update.message.reply_text(
        f"👁 <b>{esc(u['name'])}</b> видит отчёты склада «{esc(wh['name'])}» "
        f"(только просмотр — операции проводить не может).", parse_mode="HTML")


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


async def fullmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полный режим склада: выводит склад из переходного периода."""
    if await _require_admin(update) is None:
        return
    args = list(context.args or [])
    turn_off = False
    if args and args[0].lower() in ("выкл", "off"):
        turn_off = True
        args = args[1:]
    if not args:
        lines = ["🏭 <b>Режимы складов:</b>"]
        for w in db.all_warehouses():
            mode = "🟢 полный учёт" if w["full_mode"] else "📝 черновики (переходный)"
            lines.append(f"• {esc(w['name'])} — {mode}")
        lines.append("")
        lines.append("Включить: /fullmode Каракол · выключить: /fullmode выкл Каракол")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    name = " ".join(args)
    wh = db.warehouse_by_name(name)
    if wh is None:
        await update.message.reply_text(f"Склад «{esc(name)}» не найден.", parse_mode="HTML")
        return
    db.set_warehouse_full(wh["id"], not turn_off)
    if turn_off:
        await update.message.reply_text(
            f"📝 Склад «{esc(wh['name'])}» вернулся в переходный режим (черновики).",
            parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"🟢 Склад «{esc(wh['name'])}» переведён в ПОЛНЫЙ УЧЁТ:\n"
            "• накладные проводятся по-настоящему (кнопка подтверждения, "
            "остатки и долги меняются)\n"
            "• оплаты, возвраты, перемещения и сдача выручки работают\n"
            "• слово «черновик» по-прежнему делает черновик\n"
            "Остальные склады остаются на черновиках.", parse_mode="HTML")


# Реестр стартовых загрузок: склад -> (модуль с данными, имя списка, описание).
# Данные — [(id товара, количество, срок "MM.YYYY" или "")]. Новый склад:
# получить от владельца таблицу остатков, собрать <склад>_stock_data.py по
# образцу karakol_stock_data.py и добавить запись сюда.
STOCK_LOADS = {
    "каракол": ("karakol_stock_data", "KARAKOL_STOCK",
                "проверенная вами таблица от 22.07.2026"),
    "кара-балта": ("karabalta_stock_data", "KARABALTA_STOCK",
                   "ваша Excel-таблица от 21.07.2026 (ЭЛЕОВИТ 100мл — 19 шт, "
                   "пересчёт Беки 26.07)"),
    "манас": ("manas_stock_data", "MANAS_STOCK",
              "ваша Excel-таблица от 03.08.2026 (3 товара двумя партиями, "
              "4 просроченные позиции — как есть)"),
    "бишкек": ("bishkek_stock_data", "BISHKEK_STOCK",
               "ваша инвентаризация от 11.08.2026 (коробки пересчитаны в "
               "штуки по прайсу; Энротоп в списке отсутствовал)"),
}


def _stock_load_data(wh_name: str):
    entry = STOCK_LOADS.get(wh_name.lower())
    if entry is None:
        return None, None
    import importlib
    module = importlib.import_module(entry[0])
    return getattr(module, entry[1]), entry[2]


async def loadwh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Стартовая загрузка остатков склада из подготовленной таблицы (админ).

    /loadwh Склад; /loadkarakol — то же для Каракола (старое имя)."""
    if await _require_admin(update) is None:
        return
    name = " ".join(context.args or []).strip()
    if not name:
        # Голый /loadwh раньше молча подставлял Каракол (наследие
        # /loadkarakol) — владелец 13.08.2026 так чуть не загрузил не тот
        # склад. Теперь без имени — подсказка; старая команда работает.
        cmd = (update.message.text or "").split()[0].lower() if update.message else ""
        if "loadkarakol" in cmd:
            name = "Каракол"
        else:
            avail = ", ".join(k.title() for k in STOCK_LOADS)
            await update.message.reply_text(
                "Укажите склад: /loadwh Бишкек\n"
                f"Подготовленные таблицы есть для: {avail}.")
            return
    wh = db.warehouse_by_name(name)
    if wh is None:
        await update.message.reply_text(f"Склад «{esc(name)}» не найден.",
                                        parse_mode="HTML")
        return
    data, source = _stock_load_data(wh["name"])
    if data is None:
        await update.message.reply_text(
            f"Для склада «{esc(wh['name'])}» пока нет подготовленной таблицы "
            "остатков. Пришлите Джарвису таблицу (товар, количество, срок "
            "годности) — он зашьёт её в загрузчик.", parse_mode="HTML")
        return
    # Снимок текущих остатков: если на складе что-то лежит, загрузка НЕ
    # блокируется (случай Бишкека 13.08.2026 — товар перемещения №52 уже в
    # базе, а его количества вычтены из таблицы), но админ видит
    # предупреждение, а кнопка сверяет снимок — повторная загрузка или
    # параллельная операция отменяют заявку, задвоение невозможно.
    stock_sig = sorted([pid, q] for pid, q in db.stock_map(wh["id"]).items() if q)
    existing = sum(q for _, q in stock_sig)
    # Один товар может идти несколькими строками — это разные ПАРТИИ
    # (свой срок у каждой).
    items = [(pid, qty, exp) for pid, qty, exp in data if qty > 0]
    pids = {pid for pid, _, _ in items}
    total = sum(q for _, q, _ in items)
    payload = {"kind": "load_wh", "user_id": update.effective_user.id,
               "chat_id": update.effective_chat.id, "wh_id": wh["id"],
               "wh_name": wh["name"], "stock_sig": stock_sig}
    token = new_pending(payload)
    batch_note = ""
    if len(items) > len(pids):
        # Точный подсчёт (замечание владельца 13.08.2026: «у 20 товаров два
        # срока» было числом лишних СТРОК — товар с тремя партиями давал 2)
        per_pid = {}
        for pid, _, _ in items:
            per_pid[pid] = per_pid.get(pid, 0) + 1
        two = sum(1 for c in per_pid.values() if c == 2)
        more = sum(1 for c in per_pid.values() if c > 2)
        parts = []
        if two:
            parts.append(f"у {two} товаров два срока годности")
        if more:
            parts.append(f"у {more} — три и больше")
        batch_note = f"Партий: <b>{len(items)}</b> — {', '.join(parts)}.\n"
    exist_note = ""
    if stock_sig:
        exist_note = (f"⚠️ На складе уже есть остатки: {len(stock_sig)} поз., "
                      f"{fmt_num(existing)} шт — загрузка ДОБАВИТСЯ к ним, "
                      f"итог будет {fmt_num(existing + total)} шт.\n")
    await update.message.reply_text(
        f"📦 <b>Стартовая загрузка остатков — склад «{esc(wh['name'])}»</b>\n\n"
        f"Позиций с остатком: <b>{len(pids)}</b>\n"
        f"{batch_note}"
        f"Всего: <b>{total} шт</b>\n"
        f"Сроки годности будут сохранены "
        f"({sum(1 for _, q, e in data if q and e)} партий).\n"
        f"{exist_note}\n"
        f"Данные — {esc(source)}. Провести?",
        parse_mode="HTML", reply_markup=confirm_kb(token))


def _stats_line(s) -> str:
    try:
        last = datetime.fromisoformat(s["last_op_ts"]).strftime("%d.%m.%Y %H:%M") \
            if s["last_op_ts"] else "—"
    except ValueError:
        last = s["last_op_ts"]
    return (f"операций {s['operations']}, клиентов {s['clients']}, "
            f"долгов {money(s['debt'])}, последняя операция: {last}")


async def handle_db_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Файл *.db в личке от админа — восстановление базы из бэкапа."""
    import tempfile
    if update.message is None or update.effective_chat.type != "private":
        return
    doc = update.message.document
    if doc is None:
        return
    caption = (update.message.caption or "").strip()
    if CERT_CAPTION_RE.match(caption):
        # PDF сертификата от админа («сертификат Альтопен») — сохранить
        await _handle_cert_upload(update, doc.file_id, "document",
                                  doc.file_name, caption)
        return
    if not (doc.file_name or "").lower().endswith(".db"):
        # Файл без подписи: запомним — подпись «сертификат …» может прийти
        # следующим сообщением (владелец шлёт с телефона по отдельности)
        a = await get_actor(update)
        if a is not None and is_admin(a):
            _remember_cert_doc(update, doc.file_id, "document", doc.file_name)
        return
    actor = await get_actor(update)
    if actor is None:
        return
    if not is_admin(actor):
        await update.message.reply_text("⛔ Восстановление базы — только для админа.")
        return
    if (doc.file_size or 0) > 45_000_000:
        await update.message.reply_text("⚠️ Файл слишком большой для Telegram-бота.")
        return
    path = os.path.join(tempfile.gettempdir(),
                        f"restore_{secrets.token_hex(4)}.db")
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(path)
        info = db.inspect_backup(path, expected_admin_id=ADMIN_ID)
    except ValueError as e:
        await update.message.reply_text(f"⛔ Это не похоже на бэкап нашей базы: {esc(str(e))}",
                                        parse_mode="HTML")
        try:
            os.remove(path)
        except OSError:
            pass
        return
    except Exception as e:
        log.exception("Не удалось скачать/проверить файл бэкапа")
        await update.message.reply_text(f"⚠️ Не удалось обработать файл: {e}")
        try:
            os.remove(path)
        except OSError:
            pass
        return
    cur = db.current_stats()
    payload = {"kind": "restore_db", "user_id": actor["id"],
               "chat_id": update.effective_chat.id, "path": path,
               "fname": doc.file_name}
    token = new_pending(payload)
    await update.message.reply_text(
        f"💾 <b>Восстановление базы из бэкапа</b>\n"
        f"Файл: {esc(doc.file_name)}\n\n"
        f"📂 В файле: {esc(_stats_line(info))}\n"
        f"🗄 Сейчас в базе: {esc(_stats_line(cur))}\n\n"
        "Рабочая база будет ПОЛНОСТЬЮ заменена файлом — всё, что занесено "
        "после этого бэкапа, пропадёт. Перед заменой пришлю копию текущей "
        "базы. Продолжить?",
        parse_mode="HTML", reply_markup=confirm_kb(token))


async def resetwh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ПОЛНЫЙ сброс склада (только админ): журнал, остатки, клиенты с долгами.

    Для случая «загрузили с ошибками — стереть и загрузить заново»."""
    if await _require_admin(update) is None:
        return
    name = " ".join(context.args or [])
    if not name:
        await update.message.reply_text(
            "Укажите склад: /resetwh Каракол\n\n"
            "⚠️ Команда стирает по складу ВСЁ: операции из журнала, остатки, "
            "клиентов с долгами и сроки годности — чтобы загрузить склад "
            "заново с нуля. Перед удалением покажу, что будет стёрто.")
        return
    wh = db.warehouse_by_name(name)
    if wh is None:
        await update.message.reply_text(f"Склад «{esc(name)}» не найден.",
                                        parse_mode="HTML")
        return
    pv = db.reset_warehouse_preview(wh["id"])
    if not (pv["ops"] or pv["stock_positions"] or pv["clients"]):
        await update.message.reply_text(
            f"Склад «{esc(wh['name'])}» и так пуст — сбрасывать нечего.",
            parse_mode="HTML")
        return
    payload = {"kind": "reset_wh", "user_id": update.effective_user.id,
               "chat_id": update.effective_chat.id,
               "wh_id": wh["id"], "wh_name": wh["name"]}
    token = new_pending(payload)
    await update.message.reply_text(
        f"🗑 <b>ПОЛНЫЙ СБРОС склада «{esc(wh['name'])}»</b>\n\n"
        "Будет удалено безвозвратно:\n"
        f"• операций из журнала: <b>{pv['ops']}</b>\n"
        f"• остатки: <b>{pv['stock_positions']}</b> позиций, "
        f"{fmt_num(pv['stock_qty'])} шт\n"
        f"• клиентов: <b>{pv['clients']}</b> "
        f"(долгов на {money(pv['debt'])})\n"
        "• сроки годности склада\n\n"
        "Кассы сотрудников пересчитаются автоматически.\n"
        "⚠️ Отменить сброс будет НЕЛЬЗЯ — /undo не поможет. Стереть?",
        parse_mode="HTML", reply_markup=confirm_kb(token))


def _expiry_key_to_date(exp: str):
    try:
        mm, yyyy = exp.split(".")
        return datetime(int(yyyy), int(mm), 1, tzinfo=BISHKEK)
    except (ValueError, AttributeError):
        return None


async def expiry_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сроки годности: без аргумента — все доступные склады (как /stock)."""
    actor = await get_actor(update)
    if actor is None:
        return
    # В чате склада — только склад этого чата.
    whs_group, in_group = await _group_only_feed_whs(update, actor)
    name = " ".join(context.args or [])
    if in_group:
        if not whs_group:
            return
        whs = whs_group
    elif name and name.lower() != "all":
        wh = db.warehouse_by_name(name)
        if wh is None or not db.can_view_warehouse(actor, wh["id"]):
            await update.message.reply_text(
                f"Склад «{esc(name)}» не найден или нет доступа.", parse_mode="HTML")
            return
        whs = [wh]
    else:
        whs = db.visible_warehouses(actor)
        if not whs:
            await update.message.reply_text("У вас нет склада. Пример: /expiry Каракол")
            return
    now = datetime.now(BISHKEK)
    soon = now + timedelta(days=92)
    sections, summary = [], []
    for wh in whs:
        rows_db = [r for r in db.expiry_list(wh["id"]) if r["qty"] > 0]
        if not rows_db:
            summary.append(f"📅 «{wh['name']}»: сроков годности пока нет")
            continue
        # Партии одного товара — в ОДНОЙ строке (просьба владельца
        # 25.07.2026): «12.2027 — 20 шт, 11.2028 — 23 шт». Порядок списка —
        # по ближайшему сроку товара (expiry_list уже отсортирован).
        grouped, order = {}, []
        for r in rows_db:
            if r["product_id"] not in grouped:
                grouped[r["product_id"]] = []
                order.append(r["product_id"])
            grouped[r["product_id"]].append((r["expiry"], r["qty"]))
        rows, n_expired, n_soon, n_batches = [], 0, 0, 0
        for pid in order:
            batches = grouped[pid]
            n_batches += len(batches)
            p = prices.BY_ID.get(pid)
            label = p["name"].split("(")[0].strip() if p else f"товар №{pid}"
            volume = p["volume"] if p else ""
            d = _expiry_key_to_date(batches[0][0])  # ближайший срок
            if d and d <= now:
                status = "ПРОСРОЧЕНО!"
                n_expired += 1
            elif d and d <= soon:
                status = "истекает скоро"
                n_soon += 1
            else:
                status = ""
            if len(batches) == 1:
                exp_cell = batches[0][0]
            else:
                exp_cell = ", ".join(f"{e} — {q_} шт" for e, q_ in batches)
            total_qty = sum(q_ for _, q_ in batches)
            rows.append([exp_cell, label, volume, f"{total_qty} шт", status])
        sections.append(
            {"title": f"Склад «{wh['name']}»",
             "headers": ["Срок (партии)", "Товар", "Фасовка", "Остаток", "Статус"],
             "rows": rows, "widths": [34, 62, 24, 20, 27], "numbered": True,
             "footer": f"Позиций: {len(rows)} (партий: {n_batches}) · "
                       f"просрочено: {n_expired} · "
                       f"истекает в ближайшие 3 мес: {n_soon}"})
        summary.append(f"📅 «{wh['name']}»: {len(rows)} поз."
                       + (f" · ‼️ просрочено: {n_expired}" if n_expired else "")
                       + (f" · ⚠️ скоро: {n_soon}" if n_soon else ""))
    if not sections:
        await update.message.reply_text("\n".join(summary))
        return
    date_str = now.strftime("%d.%m.%Y")
    pdf = generate_report_pdf(
        "СРОКИ ГОДНОСТИ", f"ОсОО «ВЕТОП» · на {date_str}", sections)
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"сроки_{date_str.replace('.', '')}.pdf"),
        caption="\n".join(summary))


async def abc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """АВС-анализ: какие товары и клиенты дают выручку. По реальным накладным;
    пока их нет (переходный период) — по черновикам."""
    if await _require_admin(update) is None:
        return
    days_back, label = EXPORT_PERIODS["месяц"]
    if context.args:
        key = context.args[0].lower()
        if key in EXPORT_PERIODS:
            days_back, label = EXPORT_PERIODS[key]
    start = (datetime.now(BISHKEK) - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start_iso = start.isoformat(timespec="seconds")

    prod_rev, cli_rev, cli_n = {}, {}, {}
    total_rev = 0.0
    source = ""
    for op in db.operations_since(start_iso):
        if op["type"] != "invoice":
            continue
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        cname = "—"
        if op["client_id"]:
            c = db.client_get(op["client_id"])
            if c:
                cname = c["name"]
        t = data.get("total", 0)
        cli_rev[cname] = cli_rev.get(cname, 0) + t
        cli_n[cname] = cli_n.get(cname, 0) + 1
        total_rev += t
        for it in data.get("items", []):
            key = f"{it.get('name', '')} {it.get('volume', '')}".strip()
            prod_rev[key] = prod_rev.get(key, 0) + (it.get("qty") or 0) * (it.get("price") or 0)

    if total_rev == 0:  # переходный период — считаем по черновикам
        source = ", по черновикам"
        for r in db.drafts_with_items_since(start_iso):
            cli_rev[r["client"]] = cli_rev.get(r["client"], 0) + r["total"]
            cli_n[r["client"]] = cli_n.get(r["client"], 0) + 1
            total_rev += r["total"]
            if r["items"]:
                try:
                    its = json.loads(r["items"])
                except (ValueError, TypeError):
                    its = []
                for it in its:
                    key = f"{it.get('name', '')} {it.get('volume', '')}".strip()
                    prod_rev[key] = prod_rev.get(key, 0) + (it.get("sum") or 0)

    if total_rev == 0:
        await update.message.reply_text(
            f"📊 Нет данных {label}. Как появятся накладные или черновики — "
            "будет и анализ.")
        return

    sections = []
    prod_total = sum(prod_rev.values())
    if prod_total > 0:
        groups = {"A": [], "B": [], "C": []}
        cum = 0.0
        for name, rev in sorted(prod_rev.items(), key=lambda x: -x[1]):
            g = "A" if cum < 0.8 else ("B" if cum < 0.95 else "C")
            groups[g].append((name, rev))
            cum += rev / prod_total
        rows = [[name, money(rev), f"{rev / prod_total * 100:.0f}%", "A"]
                for name, rev in groups["A"]]
        rows += [[name, money(rev), f"{rev / prod_total * 100:.0f}%", "B"]
                 for name, rev in groups["B"]]
        if groups["C"]:
            c_sum = sum(r for _, r in groups["C"])
            rows.append([f"Остальные ({len(groups['C'])} поз.)", money(c_sum),
                         f"{c_sum / prod_total * 100:.0f}%", "C"])
        sections.append({
            "title": "Товары по выручке (A — главные 80%, B — ещё 15%, C — остальное)",
            "headers": ["Товар", "Выручка", "Доля", "Группа"],
            "rows": rows, "widths": [95, 38, 17, 17],
            "footer": f"Группа A: {len(groups['A'])} поз. · "
                      f"B: {len(groups['B'])} · C: {len(groups['C'])}"})
    cli_rows = [[name, money(rev), f"{rev / total_rev * 100:.0f}%", str(cli_n[name])]
                for name, rev in sorted(cli_rev.items(), key=lambda x: -x[1])[:10]]
    sections.append({"title": "Топ-10 клиентов",
                     "headers": ["Клиент", "Выручка", "Доля", "Накладных"],
                     "rows": cli_rows, "widths": [90, 38, 17, 22]})
    date_str = datetime.now(BISHKEK).strftime("%d.%m.%Y")
    pdf = generate_report_pdf("АВС-АНАЛИЗ",
                              f"ОсОО «ВЕТОП» · {label}{source} · на {date_str}",
                              sections, footer=f"ИТОГО ВЫРУЧКА: {money(total_rev)}")
    top_cli = max(cli_rev.items(), key=lambda x: x[1])
    caption = (f"📊 АВС {label}{source}: выручка {money(total_rev)}, "
               f"топ клиент — {top_cli[0]} ({money(top_cli[1])})")
    await update.message.reply_document(
        document=InputFile(pdf, filename=f"АВС_{date_str.replace('.', '')}.pdf"),
        caption=caption[:1000])


# Подсказки команд Telegram: при вводе «/» появляется меню, фильтруется по
# набранным буквам. Сотрудникам — рабочий набор, админу — полный список.
STAFF_COMMANDS = [
    ("stock", "Остатки склада"),
    ("stockprice", "Остатки с ценами и суммой"),
    ("stockat", "Остатки на дату (вчера, 22.07 14:30)"),
    ("invoices", "Накладные за период с составом"),
    ("debts", "Долги клиентов"),
    ("clients", "Справочник клиентов с телефонами"),
    ("client", "Карточка клиента: /client Имя"),
    ("act", "Акт сверки PDF: /act Имя"),
    ("expiry", "Сроки годности по партиям"),
    ("sales", "История продаж препарата: /sales Дексатоп"),
    ("cash", "Касса (наличные на руках)"),
    ("report", "Отчёт за день/неделю/месяц"),
    ("price", "Прайс-лист"),
    ("pricepdf", "PDF-прайс для клиентов"),
    ("promises", "Обещания оплаты"),
    ("undo", "Отменить последнюю операцию"),
    ("log", "Журнал операций"),
    ("op", "Детали операции: /op 26"),
    ("start", "Что умеет бот"),
]
ADMIN_COMMANDS = STAFF_COMMANDS + [
    ("margin", "Прибыль по закупочным ценам"),
    ("stockcost", "Остатки в закупочных ценах"),
    ("rate", "Курс доллара и накладные расходы"),
    ("olddebts", "Давно не платили"),
    ("hidedebts", "Скрыть долги склада от сотрудников"),
    ("drafts", "Сводка черновиков"),
    ("abc", "АВС-анализ товаров и клиентов"),
    ("deadstock", "Мёртвый товар"),
    ("forecast", "Прогноз закупки"),
    ("export", "Excel-экспорт всего учёта"),
    ("minstock", "Минимальные остатки"),
    ("pricelog", "История изменения цен"),
    ("users", "Команда и доступы"),
    ("grant", "Сделать старшим: /grant Имя"),
    ("access", "Дать доступ к складу"),
    ("watch", "Доступ-просмотр: видит отчёты, не проводит"),
    ("fullmode", "Полный режим склада"),
    ("loadwh", "Стартовая загрузка остатков"),
    ("resetwh", "ПОЛНЫЙ сброс склада"),
    ("feed", "Привязать чат-ленту склада"),
    ("warehouses", "Список складов"),
    ("backup", "Резервная копия базы"),
    ("dbinfo", "Состояние базы данных"),
    ("api", "Расходы на ИИ"),
    ("apibalance", "Остаток баланса ИИ"),
]


async def _setup_command_menu(app):
    from telegram import (BotCommand, BotCommandScopeAllGroupChats,
                          BotCommandScopeChat)
    try:
        await app.bot.set_my_commands(
            [BotCommand(c, d) for c, d in STAFF_COMMANDS])
        # В группах меню не показываем: команды с данными там всё равно
        # закрыты (_require_private), а посторонним список ни к чему.
        await app.bot.set_my_commands(
            [], scope=BotCommandScopeAllGroupChats())
        await app.bot.set_my_commands(
            [BotCommand(c, d) for c, d in ADMIN_COMMANDS],
            scope=BotCommandScopeChat(ADMIN_ID))
    except Exception:
        log.exception("Не удалось настроить меню команд")


async def _post_init(app):
    await _setup_command_menu(app)
    if not db_storage_persistent():
        try:
            await app.bot.send_message(
                ADMIN_ID,
                "🔴 <b>ВНИМАНИЕ: база данных во временном хранилище!</b>\n"
                "Volume не подключён к сервису — при каждом обновлении бота "
                "все данные (клиенты, долги, остатки) стираются.\n\n"
                "Пока не подключён Volume на Railway (папка /data), "
                "ничего не заносите в базу. Проверка: /dbinfo",
                parse_mode="HTML")
        except Exception:
            log.exception("Не удалось отправить предупреждение о хранилище")
    for prob in _static_prompt_problems():
        log.warning("Кэш промпта под угрозой: %s", prob)
        try:
            await app.bot.send_message(
                ADMIN_ID,
                f"⚠️ Проверка расходов ИИ: {prob}. Кэш промпта может не "
                f"работать — запросы будут дороже обычного. Покажите это "
                f"сообщение Джарвису.")
        except Exception:
            log.exception("Не удалось отправить предупреждение о кэше промпта")
    app.create_task(evening_summary_loop(app))
    app.create_task(cash_alert_loop(app))
    app.create_task(weekly_debt_loop(app))
    app.create_task(daily_backup_loop(app))
    app.create_task(monthly_deadstock_loop(app))
    app.create_task(draft_summary_loop(app))
    app.create_task(promise_reminder_loop(app))


if __name__ == "__main__":
    db.init(ADMIN_ID, WAREHOUSE_NAMES, STAFF)
    db.pending_cleanup(time.time())   # протухшие заявки прошлых запусков
    if db.seed_products(prices.SEED_DATA):
        log.info("Прайс перенесён в базу (%d позиций)", len(prices.SEED_DATA))
    import buy_prices_data
    if db.seed_buy_prices(buy_prices_data.BUY_USD,
                          buy_prices_data.INITIAL_USD_RATE):
        log.info("Закупочные цены инвойса F260403 заселены (%d поз.)",
                 len(buy_prices_data.BUY_USD))
    if db.seed_buy_prices(buy_prices_data.BUY_USD_TQ20251223,
                          buy_prices_data.INITIAL_USD_RATE,
                          flag="buy_prices_tq20251223"):
        log.info("Закупочные цены контракта TQ20251223C заселены (%d поз.)",
                 len(buy_prices_data.BUY_USD_TQ20251223))
    prices.set_data(db.products_active())
    _refresh_price_dependents()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("price", show_price))
    app.add_handler(CommandHandler("pricepdf", pricepdf_cmd))
    app.add_handler(CommandHandler("stock", show_stock))
    app.add_handler(CommandHandler("stockprice", show_stock_price))
    app.add_handler(CommandHandler("stockat", stockat_cmd))
    app.add_handler(CommandHandler("debts", show_debts))
    app.add_handler(CommandHandler("clients", clients_cmd))
    app.add_handler(CommandHandler("invoices", invoices_cmd))
    app.add_handler(CommandHandler("payment", payment_cmd))
    app.add_handler(CommandHandler("invoice", invoice_hint))
    app.add_handler(CommandHandler("draft", draft_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("olddebts", olddebts_cmd))
    app.add_handler(CommandHandler("hidedebts", hidedebts_cmd))
    app.add_handler(CommandHandler("sales", sales_cmd))
    app.add_handler(CommandHandler("deadstock", deadstock_cmd))
    app.add_handler(CommandHandler("forecast", forecast_cmd))
    app.add_handler(CommandHandler("client", client_cmd))
    app.add_handler(CommandHandler("act", act_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("dbinfo", dbinfo_cmd))
    app.add_handler(CommandHandler("minstock", minstock_cmd))
    app.add_handler(CommandHandler("cash", cash_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("api", api_cmd))
    app.add_handler(CommandHandler("margin", margin_cmd))
    app.add_handler(CommandHandler("stockcost", stockcost_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("drafts", drafts_cmd))
    app.add_handler(CommandHandler("pricelog", pricelog_cmd))
    app.add_handler(CommandHandler("promises", promises_cmd))
    app.add_handler(CommandHandler("abc", abc_cmd))
    app.add_handler(CommandHandler("fullmode", fullmode_cmd))
    app.add_handler(CommandHandler("loadwh", loadwh_cmd))
    app.add_handler(CommandHandler("loadkarakol", loadwh_cmd))
    app.add_handler(CommandHandler("resetwh", resetwh_cmd))
    app.add_handler(CommandHandler("expiry", expiry_cmd))
    app.add_handler(CommandHandler("cert", cert_cmd))
    app.add_handler(CommandHandler("certdel", certdel_cmd))
    app.add_handler(CommandHandler("apibalance", apibalance_cmd))
    app.add_handler(CommandHandler("log", show_log))
    app.add_handler(CommandHandler("op", op_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("add", add_user_cmd))
    app.add_handler(CommandHandler("remove", remove_user_cmd))
    app.add_handler(CommandHandler("users", list_users_cmd))
    app.add_handler(CommandHandler("grant", grant_cmd))
    app.add_handler(CommandHandler("ungrant", ungrant_cmd))
    app.add_handler(CommandHandler("access", access_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("noaccess", noaccess_cmd))
    app.add_handler(CommandHandler("whname", whname_cmd))
    app.add_handler(CommandHandler("setwh", setwh_cmd))
    app.add_handler(CommandHandler("whadd", whadd_cmd))
    app.add_handler(CommandHandler("warehouses", warehouses_cmd))
    app.add_handler(CommandHandler("feed", feed_cmd))
    app.add_handler(CommandHandler("nofeed", nofeed_cmd))
    # Список известных команд для подсказки при опечатке (/infobd -> /dbinfo)
    for h in app.handlers.get(0, []):
        if isinstance(h, CommandHandler):
            KNOWN_COMMANDS.update(h.commands)
    app.add_handler(MessageHandler(
        filters.COMMAND & filters.UpdateType.MESSAGE, unknown_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, handle_message))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.IMAGE) & filters.UpdateType.MESSAGE,
        handle_photo))
    app.add_handler(MessageHandler(
        (filters.VOICE | filters.AUDIO) & filters.UpdateType.MESSAGE, handle_voice))
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.UpdateType.MESSAGE, handle_db_file))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_chat_migrated))
    print("Бот запущен...")
    app.run_polling(stop_signals=None)
