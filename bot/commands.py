"""Public, user-installable Discord commands using the same bounded chat engine."""

import asyncio
import logging

import discord
from discord import app_commands

from .engine import Incoming


log = logging.getLogger(__name__)
PRIVATE_ONLY = (
    "this channel only allows private app replies. a server admin can check "
    "Send Messages and Use External Apps permissions. i haven't sent your question to claude"
)
COMMAND_ERROR = "couldn't finish that request, try again in a moment"


class ChatCommands(app_commands.CommandTree):
    def __init__(self, client):
        super().__init__(client)
        self.engine = client.engine
        self.add_command(app_commands.Command(
            name="ask", description="Ask Claude a question. The answer is visible to this conversation.",
            callback=self.ask,
        ))

    async def register(self):
        # Preserve existing installation scopes/permissions when adding app commands.
        # No intents, credentials, or other application settings are changed.
        app = self.client.application or await self.client.application_info()
        guild_config = app.guild_integration_config
        user_config = app.user_integration_config
        guild_params = guild_config.oauth2_install_params if guild_config else None
        user_params = user_config.oauth2_install_params if user_config else None
        legacy = app.install_params
        guild_scopes = list(guild_params.scopes if guild_params else legacy.scopes if legacy else [])
        user_scopes = list(user_params.scopes if user_params else [])
        if (guild_config is None or user_config is None or
                "applications.commands" not in guild_scopes or "bot" not in guild_scopes or
                "applications.commands" not in user_scopes):
            permissions = (guild_params.permissions if guild_params else legacy.permissions if legacy else
                           discord.Permissions(view_channel=True, send_messages=True, read_message_history=True))
            app = await app.edit(
                guild_install_scopes=list(dict.fromkeys([*guild_scopes, "bot", "applications.commands"])),
                guild_install_permissions=permissions,
                user_install_scopes=list(dict.fromkeys([*user_scopes, "applications.commands"])),
            )
        commands = await self.sync()
        log.info("Registered global /ask; commands=%d guild_install=%s user_install=%s application_id=%s",
                 len(commands), app.guild_integration_config is not None,
                 app.user_integration_config is not None, app.id)

    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(question="What do you want to ask? The answer will be visible here.")
    async def ask(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 4000]):
        task = asyncio.current_task()
        self.client._handlers.add(task)
        try:
            if interaction.channel_id is None or interaction.user.bot:
                await interaction.response.send_message("use this command in a conversation", ephemeral=True)
                return
            if not question.strip():
                await interaction.response.send_message("type a question first", ephemeral=True)
                return
            event = Incoming(
                user=str(interaction.user.id), channel=str(interaction.channel_id), text=question,
                triggered=True, is_dm=interaction.guild_id is None, message_id=str(interaction.id),
                is_app_command=True,
            )
            if question.strip().partition(" ")[0].lower() in {"!forget", "!optout"}:
                # Erasure remains available even where Discord forbids public app replies.
                await interaction.response.defer(ephemeral=True, thinking=True)
                answer = await self.engine.process(event)
                if answer:
                    await interaction.edit_original_response(content=answer,
                                                             allowed_mentions=discord.AllowedMentions.none())
                else:
                    await interaction.delete_original_response()
                return
            # Guilds outside the gateway cache still have a guild_id. Never infer
            # a DM from interaction.guild, or read history through channel APIs.
            if (interaction.guild_id is not None and interaction.is_user_integration()
                    and not interaction.is_guild_integration() and not interaction.permissions.use_external_apps):
                await interaction.response.send_message(PRIVATE_ONLY, ephemeral=True)
                return
            with self.engine.pending_incoming(event.user) as pending:
                # Acknowledge Discord before any database/model work (3 second deadline).
                callback = await interaction.response.defer(ephemeral=False, thinking=True)
                if not pending["valid"]:
                    await interaction.delete_original_response()
                    return
                # Discord can force private visibility even when public was requested.
                # Do not charge for a private answer or mix it into public chat memory.
                if callback is None or callback.is_ephemeral():
                    await interaction.edit_original_response(content=PRIVATE_ONLY,
                                                             allowed_mentions=discord.AllowedMentions.none())
                    return
                answer = await self.engine.process(event)
                if not answer or not pending["valid"]:
                    await interaction.delete_original_response()
                    return
                sent = await interaction.edit_original_response(
                    content=answer, allowed_mentions=discord.AllowedMentions.none(),
                    embeds=[], attachments=[],
                )
                if self.client.user is not None:
                    self.client._own_message_ids.append(sent.id)
                    self.engine.store.record_bot_message(event.channel, str(sent.id), str(self.client.user.id))
        except discord.HTTPException as exc:
            log.warning("Discord slash response failed status=%s", exc.status)
        except Exception as exc:
            log.error("Slash handler failed: %s", type(exc).__name__)
            await self._error_response(interaction)
        finally:
            self.client._handlers.discard(task)

    async def _error_response(self, interaction):
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=COMMAND_ERROR,
                                                         allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(COMMAND_ERROR, ephemeral=True,
                                                        allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("Discord slash error response failed status=%s", exc.status)

    async def on_error(self, interaction, error):
        # Library command validation errors must not log user input or tracebacks.
        log.error("Slash command failed: %s", type(error).__name__)
        await self._error_response(interaction)
