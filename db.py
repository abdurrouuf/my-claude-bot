# Хранилище: SQLite. На Railway подключите Volume (например, в /data) —
# файл базы должен лежать на постоянном диске, иначе данные пропадут при деплое.
import difflib
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

BISHKEK = timezone(timedelta(hours=6))

_conn = None
_lock = threading.RLock()


def _db_path() -> str:
    env = os.environ.get("DB_PATH")
    if env:
        return env
    if os.path.isdir("/data"):
        return "/data/vetop.db"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "vetop.db")


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = _db_path()
        log.info("SQLite: %s", path)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # Встроенный NOCASE не понимает кириллицу — своя коллация для имён.
        _conn.create_collation(
            "NOCASEU",
            lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()),
        )
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'employee',  -- admin | senior | employee
    active     INTEGER NOT NULL DEFAULT 1,
    default_wh INTEGER
);
CREATE TABLE IF NOT EXISTS warehouses(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL COLLATE NOCASEU UNIQUE,
    feed_chat_id    INTEGER,
    feed_chat_title TEXT
);
CREATE TABLE IF NOT EXISTS access(
    user_id      INTEGER NOT NULL,
    warehouse_id INTEGER NOT NULL,
    UNIQUE(user_id, warehouse_id)
);
CREATE TABLE IF NOT EXISTS stock(
    warehouse_id INTEGER NOT NULL,
    product_id   INTEGER NOT NULL,
    qty          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(warehouse_id, product_id)
);
CREATE TABLE IF NOT EXISTS clients(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse_id INTEGER NOT NULL,
    name         TEXT NOT NULL COLLATE NOCASEU,
    debt         REAL NOT NULL DEFAULT 0,
    phone        TEXT,
    UNIQUE(warehouse_id, name)
);
CREATE TABLE IF NOT EXISTS client_aliases(
    client_id INTEGER NOT NULL,
    alias     TEXT NOT NULL COLLATE NOCASEU,
    UNIQUE(client_id, alias)
);
CREATE TABLE IF NOT EXISTS client_prices(
    client_id  INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    price      REAL NOT NULL,
    UNIQUE(client_id, product_id)
);
CREATE TABLE IF NOT EXISTS min_stock(
    warehouse_id INTEGER NOT NULL,
    product_id   INTEGER NOT NULL,
    min_qty      INTEGER NOT NULL,
    UNIQUE(warehouse_id, product_id)
);
CREATE TABLE IF NOT EXISTS api_usage(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read    INTEGER NOT NULL DEFAULT 0,
    cache_write   INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS job_log(
    job   TEXT NOT NULL,   -- рассылка + дата, например "evening:2026-08-02"
    ts    TEXT NOT NULL,
    UNIQUE(job)
);
CREATE TABLE IF NOT EXISTS products(
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    volume TEXT NOT NULL,
    box    INTEGER NOT NULL,
    price  REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS price_history(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    old_price  REAL NOT NULL,
    new_price  REAL NOT NULL,
    user_id    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS product_expiry(
    warehouse_id INTEGER NOT NULL,
    product_id   INTEGER NOT NULL,
    expiry       TEXT NOT NULL,              -- "MM.YYYY"
    UNIQUE(warehouse_id, product_id)
);
CREATE TABLE IF NOT EXISTS product_batches(
    warehouse_id INTEGER NOT NULL,
    product_id   INTEGER NOT NULL,
    expiry       TEXT NOT NULL DEFAULT '',   -- "MM.YYYY", '' = срок неизвестен
    qty          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(warehouse_id, product_id, expiry)
);
CREATE TABLE IF NOT EXISTS pending_requests(
    token   TEXT PRIMARY KEY,   -- зеркало заявок-карточек: переживают деплой
    payload TEXT NOT NULL,      -- JSON заявки
    expires REAL NOT NULL       -- wall-clock, time.time()
);
CREATE TABLE IF NOT EXISTS product_certs(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name TEXT NOT NULL COLLATE NOCASEU,  -- препарат (без фасовки)
    file_id      TEXT NOT NULL,                  -- файл на серверах Telegram
    file_kind    TEXT NOT NULL DEFAULT 'document',  -- document | photo
    file_name    TEXT,
    note         TEXT,                           -- напр. «поставка 07.2026»
    ts           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_certs_name ON product_certs(product_name);
CREATE TABLE IF NOT EXISTS product_lots(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    expiry     TEXT NOT NULL,                    -- MM.YYYY, как в партиях
    lot        TEXT NOT NULL COLLATE NOCASEU,    -- номер серии/партии завода
    note       TEXT,                             -- напр. «приход №203, F260403»
    ts         TEXT NOT NULL,
    UNIQUE(product_id, expiry, lot)
);
CREATE TABLE IF NOT EXISTS promises(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    client   TEXT NOT NULL,
    amount   REAL NOT NULL DEFAULT 0,
    due_date TEXT NOT NULL,                  -- YYYY-MM-DD
    user_id  INTEGER NOT NULL,
    status   TEXT NOT NULL DEFAULT 'open'    -- open | done
);
CREATE TABLE IF NOT EXISTS draft_log(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    client  TEXT NOT NULL,
    total   REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS operations(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    user_id      INTEGER NOT NULL,
    type         TEXT NOT NULL,               -- invoice | payment | transfer
    warehouse_id INTEGER,
    client_id    INTEGER,
    summary      TEXT NOT NULL,
    data         TEXT NOT NULL,               -- JSON: дельты и детали
    status       TEXT NOT NULL DEFAULT 'done' -- done | cancelled
);
CREATE INDEX IF NOT EXISTS idx_operations_client ON operations(client_id);
CREATE INDEX IF NOT EXISTS idx_operations_user   ON operations(user_id);
CREATE INDEX IF NOT EXISTS idx_operations_ts     ON operations(ts);
CREATE INDEX IF NOT EXISTS idx_draft_log_ts      ON draft_log(ts);
"""


def _columns(conn, table: str):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _migrate_owner_warehouses(conn):
    """Старая схема: склад на сотрудника (owner_id). Новая: склады отдельно,
    у сотрудника default_wh. Если данных ещё нет — старые склады выбрасываем."""
    if "owner_id" not in _columns(conn, "warehouses"):
        return
    has_data = conn.execute(
        "SELECT (SELECT COUNT(*) FROM stock) + (SELECT COUNT(*) FROM clients) "
        "+ (SELECT COUNT(*) FROM operations)"
    ).fetchone()[0]
    if has_data:
        conn.execute(
            "CREATE TABLE warehouses_new("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL COLLATE NOCASEU UNIQUE, "
            "feed_chat_id INTEGER, feed_chat_title TEXT)")
        for w in conn.execute("SELECT * FROM warehouses").fetchall():
            conn.execute("INSERT INTO warehouses_new(id, name) VALUES(?,?)",
                         (w["id"], w["name"]))
            conn.execute("UPDATE users SET default_wh=? WHERE id=? AND default_wh IS NULL",
                         (w["id"], w["owner_id"]))
        conn.execute("DROP TABLE warehouses")
        conn.execute("ALTER TABLE warehouses_new RENAME TO warehouses")
        log.info("Миграция складов: owner-модель -> default_wh (данные сохранены)")
    else:
        conn.execute("DROP TABLE warehouses")
        conn.execute("DELETE FROM access")
        conn.execute("UPDATE users SET default_wh=NULL")
        conn.execute(
            "CREATE TABLE warehouses("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL COLLATE NOCASEU UNIQUE, "
            "feed_chat_id INTEGER, feed_chat_title TEXT)")
        log.info("Миграция складов: пустая база, склады пересозданы")


def _migrate_price_order(conn):
    """Перенумерация прайса под фирменный порядок владельца (прайс от 10.06.2026).

    ГАБИВИТ/КАЛЬФОТОН/СУРФАГОН были в конце (№101–104), в настоящем прайсе они
    идут сразу после БУТАСТИМА (№58–61), остальное сдвигается на +4. Номера
    меняются во всех таблицах и в журнале операций, чтобы /undo и отчёты
    продолжали работать. Выполняется один раз (флаг в settings)."""
    if conn.execute("SELECT 1 FROM settings WHERE key='price_order_v2'").fetchone():
        return
    from prices import RENUMBER_2026_07
    row = conn.execute("SELECT name FROM products WHERE id=101").fetchone()
    if row and row["name"].startswith("ГАБИВИТ"):
        tables = (("products", "id"), ("stock", "product_id"),
                  ("client_prices", "product_id"), ("min_stock", "product_id"),
                  ("price_history", "product_id"), ("product_expiry", "product_id"))
        for table, col in tables:
            # Двухфазно через +1000, чтобы старые и новые номера не столкнулись.
            conn.execute(f"UPDATE {table} SET {col} = {col} + 1000 "
                         f"WHERE {col} BETWEEN 58 AND 104")
            for old, new in RENUMBER_2026_07.items():
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?",
                             (new, old + 1000))
        for op in conn.execute("SELECT id, data FROM operations").fetchall():
            try:
                data = json.loads(op["data"])
            except (TypeError, ValueError):
                continue
            deltas = data.get("stock_deltas")
            if not deltas:
                continue
            data["stock_deltas"] = [
                [wh, RENUMBER_2026_07.get(pid, pid), d] for wh, pid, d in deltas]
            conn.execute("UPDATE operations SET data=? WHERE id=?",
                         (json.dumps(data, ensure_ascii=False), op["id"]))
    conn.execute("INSERT OR REPLACE INTO settings(key, value) "
                 "VALUES('price_order_v2', '1')")


def _migrate_price_items(conn):
    """Дополнение перенумерации 21.07.2026: _migrate_price_order правил в
    журнале только stock_deltas, а items[*].product_id оставался со СТАРЫМИ
    номерами — /margin считал бы себестоимость старых операций по чужому
    товару (аудит 02.08.2026). Чиним по имени+фасовке (они при перенумерации
    не менялись): product_id позиции приводится к номеру товара из products.
    Операциям после перенумерации ничего не меняет (номера уже совпадают)."""
    if conn.execute("SELECT 1 FROM settings WHERE key='price_order_v2_items'"
                    ).fetchone():
        return
    name_map = {(r["name"].lower(), r["volume"].lower()): r["id"]
                for r in conn.execute("SELECT id, name, volume FROM products")}
    for op in conn.execute("SELECT id, data FROM operations").fetchall():
        try:
            data = json.loads(op["data"])
        except (TypeError, ValueError):
            continue
        changed = False
        for it in data.get("items") or []:
            key = (str(it.get("name") or "").lower(),
                   str(it.get("volume") or "").lower())
            pid = name_map.get(key)
            if pid and it.get("product_id") and it["product_id"] != pid:
                it["product_id"] = pid
                changed = True
        if changed:
            conn.execute("UPDATE operations SET data=? WHERE id=?",
                         (json.dumps(data, ensure_ascii=False), op["id"]))
    conn.execute("INSERT OR REPLACE INTO settings(key, value) "
                 "VALUES('price_order_v2_items', '1')")


def _migrate_batches(conn):
    """Одноразово заводит партии для складов, загруженных до партионного
    учёта: каждый остаток становится одной партией со сроком из
    product_expiry (или «без срока»). Дальше партии живут при операциях."""
    if conn.execute("SELECT value FROM settings WHERE key='batches_v1'").fetchone():
        return
    for s in conn.execute(
            "SELECT warehouse_id, product_id, qty FROM stock WHERE qty != 0"):
        exp = conn.execute(
            "SELECT expiry FROM product_expiry WHERE warehouse_id=? AND product_id=?",
            (s["warehouse_id"], s["product_id"])).fetchone()
        conn.execute(
            "INSERT INTO product_batches(warehouse_id, product_id, expiry, qty) "
            "VALUES(?,?,?,?) ON CONFLICT(warehouse_id, product_id, expiry) "
            "DO UPDATE SET qty = excluded.qty",
            (s["warehouse_id"], s["product_id"],
             exp["expiry"] if exp else "", s["qty"]))
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('batches_v1', '1')")


def init(admin_id: int, warehouse_names: list, staff: dict):
    """Создаёт схему, склады и постоянных сотрудников.

    staff: {telegram_id: {"name": str, "role": опц., "warehouse": имя склада,
                          "access": [имена складов]}}
    """
    conn = connect()
    with _lock, conn:
        conn.executescript(SCHEMA)
        if "default_wh" not in _columns(conn, "users"):
            conn.execute("ALTER TABLE users ADD COLUMN default_wh INTEGER")
        if "phone" not in _columns(conn, "clients"):
            conn.execute("ALTER TABLE clients ADD COLUMN phone TEXT")
        if "items" not in _columns(conn, "draft_log"):
            conn.execute("ALTER TABLE draft_log ADD COLUMN items TEXT")
        if "full_mode" not in _columns(conn, "warehouses"):
            conn.execute("ALTER TABLE warehouses ADD COLUMN full_mode INTEGER NOT NULL DEFAULT 0")
        if "view_only" not in _columns(conn, "access"):
            # Доступ-просмотр (03.08.2026): сотрудник видит отчёты склада,
            # но операции проводить не может (Бека проверяет чужие склады).
            conn.execute("ALTER TABLE access ADD COLUMN view_only "
                         "INTEGER NOT NULL DEFAULT 0")
        if "buy_usd" not in _columns(conn, "products"):
            # Закупочная цена в долларах (видна только админу в /margin и
            # /stockcost); NULL — закуп неизвестен.
            conn.execute("ALTER TABLE products ADD COLUMN buy_usd REAL")
        if "feed_chat_id" not in _columns(conn, "warehouses") and \
           "owner_id" not in _columns(conn, "warehouses"):
            conn.execute("ALTER TABLE warehouses ADD COLUMN feed_chat_id INTEGER")
            conn.execute("ALTER TABLE warehouses ADD COLUMN feed_chat_title TEXT")
        _migrate_owner_warehouses(conn)
        _migrate_price_order(conn)
        _migrate_price_items(conn)
        _migrate_batches(conn)
        # Починка сроков с «двухцифровым» годом из Excel (04.30 -> 1930):
        # настоящих сроков в 19xx не бывает, безопасно переносим в 20xx.
        for table in ("product_batches", "product_expiry"):
            conn.execute(
                f"UPDATE {table} SET expiry = substr(expiry, 1, 3) || '20' || "
                f"substr(expiry, 6) WHERE substr(expiry, 4, 2) = '19'")

        for wname in warehouse_names:
            if conn.execute("SELECT 1 FROM warehouses WHERE name=?", (wname,)).fetchone() is None:
                conn.execute("INSERT INTO warehouses(name) VALUES(?)", (wname,))

        for uid, cfg in staff.items():
            name = cfg["name"]
            role = "admin" if uid == admin_id else cfg.get("role", "employee")
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            created = row is None
            if created:
                conn.execute("INSERT INTO users(id, name, role, active) VALUES(?,?,?,1)",
                             (uid, name, role))
            # Существующая запись НЕ трогается: раньше каждый деплой ставил
            # active=1 и возвращал доступы из STAFF — команды /remove и
            # /noaccess молча откатывались при следующем обновлении бота
            # (всплыло 03.08.2026 на отпуске Жуми). Конфиг STAFF применяется
            # только при первом создании; дальше рулят команды админа.
            if uid == admin_id:
                conn.execute("UPDATE users SET role='admin', active=1 WHERE id=?",
                             (uid,))
            wh = conn.execute("SELECT id FROM warehouses WHERE name=?",
                              (cfg.get("warehouse", ""),)).fetchone()
            if wh:
                conn.execute("UPDATE users SET default_wh=? WHERE id=? AND default_wh IS NULL",
                             (wh["id"], uid))
            if created:
                for aname in cfg.get("access", []):
                    aw = conn.execute("SELECT id FROM warehouses WHERE name=?",
                                      (aname,)).fetchone()
                    if aw:
                        conn.execute("INSERT OR IGNORE INTO access(user_id, warehouse_id) "
                                     "VALUES(?,?)", (uid, aw["id"]))


def set_setting(key: str, value: str):
    conn = connect()
    with _lock, conn:
        conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_setting(key: str):
    row = connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def claim_daily_job(job_key: str) -> bool:
    """Атомарная заявка «эту рассылку шлю я». При перекрытии деплоев два
    экземпляра бота живут одновременно и оба доходят до часа отправки —
    сводки задваивались. База у них одна (Volume), поэтому первый, кто
    вставил ключ (рассылка+дата), отправляет; второму вернётся False."""
    conn = connect()
    with _lock, conn:
        try:
            conn.execute("INSERT INTO job_log(job, ts) VALUES(?,?)",
                         (job_key,
                          datetime.now(BISHKEK).isoformat(timespec="seconds")))
            return True
        except sqlite3.IntegrityError:
            return False


def record_api_usage(model: str, input_tokens: int, output_tokens: int,
                     cache_read: int, cache_write: int, cost_usd: float):
    conn = connect()
    with _lock, conn:
        conn.execute(
            "INSERT INTO api_usage(ts, model, input_tokens, output_tokens, "
            "cache_read, cache_write, cost_usd) VALUES(?,?,?,?,?,?,?)",
            (datetime.now(BISHKEK).isoformat(timespec="seconds"), model,
             input_tokens, output_tokens, cache_read, cache_write, cost_usd))


def api_usage_since(start_iso: str):
    """(запросов, суммарная стоимость $) начиная с даты."""
    row = connect().execute(
        "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE ts >= ?",
        (start_iso,)).fetchone()
    return row[0], row[1]


def api_cache_stats(start_iso: str):
    """Суммы входных токенов с даты: (без кэша, чтение кэша, запись кэша).
    Нужны /api для строки о здоровье кэша промпта."""
    row = connect().execute(
        "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(cache_read), 0), "
        "COALESCE(SUM(cache_write), 0) FROM api_usage WHERE ts >= ?",
        (start_iso,)).fetchone()
    return row[0], row[1], row[2]


def seed_products(seed: list):
    """Первое заполнение прайса из кода; дальше прайс живёт в базе."""
    conn = connect()
    with _lock, conn:
        if conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            return False
        conn.executemany(
            "INSERT INTO products(id, name, volume, box, price) VALUES(?,?,?,?,?)",
            [(p["id"], p["name"], p["volume"], p["box"], p["price"]) for p in seed])
        return True


def products_active():
    """Актуальный прайс: [{id, name, volume, box, price}] в порядке номеров."""
    rows = connect().execute(
        "SELECT id, name, volume, box, price FROM products WHERE active=1 ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def seed_buy_prices(seed: dict, initial_rate: str,
                    flag: str = "buy_prices_f260403"):
    """Одноразовое заселение закупочных цен из инвойса завода (свой флаг
    в settings на каждый инвойс). Вызывается ПОСЛЕ seed_products — на
    пустом прайсе просто подождёт следующего старта. Курс сидируется,
    только если не задан."""
    conn = connect()
    with _lock, conn:
        conn.execute("INSERT OR IGNORE INTO settings(key, value) "
                     "VALUES('usd_rate', ?)", (initial_rate,))
        if conn.execute("SELECT 1 FROM settings WHERE key=?",
                        (flag,)).fetchone():
            return False
        if conn.execute("SELECT 1 FROM products LIMIT 1").fetchone() is None:
            return False
        for pid, usd in seed.items():
            # Не затираем цену, которую админ уже задал вручную.
            conn.execute("UPDATE products SET buy_usd=? WHERE id=? "
                         "AND buy_usd IS NULL", (usd, pid))
        conn.execute("INSERT OR REPLACE INTO settings(key, value) "
                     "VALUES(?, '1')", (flag,))
        return True


def products_buy_map():
    """{product_id: закупочная цена USD} — только товары с известным закупом."""
    rows = connect().execute(
        "SELECT id, buy_usd FROM products WHERE buy_usd IS NOT NULL").fetchall()
    return {r["id"]: r["buy_usd"] for r in rows}


def product_set_buy(product_id: int, usd: float):
    conn = connect()
    with _lock, conn:
        if conn.execute("SELECT 1 FROM products WHERE id=?",
                        (product_id,)).fetchone() is None:
            raise ValueError(f"товар №{product_id} не найден")
        conn.execute("UPDATE products SET buy_usd=? WHERE id=?",
                     (usd, product_id))


def product_set_price(product_id: int, new_price: float, user_id: int):
    """Меняет цену товара, пишет историю. Возвращает старую цену."""
    conn = connect()
    with _lock, conn:
        row = conn.execute("SELECT price FROM products WHERE id=?", (product_id,)).fetchone()
        if row is None:
            raise ValueError(f"товар №{product_id} не найден")
        old = row["price"]
        conn.execute("UPDATE products SET price=? WHERE id=?", (new_price, product_id))
        conn.execute(
            "INSERT INTO price_history(ts, product_id, old_price, new_price, user_id) "
            "VALUES(?,?,?,?,?)",
            (datetime.now(BISHKEK).isoformat(timespec="seconds"),
             product_id, old, new_price, user_id))
        return old


def price_history_recent(limit: int = 15):
    """Последние изменения цен: [(дата, товар, фасовка, было, стало, кто)]."""
    return connect().execute(
        "SELECT h.ts, p.name, p.volume, h.old_price, h.new_price, "
        "COALESCE(u.name, 'ID ' || h.user_id) AS user_name "
        "FROM price_history h "
        "JOIN products p ON p.id = h.product_id "
        "LEFT JOIN users u ON u.id = h.user_id "
        "ORDER BY h.id DESC LIMIT ?", (limit,)).fetchall()


def promise_add(client: str, amount: float, due_date: str, user_id: int):
    conn = connect()
    with _lock, conn:
        cur = conn.execute(
            "INSERT INTO promises(ts, client, amount, due_date, user_id) VALUES(?,?,?,?,?)",
            (datetime.now(BISHKEK).isoformat(timespec="seconds"),
             client, amount, due_date, user_id))
        return cur.lastrowid


def promises_open(user_id: int | None = None):
    """Открытые обещания (все или записанные одним сотрудником), ближайшие первыми."""
    sql = ("SELECT p.*, COALESCE(u.name, 'ID ' || p.user_id) AS user_name "
           "FROM promises p LEFT JOIN users u ON u.id = p.user_id "
           "WHERE p.status = 'open'")
    args = []
    if user_id is not None:
        sql += " AND p.user_id = ?"
        args.append(user_id)
    sql += " ORDER BY p.due_date, p.id"
    return connect().execute(sql, args).fetchall()


def promises_due(today: str):
    """Открытые обещания со сроком сегодня или раньше (просроченные)."""
    return connect().execute(
        "SELECT p.*, COALESCE(u.name, 'ID ' || p.user_id) AS user_name "
        "FROM promises p LEFT JOIN users u ON u.id = p.user_id "
        "WHERE p.status = 'open' AND p.due_date <= ? ORDER BY p.due_date, p.id",
        (today,)).fetchall()


def promises_close(client: str, user_id=None):
    """Закрывает открытые обещания клиента. user_id — только записанные этим
    сотрудником (None — все, для админа). Возвращает, сколько закрыто."""
    conn = connect()
    with _lock, conn:
        if user_id is None:
            cur = conn.execute(
                "UPDATE promises SET status='done' "
                "WHERE status='open' AND client = ? COLLATE NOCASEU", (client,))
        else:
            cur = conn.execute(
                "UPDATE promises SET status='done' "
                "WHERE status='open' AND client = ? COLLATE NOCASEU "
                "AND user_id = ?", (client, user_id))
        return cur.rowcount


def clients_add_bulk(wh_id: int, entries: list):
    """Создаёт клиентов справочником. entries: [(имя, долг[, телефон])] или [имя].
    Существующие пропускаются (долг им НЕ меняется). Возвращает (добавлены, пропущены)."""
    conn = connect()
    added, skipped = [], []
    with _lock, conn:
        for raw in entries:
            phone = None
            if isinstance(raw, (list, tuple)):
                name, debt = str(raw[0] or "").strip(), float(raw[1] or 0)
                if len(raw) > 2 and raw[2]:
                    phone = str(raw[2]).strip() or None
            else:
                name, debt = str(raw or "").strip(), 0.0
            if not name:
                continue
            row = conn.execute(
                "SELECT 1 FROM clients WHERE warehouse_id=? AND name=?",
                (wh_id, name)).fetchone()
            if row:
                skipped.append(name)
                continue
            conn.execute(
                "INSERT INTO clients(warehouse_id, name, debt, phone) VALUES(?,?,?,?)",
                (wh_id, name, debt, phone))
            added.append(name)
    return added, skipped


def set_warehouse_full(wh_id: int, on: bool):
    conn = connect()
    with _lock, conn:
        conn.execute("UPDATE warehouses SET full_mode=? WHERE id=?",
                     (1 if on else 0, wh_id))


def set_product_expiry(wh_id: int, product_id: int, expiry: str):
    conn = connect()
    with _lock, conn:
        conn.execute(
            "INSERT INTO product_expiry(warehouse_id, product_id, expiry) VALUES(?,?,?) "
            "ON CONFLICT(warehouse_id, product_id) DO UPDATE SET expiry=excluded.expiry",
            (wh_id, product_id, expiry))


def expiry_list(wh_id: int):
    """Сроки годности склада, ближайшие сначала. Если склад ведётся партиями
    (product_batches), каждая партия — своя строка; иначе — старый режим
    «один срок на товар» (product_expiry × stock)."""
    conn = connect()
    # Признак «склад на партиях» — ЛЮБЫЕ партии (в т.ч. «без срока»), иначе
    # после распродажи датированных партий показывались бы фантомные сроки
    # из легаси-таблицы product_expiry.
    has_batches = conn.execute(
        "SELECT 1 FROM product_batches WHERE warehouse_id=? AND qty != 0 "
        "LIMIT 1", (wh_id,)).fetchone()
    if has_batches:
        return conn.execute(
            "SELECT product_id, expiry, qty FROM product_batches "
            "WHERE warehouse_id=? AND qty > 0 AND expiry != '' "
            "ORDER BY substr(expiry, 4) || substr(expiry, 1, 2)",
            (wh_id,)).fetchall()
    return conn.execute(
        "SELECT e.product_id, e.expiry, COALESCE(s.qty, 0) AS qty "
        "FROM product_expiry e "
        "LEFT JOIN stock s ON s.warehouse_id = e.warehouse_id "
        "AND s.product_id = e.product_id "
        "WHERE e.warehouse_id = ? "
        "ORDER BY substr(e.expiry, 4) || substr(e.expiry, 1, 2)",
        (wh_id,)).fetchall()


def known_client_names(limit: int = 40, wh_ids: list = None):
    """Имена клиентов для подсказок. wh_ids — только клиенты этих складов
    (сотруднику в промпт не даём имена чужих складов); None — вся база.
    СПРАВОЧНИК — первым (с 14.08.2026): все склады загружены, справочник —
    источник истины; старые имена из черновиков идут дополнением в конце
    (раньше было наоборот, и ИИ разворачивал «Нурлан» в черновичное
    «Нурлан байке ит базар», которого нет в справочнике)."""
    conn = connect()
    seen, out = set(), []
    if wh_ids is None:
        clients_sql = "SELECT name FROM clients ORDER BY name"
        alias_sql = "SELECT alias FROM client_aliases ORDER BY alias"
        params = ()
    else:
        if not wh_ids:
            return []
        marks = ",".join("?" * len(wh_ids))
        clients_sql = (f"SELECT name FROM clients WHERE warehouse_id IN ({marks}) "
                       "ORDER BY name")
        alias_sql = (f"SELECT a.alias FROM client_aliases a "
                     f"JOIN clients c ON c.id = a.client_id "
                     f"WHERE c.warehouse_id IN ({marks}) ORDER BY a.alias")
        params = tuple(wh_ids)
    for r in conn.execute(clients_sql, params):
        k = r["name"].lower()
        if k not in seen:
            seen.add(k)
            out.append(r["name"])
    # Псевдонимы — их сотрудники и произносят («Виктория» вместо «Вика Уманец»)
    for r in conn.execute(alias_sql, params):
        k = r["alias"].lower()
        if k not in seen:
            seen.add(k)
            out.append(r["alias"])
    if wh_ids is None and len(out) < limit:
        # Дополнение: ходовые имена из черновиков, которых нет в справочнике
        # (речевые варианты) — в конец, чтобы не спорили со справочником.
        # Черновичное имя, содержащее имя справочника целиком («Нурлан байке
        # ит базар» ⊃ «Нурлан»), пропускаем: ИИ разворачивал короткое имя
        # в несуществующее длинное (инцидент 14.08.2026).
        known_words = [set(k.split()) for k in seen]
        for r in conn.execute(
                "SELECT client, COUNT(*) AS n FROM draft_log "
                "GROUP BY client COLLATE NOCASEU ORDER BY n DESC, MAX(id) DESC"):
            k = r["client"].lower()
            kw = set(k.split())
            if k not in seen and not any(w and w < kw for w in known_words):
                seen.add(k)
                out.append(r["client"])
    return out[:limit]


def lot_add(product_id: int, expiry: str, lot: str, note: str = None):
    """Справочник заводских серий: серия партии (товар + срок). Не учёт —
    только сведения для /expiry и документов (шаг 1 партионного учёта
    серий, «делай» владельца 14.08.2026). Дубликаты молча пропускаются."""
    lot = str(lot or "").strip()
    if not lot:
        return
    conn = connect()
    with _lock, conn:
        conn.execute(
            "INSERT OR IGNORE INTO product_lots(product_id, expiry, lot, note, ts) "
            "VALUES(?,?,?,?,?)",
            (product_id, expiry, lot, note,
             datetime.now(BISHKEK).isoformat(timespec="seconds")))


def lots_map() -> dict:
    """{(product_id, expiry): 'серия1, серия2'} — для показа в отчётах."""
    out = {}
    for r in connect().execute(
            "SELECT product_id, expiry, lot FROM product_lots ORDER BY id"):
        key = (r["product_id"], r["expiry"])
        out[key] = (out[key] + ", " + r["lot"]) if key in out else r["lot"]
    return out


def log_draft(user_id: int, client: str, total: float, items: list | None = None):
    """Журнал черновиков (переходный период): учёт не трогает, только статистика."""
    conn = connect()
    with _lock, conn:
        conn.execute(
            "INSERT INTO draft_log(ts, user_id, client, total, items) VALUES(?,?,?,?,?)",
            (datetime.now(BISHKEK).isoformat(timespec="seconds"),
             user_id, client, total,
             json.dumps(items, ensure_ascii=False) if items else None))


def drafts_with_items_since(start_iso: str):
    """Черновики с составом (для аналитики переходного периода)."""
    return connect().execute(
        "SELECT client, total, items FROM draft_log WHERE ts >= ?",
        (start_iso,)).fetchall()


def drafts_since(start_iso: str):
    """[(имя сотрудника, штук, сумма)] по черновикам начиная с даты."""
    return connect().execute(
        "SELECT COALESCE(u.name, 'ID ' || d.user_id) AS name, COUNT(*) AS n, "
        "COALESCE(SUM(d.total), 0) AS total "
        "FROM draft_log d LEFT JOIN users u ON u.id = d.user_id "
        "WHERE d.ts >= ? GROUP BY d.user_id ORDER BY total DESC",
        (start_iso,)).fetchall()


def backup_to(path: str):
    """Целостная копия базы в файл (безопасно при WAL-режиме)."""
    src = connect()
    with _lock:
        dst = sqlite3.connect(path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()


# ---------- Пользователи ----------

def get_user(uid: int):
    return connect().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def user_by_ref(ref: str):
    """Находит активного пользователя по ID или имени."""
    conn = connect()
    ref = ref.strip()
    if ref.isdigit():
        return conn.execute(
            "SELECT * FROM users WHERE id=? AND active=1", (int(ref),)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM users WHERE name=? COLLATE NOCASEU AND active=1", (ref,)
    ).fetchone()


def add_user(uid: int, name: str):
    conn = connect()
    with _lock, conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO users(id, name, role, active) VALUES(?,?, 'employee', 1)",
                         (uid, name))
        else:
            conn.execute("UPDATE users SET active=1, name=? WHERE id=?", (name, uid))


def deactivate_user(uid: int):
    conn = connect()
    with _lock, conn:
        conn.execute("UPDATE users SET active=0 WHERE id=?", (uid,))


def list_users(active_only: bool = True):
    q = "SELECT * FROM users"
    if active_only:
        q += " WHERE active=1"
    return connect().execute(q + " ORDER BY name").fetchall()


def set_role(uid: int, role: str):
    conn = connect()
    with _lock, conn:
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))


def set_default_warehouse(uid: int, wh_id: int):
    conn = connect()
    with _lock, conn:
        conn.execute("UPDATE users SET default_wh=? WHERE id=?", (wh_id, uid))


# ---------- Склады и доступ ----------

def warehouse_of(uid: int):
    """Склад по умолчанию сотрудника."""
    return connect().execute(
        "SELECT w.* FROM users u JOIN warehouses w ON w.id=u.default_wh WHERE u.id=?",
        (uid,),
    ).fetchone()


def warehouse_by_id(wh_id: int):
    return connect().execute("SELECT * FROM warehouses WHERE id=?", (wh_id,)).fetchone()


def warehouse_by_name(name: str):
    """Склад по имени; дефисы/пробелы не важны («Карабалта» = «Кара-Балта»)."""
    row = connect().execute(
        "SELECT * FROM warehouses WHERE name=?", (name.strip(),)
    ).fetchone()
    if row is not None:
        return row
    key = re.sub(r"[\s\-–—]+", "", name).lower()
    if not key:
        return None
    whs = all_warehouses()
    norm = {re.sub(r"[\s\-–—]+", "", w["name"]).lower(): w for w in whs}
    if key in norm:
        return norm[key]
    # Опечатка («Манач» → Манас, «Каракл» → Каракол): единственный близкий
    # кандидат принимается (просьба владельца 09.08.2026). Двусмысленность
    # или совсем непохожее — None, как раньше.
    close = difflib.get_close_matches(key, list(norm), n=2, cutoff=0.75)
    if len(close) == 1:
        return norm[close[0]]
    return None


def all_warehouses():
    return connect().execute("SELECT * FROM warehouses ORDER BY name").fetchall()


def create_warehouse(name: str):
    conn = connect()
    with _lock, conn:
        try:
            cur = conn.execute("INSERT INTO warehouses(name) VALUES(?)", (name.strip(),))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def rename_warehouse(wh_id: int, new_name: str) -> bool:
    conn = connect()
    with _lock, conn:
        try:
            conn.execute("UPDATE warehouses SET name=? WHERE id=?", (new_name.strip(), wh_id))
            return True
        except sqlite3.IntegrityError:
            return False


def set_feed_chat(wh_id: int, chat_id, chat_title=None):
    conn = connect()
    with _lock, conn:
        conn.execute("UPDATE warehouses SET feed_chat_id=?, feed_chat_title=? WHERE id=?",
                     (chat_id, chat_title, wh_id))


def migrate_feed_chat(old_chat_id: int, new_chat_id: int):
    """Группа Telegram «переехала» в супергруппу — у чата сменился id.
    Переносит привязку ленты на новый id. Возвращает имена складов."""
    conn = connect()
    with _lock, conn:
        rows = conn.execute(
            "SELECT name FROM warehouses WHERE feed_chat_id=?",
            (old_chat_id,)).fetchall()
        if rows:
            conn.execute("UPDATE warehouses SET feed_chat_id=? WHERE feed_chat_id=?",
                         (new_chat_id, old_chat_id))
    return [r["name"] for r in rows]


def unlink_feed_chat(chat_id: int):
    """Отвязывает все склады от данного чата. Возвращает имена отвязанных."""
    conn = connect()
    with _lock, conn:
        rows = conn.execute("SELECT name FROM warehouses WHERE feed_chat_id=?",
                            (chat_id,)).fetchall()
        conn.execute("UPDATE warehouses SET feed_chat_id=NULL, feed_chat_title=NULL "
                     "WHERE feed_chat_id=?", (chat_id,))
        return [r["name"] for r in rows]


def warehouses_of_feed(chat_id: int):
    return connect().execute(
        "SELECT * FROM warehouses WHERE feed_chat_id=? ORDER BY name", (chat_id,)
    ).fetchall()


def grant_access(uid: int, wh_id: int, view_only: bool = False):
    """Полный доступ или просмотр; повторный вызов меняет тип доступа."""
    conn = connect()
    with _lock, conn:
        conn.execute("INSERT OR IGNORE INTO access(user_id, warehouse_id) VALUES(?,?)",
                     (uid, wh_id))
        conn.execute("UPDATE access SET view_only=? WHERE user_id=? AND warehouse_id=?",
                     (1 if view_only else 0, uid, wh_id))


def revoke_access(uid: int, wh_id: int):
    conn = connect()
    with _lock, conn:
        conn.execute("DELETE FROM access WHERE user_id=? AND warehouse_id=?", (uid, wh_id))


def access_warehouses(uid: int, include_view: bool = True):
    """Выданные склады; include_view=False — только с правом операций."""
    q = ("SELECT w.*, a.view_only FROM access a "
         "JOIN warehouses w ON w.id=a.warehouse_id WHERE a.user_id=?")
    if not include_view:
        q += " AND a.view_only=0"
    return connect().execute(q + " ORDER BY w.name", (uid,)).fetchall()


def visible_warehouses(user_row, include_view: bool = True):
    """Склады, которые пользователь ВИДИТ в отчётах: свой + выданные,
    включая доступ-просмотр (админ — все)."""
    if user_row["role"] == "admin":
        return all_warehouses()
    result, seen = [], set()
    own = warehouse_of(user_row["id"])
    if own:
        result.append(own)
        seen.add(own["id"])
    for w in access_warehouses(user_row["id"], include_view=include_view):
        if w["id"] not in seen:
            result.append(w)
            seen.add(w["id"])
    return result


def operable_warehouses(user_row):
    """Склады, где пользователь может ПРОВОДИТЬ операции: свой + полные
    доступы; склады-просмотры сюда не входят."""
    return visible_warehouses(user_row, include_view=False)


def can_use_warehouse(user_row, wh_id: int) -> bool:
    """Право ПРОВОДИТЬ операции на складе (просмотр не считается)."""
    return any(w["id"] == wh_id for w in operable_warehouses(user_row))


def can_view_warehouse(user_row, wh_id: int) -> bool:
    """Право ВИДЕТЬ отчёты склада (полный доступ или просмотр)."""
    return any(w["id"] == wh_id for w in visible_warehouses(user_row))


# ---------- Остатки ----------

def stock_qty(wh_id: int, product_id: int) -> int:
    row = connect().execute(
        "SELECT qty FROM stock WHERE warehouse_id=? AND product_id=?", (wh_id, product_id)
    ).fetchone()
    return row["qty"] if row else 0


def stock_map(wh_id: int) -> dict:
    rows = connect().execute("SELECT product_id, qty FROM stock WHERE warehouse_id=?",
                             (wh_id,)).fetchall()
    return {r["product_id"]: r["qty"] for r in rows}


def _apply_stock(conn, wh_id: int, product_id: int, delta: int):
    conn.execute(
        "INSERT INTO stock(warehouse_id, product_id, qty) VALUES(?,?,?) "
        "ON CONFLICT(warehouse_id, product_id) DO UPDATE SET qty = qty + excluded.qty",
        (wh_id, product_id, delta),
    )


def set_min_stock(wh_id: int, product_id: int, min_qty: int):
    """Порог «заканчивается»; 0 или меньше — убрать порог."""
    conn = connect()
    with _lock, conn:
        if min_qty <= 0:
            conn.execute("DELETE FROM min_stock WHERE warehouse_id=? AND product_id=?",
                         (wh_id, product_id))
        else:
            conn.execute(
                "INSERT INTO min_stock(warehouse_id, product_id, min_qty) VALUES(?,?,?) "
                "ON CONFLICT(warehouse_id, product_id) DO UPDATE SET min_qty=excluded.min_qty",
                (wh_id, product_id, min_qty))


def min_stock_get(wh_id: int, product_id: int):
    row = connect().execute(
        "SELECT min_qty FROM min_stock WHERE warehouse_id=? AND product_id=?",
        (wh_id, product_id)).fetchone()
    return row["min_qty"] if row else None


def min_stock_map(wh_id: int) -> dict:
    rows = connect().execute(
        "SELECT product_id, min_qty FROM min_stock WHERE warehouse_id=?", (wh_id,)).fetchall()
    return {r["product_id"]: r["min_qty"] for r in rows}


# ---------- Клиенты ----------

def clients_of(wh_id: int):
    return connect().execute(
        "SELECT * FROM clients WHERE warehouse_id=? ORDER BY name", (wh_id,)
    ).fetchall()


def client_get(cid: int):
    return connect().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()


def _name_key(s: str) -> str:
    """Имя как набор слов: «Чооров Кубан» и «Кубан Чооров» — один ключ."""
    return " ".join(sorted(s.lower().split()))


def client_exact(wh_id: int, name: str):
    """Клиент по точному имени ИЛИ псевдониму («Виктория» → «Вика Уманец»),
    ИЛИ по тем же словам в другом порядке («Чооров Кубан» = «Кубан Чооров»,
    случай Беки на выезде 08.08.2026 — бот предлагал создать двойника)."""
    name = name.strip()
    conn = connect()
    row = conn.execute(
        "SELECT * FROM clients WHERE warehouse_id=? AND name=?", (wh_id, name)
    ).fetchone()
    if row is not None:
        return row
    row = conn.execute(
        "SELECT c.* FROM client_aliases a JOIN clients c ON c.id = a.client_id "
        "WHERE c.warehouse_id=? AND a.alias=?", (wh_id, name)
    ).fetchone()
    if row is not None:
        return row
    if " " not in name:
        return None
    # Перестановка слов: считаем совпадением, только если такой клиент
    # ровно один (двусмысленность решают кнопки похожих в fuzzy_clients).
    key = _name_key(name)
    cands = {}
    for r in conn.execute("SELECT * FROM clients WHERE warehouse_id=?", (wh_id,)):
        if _name_key(r["name"]) == key:
            cands[r["id"]] = r
    for r in conn.execute(
            "SELECT a.alias, c.* FROM client_aliases a "
            "JOIN clients c ON c.id = a.client_id WHERE c.warehouse_id=?",
            (wh_id,)):
        if _name_key(r["alias"]) == key:
            cands[r["id"]] = r
    if len(cands) == 1:
        return next(iter(cands.values()))
    return None


def add_client_alias(client_id: int, alias: str):
    conn = connect()
    with _lock, conn:
        conn.execute("INSERT OR IGNORE INTO client_aliases(client_id, alias) VALUES(?,?)",
                     (client_id, alias.strip()))


def client_aliases_list(client_id: int) -> list:
    return [r["alias"] for r in connect().execute(
        "SELECT alias FROM client_aliases WHERE client_id=? ORDER BY alias", (client_id,))]


# ---------- Заявки-карточки: зеркало в базе (переживают деплой) ----------

def pending_save(token: str, payload_json: str, expires: float):
    conn = connect()
    with _lock, conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_requests(token, payload, expires) "
            "VALUES(?,?,?)", (token, payload_json, expires))


def pending_load(token: str):
    row = connect().execute(
        "SELECT payload, expires FROM pending_requests WHERE token=?",
        (token,)).fetchone()
    return (row["payload"], row["expires"]) if row else None


def pending_delete(token: str):
    conn = connect()
    with _lock, conn:
        conn.execute("DELETE FROM pending_requests WHERE token=?", (token,))


def pending_cleanup(now: float):
    conn = connect()
    with _lock, conn:
        conn.execute("DELETE FROM pending_requests WHERE expires < ?", (now,))


# ---------- Сертификаты соответствия ----------

def cert_add(product_name: str, file_id: str, file_kind: str = "document",
             file_name=None, note=None):
    conn = connect()
    with _lock, conn:
        conn.execute(
            "INSERT INTO product_certs(product_name, file_id, file_kind, "
            "file_name, note, ts) VALUES(?,?,?,?,?,?)",
            (product_name.strip(), file_id, file_kind, file_name, note,
             datetime.now(BISHKEK).isoformat(timespec="seconds")))


def certs_of(product_name: str):
    """Сертификаты препарата, новые первыми (свежая поставка сверху)."""
    return connect().execute(
        "SELECT * FROM product_certs WHERE product_name=? ORDER BY id DESC",
        (product_name.strip(),)).fetchall()


def cert_names():
    """[(препарат, сколько сертификатов)] — у кого есть хоть один."""
    return [(r["product_name"], r["n"]) for r in connect().execute(
        "SELECT product_name, COUNT(*) n FROM product_certs "
        "GROUP BY product_name ORDER BY product_name")]


def certs_delete(product_name: str) -> int:
    conn = connect()
    with _lock, conn:
        cur = conn.execute("DELETE FROM product_certs WHERE product_name=?",
                           (product_name.strip(),))
        return cur.rowcount


def cash_on_hand(user_id: int) -> float:
    """Наличные у сотрудника: принятые оплаты минус сданная выручка.

    Считается по журналу, поэтому отмена операции автоматически
    возвращает кассу в правильное состояние.
    """
    conn = connect()
    received = conn.execute(
        "SELECT COALESCE(SUM(CASE "
        "  WHEN type='payment' THEN COALESCE(json_extract(data, '$.amount'), 0) "
        "  WHEN type='invoice' THEN COALESCE(json_extract(data, '$.payment'), 0) "
        "  ELSE 0 END), 0) "
        "FROM operations WHERE user_id=? AND status='done' AND type IN ('payment', 'invoice')",
        (user_id,)).fetchone()[0]
    handed = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(json_extract(data, '$.amount'), 0)), 0) "
        "FROM operations WHERE user_id=? AND status='done' AND type='handover'",
        (user_id,)).fetchone()[0]
    return received - handed


def cash_movements(user_id: int, limit: int = 10):
    """Последние движения кассы сотрудника: [(операция, сумма со знаком)].

    Плюс — принял деньги (оплата, приход по накладной), минус — сдал выручку."""
    rows = connect().execute(
        "SELECT * FROM operations WHERE user_id=? AND status='done' "
        "AND type IN ('payment', 'invoice', 'handover') ORDER BY id DESC LIMIT ?",
        (user_id, limit * 3)).fetchall()
    out = []
    for op in rows:
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        if op["type"] == "payment":
            amt = data.get("amount") or 0
        elif op["type"] == "invoice":
            amt = data.get("payment") or 0
        else:  # handover
            amt = -(data.get("amount") or 0)
        if amt:
            out.append((op, amt))
        if len(out) >= limit:
            break
    return out


def last_handover_ts(user_id: int):
    """Когда сотрудник в последний раз сдавал выручку (ISO или None)."""
    row = connect().execute(
        "SELECT ts FROM operations WHERE user_id=? AND type='handover' "
        "AND status='done' ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    return row["ts"] if row else None


def cash_movements_since_zero(user_id: int, cap: int = 100):
    """Движения кассы ПОСЛЕ последней инкассации, обнулившей кассу
    (просьба владельца 07.08.2026: старые движения после сдачи «под ноль»
    в /cash не нужны). Возвращает (moves, since_ts): moves — новые первыми;
    since_ts — время обнулившей инкассации, None = не найдена (тогда
    moves — просто последние cap движений)."""
    moves = cash_movements(user_id, cap)
    running = cash_on_hand(user_id)   # баланс ПОСЛЕ самой новой операции
    out = []
    for op, amt in moves:             # новые -> старые
        if op["type"] == "handover" and abs(running) < 0.005:
            return out, op["ts"]      # эта инкассация оставила ровно ноль
        out.append((op, amt))
        running -= amt                # баланс после предыдущей операции
    return out, None


def client_operations(cid: int):
    """Все проведённые операции клиента по порядку."""
    return connect().execute(
        "SELECT * FROM operations WHERE client_id=? AND status='done' ORDER BY id",
        (cid,),
    ).fetchall()


def debtors_with_age(wh_id: int):
    """Клиенты склада с долгом > 0 и датой последней оплаты.

    Возвращает [(client_row, last_ts_iso_or_None, платил_ли_вообще)].
    Если оплат не было — берётся дата первой операции клиента (долг висит с неё).
    """
    conn = connect()
    result = []
    for c in conn.execute(
            "SELECT * FROM clients WHERE warehouse_id=? AND debt > 0", (wh_id,)).fetchall():
        last_pay = conn.execute(
            "SELECT MAX(ts) FROM operations WHERE client_id=? AND status='done' AND "
            "(type='payment' OR (type='invoice' AND json_extract(data, '$.payment') > 0))",
            (c["id"],)).fetchone()[0]
        ref = last_pay
        if ref is None:
            ref = conn.execute(
                "SELECT MIN(ts) FROM operations WHERE client_id=? AND status='done'",
                (c["id"],)).fetchone()[0]
        result.append((c, ref, last_pay is not None))
    return result


def debt_growth(since_iso: str) -> dict:
    """Движение долга клиентов с момента since_iso — по дельтам журнала.

    Возвращает {client_id: (набрал, погасил)}: «набрал» — сумма положительных
    дельт долга (товар в долг, включая стартовый долг накладной нового
    клиента), «погасил» — сумма отрицательных (оплаты, возвраты).
    Отменённые операции не считаются. Стартовые долги справочника
    (clients_add_bulk) в журнал не пишутся и ростом не считаются.
    """
    out = {}
    for r in connect().execute(
            "SELECT data FROM operations WHERE status='done' AND ts >= ?",
            (since_iso,)):
        try:
            deltas = json.loads(r["data"]).get("debt_deltas") or []
        except (ValueError, TypeError):
            continue
        for cid, d in deltas:
            if cid is None:
                continue
            took, paid = out.get(cid, (0.0, 0.0))
            if d > 0:
                took += d
            else:
                paid += -d
            out[cid] = (took, paid)
    return out


def product_sales(pids, wh_ids, client_ids=None):
    """История продаж товара по журналу: накладные и возвраты (status='done'),
    новые первыми. Состав операции лежит в JSON data.items — фильтр в питоне
    (журнал невелик). Возвращает [(op_row + client_name, item_dict)]."""
    out = []
    pidset, whset = set(pids), set(wh_ids)
    for op in connect().execute(
            "SELECT o.id, o.ts, o.type, o.warehouse_id, o.client_id, o.data, "
            "c.name AS client_name FROM operations o "
            "LEFT JOIN clients c ON c.id = o.client_id "
            "WHERE o.status='done' AND o.type IN ('invoice','return') "
            "ORDER BY o.ts DESC, o.id DESC"):
        if op["warehouse_id"] not in whset:
            continue
        if client_ids is not None and op["client_id"] not in client_ids:
            continue
        try:
            items = json.loads(op["data"]).get("items") or []
        except (ValueError, TypeError):
            continue
        for it in items:
            if it.get("product_id") in pidset:
                out.append((op, it))
    return out


def draft_items_all():
    """Черновики с составом и датой — для истории продаж переходного периода."""
    return connect().execute(
        "SELECT ts, user_id, client, items FROM draft_log "
        "WHERE items IS NOT NULL ORDER BY ts DESC, id DESC").fetchall()


def set_client_price(cid: int, product_id: int, price: float):
    """Спеццена клиента; 0 или меньше — убрать."""
    conn = connect()
    with _lock, conn:
        if price <= 0:
            conn.execute("DELETE FROM client_prices WHERE client_id=? AND product_id=?",
                         (cid, product_id))
        else:
            conn.execute(
                "INSERT INTO client_prices(client_id, product_id, price) VALUES(?,?,?) "
                "ON CONFLICT(client_id, product_id) DO UPDATE SET price=excluded.price",
                (cid, product_id, price))


def client_prices_map(cid: int) -> dict:
    rows = connect().execute(
        "SELECT product_id, price FROM client_prices WHERE client_id=?", (cid,)).fetchall()
    return {r["product_id"]: r["price"] for r in rows}


def client_set_phone(cid: int, phone):
    conn = connect()
    with _lock, conn:
        conn.execute("UPDATE clients SET phone=? WHERE id=?", (phone or None, cid))


def fuzzy_clients(wh_id: int, name: str, n: int = 3):
    """Похожие клиенты внутри склада (региона), включая псевдонимы."""
    rows = clients_of(wh_id)
    mapping = {}
    for r in rows:
        mapping.setdefault(r["name"].lower(), r)
    for r in connect().execute(
            "SELECT a.alias, c.* FROM client_aliases a "
            "JOIN clients c ON c.id = a.client_id WHERE c.warehouse_id=?", (wh_id,)):
        mapping.setdefault(r["alias"].lower(), r)
    query = name.strip().lower()
    matches = difflib.get_close_matches(query, list(mapping), n=n, cutoff=0.6)
    # Перестановку слов difflib недооценивает («чооров кубан» ↔ «кубан
    # чооров» ≈ 0.58 < порога) — сравниваем ещё и отсортированные слова;
    # такие совпадения показываем первыми.
    qkey = _name_key(query)
    perm = [k for k in mapping if k not in matches and
            difflib.SequenceMatcher(None, qkey, _name_key(k)).ratio() >= 0.85]
    # Имя клиента целиком входит в запрос словами («Нурлан» ⊂ «Нурлан байке
    # ит базар») — difflib такое недооценивает (0.46), а случай реальный:
    # ИИ разворачивает короткое имя по старым черновикам, в справочнике —
    # короткое (инцидент 14.08.2026, Бишкек). Показываем первыми.
    qwords = set(query.split())
    contained = [k for k in mapping if k not in matches and k not in perm and
                 set(k.split()) <= qwords]
    out, seen = [], set()
    for m in contained + perm + matches:
        r = mapping[m]
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out[:n]


# ---------- Операции (журнал + атомарное применение) ----------

# Партии внутри склада: сортировка «сначала старые», партия без срока — в конце.
_BATCH_ORDER = ("CASE WHEN expiry='' THEN 1 ELSE 0 END, "
                "substr(expiry, 4) || substr(expiry, 1, 2)")


def _batch_add(conn, wh, pid, expiry, delta):
    conn.execute(
        "INSERT INTO product_batches(warehouse_id, product_id, expiry, qty) "
        "VALUES(?,?,?,?) ON CONFLICT(warehouse_id, product_id, expiry) "
        "DO UPDATE SET qty = qty + excluded.qty",
        (wh, pid, expiry or "", delta))


def _apply_batches(conn, wh, pid, delta, plan=None):
    """Правит партии при изменении остатка. Возвращает [(expiry, изменение)]
    для журнала (нужно /undo). Минус — по плану (продавец выбрал партию
    кнопкой), остаток плана — «сначала старые» (FEFO); плюс — по плану
    партий (загрузка со сроками) либо в партию «без срока»."""
    if not delta:
        return []
    if delta > 0:
        rows = list(plan or [])
        planned = sum(q for _, q in rows)
        if planned < delta:
            # Часть прихода без срока (или плана нет) — в партию «без срока»,
            # чтобы сумма партий всегда равнялась остатку.
            rows.append(("", delta - planned))
        for exp, q in rows:
            _batch_add(conn, wh, pid, exp, q)
        return [[exp, q] for exp, q in rows]
    out, rest = [], -delta
    for exp, q in (plan or []):
        take = min(q, rest)
        if take > 0:
            # Выбранная партия списывается, даже если в ней меньше — минус
            # останется в ней (продавец знает, что продал именно её).
            _batch_add(conn, wh, pid, exp, -take)
            out.append([exp, -take])
            rest -= take
        if not rest:
            return out
    for r in conn.execute(
            "SELECT expiry, qty FROM product_batches "
            "WHERE warehouse_id=? AND product_id=? AND qty > 0 "
            f"ORDER BY {_BATCH_ORDER}", (wh, pid)).fetchall():
        take = min(rest, r["qty"])
        _batch_add(conn, wh, pid, r["expiry"], -take)
        out.append([r["expiry"], -take])
        rest -= take
        if not rest:
            break
    if rest:
        # Партий не хватило (минусовой остаток или партии не заведены) —
        # долг вешаем на партию «без срока», сумма партий = остатку.
        _batch_add(conn, wh, pid, "", -rest)
        out.append(["", -rest])
    return out


def product_batches_of(wh_id: int, product_id: int):
    """Партии товара с остатком, «сначала старые» (для кнопок выбора)."""
    return connect().execute(
        "SELECT expiry, qty FROM product_batches "
        "WHERE warehouse_id=? AND product_id=? AND qty > 0 "
        f"ORDER BY {_BATCH_ORDER}", (wh_id, product_id)).fetchall()


def _revert_batches(conn, batch_deltas):
    for wh, pid, exp, chg in batch_deltas or []:
        _batch_add(conn, wh, pid, exp, -chg)


def _commit_op(conn, user_id: int, op_type: str, warehouse_id, client_id,
               summary: str, stock_deltas: list, debt_deltas: list,
               extra: dict = None, create_client: tuple = None,
               batch_plan: dict = None):
    """Тело проведения операции — вызывается только под _lock и открытой транзакцией.

    batch_plan: {(wh, pid): [(expiry, qty), ...]} — раскладка ПОЛОЖИТЕЛЬНОЙ
    дельты по партиям (стартовая загрузка со сроками)."""
    new_cid = None
    if create_client:
        c_wh, c_name, c_debt = create_client[:3]
        c_phone = create_client[3] if len(create_client) > 3 else None
        cur = conn.execute(
            "INSERT INTO clients(warehouse_id, name, debt, phone) VALUES(?,?,?,?)",
            (c_wh, c_name.strip(), c_debt, c_phone or None),
        )
        new_cid = cur.lastrowid
    if client_id is None:
        client_id = new_cid
    debt_deltas = [(cid if cid is not None else new_cid, d) for cid, d in debt_deltas]

    # План партий применяется РОВНО ОДИН РАЗ на (склад, товар): если товар
    # идёт несколькими дельтами, повторное применение плана задваивало бы
    # списание партий (баг найден аудитом 27.07.2026).
    bp = dict(batch_plan) if batch_plan else {}
    batch_deltas = []
    for wh, pid, d in stock_deltas:
        _apply_stock(conn, wh, pid, d)
        for exp, chg in _apply_batches(conn, wh, pid, d, bp.pop((wh, pid), None)):
            batch_deltas.append([wh, pid, exp, chg])
    for cid, d in debt_deltas:
        conn.execute("UPDATE clients SET debt = debt + ? WHERE id=?", (d, cid))

    data = json.dumps(
        {"stock_deltas": stock_deltas, "debt_deltas": debt_deltas,
         "batch_deltas": batch_deltas, **(extra or {})},
        ensure_ascii=False,
    )
    cur = conn.execute(
        "INSERT INTO operations(ts, user_id, type, warehouse_id, client_id, summary, data, status) "
        "VALUES(?,?,?,?,?,?,?, 'done')",
        (datetime.now(BISHKEK).isoformat(timespec="seconds"),
         user_id, op_type, warehouse_id, client_id, summary, data),
    )
    return cur.lastrowid, client_id


def commit_operation(user_id: int, op_type: str, warehouse_id, client_id,
                     summary: str, stock_deltas: list, debt_deltas: list,
                     extra: dict = None, create_client: tuple = None,
                     batch_plan: dict = None):
    """Применяет изменения склада/долгов и пишет операцию в журнал одной транзакцией.

    create_client: (warehouse_id, name, initial_debt) — создать клиента внутри
    той же транзакции; client_id=None в debt_deltas заменяется на нового.
    Возвращает (op_id, client_id).
    """
    conn = connect()
    with _lock, conn:
        return _commit_op(conn, user_id, op_type, warehouse_id, client_id,
                          summary, stock_deltas, debt_deltas, extra,
                          create_client, batch_plan)


def replace_operation(old_op_id: int, user_id: int, op_type: str, warehouse_id,
                      client_id, summary: str, stock_deltas: list,
                      debt_deltas: list, extra: dict = None,
                      batch_plan: dict = None):
    """Сторно старой операции + проведение новой ОДНОЙ транзакцией (замена
    накладной). Раньше это были два отдельных коммита: если второй падал,
    старая накладная оставалась отменённой без замены — учёт расходился.
    Бросает ValueError, если старую операцию отменить нельзя."""
    conn = connect()
    with _lock, conn:
        op = conn.execute("SELECT * FROM operations WHERE id=?", (old_op_id,)).fetchone()
        if op is None:
            raise ValueError(f"операция №{old_op_id} не найдена")
        if op["status"] != "done":
            raise ValueError(f"операция №{old_op_id} уже отменена")
        data = json.loads(op["data"])
        for wh, pid, d in data.get("stock_deltas", []):
            _apply_stock(conn, wh, pid, -d)
        _revert_batches(conn, data.get("batch_deltas"))
        for cid, d in data.get("debt_deltas", []):
            conn.execute("UPDATE clients SET debt = debt - ? WHERE id=?", (d, cid))
        conn.execute("UPDATE operations SET status='cancelled' WHERE id=?", (old_op_id,))
        return _commit_op(conn, user_id, op_type, warehouse_id, client_id,
                          summary, stock_deltas, debt_deltas, extra,
                          batch_plan=batch_plan)


def get_operation(op_id: int):
    return connect().execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()


def last_done_operation(user_id: int):
    return connect().execute(
        "SELECT * FROM operations WHERE user_id=? AND status='done' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def last_invoice_for_client(client_id: int):
    """Последняя проведённая накладная клиента (для дополнения товаром)."""
    return connect().execute(
        "SELECT * FROM operations WHERE type='invoice' AND status='done' "
        "AND client_id=? ORDER BY id DESC LIMIT 1", (client_id,)).fetchone()


def operations_since(start_iso: str, end_iso: str = None):
    """Проведённые операции начиная с даты (ISO), по возрастанию;
    end_iso — необязательная правая граница (не включается)."""
    q = "SELECT * FROM operations WHERE status='done' AND ts >= ?"
    params = [start_iso]
    if end_iso:
        q += " AND ts < ?"
        params.append(end_iso)
    return connect().execute(q + " ORDER BY id", params).fetchall()


def operations_all_since(start_iso: str):
    """Все операции (включая отменённые) начиная с даты, по возрастанию."""
    return connect().execute(
        "SELECT * FROM operations WHERE ts >= ? ORDER BY id", (start_iso,)
    ).fetchall()


def recent_operations(limit: int = 10, user_id=None, op_type=None, wh_id=None):
    """Последние операции журнала; user_id/op_type/wh_id — необязательные
    фильтры (/log Азамат накладные 50, /log Бишкек 30).

    Фильтр склада смотрит и в дельты остатков (data.stock_deltas), поэтому
    перемещение видно с обеих сторон, а не только у склада-источника."""
    conds, params = [], []
    if user_id is not None:
        conds.append("user_id=?")
        params.append(user_id)
    if wh_id is not None:
        # json_valid: одна битая строка data (ручная правка базы, кривой
        # бэкап) иначе роняла бы json_each и весь /log Склад (ревизия
        # 14.08.2026).
        conds.append("(warehouse_id=? OR (json_valid(operations.data) AND "
                     "EXISTS (SELECT 1 FROM "
                     "json_each(operations.data, '$.stock_deltas') je "
                     "WHERE json_extract(je.value, '$[0]')=?)))")
        params += [wh_id, wh_id]
    if op_type == "payment":
        # «Приходы» = все принятые деньги: и отдельные оплаты, и оплаты
        # прямо при накладной (решение владельца 08.08.2026 — иначе деньги
        # накладных приходилось искать другим фильтром).
        conds.append("(type='payment' OR (type='invoice' AND "
                     "COALESCE(json_extract(data, '$.payment'), 0) > 0))")
    elif op_type:
        conds.append("type=?")
        params.append(op_type)
    q = "SELECT * FROM operations"
    if conds:
        q += " WHERE " + " AND ".join(conds)
    params.append(limit)
    return connect().execute(q + " ORDER BY id DESC LIMIT ?", params).fetchall()


def operation_warehouses(op_row) -> list:
    """Все склады, которых коснулась операция (для ленты)."""
    ids = set()
    if op_row["warehouse_id"]:
        ids.add(op_row["warehouse_id"])
    try:
        data = json.loads(op_row["data"])
        for wh, _pid, _d in data.get("stock_deltas", []):
            ids.add(wh)
    except (ValueError, TypeError):
        pass
    return sorted(ids)


def cancel_operation(op_id: int):
    """Сторно: откатывает дельты операции. Возвращает (ok, summary_или_причина)."""
    conn = connect()
    with _lock, conn:
        op = conn.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        if op is None:
            return False, "Операция не найдена."
        if op["status"] != "done":
            return False, "Операция уже отменена."
        data = json.loads(op["data"])
        for wh, pid, d in data.get("stock_deltas", []):
            _apply_stock(conn, wh, pid, -d)
        _revert_batches(conn, data.get("batch_deltas"))
        for cid, d in data.get("debt_deltas", []):
            conn.execute("UPDATE clients SET debt = debt - ? WHERE id=?", (d, cid))
        conn.execute("UPDATE operations SET status='cancelled' WHERE id=?", (op_id,))
        return True, op["summary"]


def _reset_wh_doomed(conn, wh_id: int, client_ids: set):
    """Операции, которые заденет полный сброс склада: по складу (включая
    перемещения, где он одной из сторон) или по его клиентам."""
    return [op for op in conn.execute("SELECT * FROM operations").fetchall()
            if wh_id in operation_warehouses(op) or op["client_id"] in client_ids]


def stock_at(wh_id: int, moment_iso: str) -> dict:
    """Остатки склада на момент времени: текущие минус дельты операций,
    проведённых ПОСЛЕ этого момента. Отменённые операции считаются как
    будто их не было (их дельты уже сторнированы в текущих остатках)."""
    smap = dict(stock_map(wh_id))
    for op in connect().execute(
            "SELECT data FROM operations WHERE ts > ? AND status='done' "
            "ORDER BY id", (moment_iso,)):
        try:
            data = json.loads(op["data"])
        except (ValueError, TypeError):
            continue
        for wh, pid, d in data.get("stock_deltas", []):
            if wh == wh_id:
                smap[pid] = smap.get(pid, 0) - d
    return smap


def inspect_backup(path: str, expected_admin_id: int = None) -> dict:
    """Проверяет файл бэкапа: SQLite ли это, цел ли, наша ли структура,
    НАША ли это база (админ с ожидаемым id внутри). Возвращает счётчики
    для карточки; бросает ValueError с понятным текстом, если не годится."""
    with open(path, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise ValueError("файл не является базой SQLite")
    conn = sqlite3.connect(path)
    try:
        # Таблицы базы используют нашу коллацию — без неё integrity_check
        # и COUNT по ним падают на «no such collation sequence».
        conn.create_collation(
            "NOCASEU",
            lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()),
        )
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise ValueError(f"база в файле повреждена ({str(ok)[:100]})")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"operations", "clients", "warehouses", "stock",
                   "users"} - tables
        if missing:
            raise ValueError("в файле нет наших таблиц: "
                             + ", ".join(sorted(missing)))
        if expected_admin_id is not None and conn.execute(
                "SELECT 1 FROM users WHERE id=? AND role='admin'",
                (expected_admin_id,)).fetchone() is None:
            raise ValueError("в файле нет нашего администратора — похоже, "
                             "это база другого бота")
        def cnt(t):
            return conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {
            "operations": cnt("operations"),
            "clients": cnt("clients"),
            "debt": conn.execute(
                "SELECT COALESCE(SUM(debt), 0) FROM clients WHERE debt > 0"
            ).fetchone()[0],
            "last_op_ts": conn.execute(
                "SELECT MAX(ts) FROM operations").fetchone()[0],
        }
    finally:
        conn.close()


def current_stats() -> dict:
    """Те же счётчики по РАБОЧЕЙ базе (для сравнения с бэкапом)."""
    conn = connect()
    def cnt(t):
        return conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return {
        "operations": cnt("operations"),
        "clients": cnt("clients"),
        "debt": conn.execute(
            "SELECT COALESCE(SUM(debt), 0) FROM clients WHERE debt > 0"
        ).fetchone()[0],
        "last_op_ts": conn.execute(
            "SELECT MAX(ts) FROM operations").fetchone()[0],
    }


def restore_from(path: str):
    """Заменяет рабочую базу файлом бэкапа (файл уже проверен
    inspect_backup). Соединение закрывается, -wal/-shm сбрасываются.
    После вызова процесс НУЖНО перезапустить, чтобы init() прошёл заново."""
    global _conn
    import shutil
    dbp = _db_path()
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        for suf in ("-wal", "-shm"):
            try:
                os.remove(dbp + suf)
            except OSError:
                pass
        shutil.copyfile(path, dbp)


def reset_warehouse_preview(wh_id: int) -> dict:
    """Что именно удалит полный сброс склада (для карточки подтверждения)."""
    conn = connect()
    client_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM clients WHERE warehouse_id=?", (wh_id,))}
    ops = _reset_wh_doomed(conn, wh_id, client_ids)
    stock = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(qty), 0) FROM stock "
        "WHERE warehouse_id=? AND qty != 0", (wh_id,)).fetchone()
    debt = conn.execute(
        "SELECT COALESCE(SUM(debt), 0) FROM clients WHERE warehouse_id=?",
        (wh_id,)).fetchone()[0]
    return {"ops": len(ops), "stock_positions": stock[0], "stock_qty": stock[1],
            "clients": len(client_ids), "debt": debt}


def reset_warehouse(wh_id: int) -> dict:
    """ПОЛНЫЙ сброс склада: операции журнала, остатки, сроки годности и все
    клиенты с долгами удаляются безвозвратно — для перезагрузки с нуля.

    Операции именно удаляются (не сторно) — журнал склада начинается заново
    (нумерация операций продолжается, id не переиспользуются). Если операция
    задела и другой склад (перемещение), чужие дельты откатываются, чтобы
    остатки того склада не исказились. Кассы сотрудников пересчитаются сами —
    они считаются по журналу."""
    conn = connect()
    with _lock, conn:
        client_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM clients WHERE warehouse_id=?", (wh_id,))}
        doomed = _reset_wh_doomed(conn, wh_id, client_ids)
        for op in doomed:
            if op["status"] != "done":
                continue
            try:
                data = json.loads(op["data"])
            except (ValueError, TypeError):
                continue
            for wh, pid, d in data.get("stock_deltas", []):
                if wh != wh_id:
                    _apply_stock(conn, wh, pid, -d)
            for bwh, pid, exp, chg in data.get("batch_deltas", []):
                if bwh != wh_id:
                    _batch_add(conn, bwh, pid, exp, -chg)
            for cid, d in data.get("debt_deltas", []):
                if cid not in client_ids:
                    conn.execute("UPDATE clients SET debt = debt - ? WHERE id=?",
                                 (d, cid))
        conn.executemany("DELETE FROM operations WHERE id=?",
                         [(op["id"],) for op in doomed])
        stock = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(qty), 0) FROM stock "
            "WHERE warehouse_id=? AND qty != 0", (wh_id,)).fetchone()
        debt = conn.execute(
            "SELECT COALESCE(SUM(debt), 0) FROM clients WHERE warehouse_id=?",
            (wh_id,)).fetchone()[0]
        conn.execute("DELETE FROM stock WHERE warehouse_id=?", (wh_id,))
        conn.execute("DELETE FROM product_expiry WHERE warehouse_id=?", (wh_id,))
        conn.execute("DELETE FROM product_batches WHERE warehouse_id=?", (wh_id,))
        if client_ids:
            marks = ",".join("?" * len(client_ids))
            ids = tuple(client_ids)
            conn.execute(
                f"DELETE FROM client_aliases WHERE client_id IN ({marks})", ids)
            conn.execute(
                f"DELETE FROM client_prices WHERE client_id IN ({marks})", ids)
        conn.execute("DELETE FROM clients WHERE warehouse_id=?", (wh_id,))
        return {"ops": len(doomed), "stock_positions": stock[0],
                "stock_qty": stock[1], "clients": len(client_ids), "debt": debt}
