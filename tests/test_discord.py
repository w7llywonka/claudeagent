"""Discord adapter integration without logging in or making network calls."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from bot.__main__ import DiscordChat
from bot.config import Config
from bot.engine import ChatEngine
from bot.store import Store


BOT_ID = 111111111111111111
USER_ID = 222222222222222222
CHANNEL_ID = 333333333333333333


class DiscordAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved_bot_messages = set()
        store = SimpleNamespace(
            is_bot_message=Mock(side_effect=lambda channel, message_id, bot_id:
                                tuple(map(str, (channel, message_id, bot_id))) in self.saved_bot_messages),
            record_bot_message=Mock(side_effect=lambda channel, message_id, bot_id:
                                    self.saved_bot_messages.add(tuple(map(str, (channel, message_id, bot_id))))),
        )
        self.engine = SimpleNamespace(
            config=SimpleNamespace(allowed_channels=frozenset({str(CHANNEL_ID)})),
            allowed=Mock(return_value=True),
            store=store,
            process=AsyncMock(side_effect=lambda event: "hey there" if event.triggered else None),
            _pending={},
        )
        self.engine.pending_incoming = ChatEngine.pending_incoming.__get__(self.engine)
        self.client = self.new_client()

    def new_client(self, engine=None):
        client = DiscordChat(engine or self.engine)
        client._connection.user = SimpleNamespace(id=BOT_ID)
        self.addAsyncCleanup(client.close)
        return client

    def real_engine(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        config = Config(
            discord_token="discord-test-token-never-valid",
            api_key="sk-ant-fake-test-key-never-valid",
            allowed_channels=frozenset({str(CHANNEL_ID)}),
            auto_reply_channels=frozenset(), owner_ids=frozenset(),
        )
        provider = SimpleNamespace(
            available=Mock(return_value=False),
            complete=AsyncMock(side_effect=AssertionError("no provider call expected")),
        )
        return ChatEngine(config, store, provider)

    def reference(self, message_id=999, **changes):
        fields = {
            "message_id": message_id,
            "channel_id": CHANNEL_ID,
            "type": discord.MessageReferenceType.default,
            "resolved": None,
            "cached_message": None,
        }
        fields.update(changes)
        return SimpleNamespace(**fields)

    def target_message(self, author_id=BOT_ID, message_id=999):
        target = Mock(spec=discord.Message)
        target.id = message_id
        target.author = SimpleNamespace(id=author_id)
        target.channel = SimpleNamespace(id=CHANNEL_ID)
        return target

    def message(self, content="hey there", **changes):
        channel = SimpleNamespace(id=CHANNEL_ID, send=AsyncMock(return_value=SimpleNamespace(id=555)))
        channel.fetch_message = AsyncMock(
            spec=discord.abc.Messageable.fetch_message.__get__(channel), return_value=self.target_message(),
        )
        fields = {
            "id": 888888888888888888,
            "content": content,
            "author": SimpleNamespace(id=USER_ID, bot=False),
            "channel": channel,
            "guild": SimpleNamespace(id=444),
            "mentions": [],
            "reference": None,
            "webhook_id": None,
        }
        fields.update(changes)
        return SimpleNamespace(**fields)

    async def test_mention_triggers_and_only_bot_mentions_are_removed(self):
        message = self.message(
            f"<@{BOT_ID}> hey <@!{BOT_ID}> how is <@777777777777777777>",
            mentions=[SimpleNamespace(id=BOT_ID)],
            reference=self.reference(),
        )
        await self.client.on_message(message)
        self.engine.process.assert_awaited_once()
        event = self.engine.process.await_args.args[0]
        self.assertTrue(event.triggered)
        self.assertEqual(event.text, "hey  how is <@777777777777777777>")
        self.assertEqual((event.user, event.channel), (str(USER_ID), str(CHANNEL_ID)))
        self.assertEqual(event.message_id, "888888888888888888")
        self.assertFalse(event.is_dm)
        self.assertFalse(event.is_bot)
        message.channel.fetch_message.assert_not_awaited()

    async def test_reply_to_recent_bot_message_triggers_without_resolving_reference(self):
        initial = self.message(mentions=[SimpleNamespace(id=BOT_ID)])
        await self.client.on_message(initial)
        self.assertIn(555, self.client._own_message_ids)
        self.engine.process.reset_mock()
        reply = self.message(reference=self.reference(555))
        await self.client.on_message(reply)
        self.assertTrue(self.engine.process.await_args.args[0].triggered)
        reply.channel.fetch_message.assert_not_awaited()

    async def test_cached_discord_reply_to_bot_triggers(self):
        for cache_field in ("resolved", "cached_message"):
            with self.subTest(cache_field=cache_field):
                message = self.message(reference=self.reference(**{cache_field: self.target_message()}))
                await self.client.on_message(message)
                self.assertTrue(self.engine.process.await_args.args[0].triggered)
                message.channel.fetch_message.assert_not_awaited()

    async def test_unresolved_old_bot_reply_fetches_one_target_once_and_caches_result(self):
        message = self.message(reference=self.reference())
        await self.client.on_message(message)
        self.assertTrue(self.engine.process.await_args.args[0].triggered)
        message.channel.fetch_message.assert_awaited_once_with(999)
        self.assertIn((str(CHANNEL_ID), "999", str(BOT_ID)), self.saved_bot_messages)
        message.channel.fetch_message.reset_mock()
        await self.client.on_message(message)
        self.assertTrue(self.engine.process.await_args.args[0].triggered)
        message.channel.fetch_message.assert_not_awaited()

    async def test_reply_to_nonbot_does_not_trigger_and_negative_result_is_cached(self):
        message = self.message(reference=self.reference())
        message.channel.fetch_message.return_value = self.target_message(author_id=USER_ID)
        for _ in range(2):
            await self.client.on_message(message)
            self.assertFalse(self.engine.process.await_args.args[0].triggered)
        message.channel.fetch_message.assert_awaited_once_with(999)
        message.channel.send.assert_not_awaited()
        self.assertNotIn((str(CHANNEL_ID), "999", str(BOT_ID)), self.saved_bot_messages)

    async def test_disallowed_channel_skips_reference_lookup_and_engine(self):
        self.engine.allowed.return_value = False
        message = self.message(reference=self.reference())
        await self.client.on_message(message)
        message.channel.fetch_message.assert_not_awaited()
        self.engine.store.is_bot_message.assert_not_called()
        self.engine.process.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_missing_or_forbidden_reply_target_does_not_trigger_a_reply(self):
        for status, exception_type in ((404, discord.NotFound), (403, discord.Forbidden)):
            with self.subTest(status=status):
                message = self.message(reference=self.reference(message_id=1000 + status))
                response = SimpleNamespace(status=status, reason="test failure")
                message.channel.fetch_message.side_effect = exception_type(response, "target unavailable")
                await self.client.on_message(message)
                message.channel.fetch_message.assert_awaited_once()
                self.assertFalse(self.engine.process.await_args.args[0].triggered)
                message.channel.send.assert_not_awaited()

    async def test_forwarded_cross_channel_and_deleted_references_are_not_replies(self):
        for reference in (
            self.reference(type=discord.MessageReferenceType.forward, resolved=self.target_message()),
            self.reference(channel_id=CHANNEL_ID + 1, resolved=self.target_message()),
            self.reference(resolved=Mock(spec=discord.DeletedReferencedMessage)),
        ):
            with self.subTest(reference=reference):
                message = self.message(reference=reference)
                await self.client.on_message(message)
                self.assertFalse(self.engine.process.await_args.args[0].triggered)
                message.channel.fetch_message.assert_not_awaited()
                message.channel.send.assert_not_awaited()

    async def test_new_client_recognizes_persisted_bot_reply_without_fetch(self):
        original = self.message(mentions=[SimpleNamespace(id=BOT_ID)])
        await self.client.on_message(original)
        self.assertIn((str(CHANNEL_ID), "555", str(BOT_ID)), self.saved_bot_messages)
        fresh_client = self.new_client()
        self.assertEqual(len(fresh_client._own_message_ids), 0)
        reply = self.message(reference=self.reference(message_id=555))
        await fresh_client.on_message(reply)
        self.assertTrue(self.engine.process.await_args.args[0].triggered)
        reply.channel.fetch_message.assert_not_awaited()

    async def test_privacy_command_during_lookup_prevents_old_message_from_being_archived_again(self):
        for command in ("!forget", "!optout"):
            with self.subTest(command=command):
                engine = self.real_engine()
                client = self.new_client(engine)
                started, release = asyncio.Event(), asyncio.Event()
                old_message = self.message("old private text", reference=self.reference())

                async def fetch(message_id):
                    started.set()
                    await release.wait()
                    return self.target_message(message_id=message_id)

                old_message.channel.fetch_message.side_effect = fetch
                pending = asyncio.create_task(client.on_message(old_message))
                try:
                    await asyncio.wait_for(started.wait(), 1)
                    await client.on_message(self.message(command, id=999888777666555444))
                finally:
                    release.set()
                    await asyncio.wait_for(pending, 1)
                self.assertEqual(engine.store._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
                self.assertEqual(engine.store.recent(str(CHANNEL_ID)), [])
                engine.provider.complete.assert_not_awaited()
                old_message.channel.send.assert_not_awaited()
                self.assertEqual(engine._pending, {})
                self.assertEqual(client._reference_inflight, 0)

    async def test_another_users_forget_during_lookup_keeps_unrelated_incoming_message(self):
        engine = self.real_engine()
        client = self.new_client(engine)
        started, release = asyncio.Event(), asyncio.Event()
        old_message = self.message("keep this incoming text", reference=self.reference())

        async def fetch(message_id):
            started.set()
            await release.wait()
            return self.target_message(message_id=message_id)

        old_message.channel.fetch_message.side_effect = fetch
        pending = asyncio.create_task(client.on_message(old_message))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await client.on_message(self.message("!forget", author=SimpleNamespace(id=USER_ID + 1, bot=False)))
        finally:
            release.set()
            await asyncio.wait_for(pending, 1)
        rows = engine.store._db.execute("SELECT user,text FROM messages").fetchall()
        self.assertEqual([(row["user"], row["text"]) for row in rows], [(str(USER_ID), "keep this incoming text")])
        old_message.channel.send.assert_awaited_once()
        engine.provider.complete.assert_not_awaited()
        self.assertEqual(engine._pending, {})

    async def test_reference_lookup_concurrency_is_bounded_without_waiting_queue(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        fetch_count = 0

        async def fetch(message_id):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 2:
                started.set()
            await gate.wait()
            return self.target_message(message_id=message_id)

        messages = [self.message(reference=self.reference(message_id=600 + index)) for index in range(3)]
        for message in messages:
            message.channel.fetch_message.side_effect = fetch
        first_two = [asyncio.create_task(self.client.on_message(message)) for message in messages[:2]]
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(self.client.on_message(messages[2]), 0.5)
            messages[2].channel.fetch_message.assert_not_awaited()
            self.assertFalse(self.engine.process.await_args.args[0].triggered)
            self.assertEqual(fetch_count, 2)
        finally:
            gate.set()
            await asyncio.wait_for(asyncio.gather(*first_two), 1)

    async def test_reference_lookup_minute_limit_rejects_twenty_first_unknown_target(self):
        messages = [self.message(reference=self.reference(message_id=700 + index)) for index in range(21)]
        for message in messages:
            message.channel.fetch_message.return_value = self.target_message(author_id=USER_ID)
            await self.client.on_message(message)
        self.assertEqual(sum(message.channel.fetch_message.await_count for message in messages), 20)
        messages[-1].channel.fetch_message.assert_not_awaited()
        self.assertFalse(self.engine.process.await_args.args[0].triggered)

    async def test_bot_and_webhook_messages_never_reach_engine(self):
        for message in (
            self.message(author=SimpleNamespace(id=999, bot=True)),
            self.message(webhook_id=123),
        ):
            with self.subTest(author_bot=message.author.bot, webhook_id=message.webhook_id):
                await self.client.on_message(message)
                message.channel.send.assert_not_awaited()
        self.engine.process.assert_not_awaited()
        self.assertEqual(self.client._handlers, set())

    async def test_outgoing_reply_has_plain_content_no_mentions_and_embeds_suppressed(self):
        message = self.message(mentions=[SimpleNamespace(id=BOT_ID)])
        await self.client.on_message(message)
        message.channel.send.assert_awaited_once()
        args, options = message.channel.send.await_args
        self.assertEqual(args, ("hey there",))
        self.assertEqual(set(options), {"allowed_mentions", "suppress_embeds"})
        self.assertTrue(options["suppress_embeds"])
        mentions = options["allowed_mentions"]
        self.assertIsInstance(mentions, discord.AllowedMentions)
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.replied_user)
        self.assertEqual(self.client._handlers, set())

    async def test_untriggered_dm_metadata_and_silent_engine_result(self):
        self.engine.process.side_effect = None
        self.engine.process.return_value = None
        message = self.message(guild=None)
        await self.client.on_message(message)
        event = self.engine.process.await_args.args[0]
        self.assertTrue(event.is_dm)
        self.assertFalse(event.triggered)
        message.channel.send.assert_not_awaited()
        self.assertEqual(len(self.client._own_message_ids), 0)


if __name__ == "__main__":
    unittest.main()
