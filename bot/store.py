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

from .text import is_override


CONTEXT_SECONDS = 30 * 60
MEMORY_SECONDS = 30 * 24 * 60 * 60
MAX_CONTEXT_ROWS = 4096
MAX_MEMORY_ROWS = 4096
MAX_STYLE_ROWS = 4096
MAX_BOT_MESSAGE_ROWS = 4096
MAX_CONTEXT_SOURCES = 64
SQLITE_MAX_INTEGER = (1 << 63) - 1
RECALL_CANDIDATES = 32
RECALL_STOPWORDS = frozenset("""a an and are as at be been before but by can could did do does
    for from had has have how i if in is it its just know last me more my no not now of on or
    our please recall remember said say she so some tell that the their them then there these
    they this those to told us was we were what when where which who why will with would you
    your earlier again about also really ever something anything everything""".split())


def _recall_terms(query: str) -> list[str]:
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", query[:4000]).casefold())
    terms = list(dict.fromkeys(token for token in tokens
                               if 2 <= len(token) <= 64 and token not in RECALL_STOPWORDS))[:8]
    if any(term in {"name", "call", "called"} for term in terms):
        terms.extend(term for term in ("name", "call", "called") if term not in terms)
    if any(term in {"favorite", "favourite", "fave"} for term in terms):
        terms.extend(term for term in ("favorite", "favourite", "fave") if term not in terms)
    return terms[:12]


def _fts_quote(value: str) -> str:
    # Used for both internally extracted words and exact local scope identifiers.
    return '"' + value.replace('"', '""') + '"'


def _recall_excerpt(marked: str) -> str:
    """Trim around the first FTS-highlighted match, including late Unicode hits."""
    marked = " ".join(marked.split())
    match_at = marked.find("\x01")
    plain = marked.replace("\x01", "").replace("\x02", "")
    if len(plain) <= 240:
        return plain
    start = max(0, min((match_at if match_at >= 0 else 0) - 80, len(plain) - 238))
    end = min(len(plain), start + 238)
    return (("…" if start else "") + plain[start:end].strip()
            + ("…" if end < len(plain) else ""))


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
    def __init__(self, path: str, *, message_retention_days: int = 30,
                 message_max_rows: int = 50_000, message_max_bytes: int = 64 * 1024 * 1024):
        for name, value in (("message_retention_days", message_retention_days),
                            ("message_max_rows", message_max_rows), ("message_max_bytes", message_max_bytes)):
            if type(value) is not int or not 1 <= value <= SQLITE_MAX_INTEGER:
                raise ValueError(f"{name} must be a positive integer")
        self._message_retention_seconds = message_retention_days * 86400
        self._message_max_rows = message_max_rows
        self._message_max_bytes = message_max_bytes
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
        self._db.execute("PRAGMA journal_size_limit=8388608")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, message_id TEXT UNIQUE,
                channel TEXT NOT NULL, user TEXT NOT NULL, text TEXT NOT NULL,
                text_bytes INTEGER NOT NULL CHECK(text_bytes >= 0), ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_age ON messages(ts,id);
            CREATE INDEX IF NOT EXISTS messages_user ON messages(user);
            CREATE INDEX IF NOT EXISTS messages_recall ON messages(channel,user,ts DESC,id DESC);
            CREATE TABLE IF NOT EXISTS message_stats (
                id INTEGER PRIMARY KEY CHECK(id=1),
                row_count INTEGER NOT NULL CHECK(row_count >= 0),
                text_bytes INTEGER NOT NULL CHECK(text_bytes >= 0)
            );
            CREATE TRIGGER IF NOT EXISTS messages_insert_stats AFTER INSERT ON messages BEGIN
                UPDATE message_stats SET row_count=row_count+1,text_bytes=text_bytes+NEW.text_bytes WHERE id=1;
            END;
            CREATE TRIGGER IF NOT EXISTS messages_delete_stats AFTER DELETE ON messages BEGIN
                UPDATE message_stats SET row_count=row_count-1,text_bytes=text_bytes-OLD.text_bytes WHERE id=1;
            END;
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
            if self._db.execute("SELECT 1 FROM message_stats WHERE id=1").fetchone() is None:
                # This scan is migration-only. Per-message accounting is constant
                # time; triggers also keep counts correct during forget/pruning.
                self._db.execute("""INSERT INTO message_stats(id,row_count,text_bytes)
                    SELECT 1,COUNT(*),COALESCE(SUM(text_bytes),0) FROM messages""")
            self._setup_message_search()
            self._prune_messages(_timestamp())
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

    def _setup_message_search(self) -> None:
        """Create/rebuild once for an existing archive; normal writes use triggers."""
        existed = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'").fetchone()
        self._db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text, channel, user, content='messages', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2')""")
        self._db.execute("""CREATE TRIGGER IF NOT EXISTS messages_insert_fts AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid,text,channel,user) VALUES(NEW.id,NEW.text,NEW.channel,NEW.user);
        END""")
        self._db.execute("""CREATE TRIGGER IF NOT EXISTS messages_delete_fts AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts,rowid,text,channel,user)
                VALUES('delete',OLD.id,OLD.text,OLD.channel,OLD.user);
        END""")
        self._db.execute("""CREATE TRIGGER IF NOT EXISTS messages_update_fts AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts,rowid,text,channel,user)
                VALUES('delete',OLD.id,OLD.text,OLD.channel,OLD.user);
            INSERT INTO messages_fts(rowid,text,channel,user) VALUES(NEW.id,NEW.text,NEW.channel,NEW.user);
        END""")
        if not existed:
            self._db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

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
        self._prune_messages(now)
        self._next_cleanup = now + 3600

    def _prune_messages(self, now: float) -> None:
        """Caller owns a transaction; only indexed expired/oldest rows are read."""
        self._db.execute("DELETE FROM messages WHERE ts < ?", (now - self._message_retention_seconds,))
        stats = self._db.execute("SELECT row_count,text_bytes FROM message_stats WHERE id=1").fetchone()
        rows, size = stats["row_count"], stats["text_bytes"]
        while rows > self._message_max_rows or size > self._message_max_bytes:
            batch_size = 256 if size > self._message_max_bytes else min(256, rows - self._message_max_rows)
            oldest = self._db.execute("SELECT id,text_bytes FROM messages ORDER BY ts,id LIMIT ?", (batch_size,)).fetchall()
            remove = []
            for row in oldest:
                if rows <= self._message_max_rows and size <= self._message_max_bytes:
                    break
                remove.append(row["id"])
                rows -= 1
                size -= row["text_bytes"]
            if not remove:
                raise RuntimeError("archive counters are inconsistent")
            placeholders = ",".join("?" for _ in remove)
            self._db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", remove)

    def archive_message(self, channel: str, user: str, text: str, message_id: str | None = None,
                        now: float | None = None) -> bool:
        """Archive caller-sanitized human text; only bounded recall snippets become context.

        A duplicate Discord ID is ignored. Messages without IDs remain distinct.
        Limits may immediately evict an inserted message if it alone exceeds the
        configured byte cap, or its supplied timestamp is already expired.
        """
        now, channel, user = _timestamp(now), str(channel), str(user)
        text = text[:4000].strip()
        if not text:
            return False
        with self._transaction():
            if self._opted_out(user):
                return False
            result = self._db.execute("""INSERT INTO messages(message_id,channel,user,text,text_bytes,ts)
                VALUES(?,?,?,?,?,?) ON CONFLICT(message_id) DO NOTHING""",
                                      (str(message_id) if message_id is not None else None,
                                       channel, user, text, len(text.encode("utf-8")), now))
            inserted = result.rowcount == 1
            self._prune_messages(now)
            return inserted

    def recall(self, channel: str, user: str, query: str, limit: int = 3,
               now: float | None = None, exclude_message_id: str | None = None) -> list[str]:
        """Return bounded, untrusted excerpts from this person's earlier messages.

        Full-text search narrows candidates before reading message bodies. Generic
        questions fall back to a small indexed recent slice, never an archive scan.
        Current-timestamp rows are excluded even when no Discord ID was supplied.
        """
        now, channel, user = _timestamp(now), str(channel), str(user)
        limit = max(0, min(int(limit), 3))
        if not limit:
            return []
        terms = _recall_terms(query)
        excluded = str(exclude_message_id) if exclude_message_id is not None else None
        scope = (channel, user, now - self._message_retention_seconds, now, excluded, excluded)

        def excerpts(rows):
            result = []
            seen = set()
            for row in rows:
                text = row["text"]
                if text.lstrip().startswith("!") or is_override(text):
                    continue
                snippet = _recall_excerpt(row["excerpt"])
                if snippet and snippet not in seen:
                    result.append(snippet)
                    seen.add(snippet)
                if len(result) >= limit:
                    break
            return result

        with self._lock:
            if self._opted_out(user):
                return []
            if terms:
                # Scope terms narrow the FTS posting lists; SQL repeats exact
                # equality so tokenization can never mix similar channel/user IDs.
                match = ("channel : " + _fts_quote(channel) + " AND user : " + _fts_quote(user)
                         + " AND text : (" + " OR ".join(_fts_quote(term) for term in terms) + ")")
                rows = self._db.execute("""SELECT m.text,
                    snippet(messages_fts,0,char(1),char(2),'…',24) AS excerpt
                    FROM messages_fts JOIN messages AS m ON m.id=messages_fts.rowid
                    WHERE messages_fts MATCH ? AND m.channel=? AND m.user=? AND m.ts>=? AND m.ts<?
                    AND (? IS NULL OR m.message_id IS NULL OR m.message_id<>?)
                    ORDER BY messages_fts.rank LIMIT ?""", (match, *scope, RECALL_CANDIDATES)).fetchall()
                found = excerpts(rows)
                if found:
                    return found
            rows = self._db.execute("""SELECT text,text AS excerpt FROM messages
                WHERE channel=? AND user=? AND ts>=? AND ts<?
                AND (? IS NULL OR message_id IS NULL OR message_id<>?)
                ORDER BY ts DESC,id DESC LIMIT ?""", (*scope, RECALL_CANDIDATES)).fetchall()
            return excerpts(rows)

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
        style_text = _text(text, 4000)
        text = style_text[:240]
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
            letters = [char for char in style_text if char.islower() or char.isupper()]
            values = (
                sum(char.islower() for char in letters) / max(1, len(letters)),
                float(len(style_text) <= 80), float("?" in style_text), float("!" in style_text),
                float(any(ord(char) > 127 and unicodedata.category(char) in {"So", "Sk"} for char in style_text)),
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

    def style(self, channel: str, user: str, now: float | None = None) -> dict:
        with self._lock:
            row = self._db.execute("""SELECT lowercase,short_messages,questions,exclamations,emoji,samples
                FROM styles WHERE channel=? AND user=? AND ts>=?""",
                                   (str(channel), str(user), _timestamp(now) - MEMORY_SECONDS)).fetchone()
            return {key: round(value, 3) if isinstance(value, float) else value
                    for key, value in dict(row).items()} if row else {}

    def channel_style(self, channel: str, now: float | None = None) -> dict:
        """Equal weight per active person, so prolific users do not own a room's style."""
        with self._lock:
            row = self._db.execute("""SELECT AVG(lowercase) AS lowercase,
                AVG(short_messages) AS short_messages,AVG(questions) AS questions,
                AVG(exclamations) AS exclamations,AVG(emoji) AS emoji,COUNT(*) AS profiles
                FROM styles WHERE channel=? AND ts>=?""",
                                   (str(channel), _timestamp(now) - MEMORY_SECONDS)).fetchone()
            return {key: round(value, 3) if isinstance(value, float) else value
                    for key, value in dict(row).items()} if row["profiles"] else {}

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
        self._db.execute("DELETE FROM messages WHERE user=?", (user,))
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
