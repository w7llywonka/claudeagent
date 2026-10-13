"""Persistence, isolation, retention, and concurrent budget safety checks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bot.store import MEMORY_SECONDS, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "nested", "bot.sqlite3")
        self.now = time.time()
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_context_is_bounded_truncated_and_channel_isolated(self):
        for index in range(12):
            self.store.observe("one", str(index), "x" * 300, now=self.now + index)
        self.store.observe("two", "different", "private", now=self.now)
        recent = self.store.recent("one", 100, now=self.now + 20)
        self.assertEqual([row["user"] for row in recent], [str(i) for i in range(4, 12)])
        self.assertTrue(all(len(row["text"]) == 240 and row["is_bot"] is False for row in recent))
        self.assertEqual(self.store.recent("two", now=self.now)[0]["text"], "private")
        self.assertEqual(self.store.recent("one", now=self.now + 1900), [])

    def test_human_context_and_style_sampling_is_global_per_user(self):
        self.store.observe("one", "alice", "hey there", now=self.now)
        self.store.observe("one", "alice", "SPAM!", now=self.now + 1)
        self.store.observe("two", "alice", "SPAM!", now=self.now + 2)
        self.store.observe("one", "alice", "WHAT?", now=self.now + 15)
        self.store.observe("one", "bot", "reply", is_bot=True, now=self.now + 15)
        self.store.observe("one", "bot", "another reply", is_bot=True, now=self.now + 16)
        style = self.store.style("one", "alice")
        self.assertEqual(style["samples"], 2)
        self.assertAlmostEqual(style["lowercase"], 0.8)
        self.assertAlmostEqual(style["questions"], 0.2)
        self.assertTrue(all(isinstance(value, (int, float, bool)) for value in style.values()))
        self.assertEqual(self.store.style("one", "bot"), {})
        self.assertEqual(self.store.recent("two", now=self.now + 16), [])
        self.assertEqual(len(self.store.recent("one", now=self.now + 16)), 4)

    def test_fact_retention_retrieval_and_user_channel_isolation(self):
        self.store.remember("one", "alice", "I like chess", now=self.now)
        self.store.remember("one", "alice", "I drink tea", now=self.now + 1)
        self.store.remember("two", "alice", "other channel", now=self.now)
        self.store.remember("one", "bob", "other user", now=self.now)
        self.assertEqual(self.store.memories("one", "alice", "chess", 1), ["I like chess"])
        self.assertEqual(self.store.memories("one", "alice", "unrelated", 1), ["I drink tea"])
        for index in range(10):
            self.store.remember("bounded", "alice", str(index) + "x" * 200, now=self.now + index)
        facts = self.store.memories("bounded", "alice", "", 20)
        self.assertEqual(len(facts), 8)
        self.assertTrue(all(len(fact) == 160 for fact in facts))
        self.assertTrue(facts[-1].startswith("2x"))

    def test_read_filters_expired_facts_and_styles_without_a_write(self):
        self.store.remember("one", "alice", "old fact", now=self.now)
        self.store.observe("one", "alice", "old style", now=self.now)
        with patch("bot.store.time.time", return_value=self.now + MEMORY_SECONDS + 1):
            self.assertEqual(self.store.memories("one", "alice", "old"), [])
            self.assertEqual(self.store.style("one", "alice"), {})

    def test_global_caps_prevent_many_channels_growing_memory(self):
        with patch("bot.store.MAX_CONTEXT_ROWS", 12), patch("bot.store.MAX_MEMORY_ROWS", 12), \
                patch("bot.store.MAX_STYLE_ROWS", 12):
            for index in range(20):
                self.store.observe(str(index), str(index), "hello", now=self.now + index)
                self.store.remember(str(index), str(index), "fact", now=self.now + index)
        self.assertEqual(self.rows("context"), 12)
        self.assertEqual(self.rows("facts"), 12)
        self.assertEqual(self.rows("styles"), 12)
        self.assertEqual(self.rows("samples"), 12)

    def test_forget_clears_all_user_channels_and_all_bot_replies(self):
        for channel in ("one", "two"):
            self.store.remember(channel, "alice", "secret", now=self.now)
            self.store.observe(channel, "alice", "secret", now=self.now + (15 if channel == "two" else 0))
            self.store.observe(channel, "bot", "secret quote", is_bot=True, now=self.now)
        self.store.observe("one", "bob", "keep this", now=self.now)
        self.assertEqual(self.store.admit("alice", self.now), "ok")
        self.store.forget("alice")
        self.assertEqual(self.store.recent("one", now=self.now), [
            {"user": "bob", "text": "keep this", "is_bot": False, "sources": []}])
        self.assertEqual(self.store.recent("two", now=self.now), [])
        self.assertEqual(self.store.memories("one", "alice", "secret"), [])
        self.assertEqual(self.store.style("two", "alice"), {})
        self.assertEqual(self.store.admit("alice", self.now + 1), "cooldown")

    def test_opt_out_is_persistent_and_prevents_future_storage(self):
        self.store.observe("one", "alice", "old message", now=self.now)
        self.store.remember("one", "alice", "old fact", now=self.now)
        self.store.set_opt_out("alice", True)
        self.reopen()
        self.assertTrue(self.store.opted_out("alice"))
        self.store.observe("one", "alice", "new message", now=self.now + 20)
        self.store.remember("one", "alice", "new fact", now=self.now + 20)
        self.assertEqual(self.store.recent("one", now=self.now + 20), [])
        self.assertEqual(self.store.memories("one", "alice", ""), [])
        self.store.set_opt_out("alice", False)
        self.assertFalse(self.store.opted_out("alice"))
        self.store.observe("one", "alice", "allowed", now=self.now + 21)
        self.assertEqual(len(self.store.recent("one", now=self.now + 21)), 1)

    def test_cooldown_and_global_sliding_rate_limit_survive_restart(self):
        self.assertEqual(self.store.admit("alice", self.now, per_minute=2), "ok")
        self.reopen()
        self.assertEqual(self.store.admit("alice", self.now + 14.99, per_minute=2), "cooldown")
        self.assertEqual(self.store.admit("alice", self.now + 15, per_minute=2), "ok")
        self.assertEqual(self.store.admit("bob", self.now + 30, per_minute=2), "rate")
        self.assertEqual(self.store.admit("bob", self.now + 60, per_minute=2), "ok")
        self.assertEqual(self.store.admit("carol", self.now + 60, per_minute=2), "rate")
        self.assertEqual(self.store.admit("carol", self.now + 75, per_minute=2), "ok")

    def test_reservations_are_included_and_settlement_is_idempotent(self):
        first = self.store.reserve(60, 100, 200, 300, self.now)
        self.assertIsNotNone(first)
        self.assertEqual(self.store.spend(self.now), {"day": 60, "month": 60, "total": 60})
        self.assertIsNone(self.store.reserve(41, 100, 200, 300, self.now))
        self.store.settle(first, 20)
        self.store.settle(first, 0)
        self.assertEqual(self.store.spend(self.now)["total"], 20)
        self.assertIsNotNone(self.store.reserve(80, 100, 200, 300, self.now))
        self.assertIsNone(self.store.reserve(1, 100, 200, 300, self.now))

    def test_concurrent_connections_cannot_over_reserve(self):
        second = Store(self.path)
        barrier = threading.Barrier(2)

        def reserve(store):
            barrier.wait(timeout=5)
            return store.reserve(60, 100, 100, 100, self.now)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(reserve, [self.store, second]))
            self.assertEqual(sum(result is not None for result in results), 1)
            self.assertEqual(self.store.spend(self.now)["total"], 60)
        finally:
            second.close()

    def test_unknown_or_failed_call_remains_fully_charged_after_restart(self):
        reservation = self.store.reserve(90, 100, 100, 100, self.now)
        self.reopen()
        self.assertEqual(self.store.spend(self.now)["total"], 90)
        self.assertEqual(self.rows("reservations"), 0)
        self.store.settle(reservation, 0)
        self.assertEqual(self.store.spend(self.now)["total"], 90)
        self.assertIsNone(self.store.reserve(11, 100, 100, 100, self.now))

    def test_calendar_boundaries_and_late_settlement_use_reservation_day(self):
        jan = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        feb = jan + 2
        reservation = self.store.reserve(60, 100, 100, 100, jan)
        self.assertEqual(self.store.spend(feb), {"day": 0, "month": 0, "total": 60})
        self.assertIsNone(self.store.reserve(41, 100, 100, 100, feb))
        self.store.settle(reservation, 40)
        self.assertEqual(self.store.spend(feb), {"day": 0, "month": 0, "total": 40})
        self.assertEqual(self.store.spend(jan), {"day": 40, "month": 40, "total": 40})
        self.assertIsNotNone(self.store.reserve(60, 100, 100, 100, feb))
        self.assertEqual(self.store.spend(feb), {"day": 60, "month": 60, "total": 100})

    def test_same_month_new_day_preserves_monthly_limit(self):
        day1 = datetime(2026, 2, 1, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        day2 = day1 + 2
        reservation = self.store.reserve(60, 100, 100, 1000, day1)
        self.store.settle(reservation, 60)
        self.assertEqual(self.store.spend(day2), {"day": 0, "month": 60, "total": 60})
        self.assertIsNone(self.store.reserve(41, 100, 100, 1000, day2))

    def test_completed_calls_compact_into_daily_aggregates(self):
        for _ in range(100):
            reservation = self.store.reserve(10, 2000, 2000, 2000, self.now)
            self.store.settle(reservation, 3)
        self.assertEqual(self.rows("ledger"), 1)
        self.assertEqual(self.rows("reservations"), 0)
        self.assertEqual(self.store.spend(self.now)["total"], 300)
        self.reopen()
        self.assertEqual(self.store.spend(self.now)["total"], 300)

    def test_money_inputs_reject_negative_fractional_or_boolean_values(self):
        for invalid in (-1, 0.5, True, 1 << 64):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    self.store.reserve(invalid, 100, 100, 100, self.now)
                with self.assertRaises(ValueError):
                    self.store.settle("missing", invalid)
        with self.assertRaises(ValueError):
            self.store.reserve(1, -1, 100, 100, self.now)


    def test_bot_provenance_survives_source_message_eviction(self):
        self.store.observe("one", "alice", "my original message", now=self.now)
        for index in range(7):
            self.store.observe("one", f"other{index}", "hello", now=self.now + index + 1)
        self.store.observe("one", "bot", "an answer referencing alice", is_bot=True,
                           now=self.now + 9, sources=["bob", "alice", "alice"])
        recent = self.store.recent("one", limit=8, now=self.now + 10)
        self.assertNotIn("alice", [row["user"] for row in recent])
        self.assertEqual(recent[-1]["sources"], ["alice", "bob"])
        self.assertTrue(all(row["sources"] == [] for row in recent[:-1]))
        self.reopen()
        self.assertEqual(self.store.recent("one", now=self.now + 10)[-1]["sources"], ["alice", "bob"])
        self.store.forget("alice")
        self.assertFalse(any(row["is_bot"] for row in self.store.recent("one", now=self.now + 10)))

    def test_bot_with_too_many_sources_is_not_retained(self):
        self.store.observe("one", "bot", "safe answer", is_bot=True, now=self.now,
                           sources=[str(index) for index in range(64)])
        self.store.observe("one", "bot", "too many dependencies", is_bot=True, now=self.now + 1,
                           sources=[str(index) for index in range(65)])
        recent = self.store.recent("one", now=self.now + 1)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["text"], "safe answer")
        self.assertEqual(len(recent[0]["sources"]), 64)

    def test_legacy_schema_migrates_without_unattributed_bot_quotes(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("ALTER TABLE context DROP COLUMN sources")
            connection.execute("INSERT INTO context(channel,user,text,is_bot,ts) VALUES(?,?,?,?,?)",
                               ("one", "alice", "human message", 0, self.now))
            connection.execute("INSERT INTO context(channel,user,text,is_bot,ts) VALUES(?,?,?,?,?)",
                               ("one", "bot", "unattributed quote", 1, self.now))
            connection.commit()
        self.store = Store(self.path)
        self.assertEqual(self.store.recent("one", now=self.now), [
            {"user": "alice", "text": "human message", "is_bot": False, "sources": []}])


if __name__ == "__main__":
    unittest.main()
