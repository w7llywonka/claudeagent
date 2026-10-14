"""Public slash-command integration using a real engine and offline interactions."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from bot.__main__ import DiscordChat
from bot.config import Config
from bot.engine import ChatEngine, Incoming
from bot.provider import Completion
from bot.store import Store


BOT_ID = 111111111111111111
USER_ID = 222222222222222222
CHANNEL_ID = 333333333333333333
ALLOWED_CHANNEL_ID = 444444444444444444
GUILD_ID = 555555555555555555


class LocalProvider:
    def __init__(self):
        self.calls = []
        self.error = None
        self.blocked_until = 0

    def available(self):
        return True

    async def complete(self, payload):
        self.calls.append(payload)
        if self.error:
            raise self.error
        return Completion("yeah, that sounds pretty nice", 10, 20, 8)


class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = time.time()
        self.config = Config(
            discord_token="discord-fake-test-token-never-valid",
            api_key="sk-ant-fake-test-key-never-valid",
            allowed_channels=frozenset({str(ALLOWED_CHANNEL_ID)}),
            auto_reply_channels=frozenset(), owner_ids=frozenset(),
        )
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.provider = LocalProvider()
        self.engine = ChatEngine(self.config, self.store, self.provider, clock=lambda: self.now)
        self.client = DiscordChat(self.engine)
        self.client._connection.user = SimpleNamespace(id=BOT_ID)
        self.addAsyncCleanup(self.client.close)

    def interaction(self, *, user_id=USER_ID, channel_id=CHANNEL_ID, guild_id=GUILD_ID,
                    interaction_id=666666666666666666, external_apps=True):
        state = {"responded": False}

        async def defer(**kwargs):
            state["responded"] = True
            return SimpleNamespace(is_ephemeral=Mock(return_value=kwargs.get("ephemeral", False)))

        return SimpleNamespace(
            id=interaction_id,
            user=SimpleNamespace(id=user_id, bot=False),
            guild_id=guild_id,
            guild=None,  # User-installed apps need not have a cached Guild object.
            channel_id=channel_id,
            channel=SimpleNamespace(
                id=channel_id,
                send=AsyncMock(side_effect=AssertionError("must not broadcast through channel.send")),
                fetch_message=AsyncMock(side_effect=AssertionError("must not fetch unrelated chat")),
                history=Mock(side_effect=AssertionError("must not fetch chat history")),
            ),
            permissions=SimpleNamespace(use_external_apps=external_apps),
            app_permissions=SimpleNamespace(use_external_apps=external_apps),
            is_user_integration=Mock(return_value=True),
            is_guild_integration=Mock(return_value=False),
            response=SimpleNamespace(
                defer=AsyncMock(side_effect=defer),
                send_message=AsyncMock(),
                is_done=Mock(side_effect=lambda: state["responded"]),
            ),
            edit_original_response=AsyncMock(return_value=SimpleNamespace(id=777777777777777777)),
            delete_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def archived(self):
        return [dict(row) for row in self.store._db.execute(
            "SELECT user,channel,text FROM messages ORDER BY id"
        )]

    def gateway_message(self, interaction, *, message_id, referenced_id=777777777777777777,
                        mentioned=False, resolved=True, has_reference=True, target_author=BOT_ID, client=None):
        client = client or self.client
        guild = SimpleNamespace(id=interaction.guild_id) if interaction.guild_id is not None else None
        channel = SimpleNamespace(
            id=interaction.channel_id, guild=guild,
            send=AsyncMock(return_value=SimpleNamespace(id=message_id + 1000)),
            fetch_message=AsyncMock(side_effect=AssertionError("resolved or persisted reference should not fetch")),
        )

        def author(user_id):
            return {"id": str(user_id), "username": "offline_user", "discriminator": "0000",
                    "avatar": None, "bot": user_id == BOT_ID}

        data = {
            "id": str(message_id), "channel_id": str(channel.id), "type": 19 if has_reference else 0,
            "content": (f"<@{BOT_ID}> " if mentioned else "") + "what about our orchids?",
            "author": author(interaction.user.id), "mentions": [author(BOT_ID)] if mentioned else [],
            "mention_roles": [], "attachments": [], "embeds": [],
        }
        if interaction.guild_id is not None:
            data["guild_id"] = str(interaction.guild_id)
        if has_reference:
            data["message_reference"] = {"message_id": str(referenced_id), "channel_id": str(channel.id)}
            if interaction.guild_id is not None:
                data["message_reference"]["guild_id"] = str(interaction.guild_id)
            if resolved:
                data["referenced_message"] = {
                    "id": str(referenced_id), "channel_id": str(channel.id), "type": 20,
                    "content": "earlier slash response", "author": author(target_author),
                    "mentions": [], "mention_roles": [], "attachments": [], "embeds": [],
                    "webhook_id": str(BOT_ID),
                }
        return discord.Message(state=client._connection, channel=channel, data=data)

    def application(self, *, guild_scopes=("bot", "applications.commands"),
                    user_scopes=("applications.commands",), permissions=None, legacy=None):
        permissions = permissions or discord.Permissions(view_channel=True, send_messages=True)
        app = SimpleNamespace(
            id=BOT_ID,
            guild_integration_config=(SimpleNamespace(oauth2_install_params=SimpleNamespace(
                scopes=list(guild_scopes), permissions=permissions)) if guild_scopes is not None else None),
            user_integration_config=(SimpleNamespace(oauth2_install_params=SimpleNamespace(
                scopes=list(user_scopes), permissions=discord.Permissions.none())) if user_scopes is not None else None),
            install_params=legacy,
            edit=AsyncMock(),
        )
        app.edit.return_value = app
        return app

    async def test_global_command_metadata_allows_both_installs_and_all_contexts(self):
        command = self.client.tree.get_command("ask")
        self.assertIsNotNone(command)
        metadata = command.to_dict(self.client.tree)
        self.assertEqual(set(metadata["integration_types"]), {0, 1})
        self.assertEqual(set(metadata["contexts"]), {0, 1, 2})
        self.assertEqual(self.client.tree.get_commands(guild=discord.Object(id=GUILD_ID)), [])
        question = next(option for option in metadata["options"] if option["name"] == "question")
        self.assertTrue(question["required"])
        self.assertEqual((question["min_length"], question["max_length"]), (1, 4000))

    async def test_setup_syncs_global_commands_without_guild_override(self):
        app = self.application()
        self.client._application = app
        self.client.tree.sync = AsyncMock(return_value=[self.client.tree.get_command("ask")])
        await self.client.setup_hook()
        app.edit.assert_not_awaited()
        self.client.tree.sync.assert_awaited_once_with()

    async def test_registration_adds_missing_command_scopes_without_changing_permissions_or_other_fields(self):
        permissions = discord.Permissions(view_channel=True, send_messages=True, manage_messages=True)
        app = self.application(guild_scopes=("bot", "identify"), user_scopes=("identify",), permissions=permissions)
        self.client._application = app
        self.client.tree.sync = AsyncMock(return_value=[])
        await self.client.tree.register()
        app.edit.assert_awaited_once()
        fields = app.edit.await_args.kwargs
        self.assertEqual(set(fields), {"guild_install_scopes", "guild_install_permissions", "user_install_scopes"})
        self.assertEqual(fields["guild_install_scopes"], ["bot", "identify", "applications.commands"])
        self.assertEqual(fields["user_install_scopes"], ["identify", "applications.commands"])
        self.assertIs(fields["guild_install_permissions"], permissions)
        self.client.tree.sync.assert_awaited_once_with()

    async def test_registration_preserves_legacy_guild_permissions_when_install_configs_are_missing(self):
        permissions = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True)
        legacy = SimpleNamespace(scopes=["bot", "identify"], permissions=permissions)
        app = self.application(guild_scopes=None, user_scopes=None, legacy=legacy)
        self.client._application = app
        self.client.tree.sync = AsyncMock(return_value=[])
        await self.client.tree.register()
        fields = app.edit.await_args.kwargs
        self.assertEqual(fields["guild_install_scopes"], ["bot", "identify", "applications.commands"])
        self.assertIs(fields["guild_install_permissions"], permissions)
        self.assertEqual(fields["user_install_scopes"], ["applications.commands"])

    async def test_public_defer_happens_before_engine_and_only_original_response_is_edited(self):
        interaction = self.interaction()
        started, release = asyncio.Event(), asyncio.Event()

        async def defer(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(is_ephemeral=Mock(return_value=False))

        interaction.response.defer.side_effect = defer
        pending = asyncio.create_task(self.client.tree.ask(interaction, "how is it going?"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(self.provider.calls, [])
            self.assertEqual(self.archived(), [])
        finally:
            release.set()
            await asyncio.wait_for(pending, 1)
        interaction.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
        interaction.edit_original_response.assert_awaited_once()
        options = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(options["content"], "yeah, that sounds pretty nice")
        mentions = options["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.replied_user)
        self.assertNotIn("file", options)
        self.assertNotIn("files", options)
        self.assertFalse(options.get("embed"))
        self.assertFalse(options.get("embeds"))
        self.assertFalse(options.get("attachments"))
        interaction.channel.send.assert_not_awaited()
        interaction.channel.fetch_message.assert_not_awaited()
        interaction.channel.history.assert_not_called()
        interaction.followup.send.assert_not_awaited()

    async def test_external_slash_memory_is_scoped_to_invoker_and_channel(self):
        scope = f"ask:{CHANNEL_ID}:{USER_ID}"
        self.store.observe(str(CHANNEL_ID), "ambient-user", "ambient server secret", now=self.now)
        self.store.observe(f"ask:{CHANNEL_ID}:{USER_ID + 1}", str(USER_ID + 1), "another invoker secret", now=self.now)
        self.store.observe(f"ask:{CHANNEL_ID + 1}:{USER_ID}", str(USER_ID), "another channel secret", now=self.now)
        self.store.observe(scope, str(USER_ID), "my previous chess message", now=self.now)
        self.store.remember(scope, str(USER_ID), "I like chess", now=self.now)
        interaction = self.interaction()
        await self.client.tree.ask(interaction, "let's chat about chess")
        payload = self.provider.calls[0]
        data = json.loads(payload["messages"][0]["content"])
        self.assertIn("my previous chess message", json.dumps(data))
        self.assertEqual(data["remembered_facts_from_current_speaker"], ["I like chess"])
        for excluded in ("ambient server secret", "another invoker secret", "another channel secret"):
            self.assertNotIn(excluded, json.dumps(payload))
        self.assertEqual(self.archived(), [{"user": str(USER_ID), "channel": scope, "text": "let's chat about chess"}])
        interaction.channel.history.assert_not_called()
        interaction.channel.fetch_message.assert_not_awaited()

    async def test_allowed_guild_slash_uses_existing_channel_context(self):
        self.store.observe(str(ALLOWED_CHANNEL_ID), "other", "shared room context", now=self.now)
        await self.client.tree.ask(self.interaction(channel_id=ALLOWED_CHANNEL_ID), "hello room")
        data = json.loads(self.provider.calls[0]["messages"][0]["content"])
        self.assertIn("shared room context", json.dumps(data["recent_conversation"]))
        self.assertEqual(self.archived()[0]["channel"], str(ALLOWED_CHANNEL_ID))

    async def test_real_replies_to_slash_work_outside_allowlist_and_in_dms_with_either_mention_toggle(self):
        for index, (is_dm, mentioned) in enumerate(((False, False), (False, True), (True, False), (True, True))):
            with self.subTest(is_dm=is_dm, mentioned=mentioned):
                self.now += 16
                interaction = self.interaction(
                    channel_id=CHANNEL_ID + int(is_dm), guild_id=None if is_dm else GUILD_ID,
                    interaction_id=666666666666666700 + index,
                )
                response_id = 777777777777777700 + index
                interaction.edit_original_response.return_value = SimpleNamespace(id=response_id)
                await self.client.tree.ask(interaction, "our original orchids conversation")
                before = len(self.provider.calls)
                self.now += 16
                message = self.gateway_message(interaction, message_id=888888888888888700 + index,
                                               referenced_id=response_id, mentioned=mentioned)
                self.assertEqual(message.type, discord.MessageType.reply)
                self.assertEqual(message.reference.type, discord.MessageReferenceType.reply)
                self.assertIsInstance(message.reference.resolved, discord.Message)
                await self.client.on_message(message)
                message.channel.send.assert_awaited_once()
                message.channel.fetch_message.assert_not_awaited()
                self.assertEqual(len(self.provider.calls), before + 1)
                data = json.loads(self.provider.calls[-1]["messages"][0]["content"])
                self.assertIn("our original orchids conversation", json.dumps(data["recent_conversation"]))
                self.assertEqual(self.archived()[-1]["channel"], f"ask:{interaction.channel_id}:{USER_ID}")

    async def test_real_reply_after_restart_uses_saved_slash_response_id_without_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "reply.sqlite3")
            store = Store(path)
            first_engine = ChatEngine(self.config, store, self.provider, clock=lambda: self.now)
            first_client = DiscordChat(first_engine)
            first_client._connection.user = SimpleNamespace(id=BOT_ID)
            interaction = self.interaction()
            try:
                await first_client.tree.ask(interaction, "our original orchids conversation")
            finally:
                await first_client.close()
                store.close()
            self.now += 16
            reopened = Store(path)
            restarted_engine = ChatEngine(self.config, reopened, self.provider, clock=lambda: self.now)
            restarted_client = DiscordChat(restarted_engine)
            restarted_client._connection.user = SimpleNamespace(id=BOT_ID)
            try:
                self.assertEqual(len(restarted_client._own_message_ids), 0)
                message = self.gateway_message(interaction, message_id=888888888888888799,
                                               resolved=False, client=restarted_client)
                self.assertIsNone(message.reference.resolved)
                self.assertIsNone(message.reference.cached_message)
                await restarted_client.on_message(message)
                message.channel.fetch_message.assert_not_awaited()
                message.channel.send.assert_awaited_once()
                self.assertEqual(len(self.provider.calls), 2)
                data = json.loads(self.provider.calls[-1]["messages"][0]["content"])
                self.assertIn("our original orchids conversation", json.dumps(data["recent_conversation"]))
            finally:
                await restarted_client.close()
                reopened.close()

    async def test_unrelated_gateway_messages_and_replies_to_people_stay_ignored_outside_allowlist(self):
        interaction = self.interaction()
        await self.client.tree.ask(interaction, "initial addressed question")
        self.now += 16
        before_archive = list(self.archived())
        for index, (has_reference, mentioned) in enumerate(((False, False), (False, True), (True, False))):
            with self.subTest(has_reference=has_reference, mentioned=mentioned):
                message = self.gateway_message(interaction, message_id=888888888888888800 + index,
                                               has_reference=has_reference, mentioned=mentioned,
                                               target_author=USER_ID)
                await self.client.on_message(message)
                message.channel.send.assert_not_awaited()
                message.channel.fetch_message.assert_not_awaited()
        self.assertEqual(self.archived(), before_archive)
        self.assertEqual(len(self.provider.calls), 1)

    async def test_slash_and_normal_messages_share_user_cooldown(self):
        await self.engine.process(Incoming(str(USER_ID), str(ALLOWED_CHANNEL_ID), "ordinary chat", True))
        interaction = self.interaction()
        await self.client.tree.ask(interaction, "try slash during cooldown")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertTrue(interaction.edit_original_response.await_args.kwargs["content"])
        self.now += 15
        await self.client.tree.ask(self.interaction(interaction_id=666666666666666667), "after cooldown")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertIsNone(await self.engine.process(Incoming(str(USER_ID), str(ALLOWED_CHANNEL_ID), "still shared", True)))
        self.assertEqual(len(self.provider.calls), 2)

    async def test_slash_obeys_existing_budget_without_paid_call(self):
        self.engine.config = replace(self.config, day_limit=1)
        interaction = self.interaction()
        await self.client.tree.ask(interaction, "can we talk?")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.store.spend(self.now)["total"], 0)
        self.assertTrue(interaction.edit_original_response.await_args.kwargs["content"])
        self.assertEqual(len(self.archived()), 1)

    async def test_direct_message_slash_works_without_enabling_passive_dm_messages(self):
        self.assertFalse(self.config.enable_dms)
        interaction = self.interaction(guild_id=None, external_apps=False)
        await self.client.tree.ask(interaction, "hello from a dm")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.archived()[0]["channel"], f"ask:{CHANNEL_ID}:{USER_ID}")
        interaction.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
        interaction.channel.fetch_message.assert_not_awaited()

    async def test_server_blocking_external_public_apps_gets_private_notice_without_api(self):
        interaction = self.interaction(external_apps=False)
        await self.client.tree.ask(interaction, "do not charge for a hidden reply")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.archived(), [])
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        interaction.response.defer.assert_not_awaited()
        interaction.channel.send.assert_not_awaited()

    async def test_discord_forced_private_or_unknown_visibility_never_spends_or_archives(self):
        for callback in (None, SimpleNamespace(is_ephemeral=Mock(return_value=True))):
            with self.subTest(callback=callback):
                interaction = self.interaction()
                interaction.response.defer.side_effect = None
                interaction.response.defer.return_value = callback
                await self.client.tree.ask(interaction, "keep this out of a private response")
                self.assertEqual(self.provider.calls, [])
                self.assertEqual(self.archived(), [])
                interaction.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
                interaction.edit_original_response.assert_awaited_once()
                interaction.channel.send.assert_not_awaited()

    async def test_privacy_commands_acknowledge_erasure_even_when_external_public_apps_are_blocked(self):
        scope = f"ask:{CHANNEL_ID}:{USER_ID}"
        for command in ("!forget", "!optout"):
            with self.subTest(command=command):
                self.now += 15
                self.store.archive_message(scope, str(USER_ID), "erase local data", now=self.now)
                interaction = self.interaction(external_apps=False)
                await self.client.tree.ask(interaction, command)
                self.assertEqual(self.archived(), [])
                self.assertEqual(self.provider.calls, [])
                interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                interaction.edit_original_response.assert_awaited_once()
                self.assertTrue(interaction.edit_original_response.await_args.kwargs["content"])
                interaction.delete_original_response.assert_not_awaited()
        self.assertTrue(self.store.opted_out(str(USER_ID)))

    async def test_forget_during_defer_discards_invocation_before_archiving_or_api(self):
        interaction = self.interaction()
        started, release = asyncio.Event(), asyncio.Event()

        async def defer(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(is_ephemeral=Mock(return_value=False))

        interaction.response.defer.side_effect = defer
        pending = asyncio.create_task(self.client.tree.ask(interaction, "erase this waiting question"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await self.engine.process(Incoming(str(USER_ID), str(ALLOWED_CHANNEL_ID), "!forget", False))
        finally:
            release.set()
            await asyncio.wait_for(pending, 1)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.archived(), [])
        interaction.delete_original_response.assert_awaited_once()
        interaction.edit_original_response.assert_not_awaited()
        self.assertEqual(self.engine._pending, {})

    async def test_unexpected_error_is_logged_without_question_or_exception_body(self):
        marker = "private-question-and-provider-body-sentinel"
        self.provider.error = RuntimeError(marker)
        interaction = self.interaction()
        with self.assertLogs("bot.commands", level="ERROR") as logs:
            await self.client.tree.ask(interaction, marker)
        self.assertNotIn(marker, " ".join(logs.output))
        self.assertIn("RuntimeError", " ".join(logs.output))
        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertTrue(content)
        self.assertNotIn(marker, content)
        self.assertEqual(self.engine._pending, {})


if __name__ == "__main__":
    unittest.main()
