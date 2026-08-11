# Смоук-тесты ВЕТОП-бота. Запуск: python3 tests.py
# Гоняются на ВРЕМЕННОЙ базе (боевая не трогается), API-ключи не нужны.
# ПРАВИЛО ПРОЕКТА: прогонять перед каждым пушем; новые функции — добавлять
# сюда сценарий. Каждый тест — отдельная функция test_*, падение assert
# показывает имя теста.
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="vetop_tests_")
os.environ.setdefault("TELEGRAM_TOKEN", "1:test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")

import bot                                    # noqa: E402
import buy_prices_data                        # noqa: E402
import db                                     # noqa: E402
import prices                                 # noqa: E402

ADMIN = bot.ADMIN_ID
DANIYAR = 1616348285
AZAMAT = 6525019701


def _fresh_db():
    """Чистая база с прайсом, закупочными ценами и полным Караколом."""
    if db._conn is not None:
        db._conn.close()
        db._conn = None          # connect() кэширует соединение
    try:
        os.remove(os.environ["DB_PATH"])
    except OSError:
        pass
    db.init(ADMIN, bot.WAREHOUSE_NAMES, bot.STAFF)
    db.seed_products(prices.SEED_DATA)
    db.seed_buy_prices(buy_prices_data.BUY_USD, buy_prices_data.INITIAL_USD_RATE)
    db.seed_buy_prices(buy_prices_data.BUY_USD_TQ20251223,
                       buy_prices_data.INITIAL_USD_RATE,
                       flag="buy_prices_tq20251223")
    prices.set_data(db.products_active())
    wh = db.warehouse_by_name("Каракол")
    conn = db.connect()
    conn.execute("UPDATE warehouses SET full_mode=1 WHERE id=?", (wh["id"],))
    conn.commit()
    return wh


def _load(wh, loads, batches=None):
    db.commit_operation(ADMIN, "inventory", wh["id"], None, "загрузка",
                        [(wh["id"], pid, q) for pid, q in loads.items()], [],
                        {}, batch_plan=batches)


def _invoice(wh, user_id, client, items, payment=0.0, debt=0.0, client_id=None):
    p = {"kind": "invoice", "user_id": user_id, "wh_id": wh["id"],
         "wh_name": wh["name"], "client_name": client, "client_id": client_id,
         "items": items, "payment": payment, "parsed_debt": debt, "phone": None}
    return bot.commit_invoice(p), p


def _item(pid, qty, price):
    pr = prices.BY_ID[pid]
    return {"name": pr["name"], "volume": pr["volume"], "qty": qty,
            "price": price, "box_qty": None, "product_id": pid,
            "price_explicit": True}


def _stock(wh, pid):
    return db.stock_qty(wh["id"], pid)


def _batch_sum(wh, pid):
    row = db.connect().execute(
        "SELECT COALESCE(SUM(qty),0) s FROM product_batches "
        "WHERE warehouse_id=? AND product_id=?", (wh["id"], pid)).fetchone()
    return row["s"]


# ---------- базовый учёт ----------

def test_invoice_new_client_starting_debt():
    wh = _fresh_db()
    _load(wh, {16: 100})
    (op_id, label, old_debt, total, _), _ = _invoice(
        wh, DANIYAR, "Клиент А", [_item(16, 10, 180)], payment=500, debt=1000)
    c = db.client_exact(wh["id"], "Клиент А")
    assert total == 1800 and old_debt == 1000
    assert c["debt"] == 1000 + 1800 - 500
    assert _stock(wh, 16) == 90
    # /undo снимает всё, включая стартовый долг (он — дельта операции)
    ok, _ = db.cancel_operation(op_id)
    assert ok and db.client_get(c["id"])["debt"] == 0 and _stock(wh, 16) == 100


def test_batches_fefo_and_undo():
    wh = _fresh_db()
    _load(wh, {16: 50},
          {(wh["id"], 16): [("10.2027", 20), ("11.2028", 30)]})
    (op_id, *_), _ = _invoice(wh, DANIYAR, "Клиент Б", [_item(16, 25, 180)])
    rows = {r["expiry"]: r["qty"] for r in db.connect().execute(
        "SELECT expiry, qty FROM product_batches WHERE warehouse_id=? AND "
        "product_id=?", (wh["id"], 16))}
    assert rows.get("10.2027", 0) == 0 or "10.2027" not in rows  # FEFO: старая ушла
    assert _batch_sum(wh, 16) == _stock(wh, 16) == 25
    db.cancel_operation(op_id)
    assert _batch_sum(wh, 16) == _stock(wh, 16) == 50


# ---------- замена накладной ----------

def test_replace_rights():
    wh = _fresh_db()
    _load(wh, {16: 100})
    (op_id, *_), _ = _invoice(wh, DANIYAR, "Клиент В", [_item(16, 5, 180)])
    op = db.get_operation(op_id)
    daniyar, azamat, admin = (db.get_user(u) for u in (DANIYAR, AZAMAT, ADMIN))
    assert bot._replace_rights_error(daniyar, op) is None
    assert "другой сотрудник" in bot._replace_rights_error(azamat, op)
    assert bot._replace_rights_error(admin, op) is None
    conn = db.connect()
    conn.execute("UPDATE operations SET ts=datetime('now','-2 hours') WHERE id=?",
                 (op_id,))
    conn.commit()
    assert "больше часа" in bot._replace_rights_error(daniyar, db.get_operation(op_id))
    assert bot._replace_rights_error(admin, db.get_operation(op_id)) is None


def test_replace_full_carries_starting_debt():
    wh = _fresh_db()
    _load(wh, {16: 100, 28: 50})
    (op_id, label, *_), _ = _invoice(
        wh, DANIYAR, "Клиент Г", [_item(16, 10, 180), _item(28, 4, 440)],
        payment=500, debt=1000)
    cid = db.client_exact(wh["id"], "Клиент Г")["id"]
    old = json.loads(db.get_operation(op_id)["data"])
    back = {pid: -d for _, pid, d in old["stock_deltas"] if d < 0}
    rp = {"kind": "amend_invoice", "user_id": ADMIN, "op_user_id": DANIYAR,
          "wh_id": wh["id"], "wh_name": wh["name"], "client_name": label,
          "client_id": cid, "items": [_item(16, 7, 180)], "payment": 500.0,
          "parsed_debt": 0, "phone": None, "old_op_id": op_id, "old_count": 0,
          "full_replace": True, "returned_qty": back, "warnings": []}
    new_id, _, old_debt2, total2, _ = bot.commit_invoice(rp, replace_op_id=op_id)
    assert total2 == 1260 and old_debt2 == 1000
    # стартовый долг 1000 сохранился: 1000 + 1260 - 500
    assert db.client_get(cid)["debt"] == 1760
    assert db.get_operation(op_id)["status"] == "cancelled"
    assert db.get_operation(new_id)["user_id"] == DANIYAR
    assert _stock(wh, 16) == 93 and _stock(wh, 28) == 50
    # /undo замены снимает и перенесённый стартовый долг
    db.cancel_operation(new_id)
    assert db.client_get(cid)["debt"] == 0


def test_replace_already_cancelled_raises():
    wh = _fresh_db()
    _load(wh, {16: 100})
    (op_id, label, *_), _ = _invoice(wh, DANIYAR, "Клиент Д", [_item(16, 5, 180)])
    cid = db.client_exact(wh["id"], "Клиент Д")["id"]
    db.cancel_operation(op_id)
    rp = {"kind": "amend_invoice", "user_id": ADMIN, "op_user_id": DANIYAR,
          "wh_id": wh["id"], "wh_name": wh["name"], "client_name": label,
          "client_id": cid, "items": [_item(16, 3, 180)], "payment": 0.0,
          "parsed_debt": 0, "phone": None, "old_op_id": op_id, "old_count": 0,
          "full_replace": True, "returned_qty": {}, "warnings": []}
    try:
        bot.commit_invoice(rp, replace_op_id=op_id)
        assert False, "замена отменённой накладной должна падать"
    except ValueError:
        pass


# ---------- закупочные цены и маржа ----------

def test_buy_prices_seed_and_map():
    _fresh_db()
    assert bot.usd_rate() == 87.5
    bm = bot.buy_som_map()
    # два инвойса без пересечений по товарам
    both = {**buy_prices_data.BUY_USD, **buy_prices_data.BUY_USD_TQ20251223}
    assert not (set(buy_prices_data.BUY_USD)
                & set(buy_prices_data.BUY_USD_TQ20251223))
    assert len(bm) == len(both)
    assert abs(bm[16] - 0.47 * 87.5) < 0.01
    assert abs(bm[76] - 0.54 * 87.5) < 0.01   # Албенивер — со своего завода
    assert bm[76] != bm[10]                   # и НЕ равен Альтоперу
    db.set_setting("buy_markup_pct", "10")
    assert abs(bot.buy_som_map()[16] - 0.47 * 87.5 * 1.1) < 0.01
    db.set_setting("buy_markup_pct", "0")
    # повторное сидирование не затирает ручную правку
    db.product_set_buy(16, 9.99)
    assert not db.seed_buy_prices(buy_prices_data.BUY_USD, "50")
    assert db.products_buy_map()[16] == 9.99
    assert bot.usd_rate() == 87.5  # курс тоже не перезаписан


def test_secret_not_in_prompt():
    _fresh_db()
    assert bot._static_prompt_problems() == []
    for v in ("2.21", "4.31", "1.36", "3.18"):
        assert v not in bot.STATIC_SYSTEM, f"закупочная цена {v} утекла в промпт"


# ---------- разбор и фильтры (бесплатная защита API) ----------

def test_extract_action():
    a = bot.extract_action('до JSON {"action": "invoice", "client": "Асан"} после')
    assert a and a["client"] == "Асан"
    assert bot.extract_action("просто текст") is None


def test_chat_filter():
    _fresh_db()
    assert bot._looks_like_operation("Асан приход 5000")
    assert bot._looks_like_operation("замени накладную №45: Асан, Альтопен 10 шт")
    assert not bot._looks_like_operation("Всем привет, как дела?")
    assert not bot._looks_like_operation("Долго ждать ещё?")


def test_wake_word():
    assert bot._strip_wake_word("Джарвис, Асель приход 5000") == "Асель приход 5000"
    assert bot._strip_wake_word("жарвис приход 100") == "приход 100"
    assert bot._strip_wake_word("Борис, привет") is None
    assert bot._strip_wake_word("завис телефон 100") is None


def test_expiry_parse():
    assert bot._norm_expiry("11.2028") == "11.2028"
    assert bot._norm_expiry("11/28") == "11.2028"
    assert bot._norm_expiry("до 12.27") == "12.2027"
    assert bot._norm_expiry("привет") == ""


# ---------- дедупликация заявок ----------

def test_pending_dedup_window():
    _fresh_db()
    bot.PENDING.clear()
    p1 = {"kind": "invoice", "user_id": DANIYAR, "chat_id": 1, "wh_id": 1,
          "client_name": "Асан", "items": [_item(16, 5, 180)], "payment": 0}
    t1 = bot.new_pending(dict(p1))
    t2 = bot.new_pending(dict(p1))     # дубль в окне 120 сек
    assert t1 not in bot.PENDING and t2 in bot.PENDING
    p2 = dict(p1, client_name="Болот")  # другая заявка — живут обе
    t3 = bot.new_pending(dict(p2))
    assert t2 in bot.PENDING and t3 in bot.PENDING
    bot.PENDING.clear()


# ---------- форматирование ----------

def test_fmt_usd():
    assert bot.fmt_usd(0.47) == "0.47"
    assert bot.fmt_usd(1.7) == "1.7"
    assert bot.fmt_usd(87.5) == "87.5"
    assert bot.fmt_usd(88) == "88"


def test_api_cache_stats_empty():
    _fresh_db()
    assert db.api_cache_stats("2020-01-01") == (0, 0, 0)


def test_claim_daily_job():
    _fresh_db()
    assert db.claim_daily_job("evening:2099-01-01")
    assert not db.claim_daily_job("evening:2099-01-01")
    assert db.claim_daily_job("evening:2099-01-02")


def test_migrate_price_items_fixes_old_ids():
    wh = _fresh_db()
    _load(wh, {16: 50})
    (op_id, *_), _ = _invoice(wh, DANIYAR, "Клиент М", [_item(16, 5, 180)])
    conn = db.connect()
    # имитируем операцию со «старым» номером товара в items (до перенумерации)
    data = json.loads(db.get_operation(op_id)["data"])
    data["items"][0]["product_id"] = 999
    conn.execute("UPDATE operations SET data=? WHERE id=?",
                 (json.dumps(data, ensure_ascii=False), op_id))
    conn.execute("DELETE FROM settings WHERE key='price_order_v2_items'")
    conn.commit()
    db._migrate_price_items(conn)
    conn.commit()
    fixed = json.loads(db.get_operation(op_id)["data"])
    assert fixed["items"][0]["product_id"] == 16  # восстановлен по имени


def test_group_commands_scoped_to_feed_warehouse():
    import asyncio
    from types import SimpleNamespace
    wh = _fresh_db()
    conn = db.connect()
    conn.execute("UPDATE warehouses SET feed_chat_id=-100500 WHERE id=?",
                 (wh["id"],))
    conn.commit()
    replies = []

    class Msg:
        async def reply_text(self, text, **kw):
            replies.append(text)

    def upd(chat_id, chat_type):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
            message=Msg())

    async def run():
        daniyar = db.get_user(DANIYAR)   # склад Каракол — доступ есть
        azamat = db.get_user(AZAMAT)     # склад Манас — доступа нет
        # в чате-ленте Каракола: Данияр видит только Каракол
        whs, in_group = await bot._group_only_feed_whs(
            upd(-100500, "supergroup"), daniyar)
        assert in_group and [w["id"] for w in whs] == [wh["id"]]
        # Азамат в том же чате — отказ
        whs, in_group = await bot._group_only_feed_whs(
            upd(-100500, "supergroup"), azamat)
        assert in_group and whs == [] and "личку" in replies[-1]
        # чужая группа без привязки — отказ
        whs, in_group = await bot._group_only_feed_whs(
            upd(-42, "supergroup"), daniyar)
        assert in_group and whs == []
        # личка — обычное поведение
        whs, in_group = await bot._group_only_feed_whs(
            upd(1, "private"), daniyar)
        assert not in_group and whs is None
    asyncio.run(run())


def test_return_requires_expiry():
    wh = _fresh_db()
    _load(wh, {16: 50, 28: 30},
          {(wh["id"], 16): [("10.2027", 50)]})
    # возврат без срока — бот должен спросить срок для каждой позиции
    p = {"kind": "return", "user_id": ADMIN, "wh_id": wh["id"],
         "wh_name": wh["name"], "client_id": 1, "client_name": "Тест",
         "items": [_item(16, 5, 180), _item(28, 2, 440)], "warnings": []}
    need = bot._expiry_questions(p)
    assert [i for i, _ in need] == [0, 1]
    # партии кнопками у возврата не спрашиваются (товар приходит, не уходит)
    assert bot._batch_questions(p) == []
    # срок указан у первой — спросится только вторая
    p["items"][0]["expiry"] = "11.2028"
    assert [i for i, _ in bot._expiry_questions(p)] == [1]
    p["items"][1]["expiry"] = "12.2027"
    assert bot._expiry_questions(p) == []
    # у перемещения поведение прежнее: датированных хватает — не спрашиваем
    t = {"kind": "transfer", "user_id": ADMIN, "wh_id": wh["id"],
         "wh_name": wh["name"], "from_wh_id": wh["id"],
         "items": [_item(16, 5, 180)], "warnings": []}
    assert bot._expiry_questions(t) == []


def test_view_only_access():
    """Доступ-просмотр (/watch): отчёты видны, операции запрещены."""
    wh = _fresh_db()   # Каракол
    beka = 5808155644
    u = db.get_user(beka)
    db.grant_access(beka, wh["id"], view_only=True)
    # видит, но не проводит
    assert db.can_view_warehouse(u, wh["id"])
    assert not db.can_use_warehouse(u, wh["id"])
    assert wh["id"] in [w["id"] for w in db.visible_warehouses(u)]
    assert wh["id"] not in [w["id"] for w in db.operable_warehouses(u)]
    # resolve_warehouse (операции) отказывает с понятным текстом
    wh_res, err = bot.resolve_warehouse(u, wh["name"])
    assert wh_res is None and "ПРОСМОТР" in err, err
    # повторный /access превращает просмотр в полный доступ
    db.grant_access(beka, wh["id"], view_only=False)
    assert db.can_use_warehouse(db.get_user(beka), wh["id"])
    # /noaccess снимает
    db.revoke_access(beka, wh["id"])
    assert not db.can_view_warehouse(db.get_user(beka), wh["id"])


def test_staff_config_not_resurrected_on_deploy():
    """/remove и /noaccess для штатных сотрудников должны переживать деплой:
    повторный db.init (каждое обновление бота) не воскрешает active и не
    возвращает доступы из STAFF (баг всплыл 03.08.2026, отпуск Жуми)."""
    _fresh_db()
    zhumi = 607647629
    db.deactivate_user(zhumi)
    kb = db.warehouse_by_name("Кара-Балта")
    db.revoke_access(zhumi, kb["id"])
    db.init(ADMIN, bot.WAREHOUSE_NAMES, bot.STAFF)   # «деплой»
    row = db.get_user(zhumi)
    assert row["active"] == 0, "/remove откатился деплоем"
    assert kb["id"] not in [w["id"] for w in db.access_warehouses(zhumi)], \
        "/noaccess откатился деплоем"
    # админ при этом жив и роль на месте
    assert db.get_user(ADMIN)["active"] == 1
    assert db.get_user(ADMIN)["role"] == "admin"


def test_training_wh_excluded():
    wh = _fresh_db()
    conn = db.connect()
    conn.execute("INSERT INTO warehouses(name, full_mode) VALUES('Учебный', 1)")
    conn.commit()
    uch = db.warehouse_by_name("Учебный")
    assert bot.is_training_wh(uch) and not bot.is_training_wh(wh)
    assert bot.training_wh_ids() == {uch["id"]}
    # операции на обоих складах — в прибыли только настоящий склад
    for w, qty in ((wh, 10), (uch, 7)):
        _load(w, {16: 100})
        _invoice(w, ADMIN, f"К{w['id']}", [_item(16, qty, 180)])
    from datetime import datetime, timedelta
    start = (datetime.now(bot.BISHKEK) - timedelta(days=1)).isoformat(
        timespec="seconds")
    _, caption, _ = bot.build_margin_report(start, None, "тест")
    assert "1'800" in caption  # 10 шт, без учебных 7
    # тревога о сроках: просрочка на учебном не тревожит
    db.commit_operation(ADMIN, "inventory", uch["id"], None, "п",
                        [(uch["id"], 28, 20)], [], {},
                        batch_plan={(uch["id"], 28): [("01.2026", 20)]})
    assert bot.expiry_alert_report(db.all_warehouses()) is None
    db.commit_operation(ADMIN, "inventory", wh["id"], None, "п",
                        [(wh["id"], 28, 20)], [], {},
                        batch_plan={(wh["id"], 28): [("05.2026", 20)]})
    rep = bot.expiry_alert_report(db.all_warehouses())
    assert rep is not None and rep[1] == 1  # одна просроченная партия


def test_unknown_command_hint():
    """Опечатка в команде: подсказка сотруднику, тишина чужим и в группах."""
    import asyncio
    from types import SimpleNamespace
    _fresh_db()
    bot.KNOWN_COMMANDS.update({"dbinfo", "stock", "debts", "report", "start"})
    replies = []

    class Msg:
        def __init__(self, text):
            self.text = text

        async def reply_text(self, text, **kw):
            replies.append(text)

    def upd(text, user_id=ADMIN, chat_type="private"):
        return SimpleNamespace(
            effective_message=Msg(text),
            effective_chat=SimpleNamespace(id=1, type=chat_type),
            effective_user=SimpleNamespace(id=user_id))

    ctx = SimpleNamespace(bot=SimpleNamespace(username="vetop_bot"))

    async def run():
        # /infobd — перестановка букв, подсказывается /dbinfo
        await bot.unknown_command(upd("/infobd"), ctx)
        assert "/dbinfo" in replies[-1]
        # /stok — близкая опечатка
        await bot.unknown_command(upd("/stok"), ctx)
        assert "/stock" in replies[-1]
        # совсем непохожее — общий ответ со ссылкой на /start
        await bot.unknown_command(upd("/qwerty123"), ctx)
        assert "/start" in replies[-1]
        n = len(replies)
        # известная команда — не наш случай, молчим
        await bot.unknown_command(upd("/dbinfo"), ctx)
        # незнакомец — тишина
        await bot.unknown_command(upd("/infobd", user_id=999999), ctx)
        # группа без обращения к боту — тишина (команда могла быть чужому боту)
        await bot.unknown_command(upd("/infobd", chat_type="supergroup"), ctx)
        assert len(replies) == n
        # группа с явным обращением @vetop_bot — подсказываем
        await bot.unknown_command(
            upd("/infobd@vetop_bot", chat_type="supergroup"), ctx)
        assert "/dbinfo" in replies[-1]
    asyncio.run(run())


def test_cash_pdf_sections():
    """/cash — PDF: секция движений, подбор сотрудника по имени."""
    wh = _fresh_db()
    _load(wh, {16: 100})
    _invoice(wh, DANIYAR, "Клиент К", [_item(16, 5, 180)], payment=900)
    u = db.get_user(DANIYAR)
    cash, n_moves, sec = bot._cash_section(u)
    assert cash == 900 and n_moves == 1
    assert sec["rows"][0][1] == "+900" and "Сейчас на руках: 900 сом" in sec["footer"]
    # подбор по имени: точное, регистр, опечатка, неизвестное
    users = db.list_users()
    assert bot._match_employee("Данияр", users)["id"] == DANIYAR
    assert bot._match_employee("данияр", users)["id"] == DANIYAR
    assert bot._match_employee("Данеяр", users)["id"] == DANIYAR
    assert bot._match_employee("никто такой", users) is None
    # PDF собирается
    from invoice_pdf import generate_report_pdf
    pdf = generate_report_pdf("КАССА СОТРУДНИКА", "тест", [sec])
    assert pdf.getvalue().startswith(b"%PDF")
    # имя файла оканчивается на «.pdf»: safe_filename съедала точку,
    # телефон не открывал файл (скриншот владельца 07.08.2026)
    import asyncio
    from types import SimpleNamespace
    sent = {}

    class M:
        async def reply_document(self, document=None, caption=None, **kw):
            sent["filename"] = document.filename

        async def reply_text(self, *a, **kw):
            pass

    asyncio.run(bot._send_cash_pdf(M(), u))
    assert sent["filename"].endswith(".pdf"), sent["filename"]


def test_act_statement_invoice_with_payment():
    """Акт сверки: накладная с приходом — одна строка (товар+ и оплата−),
    долг сходится. Заодно охраняет db.client_operations (инцидент 08.08:
    правка соседней функции случайно стёрла её заголовок — /act и /client
    падали, тесты молчали)."""
    wh = _fresh_db()
    _load(wh, {16: 100})
    # стартовый долг 34'300 + накладная 1'800
    (_, _, _, _, _), _ = _invoice(wh, DANIYAR, "Эркинбу эже",
                                  [_item(16, 10, 180)], debt=34300)
    c = db.client_exact(wh["id"], "Эркинбу эже")
    # накладная 11'200 с приходом 5'000 (как №140 у Азамата)
    _invoice(wh, DANIYAR, c["name"], [_item(16, 8, 1400)], payment=5000,
             client_id=c["id"])
    rows, start = bot.client_statement(c["id"])
    assert start == 0 and len(rows) == 2
    d1, doc1, plus1, minus1, bal1 = rows[0]
    d2, doc2, plus2, minus2, bal2 = rows[1]
    assert plus1 == 1800 and minus1 == 0 and bal1 == 36100
    assert plus2 == 11200 and minus2 == 5000 and bal2 == 42300
    assert db.client_get(c["id"])["debt"] == 42300
    # PDF собирается
    from invoice_pdf import generate_act_pdf
    pdf = generate_act_pdf(c["name"], wh["name"], rows, start, 42300, "тест")
    assert pdf.getvalue().startswith(b"%PDF")


def test_report_calendar_day_and_tail():
    """Сводка — календарный день; вчерашний вечер отдельным «хвостом»."""
    from datetime import datetime, timedelta
    wh = _fresh_db()
    _load(wh, {16: 100})
    (op_id, *_), _ = _invoice(wh, DANIYAR, "Клиент Хвост",
                              [_item(16, 2, 180)], payment=360)
    # передвинем накладную на «вчера 23:00»
    y = (datetime.now(db.BISHKEK) - timedelta(days=1)).replace(
        hour=23, minute=0, second=0, microsecond=0)
    conn = db.connect()
    conn.execute("UPDATE operations SET ts=? WHERE id=?",
                 (y.isoformat(timespec="seconds"), op_id))
    conn.commit()
    midnight = datetime.now(db.BISHKEK).replace(hour=0, minute=0, second=0,
                                                microsecond=0)
    today = bot.report_data([wh], 0)
    assert today[0]["empty"]          # сегодня операций нет (загрузка не в счёт)
    tail = bot.report_data([wh], 0, start_dt=midnight - timedelta(hours=4),
                           end_dt=midnight)
    assert not tail[0]["empty"]
    assert tail[0]["sales"] == 360 and tail[0]["money"] == 360
    # правая граница строгая: операция «вчера 23:00» не попадает в «сегодня»
    assert db.operations_since(midnight.isoformat(timespec="seconds"),
                               None)[-1]["id"] != op_id


def test_arrival_requires_expiry():
    """Приход товара извне без срока годности не проводится — бот
    спрашивает срок по каждой позиции (решение владельца 09.08.2026)."""
    wh = _fresh_db()
    a = {"kind": "transfer", "user_id": ADMIN, "wh_id": wh["id"],
         "wh_name": wh["name"], "from_wh_id": None,
         "items": [_item(16, 10, 180), _item(28, 5, 440)], "warnings": []}
    assert [i for i, _ in bot._expiry_questions(a)] == [0, 1]
    a["items"][0]["expiry"] = "11.2028"
    assert [i for i, _ in bot._expiry_questions(a)] == [1]
    a["items"][1]["expiry"] = "12.2027"
    assert bot._expiry_questions(a) == []
    # у перемещения со склада поведение прежнее: датированных хватает —
    # вопросов нет (см. test_return_requires_expiry)


def test_warehouse_name_typos():
    """«Манач» → Манас: опечатки в имени склада прощаются (отчёты)."""
    _fresh_db()
    assert db.warehouse_by_name("Манач")["name"] == "Манас"
    assert db.warehouse_by_name("манас")["name"] == "Манас"
    assert db.warehouse_by_name("Каракл")["name"] == "Каракол"
    assert db.warehouse_by_name("карабалта")["name"] == "Кара-Балта"
    assert db.warehouse_by_name("Кара балта")["name"] == "Кара-Балта"
    assert db.warehouse_by_name("Бишкек")["name"] == "Бишкек"
    # совсем непохожее — по-прежнему не находится
    assert db.warehouse_by_name("Ош") is None
    assert db.warehouse_by_name("юг") is None


def test_cash_alert():
    """Ежедневное напоминание админу о несданных кассах (любая сумма)."""
    import asyncio
    from types import SimpleNamespace
    wh = _fresh_db()
    _load(wh, {16: 100})
    _invoice(wh, DANIYAR, "Клиент К", [_item(16, 1, 180)], payment=5)
    sent = []

    class B:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    asyncio.run(bot.send_cash_alert(B()))
    assert sent and sent[0][0] == ADMIN
    assert "Данияр" in sent[0][1] and "5 сом" in sent[0][1]
    assert "сдач ещё не было" in sent[0][1]
    # сдал всё — тишина
    db.commit_operation(DANIYAR, "handover", wh["id"], None,
                        "Инкассация: сдал", [], [], {"amount": 5})
    sent.clear()
    asyncio.run(bot.send_cash_alert(B()))
    assert sent == []


def test_report_handover_line():
    """«Сдано выручки» в отчёте дня; день с одной сдачей — не пустой."""
    wh = _fresh_db()
    _load(wh, {16: 100})
    _invoice(wh, DANIYAR, "Клиент С", [_item(16, 2, 180)], payment=360)
    db.commit_operation(DANIYAR, "handover", wh["id"], None,
                        "Инкассация: Данияр сдал 360 сом", [], [],
                        {"amount": 360})
    d = bot.report_data([wh], 0)[0]
    assert d["hand_sum"] == 360
    assert d["hand_list"] == [("Данияр", 360)]
    assert not d["empty"]
    pdf, caption = bot.build_report_pdf([wh], 0, "за день")
    assert pdf is not None and "Сдано выручки" in caption
    # день, где была ТОЛЬКО сдача — сводка всё равно приходит
    wh2 = db.warehouse_by_name("Манас")
    db.commit_operation(AZAMAT, "handover", wh2["id"], None,
                        "Инкассация: Азамат сдал 100 сом", [], [],
                        {"amount": 100})
    d2 = bot.report_data([wh2], 0)[0]
    assert not d2["empty"] and d2["hand_sum"] == 100 and d2["money"] == 0


def test_pending_survives_restart():
    """Заявка-карточка переживает перезапуск бота (зеркало в базе)."""
    wh = _fresh_db()
    p = {"kind": "writeoff", "user_id": ADMIN, "chat_id": 1,
         "wh_id": wh["id"], "wh_name": wh["name"], "reason": "бой",
         "items": [_item(16, 7, 180)]}
    token = bot.new_pending(dict(p))
    dict.clear(bot.PENDING)                     # «перезапуск»: память пуста
    got = bot.get_pending(token)
    assert got is not None and got["kind"] == "writeoff"
    assert got["items"][0]["qty"] == 7 and got["reason"] == "бой"
    # правка заявки между кнопками тоже переживает перезапуск
    got["reason"] = "просрочка"
    bot.get_pending(token)                      # обращение обновляет снимок
    dict.clear(bot.PENDING)
    assert bot.get_pending(token)["reason"] == "просрочка"
    # pop гасит заявку и в памяти, и в базе
    bot.PENDING.pop(token, None)
    dict.clear(bot.PENDING)
    assert bot.get_pending(token) is None
    # протухшая заявка из базы не поднимается
    token2 = bot.new_pending({"kind": "payment", "user_id": ADMIN,
                              "chat_id": 1}, ttl=-1)
    dict.clear(bot.PENDING)
    assert bot.get_pending(token2) is None


def test_cert_multi_query_split():
    """/cert альтопен, дексатоп — разбор списка через запятую."""
    _fresh_db()
    db.cert_add("АЛЬТОПЕН", "F1")
    db.cert_add("ДЕКСАТОП", "F2")
    parts = [x.strip() for x in "альтопен, дексатоп, ерунда".split(",")
             if x.strip()]
    names = [bot._cert_product_match(x)[0] for x in parts]
    assert names[:2] == ["АЛЬТОПЕН", "ДЕКСАТОП"] and names[2] is None


def test_writeoff_pdf_and_summary():
    """Акт списания PDF; сводка без «(не указана)» при пустой причине."""
    wh = _fresh_db()
    _load(wh, {16: 100}, {(wh["id"], 16): [("10.2027", 100)]})
    p = {"kind": "writeoff", "user_id": ADMIN, "wh_id": wh["id"],
         "wh_name": wh["name"], "reason": "не указана",
         "items": [{**_item(16, 82, 180), "src_batches": [("10.2027", 82)]}]}
    op_id, summary, total = bot.commit_writeoff(p)
    assert summary.startswith("Списание: ") and "не указана" not in summary
    assert total == 82 * 180 and db.stock_qty(wh["id"], 16) == 18
    pdf = bot._writeoff_pdf(p, op_id, total)
    assert pdf.getvalue().startswith(b"%PDF")
    # с причиной — причина в сводке
    p2 = {"kind": "writeoff", "user_id": ADMIN, "wh_id": wh["id"],
          "wh_name": wh["name"], "reason": "просрочка",
          "items": [_item(16, 3, 180)]}
    _, s2, _ = bot.commit_writeoff(p2)
    assert s2.startswith("Списание (просрочка): ")


def test_certs():
    """Сертификаты соответствия: хранение, поиск препарата, удаление."""
    _fresh_db()
    # подбор препарата: точное короткое имя, подстрока, опечатка
    name, _ = bot._cert_product_match("альтопен")
    assert name == "АЛЬТОПЕН"
    name, cands = bot._cert_product_match("альтопен-фор")
    assert name is None and any("ФОРТЕ" in c for c in cands)  # уточнение
    # опечатка «альтапен»: в прайсе есть и АЛЬТОПЕР — бот не гадает,
    # а предлагает уточнить, показывая обоих кандидатов
    name, cands = bot._cert_product_match("альтапен")
    assert name is None and "АЛЬТОПЕН" in cands and "АЛЬТОПЕР" in cands
    # хранение: новые первыми, список, удаление
    db.cert_add("АЛЬТОПЕН", "FILE1", "document", "cert.pdf", "поставка 07.2026")
    db.cert_add("АЛЬТОПЕН", "FILE2", "photo")
    certs = db.certs_of("АЛЬТОПЕН")
    assert [c["file_id"] for c in certs] == ["FILE2", "FILE1"]
    assert certs[1]["note"] == "поставка 07.2026"
    assert db.cert_names() == [("АЛЬТОПЕН", 2)]
    # регистр не важен (коллация NOCASEU)
    assert len(db.certs_of("альтопен")) == 2
    # разбор подписи: «сертификат на Альтопен — поставка 07.2026»
    m = bot.CERT_CAPTION_RE.match("Сертификат на Альтопен — поставка 07.2026")
    assert m and m.group(1).strip() == "Альтопен — поставка 07.2026"
    # старая поставка, загруженная ПОЗЖЕ новой, не перекрывает дефолт —
    # контракт F241009 бот знает как старый и без слова «старая»
    db.cert_add("АЛЬТОПЕН", "FILE_OLD", "document",
                note="поставка F241009")
    certs = db.certs_of("АЛЬТОПЕН")
    assert certs[0]["file_id"] == "FILE_OLD"          # свежая по загрузке
    assert bot._cert_default(certs)[0]["file_id"] == "FILE2"  # дефолт — новая
    assert db.certs_delete("АЛЬТОПЕН") == 3 and db.cert_names() == []
    # сертификат на ВСЮ поставку: подпись «сертификат поставки F260403»
    assert bot.CERT_SUPPLY_RE.match("поставки F260403").group(1) == "F260403"
    db.cert_add("Поставка F260403", "FILE3", "document")
    name, _ = bot._cert_product_match("поставка f260403")
    assert name == "Поставка F260403"
    # одно слово «поставка» при единственной поставке тоже находит её
    name, _ = bot._cert_product_match("поставка")
    assert name == "Поставка F260403"
    # файл и подпись ОТДЕЛЬНЫМИ сообщениями (случай владельца 08.08.2026)
    import asyncio
    from types import SimpleNamespace
    replies = []

    class Msg:
        async def reply_text(self, t, **kw):
            replies.append(t)

    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=1, type="private"),
                          effective_user=SimpleNamespace(id=ADMIN),
                          message=Msg())
    bot._remember_cert_doc(upd, "FILE9", "document", "s.pdf")
    admin = db.get_user(ADMIN)
    asyncio.run(bot.cert_text_reply(
        upd, admin, "сертификат Дексатоп — поставка F250918."))
    certs = db.certs_of("ДЕКСАТОП")
    assert certs and certs[0]["file_id"] == "FILE9"
    assert "ДЕКСАТОП" in replies[-1]
    # без свежего файла — подсказка, ничего не пишется
    asyncio.run(bot.cert_text_reply(upd, admin, "сертификат Бутатоп"))
    assert db.certs_of("БУТАТОП") == [] and "/cert" in replies[-1]


def test_client_word_order():
    """«Чооров Кубан» = «Кубан Чооров»: перестановка слов имени находит
    клиента, двойник не создаётся (случай Беки на выезде 08.08.2026)."""
    wh = _fresh_db()
    _load(wh, {16: 50})
    _invoice(wh, DANIYAR, "Кубан Чооров", [_item(16, 1, 180)], debt=148000)
    c = db.client_exact(wh["id"], "Чооров Кубан")
    assert c is not None and c["name"] == "Кубан Чооров"
    # регистр не мешает
    assert db.client_exact(wh["id"], "чооров кубан")["name"] == "Кубан Чооров"
    # и в кандидатах-кнопках перестановка тоже есть (первой)
    cands = db.fuzzy_clients(wh["id"], "Чооров Кубан")
    assert cands and cands[0]["name"] == "Кубан Чооров"
    # одно слово — как раньше, без сюрпризов
    assert db.client_exact(wh["id"], "Кубан") is None
    # два клиента с одинаковым набором слов — авто-совпадения нет
    _invoice(wh, DANIYAR, "Чооров Кубан", [_item(16, 1, 180)])
    assert db.client_exact(wh["id"], "Кубан Чооров")["name"] == "Кубан Чооров"


def test_log_filters():
    """/log Азамат накладные — фильтры журнала по сотруднику и типу."""
    wh = _fresh_db()
    _load(wh, {16: 100})
    _invoice(wh, DANIYAR, "Клиент Л", [_item(16, 2, 180)], payment=100)
    c = db.client_exact(wh["id"], "Клиент Л")
    bot.commit_payment({"user_id": DANIYAR, "wh_id": wh["id"],
                        "wh_name": wh["name"], "client_id": c["id"],
                        "amount": 200})
    db.commit_operation(AZAMAT, "handover", wh["id"], None,
                        "Инкассация: сдал", [], [], {"amount": 50})
    assert {o["type"] for o in db.recent_operations(50, DANIYAR)} == \
        {"invoice", "payment"}
    # «приходы» = отдельные оплаты + накладные с приходом (решение
    # владельца 08.08.2026); накладная без денег в фильтр не попадает
    pays = db.recent_operations(50, DANIYAR, "payment")
    assert {p["type"] for p in pays} == {"payment", "invoice"}
    assert all(json.loads(p["data"]).get("payment") or p["type"] == "payment"
               for p in pays)
    _invoice(wh, DANIYAR, "Клиент Б/Д", [_item(16, 1, 180)])  # без прихода
    assert len(db.recent_operations(50, DANIYAR, "payment")) == len(pays)
    assert db.recent_operations(50, None, "handover")[0]["user_id"] == AZAMAT
    assert db.recent_operations(50, DANIYAR, "handover") == []
    # слова-фильтры понимают единственное и множественное число
    assert bot.LOG_TYPE_FILTERS["накладные"] == "invoice"
    assert bot.LOG_TYPE_FILTERS["возврат"] == "return"
    assert bot.LOG_TYPE_FILTERS["приходы"] == "payment"


def test_summary_debt_suffix():
    """Сводка операции показывает долг клиента после неё (лента, /log)."""
    wh = _fresh_db()
    _load(wh, {16: 100})
    (_, _, _, _, s), _ = _invoice(wh, DANIYAR, "Клиент Д",
                                  [_item(16, 8, 1400)], payment=5000,
                                  debt=31100)
    assert "приход 5'000" in s and s.endswith("долг: 37'300 сом"), s
    c = db.client_exact(wh["id"], "Клиент Д")
    _, _, _, s2 = bot.commit_payment(
        {"user_id": DANIYAR, "wh_id": wh["id"], "wh_name": wh["name"],
         "client_id": c["id"], "amount": 37300})
    assert s2.endswith("долг погашен"), s2
    assert db.client_get(c["id"])["debt"] == 0


def test_growing_debts():
    """Раздел риска «Растущие долги»: в списке — у кого долг за окно вырос;
    погасивший больше, чем набрал, и стартовые долги справочника — не рост;
    отмена операции убирает рост (журнальная модель)."""
    wh = _fresh_db()
    _load(wh, {16: 100})
    # Накладная 2000 с оплатой 500 на месте: дельта журнала чистая (+1500),
    # отдельная оплата 400 — «погасил»; итоговый рост 1100
    (op_id, *_), _ = _invoice(wh, DANIYAR, "Растёт", [_item(16, 10, 200)],
                              payment=500)
    r = db.client_exact(wh["id"], "Растёт")
    bot.commit_payment({"user_id": DANIYAR, "wh_id": wh["id"],
                        "wh_name": wh["name"], "client_id": r["id"],
                        "amount": 400})
    # Старый долг из справочника + оплата 1000 — рост отрицательный
    db.clients_add_bulk(wh["id"], [("Старый", 5000)])
    s = db.client_exact(wh["id"], "Старый")
    bot.commit_payment({"user_id": DANIYAR, "wh_id": wh["id"],
                        "wh_name": wh["name"], "client_id": s["id"],
                        "amount": 1000})
    rows = {c["name"]: (delta, took, paid)
            for _, rs in bot.growing_debt_rows(db.all_warehouses(),
                                               bot.DEBT_GROWTH_DAYS)
            for delta, c, took, paid in rs}
    assert rows.get("Растёт") == (1100, 1500, 400), rows
    assert "Старый" not in rows
    # Зависших долгов нет (все операции свежие), но отчёт всё равно есть —
    # из-за растущих; found=0, grew_n=1
    report = bot.overdue_report(db.all_warehouses(), bot.DEBT_ALERT_DAYS)
    assert report is not None
    pdf, found, total, grew_n, grew_total = report
    assert found == 0 and grew_n == 1 and grew_total == 1100
    assert pdf.getvalue()[:4] == b"%PDF"
    db.cancel_operation(op_id)
    rows = {c["name"] for _, rs in
            bot.growing_debt_rows(db.all_warehouses(), bot.DEBT_GROWTH_DAYS)
            for _, c, *_ in rs}
    assert "Растёт" not in rows


def test_sales_history():
    """/sales: разбор «препарат [фасовка] [клиент]», история по журналу,
    возвраты и черновики видны, чужой товар не попадает."""
    wh = _fresh_db()
    _load(wh, {16: 100, 17: 100})
    pr16, pr17 = prices.BY_ID[16], prices.BY_ID[17]
    base16 = pr16["name"].split("(")[0].strip()
    # Разбор: точное имя без фасовки — обе фасовки товара с этим именем
    label, pids, cli, _ = bot._parse_sales_query(base16.lower())
    assert 16 in pids and cli is None
    # С фасовкой и клиентом
    label, pids, cli, _ = bot._parse_sales_query(
        f"{base16} {pr16['volume']} Мустанг".lower())
    assert pids == [16] and cli == "мустанг", (label, pids, cli)
    # Продажи: два клиента, товар 17 — шум, который не должен попасть
    _invoice(wh, DANIYAR, "Мустанг", [_item(16, 10, 200), _item(17, 3, 500)])
    _invoice(wh, DANIYAR, "Болот", [_item(16, 5, 210)])
    c = db.client_exact(wh["id"], "Мустанг")
    db.commit_operation(DANIYAR, "return", wh["id"], c["id"], "возврат",
                        [(wh["id"], 16, 2)], [(c["id"], -400)],
                        {"items": [{"name": pr16["name"],
                                    "volume": pr16["volume"], "qty": 2,
                                    "price": 200, "product_id": 16}]})
    db.log_draft(DANIYAR, "Мустанг", 600,
                 [{"name": pr16["name"], "volume": pr16["volume"],
                   "qty": 3, "sum": 600}])
    rows = db.product_sales([16], {wh["id"]})
    assert len(rows) == 3          # две накладные + возврат, без товара 17
    pdf, cap = bot.sales_history_report(
        [wh], base16, [16], {c["id"]}, "Мустанг")
    assert "продано 10 шт" in cap and "2'000" in cap, cap
    assert "Возвраты: 2 шт" in cap and "Черновики" in cap, cap
    assert pdf.getvalue()[:4] == b"%PDF"
    # Без фильтра клиента — обе накладные
    _, cap_all = bot.sales_history_report([wh], base16, [16])
    assert "продано 15 шт" in cap_all, cap_all


def test_cash_since_zero_handover():
    """/cash показывает движения только после инкассации «под ноль»."""
    wh = _fresh_db()
    _load(wh, {16: 100})
    _invoice(wh, DANIYAR, "Клиент А", [_item(16, 2, 180)], payment=5000)
    _invoice(wh, DANIYAR, "Клиент Б", [_item(16, 2, 180)], payment=2600)
    # сдал всё — касса ровно ноль
    db.commit_operation(DANIYAR, "handover", wh["id"], None,
                        "Инкассация: Данияр сдал 7'600 сом", [], [],
                        {"amount": 7600})
    moves, since = db.cash_movements_since_zero(DANIYAR)
    assert moves == [] and since is not None      # после обнуления — пусто
    # новые операции после обнуления
    _invoice(wh, DANIYAR, "Клиент В", [_item(16, 2, 180)], payment=900)
    moves, since = db.cash_movements_since_zero(DANIYAR)
    assert since is not None and [amt for _, amt in moves] == [900]
    cash, n_moves, sec = bot._cash_section(db.get_user(DANIYAR))
    assert cash == 900 and n_moves == 1
    assert "после инкассации" in sec["title"]
    # частичная сдача (не под ноль) — история не отрезается
    db.commit_operation(DANIYAR, "handover", wh["id"], None,
                        "Инкассация: Данияр сдал 400 сом", [], [],
                        {"amount": 400})
    moves, since = db.cash_movements_since_zero(DANIYAR)
    assert [amt for _, amt in moves] == [-400, 900]
    # а до первой инкассации истории вообще не было бы видно
    assert all(amt != 5000 for _, amt in moves)


def test_feed_chat_migration():
    """Группа стала супергруппой (id сменился) — привязка ленты переезжает."""
    wh = _fresh_db()
    conn = db.connect()
    conn.execute("UPDATE warehouses SET feed_chat_id=-500 WHERE id=?", (wh["id"],))
    conn.commit()
    # первое служебное сообщение переносит привязку
    assert db.migrate_feed_chat(-500, -100500) == [wh["name"]]
    assert [w["id"] for w in db.warehouses_of_feed(-100500)] == [wh["id"]]
    assert db.warehouses_of_feed(-500) == []
    # второе (дубль из нового чата) уже ничего не находит — без двойного алерта
    assert db.migrate_feed_chat(-500, -100500) == []
    # чат без привязки — тишина
    assert db.migrate_feed_chat(-42, -100042) == []


def test_stranger_full_silence():
    """Посторонний не получает НИЧЕГО — ни в личке, ни в группе."""
    import asyncio
    from types import SimpleNamespace
    _fresh_db()
    replies = []

    class Msg:
        text = "привет"

        async def reply_text(self, text, **kw):
            replies.append(text)

    def upd(user_id, chat_type):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_message=Msg(),
            effective_chat=SimpleNamespace(id=1, type=chat_type))

    async def run():
        assert await bot.get_actor(upd(999999, "private")) is None
        assert await bot.get_actor(upd(999999, "supergroup")) is None
        assert replies == []                      # полная тишина
        row = await bot.get_actor(upd(DANIYAR, "private"))
        assert row is not None and row["active"]  # свои работают как раньше
    asyncio.run(run())


def test_margin_op_id_only_for_invoice_actions():
    # op_id, случайно приехавший в оплату, не должен отключать выбор склада
    for action, expected in (("replace_invoice", True), ("amend_invoice", True),
                             ("payment", False), ("invoice", False)):
        data = {"action": action, "op_id": 45}
        got = (data.get("action") in ("replace_invoice", "amend_invoice")
               and bool(data.get("op_id")))
        assert got is expected, action


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
        except Exception as e:                       # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} прошло")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
