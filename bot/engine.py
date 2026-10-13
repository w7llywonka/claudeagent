from dataclasses import dataclass
import asyncio
import logging
import time

from .prompt import build_request
from .provider import ProviderError, reserve_cost
from .text import scrub, is_override, words_only


log = logging.getLogger(__name__)
COMMANDS = {"!help", "!privacy", "!remember", "!forget", "!optout", "!optin", "!budget"}


@dataclass(frozen=True)
class Incoming:
    user: str
    channel: str
    text: str
    triggered: bool
    is_dm: bool = False
    is_bot: bool = False


class ChatEngine:
    def __init__(self, config, store, provider, clock=time.time):
        self.config, self.store, self.provider, self.clock = config, store, provider, clock
        self._active_channels = set()
        self._active_users = set()
        self._pending = {}
        self._privacy_notices = {}  # Only a short-lived local notice rate gate.

    def allowed(self, event: Incoming) -> bool:
        return not event.is_bot and ((event.is_dm and self.config.enable_dms) or
                                    (not event.is_dm and event.channel in self.config.allowed_channels))

    def clean(self, text):
        return scrub(text, (self.config.discord_token, self.config.api_key))

    def _reply(self, text):
        return words_only(text, max_words=self.config.max_reply_words,
                          max_chars=self.config.max_reply_chars,
                          secrets=(self.config.discord_token, self.config.api_key))

    def _privacy_command(self, event, command, now):
        # Erasure always works, including while another request is in flight or on cooldown.
        # Only invalidate requests that could contain this user's data.
        for ticket in self._pending.values():
            if event.user in ticket["users"]:
                ticket["valid"] = False
        if command == "!optout":
            self.store.set_opt_out(event.user, True)
            answer = "done, i cleared your saved stuff and stopped learning from your messages"
        else:
            self.store.forget(event.user)
            answer = "done, i cleared your saved stuff. say optout with an exclamation mark to stop future learning too"
        self._privacy_notices = {k: v for k, v in self._privacy_notices.items() if now - v < 15}
        if event.user in self._privacy_notices:
            return None
        self._privacy_notices[event.user] = now
        return self._reply(answer)

    async def process(self, event: Incoming) -> str | None:
        if not self.allowed(event) or not event.text.strip():
            return None
        now = self.clock()
        # Bound before cleaning to avoid expensive work on long inputs.
        raw = event.text[:4000].strip()
        command, _, argument = raw.partition(" ")
        command = command.lower()
        if command in {"!forget", "!optout"}:
            return self._privacy_command(event, command, now)
        is_command = command in COMMANDS
        if raw.startswith("!") and not is_command:
            return None  # Don't spend money reacting to other bots' commands.
        text = self.clean(raw[:self.config.max_input_chars])
        if not text:
            return None
        opted_out = self.store.opted_out(event.user)
        override = is_override(text)
        triggered = event.triggered or event.is_dm or event.channel in self.config.auto_reply_channels
        if not triggered and not is_command:
            if self.config.passive_learning and not opted_out and not override:
                self.store.observe(event.channel, event.user, text, now=now)
            return None  # Most Discord messages cost zero API tokens.
        if self.store.admit(event.user, now, cooldown=self.config.cooldown_seconds,
                            per_minute=self.config.max_per_minute) != "ok":
            return None  # Silent cooldown: no bot spam and no model calls.
        if is_command:
            return self._command(event, command, argument)
        if override:
            return self._reply("i'm just here to chat, let's keep it simple")
        if (len(self._active_channels) >= self.config.max_concurrent or event.channel in self._active_channels
                or event.user in self._active_users):
            return self._reply("one sec, i'm still replying")
        if not self.provider.available():
            return self._reply("can't chat right now, try again later")

        # No waiting queue: concurrent capacity and channel ordering cannot grow unbounded.
        self._active_channels.add(event.channel)
        self._active_users.add(event.user)
        ticket = {"valid": True, "users": {event.user}}
        self._pending[event.user] = ticket
        try:
            recent = [] if opted_out else self.store.recent(event.channel, self.config.context_messages, now=now)
            aliases = {event.user: "you"}
            rendered = []
            for entry in recent:
                ticket["users"].update(entry.get("sources", []))
                if not entry["is_bot"]:
                    ticket["users"].add(entry["user"])
                if not entry["is_bot"] and entry["user"] not in aliases:
                    aliases[entry["user"]] = f"person{len(aliases)}"
                rendered.append({"speaker": "bot" if entry["is_bot"] else aliases[entry["user"]], "text": entry["text"]})
            memories = [] if opted_out else self.store.memories(event.channel, event.user, text, limit=3)
            style = {} if opted_out else self.store.style(event.channel, event.user)
            payload = build_request(self.config, current=text, recent=rendered, memories=memories, style=style)
            estimate = reserve_cost(payload)
            reservation = self.store.reserve(estimate, self.config.day_limit, self.config.month_limit, self.config.total_limit, now)
            if reservation is None:
                return self._reply("taking a break for now, my chat budget is used up")
            if not opted_out:
                self.store.observe(event.channel, event.user, text, now=now)
            try:
                # A total wall-clock timeout also bounds slow chunk-by-chunk responses.
                async with asyncio.timeout(30):
                    result = await self.provider.complete(payload)
            except TimeoutError:
                self.provider.note_failure()
                return self._reply("having trouble replying right now, try again later")
            except ProviderError:
                # Unknown provider outcome: keep the reserved charge. Never retry automatically.
                return self._reply("having trouble replying right now, try again later")
            self.store.settle(reservation, result.cost)
            if result.cost > estimate:
                log.error("Usage exceeded conservative reservation; disabling further API calls")
                self.provider.blocked_until = float("inf")
            log.info("reply input_tokens=%s output_tokens=%s micro_usd=%s", result.input_tokens, result.output_tokens, result.cost)
            if not ticket["valid"]:
                return None
            answer = self._reply(result.text)
            if not opted_out and not self.store.opted_out(event.user):
                self.store.observe(event.channel, "bot", answer, is_bot=True, now=self.clock(),
                                   sources=ticket["users"])
            return answer
        finally:
            self._active_channels.discard(event.channel)
            self._active_users.discard(event.user)
            if self._pending.get(event.user) is ticket:
                self._pending.pop(event.user, None)

    def _command(self, event, command, argument):
        if command == "!help":
            interval = (f"one chat every {self.config.cooldown_seconds} seconds" if self.config.cooldown_seconds
                        else "no per-user cooldown")
            return ("mention me or reply to me to chat. commands are !remember a fact, !forget, !optout, "
                    "!optin, !privacy and !budget. " + interval)
        if command == "!privacy":
            return "i use recent channel chat and simple style habits. small amounts go to anthropic when i reply. use !forget to erase local memory or !optout to stop storage. !remember saves a short fact"
        if command == "!optin":
            self.store.set_opt_out(event.user, False)
            return "okay, i can learn your chat style again"
        if command == "!remember":
            if self.store.opted_out(event.user):
                return "you're opted out. use !optin first if you want me to remember things"
            fact = self.clean(argument.strip()[:160])
            if not fact:
                return "say !remember followed by a short fact you want me to keep"
            if is_override(fact):
                return "i can remember little facts, but not instructions to change how i work"
            self.store.remember(event.channel, event.user, fact, now=self.clock())
            return "got it, i'll keep that in mind here"
        if command == "!budget":
            if event.user not in self.config.owner_ids:
                return "only my owner can check that"
            spend = self.store.spend(self.clock())
            return (f"estimated api spend including holds: today {spend['day']/1e6:.4f} dollars, "
                    f"month {spend['month']/1e6:.4f}, lifetime {spend['total']/1e6:.4f}. "
                    f"limits are {self.config.day_limit/1e6:g}, {self.config.month_limit/1e6:g}, "
                    f"and {self.config.total_limit/1e6:g} dollars")
        return None
