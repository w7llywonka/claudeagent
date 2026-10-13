"""Discord adapter integration without logging in or making network calls."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from bot.__main__ import DiscordChat


BOT_ID = 111111111111111111
USER_ID = 222222222222222222
CHANNEL_ID = 333333333333333333


class DiscordAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = SimpleNamespace(
            config=SimpleNamespace(allowed_channels=frozenset({str(CHANNEL_ID)})),
            process=AsyncMock(return_value="hey there"),
        )
        self.client = DiscordChat(self.engine)
        self.client._connection.user = SimpleNamespace(id=BOT_ID)
        self.addAsyncCleanup(self.client.close)

    def message(self, content="hey there", **changes):
        fields = {
            "content": content,
            "author": SimpleNamespace(id=USER_ID, bot=False),
            "channel": SimpleNamespace(id=CHANNEL_ID, send=AsyncMock(return_value=SimpleNamespace(id=555))),
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
        )
        await self.client.on_message(message)
        self.engine.process.assert_awaited_once()
        event = self.engine.process.await_args.args[0]
        self.assertTrue(event.triggered)
        self.assertEqual(event.text, "hey  how is <@777777777777777777>")
        self.assertEqual((event.user, event.channel), (str(USER_ID), str(CHANNEL_ID)))
        self.assertFalse(event.is_dm)
        self.assertFalse(event.is_bot)

    async def test_reply_to_recent_bot_message_triggers_without_resolving_reference(self):
        initial = self.message()
        await self.client.on_message(initial)
        self.assertIn(555, self.client._own_message_ids)
        self.engine.process.reset_mock()
        reply = self.message(reference=SimpleNamespace(message_id=555, resolved=None))
        await self.client.on_message(reply)
        self.assertTrue(self.engine.process.await_args.args[0].triggered)

    async def test_cached_discord_reply_to_bot_triggers(self):
        resolved = Mock(spec=discord.Message)
        resolved.author = SimpleNamespace(id=BOT_ID)
        message = self.message(reference=SimpleNamespace(message_id=999, resolved=resolved))
        await self.client.on_message(message)
        self.assertTrue(self.engine.process.await_args.args[0].triggered)

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
        message = self.message()
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
