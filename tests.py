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
