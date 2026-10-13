"""Small, bounded SQLite memory and conservative integer-USD budget accounting.

Stored message/fact text is untrusted user content. It must never be promoted to
system instructions. Run one bot replica against this database; opening a Store
recovers unfinished calls by charging their full reservation.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterable
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import unicodedata
import uuid


CONTEXT_SECONDS = 30 * 60
MEMORY_SECONDS = 30 * 24 * 60 * 60
MAX_CONTEXT_ROWS = 4096
MAX_MEMORY_ROWS = 4096
MAX_STYLE_ROWS = 4096
MAX_BOT_MESSAGE_ROWS = 4096
MAX_CONTEXT_SOURCES = 64
SQLITE_MAX_INTEGER = (1 << 63) - 1


def _timestamp(value: float | None = None) -> float:
    value = time.time() if value is None else float(value)
    if not math.isfinite(value):
        raise ValueError("timestamp must be finite")
    return value


def _money(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("costs and limits must be integer microUSD")
    if not 0 <= value <= SQLITE_MAX_INTEGER:
        raise ValueError("costs and limits must be nonnegative SQLite integers")
    return value


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def _text(value: str, maximum: int) -> str:
    return " ".join(value.split())[:maximum]


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._next_cleanup = 0.0
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS context (
                id INTEGER PRIMARY KEY, channel TEXT NOT NULL, user TEXT NOT NULL,
                text TEXT NOT NULL, is_bot INTEGER NOT NULL, ts REAL NOT NULL,
                sources TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS context_channel ON context(channel, ts DESC, id DESC);
            CREATE INDEX IF NOT EXISTS context_user ON context(user);
            CREATE TABLE IF NOT EXISTS bot_messages (
                message_id TEXT PRIMARY KEY, channel TEXT NOT NULL,
                bot_id TEXT NOT NULL, ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS bot_messages_recent ON bot_messages(ts DESC, message_id DESC);
            CREATE TABLE IF NOT EXISTS styles (
                channel TEXT NOT NULL, user TEXT NOT NULL,
                lowercase REAL NOT NULL, short_messages REAL NOT NULL,
                questions REAL NOT NULL, exclamations REAL NOT NULL, emoji REAL NOT NULL,
                samples INTEGER NOT NULL, ts REAL NOT NULL,
                PRIMARY KEY(channel, user)
            );
            CREATE INDEX IF NOT EXISTS styles_user ON styles(user);
            CREATE TABLE IF NOT EXISTS samples (user TEXT PRIMARY KEY, ts REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY, channel TEXT NOT NULL, user TEXT NOT NULL,
                text TEXT NOT NULL, ts REAL NOT NULL,
                UNIQUE(channel, user, text)
            );
            CREATE INDEX IF NOT EXISTS facts_user ON facts(user);
            CREATE TABLE IF NOT EXISTS opt_out (user TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS cooldowns (
                user TEXT PRIMARY KEY, ts REAL NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admissions (id INTEGER PRIMARY KEY, ts REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS admissions_ts ON admissions(ts);
            CREATE TABLE IF NOT EXISTS ledger (
                day TEXT PRIMARY KEY,
                spent INTEGER NOT NULL DEFAULT 0 CHECK(spent >= 0),
                reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0)
            );
            CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY, day TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount >= 0)
            );
        """)
        # A crash can occur after the provider charged but before usage arrived.
        # Never refund such a call, including when the clock rolls into a new day.
        with self._transaction():
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(context)")}
            if "sources" not in columns:
                self._db.execute("ALTER TABLE context ADD COLUMN sources TEXT NOT NULL DEFAULT '[]'")
                # Legacy bot rows have no reliable provenance. Discard them rather
                # than risk replaying a quote after its original speaker opts out.
                self._db.execute("DELETE FROM context WHERE is_bot=1")
            self._db.execute("UPDATE ledger SET spent=spent+reserved, reserved=0")
            self._db.execute("DELETE FROM reservations")

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _cleanup(self, now: float) -> None:
        """Caller owns a transaction. Expensive retention cleanup runs hourly."""
        if now < self._next_cleanup:
            return
        self._db.execute("DELETE FROM context WHERE ts < ?", (now - CONTEXT_SECONDS,))
        self._db.execute("DELETE FROM facts WHERE ts < ?", (now - MEMORY_SECONDS,))
        self._db.execute("DELETE FROM styles WHERE ts < ?", (now - MEMORY_SECONDS,))
        self._db.execute("DELETE FROM samples WHERE ts < ?", (now - 60,))
        self._db.execute("DELETE FROM cooldowns WHERE expires <= ?", (now,))
        self._db.execute("DELETE FROM admissions WHERE ts <= ?", (now - 60,))
        self._next_cleanup = now + 3600

    def _opted_out(self, user: str) -> bool:
        return self._db.execute("SELECT 1 FROM opt_out WHERE user=?", (user,)).fetchone() is not None

    def opted_out(self, user: str) -> bool:
        with self._lock:
            return self._opted_out(str(user))

    def record_bot_message(self, channel: str, message_id: str, bot_id: str,
                           now: float | None = None) -> None:
        """Remember reply targets without storing message content or human IDs."""
        now = _timestamp(now)
        with self._transaction():
            self._db.execute("""INSERT INTO bot_messages(message_id,channel,bot_id,ts)
                VALUES(?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET
                channel=excluded.channel,bot_id=excluded.bot_id,ts=excluded.ts""",
                             (str(message_id), str(channel), str(bot_id), now))
            self._db.execute("""DELETE FROM bot_messages WHERE message_id IN
                (SELECT message_id FROM bot_messages ORDER BY ts DESC,message_id DESC
                 LIMIT -1 OFFSET ?)""", (MAX_BOT_MESSAGE_ROWS,))

    def is_bot_message(self, channel: str, message_id: str, bot_id: str) -> bool:
        with self._lock:
            return self._db.execute("""SELECT 1 FROM bot_messages
                WHERE message_id=? AND channel=? AND bot_id=?""",
                                    (str(message_id), str(channel), str(bot_id))).fetchone() is not None

    def observe(self, channel: str, user: str, text: str, is_bot: bool = False,
                now: float | None = None, sources: Iterable[str] | None = None) -> None:
        now, channel, user = _timestamp(now), str(channel), str(user)
        text = _text(text, 240)
        if not text:
            return
        dependencies = set()
        if is_bot:
            for source in sources or ():
                dependencies.add(str(source))
                if len(dependencies) > MAX_CONTEXT_SOURCES:
                    return  # Never truncate provenance and retain the quoted text.
        serialized_sources = json.dumps(sorted(dependencies), separators=(",", ":"))
        with self._transaction():
            self._cleanup(now)
            if self._opted_out(user):
                return
            if not is_bot:
                last_sample = self._db.execute("SELECT ts FROM samples WHERE user=?", (user,)).fetchone()
                if last_sample is not None and now - last_sample[0] < 15:
                    return
                self._db.execute("INSERT INTO samples(user,ts) VALUES(?,?) ON CONFLICT(user) DO UPDATE SET ts=excluded.ts",
                                 (user, now))
            self._db.execute("INSERT INTO context(channel,user,text,is_bot,ts,sources) VALUES(?,?,?,?,?,?)",
                             (channel, user, text, int(is_bot), now, serialized_sources))
            self._db.execute("""DELETE FROM context WHERE channel=? AND id NOT IN
                (SELECT id FROM context WHERE channel=? ORDER BY ts DESC,id DESC LIMIT 8)""",
                             (channel, channel))
            self._db.execute("""DELETE FROM context WHERE id IN
                (SELECT id FROM context ORDER BY ts DESC,id DESC LIMIT -1 OFFSET ?)""",
                             (MAX_CONTEXT_ROWS,))
            if is_bot:
                return
            letters = [char for char in text if char.islower() or char.isupper()]
            values = (
                sum(char.islower() for char in letters) / max(1, len(letters)),
                float(len(text) <= 80), float("?" in text), float("!" in text),
                float(any(ord(char) > 127 and unicodedata.category(char) in {"So", "Sk"} for char in text)),
            )
            self._db.execute("""INSERT INTO styles
                (channel,user,lowercase,short_messages,questions,exclamations,emoji,samples,ts)
                VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(channel,user) DO UPDATE SET
                lowercase=styles.lowercase*0.8+excluded.lowercase*0.2,
                short_messages=styles.short_messages*0.8+excluded.short_messages*0.2,
                questions=styles.questions*0.8+excluded.questions*0.2,
                exclamations=styles.exclamations*0.8+excluded.exclamations*0.2,
                emoji=styles.emoji*0.8+excluded.emoji*0.2,
                samples=MIN(styles.samples+1,1000000), ts=excluded.ts""",
                             (channel, user, *values, now))
            self._db.execute("""DELETE FROM styles WHERE rowid IN
                (SELECT rowid FROM styles ORDER BY ts DESC,rowid DESC LIMIT -1 OFFSET ?)""",
                             (MAX_STYLE_ROWS,))
            self._db.execute("""DELETE FROM samples WHERE rowid IN
                (SELECT rowid FROM samples ORDER BY ts DESC,rowid DESC LIMIT -1 OFFSET ?)""",
                             (MAX_STYLE_ROWS,))

    def recent(self, channel: str, limit: int = 6, now: float | None = None) -> list[dict]:
        now = _timestamp(now)
        with self._lock:
            rows = self._db.execute("""SELECT user,text,is_bot,sources FROM context
                WHERE channel=? AND ts>=? ORDER BY ts DESC,id DESC LIMIT ?""",
                                    (str(channel), now - CONTEXT_SECONDS, max(0, min(int(limit), 8)))).fetchall()
        entries = []
        for row in reversed(rows):
            dependencies = []
            if row["is_bot"]:
                try:
                    dependencies = json.loads(row["sources"])
                except (ValueError, TypeError):
                    continue
                if (not isinstance(dependencies, list) or len(dependencies) > MAX_CONTEXT_SOURCES
                        or not all(isinstance(source, str) for source in dependencies)):
                    continue
            entries.append({"user": row["user"], "text": row["text"],
                            "is_bot": bool(row["is_bot"]), "sources": dependencies})
        return entries

    def style(self, channel: str, user: str) -> dict:
        with self._lock:
            row = self._db.execute("""SELECT lowercase,short_messages,questions,exclamations,emoji,samples
                FROM styles WHERE channel=? AND user=? AND ts>=?""",
                                   (str(channel), str(user), _timestamp() - MEMORY_SECONDS)).fetchone()
            return dict(row) if row else {}

    def remember(self, channel: str, user: str, text: str, now: float | None = None) -> None:
        """Called only for a user's explicit opt-in remember command."""
        now, channel, user = _timestamp(now), str(channel), str(user)
        text = _text(text, 160)
        if not text:
            return
        with self._transaction():
            self._cleanup(now)
            if self._opted_out(user):
                return
            self._db.execute("""INSERT INTO facts(channel,user,text,ts) VALUES(?,?,?,?)
                ON CONFLICT(channel,user,text) DO UPDATE SET ts=excluded.ts""", (channel, user, text, now))
            self._db.execute("""DELETE FROM facts WHERE channel=? AND user=? AND id NOT IN
                (SELECT id FROM facts WHERE channel=? AND user=? ORDER BY ts DESC,id DESC LIMIT 8)""",
                             (channel, user, channel, user))
            self._db.execute("""DELETE FROM facts WHERE id IN
                (SELECT id FROM facts ORDER BY ts DESC,id DESC LIMIT -1 OFFSET ?)""", (MAX_MEMORY_ROWS,))

    def memories(self, channel: str, user: str, query: str, limit: int = 3) -> list[str]:
        with self._lock:
            rows = self._db.execute("""SELECT text,ts,id FROM facts
                WHERE channel=? AND user=? AND ts>=? ORDER BY ts DESC,id DESC LIMIT 8""",
                                   (str(channel), str(user), _timestamp() - MEMORY_SECONDS)).fetchall()
        terms = set(re.findall(r"[^\W_]{2,}", query.casefold()))
        ranked = sorted(rows, key=lambda row: (
            len(terms.intersection(re.findall(r"[^\W_]{2,}", row["text"].casefold()))), row["ts"], row["id"]),
                        reverse=True)
        return [row["text"] for row in ranked[:max(0, min(int(limit), 8))]]

    def _forget(self, user: str) -> None:
        self._db.execute("DELETE FROM context WHERE user=? OR is_bot=1", (user,))
        self._db.execute("DELETE FROM styles WHERE user=?", (user,))
        self._db.execute("DELETE FROM samples WHERE user=?", (user,))
        self._db.execute("DELETE FROM facts WHERE user=?", (user,))
        # Cooldowns survive forget, so deleting memory cannot bypass abuse limits.

    def forget(self, user: str) -> None:
        with self._transaction():
            self._forget(str(user))

    def set_opt_out(self, user: str, value: bool) -> None:
        user = str(user)
        with self._transaction():
            if value:
                self._db.execute("INSERT OR IGNORE INTO opt_out(user) VALUES(?)", (user,))
            else:
                self._db.execute("DELETE FROM opt_out WHERE user=?", (user,))
            self._forget(user)

    def admit(self, user: str, now: float, cooldown: float = 15, per_minute: int = 20) -> str:
        now, cooldown = _timestamp(now), float(cooldown)
        if not math.isfinite(cooldown) or cooldown < 0:
            raise ValueError("cooldown must be nonnegative and finite")
        if isinstance(per_minute, bool) or not isinstance(per_minute, int) or per_minute < 1:
            raise ValueError("per_minute must be a positive integer")
        with self._transaction():
            self._cleanup(now)
            previous = self._db.execute("SELECT ts FROM cooldowns WHERE user=?", (str(user),)).fetchone()
            if cooldown > 0 and previous is not None and now - previous[0] < cooldown:
                return "cooldown"
            self._db.execute("DELETE FROM admissions WHERE ts<=?", (now - 60,))
            if self._db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] >= per_minute:
                return "rate"
            self._db.execute("""INSERT INTO cooldowns(user,ts,expires) VALUES(?,?,?)
                ON CONFLICT(user) DO UPDATE SET ts=excluded.ts,expires=excluded.expires""",
                             (str(user), now, now + max(60, cooldown)))
            self._db.execute("INSERT INTO admissions(ts) VALUES(?)", (now,))
            return "ok"

    def _spend(self, now: float) -> dict[str, int]:
        day = _day(now)
        row = self._db.execute("""SELECT
            COALESCE(SUM(CASE WHEN day=? THEN spent+reserved ELSE 0 END),0) AS day,
            COALESCE(SUM(CASE WHEN substr(day,1,7)=? THEN spent+reserved ELSE 0 END),0) AS month,
            COALESCE(SUM(spent+reserved),0) AS total FROM ledger""", (day, day[:7])).fetchone()
        return {key: int(row[key]) for key in ("day", "month", "total")}

    def spend(self, now: float) -> dict[str, int]:
        with self._lock:
            return self._spend(_timestamp(now))

    def reserve(self, amount_microusd: int, day_limit: int, month_limit: int,
                total_limit: int, now: float) -> str | None:
        amount = _money(amount_microusd)
        limits = tuple(_money(value) for value in (day_limit, month_limit, total_limit))
        now = _timestamp(now)
        with self._transaction():
            self._cleanup(now)
            spend = self._spend(now)
            if any(spend[key] + amount > limit for key, limit in zip(("day", "month", "total"), limits)):
                return None
            day, reservation_id = _day(now), uuid.uuid4().hex
            self._db.execute("""INSERT INTO ledger(day,reserved) VALUES(?,?)
                ON CONFLICT(day) DO UPDATE SET reserved=ledger.reserved+excluded.reserved""", (day, amount))
            self._db.execute("INSERT INTO reservations(id,day,amount) VALUES(?,?,?)", (reservation_id, day, amount))
            return reservation_id

    def settle(self, reservation_id: str, actual_microusd: int) -> None:
        actual = _money(actual_microusd)
        with self._transaction():
            row = self._db.execute("SELECT day,amount FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                return
            total = self._db.execute("SELECT COALESCE(SUM(spent+reserved),0) FROM ledger").fetchone()[0]
            if total - row["amount"] + actual > SQLITE_MAX_INTEGER:
                raise ValueError("settlement exceeds integer ledger capacity")
            # Known actual usage can release unused headroom. Unknown/failed calls
            # must not call this method: their full reservation remains charged.
            self._db.execute("UPDATE ledger SET reserved=reserved-?,spent=spent+? WHERE day=?",
                             (row["amount"], actual, row["day"]))
            self._db.execute("DELETE FROM reservations WHERE id=?", (reservation_id,))
