"""Мост «Джарвис ↔ бот» (21.11.2026, «делай» владельца).

Маленький HTTP-сервер внутри бота. Через него ассистент владельца (чат
Claude) видит живую базу и ПРЕДЛАГАЕТ сообщения боту. Правила безопасности:

- Ключ выдаёт сам бот командой /bridge (админ, личка); в базе хранится
  только хеш. Ключа нет — мост закрыт (на всё 404).
- Чтение — через отдельное соединение SQLite в режиме «только чтение».
- Запись невозможна: единственное «действие» — предложение, которое бот
  показывает владельцу в Telegram с кнопкой «Выполнить». Без нажатия ничего
  не обрабатывается; накладная после этого ещё ждёт обычную «Провести».
- Команды («/…») через мост не принимаются.

Зависимостей нет (asyncio из стандартной библиотеки) — сетевой стек бота
закреплён, новые пакеты не добавляем.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import time

import db

log = logging.getLogger("bridge")

TOKEN_SETTING = "bridge_token_hash"
STAT_SETTING = "bridge_stat"
MAX_BODY = 256 * 1024
MAX_ROWS = 5000
SQL_BUDGET = 3.0            # секунд на один запрос — база не должна зависать
MAX_MESSAGES = 10
MAX_TEXT = 3500

_propose = None             # async (messages: list[str], note: str) -> dict
_bad = {"n": 0, "ts": 0.0}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    """Создаёт ключ, сохраняет хеш, возвращает ключ (показать один раз)."""
    token = secrets.token_urlsafe(36)
    db.set_setting(TOKEN_SETTING, _hash(token))
    return token


def disable():
    db.set_setting(TOKEN_SETTING, "")


def enabled() -> bool:
    return bool(db.get_setting(TOKEN_SETTING))


def stats() -> dict:
    try:
        return json.loads(db.get_setting(STAT_SETTING) or "{}")
    except ValueError:
        return {}


def _note_use(kind: str):
    s = stats()
    s["n"] = int(s.get("n", 0)) + 1
    s["last"] = time.time()
    s["last_kind"] = kind
    db.set_setting(STAT_SETTING, json.dumps(s))


def check_token(token: str) -> bool:
    want = db.get_setting(TOKEN_SETTING) or ""
    if not want or not token:
        return False
    return hmac.compare_digest(want, _hash(token))


def _ro_conn() -> sqlite3.Connection:
    path = db._db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.create_collation(
        "NOCASEU", lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()))
    conn.execute("PRAGMA query_only=ON")
    return conn


def run_sql(sql: str, params=None) -> dict:
    """Один запрос на чтение. Соединение только для чтения — INSERT/UPDATE/
    DELETE/ATTACH невозможны технически, а не по списку слов."""
    sql = (sql or "").strip().rstrip(";")
    if not sql or ";" in sql:
        return {"error": "один запрос без «;»"}
    if "settings" in sql.lower() and TOKEN_SETTING.split("_")[0] in sql.lower():
        return {"error": "недоступно"}
    conn = _ro_conn()
    deadline = time.monotonic() + SQL_BUDGET
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        cur = conn.execute(sql, params or [])
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(MAX_ROWS + 1)
        more = len(rows) > MAX_ROWS
        out = []
        for r in rows[:MAX_ROWS]:
            out.append([v.hex() if isinstance(v, bytes) else v for v in r])
        # Хеш ключа моста не отдаём даже по «SELECT * FROM settings».
        if "settings" in sql.lower():
            out = [r for r in out if TOKEN_SETTING not in [str(v) for v in r]]
        return {"columns": cols, "rows": out, "truncated": more}
    except sqlite3.Error as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


def backup_bytes() -> bytes:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db.backup_to(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _clean_messages(body: dict):
    msgs = body.get("messages")
    if isinstance(body.get("text"), str):
        msgs = [body["text"]]
    if not isinstance(msgs, list) or not msgs:
        return None, "нужен список messages"
    if len(msgs) > MAX_MESSAGES:
        return None, f"не больше {MAX_MESSAGES} сообщений"
    out = []
    for m in msgs:
        if not isinstance(m, str) or not m.strip():
            return None, "пустое сообщение"
        m = m.strip()
        if len(m) > MAX_TEXT:
            return None, f"сообщение длиннее {MAX_TEXT} символов"
        if m.startswith("/"):
            return None, "команды через мост не принимаются"
        out.append(m)
    return out, None


async def handle(method: str, path: str, headers: dict, body: bytes):
    """-> (status, content_type, bytes). Вынесено для тестов."""
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not check_token(token):
        # Чужому — то же, что и на несуществующий адрес; подбор тормозим.
        now = time.monotonic()
        if now - _bad["ts"] > 60:
            _bad["n"] = 0
        _bad["n"] += 1
        _bad["ts"] = now
        await asyncio.sleep(min(5.0, 0.5 * _bad["n"]))
        return 404, "text/plain", b"not found"
    path = path.split("?", 1)[0].rstrip("/")

    def js(obj, status=200):
        return status, "application/json", json.dumps(
            obj, ensure_ascii=False, default=str).encode()

    if method == "GET" and path == "/bridge/ping":
        _note_use("ping")
        return js({"ok": True, "db": os.path.basename(db._db_path()),
                   "time": time.strftime("%Y-%m-%d %H:%M:%S")})
    if method == "GET" and path == "/bridge/backup":
        _note_use("backup")
        return 200, "application/octet-stream", backup_bytes()
    try:
        data = json.loads(body.decode() or "{}") if method == "POST" else {}
    except (ValueError, UnicodeDecodeError):
        return js({"error": "не JSON"}, 400)
    if not isinstance(data, dict):
        return js({"error": "нужен объект"}, 400)
    if method == "POST" and path == "/bridge/sql":
        _note_use("sql")
        res = run_sql(str(data.get("sql") or ""), data.get("params"))
        return js(res, 400 if "error" in res else 200)
    if method == "POST" and path == "/bridge/propose":
        msgs, err = _clean_messages(data)
        if err:
            return js({"error": err}, 400)
        if _propose is None:
            return js({"error": "бот ещё запускается"}, 503)
        _note_use("propose")
        try:
            return js(await _propose(msgs, str(data.get("note") or "")[:500]))
        except Exception as e:                      # noqa: BLE001
            log.exception("Мост: предложение не отправлено")
            return js({"error": f"{type(e).__name__}"}, 500)
    return 404, "text/plain", b"not found"


async def _client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = (lines[0].split(" ") + ["", ""])[:3]
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length") or 0)
        if n > MAX_BODY:
            status, ctype, out = 413, "text/plain", b"too large"
        else:
            body = await asyncio.wait_for(reader.readexactly(n), 20) if n else b""
            status, ctype, out = await handle(method.upper(), path, headers, body)
    except Exception:                               # noqa: BLE001
        status, ctype, out = 400, "text/plain", b"bad request"
    try:
        writer.write((f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\n"
                      f"Content-Length: {len(out)}\r\nConnection: close\r\n\r\n"
                      ).encode() + out)
        await writer.drain()
    except Exception:                               # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:                           # noqa: BLE001
            pass


async def start(propose, port=None):
    """Запуск сервера. Сбой (порт занят) бота не роняет."""
    global _propose
    _propose = propose
    port = int(port or os.environ.get("PORT") or 8080)
    try:
        server = await asyncio.start_server(_client, "0.0.0.0", port, limit=MAX_BODY)
    except OSError as e:
        log.warning("Мост не запущен (порт %s): %s", port, e)
        return None
    log.info("Мост слушает порт %s (%s)", port,
             "ключ выдан" if enabled() else "закрыт — ключ не выдан")
    return server
