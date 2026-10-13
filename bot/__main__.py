import asyncio
from collections import deque, OrderedDict
import logging
import re
import signal
import time

import discord

from .config import Config
from .engine import ChatEngine, Incoming
from .provider import ClaudeClient
from .store import Store


log = logging.getLogger(__name__)


class DiscordChat(discord.Client):
    def __init__(self, engine):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(),
                         max_messages=256, member_cache_flags=discord.MemberCacheFlags.none())
        self.engine = engine
        self._own_message_ids = deque(maxlen=512)
        self._reference_results = OrderedDict()
        self._reference_lookups = deque(maxlen=20)
        self._reference_inflight = 0
        self._handlers = set()

    async def on_ready(self):
        log.info("Connected to Discord; model=claude-haiku-5-5 effort=low; allowed_channels=%d", len(self.engine.config.allowed_channels))
        config = self.engine.config
        log.info("Message archive: passive=%s retention_days=%d max_rows=%d max_text_mib=%d",
                 config.passive_learning, config.message_retention_days, config.message_max_rows, config.message_max_mb)

    async def _is_reply_to_bot(self, message):
        reference = message.reference
        if reference is None or self.user is None or not reference.message_id:
            return False
        # Forwards/cross-channel references are not a reply addressed to this bot.
        if getattr(reference, "type", discord.MessageReferenceType.reply) != discord.MessageReferenceType.reply:
            return False
        if getattr(reference, "channel_id", message.channel.id) != message.channel.id:
            return False
        resolved = reference.resolved
        if isinstance(resolved, discord.DeletedReferencedMessage):
            return False
        if isinstance(resolved, discord.Message):
            return resolved.author.id == self.user.id
        cached = getattr(reference, "cached_message", None)
        if isinstance(cached, discord.Message):
            return cached.author.id == self.user.id
        if reference.message_id in self._own_message_ids:
            return True
        channel, message_id, bot_id = str(message.channel.id), str(reference.message_id), str(self.user.id)
        if self.engine.store.is_bot_message(channel, message_id, bot_id):
            return True
        key = (channel, message_id)
        if key in self._reference_results:
            self._reference_results.move_to_end(key)
            return self._reference_results[key]
        now = time.monotonic()
        while self._reference_lookups and now - self._reference_lookups[0] >= 60:
            self._reference_lookups.popleft()
        if len(self._reference_lookups) >= 20 or self._reference_inflight >= 2:
            return False
        self._reference_lookups.append(now)
        self._reference_inflight += 1
        try:
            # Only inspect the referenced message's author, never scan chat history.
            # This Discord lookup costs no Claude tokens and has no waiting queue.
            async with asyncio.timeout(5):
                target = await message.channel.fetch_message(reference.message_id)
            replied = target.author.id == self.user.id
            self._reference_results[key] = replied
            if len(self._reference_results) > 512:
                self._reference_results.popitem(last=False)
            if replied:
                self.engine.store.record_bot_message(channel, message_id, bot_id)
            return replied
        except discord.Forbidden:
            log.warning("Cannot resolve old reply: grant Read Message History in the bot's channel")
            return False
        except (discord.HTTPException, TimeoutError):
            return False
        finally:
            self._reference_inflight -= 1

    async def on_message(self, message):
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            if self.user is None or message.author.bot or message.webhook_id:
                return
            bot_id = self.user.id
            mentioned = any(user.id == bot_id for user in message.mentions)
            text = re.sub(rf"<@!?{bot_id}>", "", message.content).strip()
            candidate = Incoming(str(message.author.id), str(message.channel.id), text,
                                 mentioned, message.guild is None, message.author.bot)
            if not self.engine.allowed(candidate):
                return
            replied = False if mentioned else await self._is_reply_to_bot(message)
            event = Incoming(str(message.author.id), str(message.channel.id), text,
                             mentioned or replied, message.guild is None, message.author.bot,
                             message_id=str(message.id))
            answer = await self.engine.process(event)
            if answer:
                # Plain content only. No embeds, files, tools, eval or command dispatch.
                sent = await message.channel.send(answer, allowed_mentions=discord.AllowedMentions.none(),
                                                  suppress_embeds=True)
                self._own_message_ids.append(sent.id)
                self.engine.store.record_bot_message(str(message.channel.id), str(sent.id), str(bot_id))
        except discord.HTTPException as exc:
            log.warning("Discord send failed status=%s", exc.status)
        except Exception as exc:
            # Do not dump user text, API bodies, or environment secrets in tracebacks.
            log.error("Message handler failed: %s", type(exc).__name__)
        finally:
            self._handlers.discard(task)

    async def on_error(self, event_method, *args, **kwargs):
        log.error("Discord event failed: %s", event_method)


async def run(config):
    store = Store(config.db_path, message_retention_days=config.message_retention_days,
                  message_max_rows=config.message_max_rows, message_max_bytes=config.message_max_mb * 1024 * 1024)
    provider = ClaudeClient(config.api_key)
    client = DiscordChat(ChatEngine(config, store, provider))
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(name, lambda: asyncio.create_task(client.close()))
        except NotImplementedError:
            pass  # Windows Ctrl-C is handled by asyncio.run.
    try:
        async with client:
            await client.start(config.discord_token)
    finally:
        for task in list(client._handlers):
            task.cancel()
        await asyncio.gather(*list(client._handlers), return_exceptions=True)
        await provider.close()
        store.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        config = Config.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except discord.LoginFailure:
        raise SystemExit("Discord rejected DISCORD_TOKEN; check Railway variables") from None
    except discord.PrivilegedIntentsRequired:
        raise SystemExit("Enable Message Content Intent for this bot in the Discord Developer Portal") from None


if __name__ == "__main__":
    main()
