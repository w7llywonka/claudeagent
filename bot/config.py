from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Mapping


ABSOLUTE_MAX_OUTPUT_TOKENS = 20_000
MODEL = "claude-haiku-5-5"
# USD per million tokens, verified 2026-10-12. Numerically also microUSD/token.
INPUT_RATE = Decimal("0.10")
OUTPUT_RATE = Decimal("0.50")
CACHE_WRITE_RATE = Decimal("0.125")
CACHE_READ_RATE = Decimal("0.01")


def _integer(env, name, default, minimum, maximum):
    try:
        value = int(env.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(env, name, default):
    value = env.get(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def _ids(env, name):
    values = {v.strip() for v in env.get(name, "").split(",") if v.strip()}
    if any(not re.fullmatch(r"[0-9]{15,22}", v) for v in values):
        raise ValueError(f"{name} must contain comma-separated Discord IDs")
    return frozenset(values)


def _money(env, name, default):
    try:
        value = Decimal(env.get(name, str(default)))
        if not value.is_finite() or value <= 0 or value > 1_000_000:
            raise ValueError
        return int(value * 1_000_000)
    except (ValueError, InvalidOperation):
        raise ValueError(f"{name} must be a positive USD amount (at most 1000000)") from None


@dataclass(frozen=True)
class Config:
    discord_token: str
    api_key: str
    allowed_channels: frozenset[str]
    auto_reply_channels: frozenset[str]
    owner_ids: frozenset[str]
    enable_dms: bool = False
    passive_learning: bool = True
    cache_system_prompt: bool = False
    db_path: str = "data/chat.db"
    day_limit: int = 500_000
    month_limit: int = 5_000_000
    total_limit: int = 5_000_000
    max_output_tokens: int = 120
    max_reply_words: int = 45
    max_reply_chars: int = 300
    max_input_chars: int = 800
    context_messages: int = 6
    max_concurrent: int = 2
    max_per_minute: int = 20
    cooldown_seconds: int = 15
    message_retention_days: int = 30
    message_max_rows: int = 50_000
    message_max_mb: int = 64

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        for name in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY"):
            if not env.get(name, "").strip():
                raise ValueError(f"{name} is required")
        allowed = _ids(env, "ALLOWED_CHANNEL_IDS")
        dms = _boolean(env, "ENABLE_DMS", False)
        if not allowed and not dms:
            raise ValueError("Set ALLOWED_CHANNEL_IDS (or explicitly enable DMs)")
        auto = _ids(env, "AUTO_REPLY_CHANNEL_IDS")
        if not auto <= allowed:
            raise ValueError("AUTO_REPLY_CHANNEL_IDS must be a subset of ALLOWED_CHANNEL_IDS")
        db_path = env.get("DB_PATH", "data/chat.db").strip()
        if not db_path or db_path == ":memory:":
            raise ValueError("DB_PATH must be a persistent file")
        return cls(
            discord_token=env["DISCORD_TOKEN"].strip(), api_key=env["ANTHROPIC_API_KEY"].strip(),
            allowed_channels=allowed, auto_reply_channels=auto, owner_ids=_ids(env, "OWNER_IDS"),
            enable_dms=dms, passive_learning=_boolean(env, "PASSIVE_LEARNING", True),
            cache_system_prompt=_boolean(env, "CACHE_SYSTEM_PROMPT", False), db_path=db_path,
            day_limit=_money(env, "DAILY_BUDGET_USD", "0.50"),
            month_limit=_money(env, "MONTHLY_BUDGET_USD", "5"),
            total_limit=_money(env, "TOTAL_BUDGET_USD", "5"),
            max_output_tokens=_integer(env, "MAX_OUTPUT_TOKENS", 120, 16, ABSOLUTE_MAX_OUTPUT_TOKENS),
            max_reply_words=_integer(env, "MAX_REPLY_WORDS", 45, 5, 100),
            max_reply_chars=_integer(env, "MAX_REPLY_CHARS", 300, 30, 1000),
            max_input_chars=_integer(env, "MAX_INPUT_CHARS", 800, 80, 2000),
            context_messages=_integer(env, "CONTEXT_MESSAGES", 6, 0, 8),
            max_concurrent=_integer(env, "MAX_CONCURRENT_REQUESTS", 2, 1, 4),
            max_per_minute=_integer(env, "MAX_REQUESTS_PER_MINUTE", 20, 1, 120),
            cooldown_seconds=_integer(env, "COOLDOWN_SECONDS", 15, 0, 86400),
            message_retention_days=_integer(env, "MESSAGE_RETENTION_DAYS", 30, 1, 365),
            message_max_rows=_integer(env, "MESSAGE_MAX_ROWS", 50_000, 100, 1_000_000),
            message_max_mb=_integer(env, "MESSAGE_MAX_MB", 64, 1, 128),
        )
