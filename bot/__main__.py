import asyncio
from collections import deque
import logging
import re
import signal

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
        self._handlers = set()

    async def on_ready(self):
        log.info("Connected to Discord; model=claude-haiku-5-5 effort=low; allowed_channels=%d", len(self.engine.config.allowed_channels))

    async def on_message(self, message):
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            if self.user is None or message.author.bot or message.webhook_id:
                return
            bot_id = self.user.id
            mentioned = any(user.id == bot_id for user in message.mentions)
            replied = False
            if message.reference:
                resolved = message.reference.resolved
                replied = ((isinstance(resolved, discord.Message) and resolved.author.id == bot_id)
                           or message.reference.message_id in self._own_message_ids)
            text = re.sub(rf"<@!?{bot_id}>", "", message.content).strip()
            event = Incoming(str(message.author.id), str(message.channel.id), text,
                             mentioned or replied, message.guild is None, message.author.bot)
            answer = await self.engine.process(event)
            if answer:
                # Plain content only. No embeds, files, tools, eval or command dispatch.
                sent = await message.channel.send(answer, allowed_mentions=discord.AllowedMentions.none(),
                                                  suppress_embeds=True)
                self._own_message_ids.append(sent.id)
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
    store = Store(config.db_path)
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
