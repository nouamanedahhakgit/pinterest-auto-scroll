"""
db_logger.py — drop-in MySQL log sink for all bots.

Add ONE line at the very top of any bot script:
    import db_logger

That's it. All print() output + stderr is automatically:
  • still printed to the terminal as normal
  • batched and saved to MySQL `bot_logs` table every 2 seconds

The dashboard reads from this table — no log files, no restart needed.
"""

import os
import sys
import queue
import threading
import traceback
from datetime import datetime
from pathlib import Path

# ── detect which bot is calling us ────────────────────────────────────────────
_SCRIPT_MAP = {
    "magic_scroll":              "magic_scroll",
    "10_domain_quick_scrape":    "bot10",
    "13_scan-website":           "bot13",
    "14_download_blog":          "bot14",
    "7_scrape_profiles":         "bot07",
    "4_build_database":          "bot04",
}

def _detect_bot():
    main = Path(sys.argv[0]).stem if sys.argv else ""
    for fragment, name in _SCRIPT_MAP.items():
        if fragment in main:
            return name
    return main[:30] or "unknown"

BOT_NAME = _detect_bot()

# ── load .env for MySQL ────────────────────────────────────────────────────────
def _load_env():
    env_path = Path(__file__).parent / ".env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

# ── MySQL writer ───────────────────────────────────────────────────────────────
_log_queue   = queue.Queue(maxsize=5000)
_mysql_ready = threading.Event()
_stop_event  = threading.Event()

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bot_logs (
    id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    bot      VARCHAR(50)  NOT NULL,
    level    VARCHAR(20)  NOT NULL DEFAULT 'INFO',
    message  TEXT,
    ts       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_bot_ts (bot, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

def _classify(line: str) -> str:
    low = line.lower()
    if any(w in low for w in ("error", "exception", "traceback", "critical")):
        return "ERROR"
    if any(w in low for w in ("warn", "warning")):
        return "WARN"
    if any(w in low for w in ("done", "success", "complete", "✓", "✅")):
        return "OK"
    return "INFO"

def _writer_thread():
    """Background thread: drains _log_queue → MySQL, batched every 2 s."""
    env = _load_env()
    pw  = env.get("MYSQL_PASSWORD", "")
    if not pw:
        return  # no MySQL configured — silently skip

    try:
        import pymysql
    except ImportError:
        return

    conn = None
    def get_conn():
        nonlocal conn
        try:
            if conn:
                conn.ping(reconnect=True)
            else:
                conn = pymysql.connect(
                    host=env.get("MYSQL_HOST", "72.61.197.144"),
                    port=int(env.get("MYSQL_PORT", 3306)),
                    db=env.get("MYSQL_DB", "data_pint"),
                    user=env.get("MYSQL_USER", "data_pint_user"),
                    password=pw,
                    charset="utf8mb4",
                    connect_timeout=6,
                    autocommit=True,
                )
                with conn.cursor() as c:
                    c.execute(TABLE_SQL)
            return conn
        except Exception:
            conn = None
            return None

    _mysql_ready.set()   # signal that startup is done (even if conn failed)

    batch = []
    while not _stop_event.is_set():
        # collect up to 2 seconds of lines
        deadline = datetime.utcnow().timestamp() + 2.0
        while datetime.utcnow().timestamp() < deadline:
            try:
                item = _log_queue.get(timeout=0.2)
                batch.append(item)
                _log_queue.task_done()
            except queue.Empty:
                pass

        if not batch:
            continue

        db = get_conn()
        if not db:
            batch.clear()
            continue

        try:
            with db.cursor() as c:
                c.executemany(
                    "INSERT INTO bot_logs (bot, level, message, ts) VALUES (%s,%s,%s,%s)",
                    batch,
                )
            batch.clear()
        except Exception:
            batch.clear()

    # flush remaining on shutdown
    if batch:
        db = get_conn()
        if db:
            try:
                with db.cursor() as c:
                    c.executemany(
                        "INSERT INTO bot_logs (bot, level, message, ts) VALUES (%s,%s,%s,%s)",
                        batch,
                    )
            except Exception:
                pass


# ── stdout/stderr patcher ──────────────────────────────────────────────────────
class _Tee:
    """Wraps a real stream; also queues each line to MySQL."""
    def __init__(self, real_stream, level_hint="INFO"):
        self._real  = real_stream
        self._level = level_hint
        self._buf   = ""

    def write(self, text):
        self._real.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                level = _classify(line) if self._level == "INFO" else "ERROR"
                ts    = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
                try:
                    _log_queue.put_nowait((BOT_NAME, level, line[:4000], ts))
                except queue.Full:
                    pass

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


# ── init (runs once on `import db_logger`) ─────────────────────────────────────
def _init():
    # start background writer
    t = threading.Thread(target=_writer_thread, daemon=True, name="db_logger")
    t.start()

    # patch stdout + stderr
    sys.stdout = _Tee(sys.stdout, "INFO")
    sys.stderr = _Tee(sys.stderr, "ERROR")

    # log startup marker
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
    try:
        _log_queue.put_nowait((BOT_NAME, "INFO", f"=== {BOT_NAME} started ===", now))
    except queue.Full:
        pass

_init()
