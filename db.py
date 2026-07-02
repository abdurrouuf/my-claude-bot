# Хранилище: SQLite. На Railway подключите Volume (например, в /data) —
# файл базы должен лежать на постоянном диске, иначе данные пропадут при деплое.
import difflib
import json
import logging
import os
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
    UNIQUE(warehouse_id, name)
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
        if "feed_chat_id" not in _columns(conn, "warehouses") and \
           "owner_id" not in _columns(conn, "warehouses"):
            conn.execute("ALTER TABLE warehouses ADD COLUMN feed_chat_id INTEGER")
            conn.execute("ALTER TABLE warehouses ADD COLUMN feed_chat_title TEXT")
        _migrate_owner_warehouses(conn)

        for wname in warehouse_names:
            if conn.execute("SELECT 1 FROM warehouses WHERE name=?", (wname,)).fetchone() is None:
                conn.execute("INSERT INTO warehouses(name) VALUES(?)", (wname,))

        for uid, cfg in staff.items():
            name = cfg["name"]
            role = "admin" if uid == admin_id else cfg.get("role", "employee")
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO users(id, name, role, active) VALUES(?,?,?,1)",
                             (uid, name, role))
            else:
                conn.execute("UPDATE users SET active=1, name=? WHERE id=?", (name, uid))
            if uid == admin_id:
                conn.execute("UPDATE users SET role='admin' WHERE id=?", (uid,))
            wh = conn.execute("SELECT id FROM warehouses WHERE name=?",
                              (cfg.get("warehouse", ""),)).fetchone()
            if wh:
                conn.execute("UPDATE users SET default_wh=? WHERE id=? AND default_wh IS NULL",
                             (wh["id"], uid))
            for aname in cfg.get("access", []):
                aw = conn.execute("SELECT id FROM warehouses WHERE name=?", (aname,)).fetchone()
                if aw:
                    conn.execute("INSERT OR IGNORE INTO access(user_id, warehouse_id) VALUES(?,?)",
                                 (uid, aw["id"]))


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
    return connect().execute(
        "SELECT * FROM warehouses WHERE name=?", (name.strip(),)
    ).fetchone()


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


def grant_access(uid: int, wh_id: int):
    conn = connect()
    with _lock, conn:
        conn.execute("INSERT OR IGNORE INTO access(user_id, warehouse_id) VALUES(?,?)",
                     (uid, wh_id))


def revoke_access(uid: int, wh_id: int):
    conn = connect()
    with _lock, conn:
        conn.execute("DELETE FROM access WHERE user_id=? AND warehouse_id=?", (uid, wh_id))


def access_warehouses(uid: int):
    return connect().execute(
        "SELECT w.* FROM access a JOIN warehouses w ON w.id=a.warehouse_id "
        "WHERE a.user_id=? ORDER BY w.name",
        (uid,),
    ).fetchall()


def visible_warehouses(user_row):
    """Склады, которые пользователь видит: свой + выданные (админ — все)."""
    if user_row["role"] == "admin":
        return all_warehouses()
    result, seen = [], set()
    own = warehouse_of(user_row["id"])
    if own:
        result.append(own)
        seen.add(own["id"])
    for w in access_warehouses(user_row["id"]):
        if w["id"] not in seen:
            result.append(w)
            seen.add(w["id"])
    return result


def can_use_warehouse(user_row, wh_id: int) -> bool:
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


# ---------- Клиенты ----------

def clients_of(wh_id: int):
    return connect().execute(
        "SELECT * FROM clients WHERE warehouse_id=? ORDER BY name", (wh_id,)
    ).fetchall()


def client_get(cid: int):
    return connect().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()


def client_exact(wh_id: int, name: str):
    return connect().execute(
        "SELECT * FROM clients WHERE warehouse_id=? AND name=?", (wh_id, name.strip())
    ).fetchone()


def fuzzy_clients(wh_id: int, name: str, n: int = 3):
    """Похожие клиенты внутри склада (региона)."""
    rows = clients_of(wh_id)
    mapping = {}
    for r in rows:
        mapping.setdefault(r["name"].lower(), r)
    matches = difflib.get_close_matches(name.strip().lower(), list(mapping), n=n, cutoff=0.6)
    return [mapping[m] for m in matches]


# ---------- Операции (журнал + атомарное применение) ----------

def commit_operation(user_id: int, op_type: str, warehouse_id, client_id,
                     summary: str, stock_deltas: list, debt_deltas: list,
                     extra: dict = None, create_client: tuple = None):
    """Применяет изменения склада/долгов и пишет операцию в журнал одной транзакцией.

    create_client: (warehouse_id, name, initial_debt) — создать клиента внутри
    той же транзакции; client_id=None в debt_deltas заменяется на нового.
    Возвращает (op_id, client_id).
    """
    conn = connect()
    with _lock, conn:
        new_cid = None
        if create_client:
            c_wh, c_name, c_debt = create_client
            cur = conn.execute(
                "INSERT INTO clients(warehouse_id, name, debt) VALUES(?,?,?)",
                (c_wh, c_name.strip(), c_debt),
            )
            new_cid = cur.lastrowid
        if client_id is None:
            client_id = new_cid
        debt_deltas = [(cid if cid is not None else new_cid, d) for cid, d in debt_deltas]

        for wh, pid, d in stock_deltas:
            _apply_stock(conn, wh, pid, d)
        for cid, d in debt_deltas:
            conn.execute("UPDATE clients SET debt = debt + ? WHERE id=?", (d, cid))

        data = json.dumps(
            {"stock_deltas": stock_deltas, "debt_deltas": debt_deltas, **(extra or {})},
            ensure_ascii=False,
        )
        cur = conn.execute(
            "INSERT INTO operations(ts, user_id, type, warehouse_id, client_id, summary, data, status) "
            "VALUES(?,?,?,?,?,?,?, 'done')",
            (datetime.now(BISHKEK).isoformat(timespec="seconds"),
             user_id, op_type, warehouse_id, client_id, summary, data),
        )
        return cur.lastrowid, client_id


def get_operation(op_id: int):
    return connect().execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()


def last_done_operation(user_id: int):
    return connect().execute(
        "SELECT * FROM operations WHERE user_id=? AND status='done' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def operations_since(start_iso: str):
    """Проведённые операции начиная с даты (ISO), по возрастанию."""
    return connect().execute(
        "SELECT * FROM operations WHERE status='done' AND ts >= ? ORDER BY id",
        (start_iso,),
    ).fetchall()


def recent_operations(limit: int = 10, user_id=None):
    conn = connect()
    if user_id is None:
        return conn.execute(
            "SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM operations WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


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
        for cid, d in data.get("debt_deltas", []):
            conn.execute("UPDATE clients SET debt = debt - ? WHERE id=?", (d, cid))
        conn.execute("UPDATE operations SET status='cancelled' WHERE id=?", (op_id,))
        return True, op["summary"]
