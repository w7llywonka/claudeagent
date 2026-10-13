from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import json
import logging
import time

import httpx

from .config import INPUT_RATE, OUTPUT_RATE, CACHE_WRITE_RATE, CACHE_READ_RATE


log = logging.getLogger(__name__)
API_URL = "https://api.anthropic.com/v1/messages"
MAX_RESPONSE_BYTES = 512_000


class ProviderError(Exception):
    """Safe, body-free error; never includes an API key or user message."""


@dataclass(frozen=True)
class Completion:
    text: str
    cost: int
    input_tokens: int
    output_tokens: int


def reserve_cost(payload: dict) -> int:
    # UTF-8 bytes upper-bound byte-tokenized text; extra allowance covers framing.
    # Reserve at the highest enabled input rate so cache writes are covered too.
    byte_bound = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 2048
    rate = CACHE_WRITE_RATE if any("cache_control" in b for b in payload["system"]) else INPUT_RATE
    return int((Decimal(byte_bound) * rate + Decimal(payload["max_tokens"]) * OUTPUT_RATE).to_integral_value(rounding=ROUND_CEILING))


def usage_cost(usage: dict) -> tuple[int, int, int]:
    fields = ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"]
    numbers = []
    for key in fields:
        if key in {"input_tokens", "output_tokens"} and key not in usage:
            raise ProviderError("missing_usage")
        value = usage.get(key, 0)
        if type(value) is not int or value < 0:
            raise ProviderError("invalid_usage")
        numbers.append(value)
    incoming, outgoing, writes, reads = numbers
    cost = incoming * INPUT_RATE + outgoing * OUTPUT_RATE + writes * CACHE_WRITE_RATE + reads * CACHE_READ_RATE
    return int(cost.to_integral_value(rounding=ROUND_CEILING)), incoming + writes + reads, outgoing


class ClaudeClient:
    def __init__(self, api_key: str, *, transport=None):
        self._client = httpx.AsyncClient(
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            timeout=httpx.Timeout(25.0, connect=8.0),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        self.blocked_until = 0.0
        self.failures = 0

    def available(self) -> bool:
        return time.monotonic() >= self.blocked_until

    async def close(self):
        await self._client.aclose()

    def note_failure(self):
        self.failures += 1
        if self.failures >= 3:
            self.blocked_until = max(self.blocked_until, time.monotonic() + 60)

    async def complete(self, payload: dict) -> Completion:
        if not self.available():
            raise ProviderError("backoff")
        try:
            # One POST, no automatic retries, no tools, no URL configurable by chat.
            async with self._client.stream("POST", API_URL, json=payload) as response:
                if response.status_code != 200:
                    if response.status_code in {400, 401, 403, 404}:
                        self.blocked_until = float("inf")  # Fix config/account and restart.
                    elif response.status_code == 429:
                        self.blocked_until = time.monotonic() + 60
                    raise ProviderError(f"http_{response.status_code}")
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ProviderError("oversized_response")
            result = json.loads(data)
            if not isinstance(result, dict) or not isinstance(result.get("content"), list):
                raise ProviderError("invalid_response")
            cost, input_tokens, output_tokens = usage_cost(result.get("usage", {}))
            # Unknown or tool blocks are never rendered or executed.
            if result.get("stop_reason") == "refusal":
                reply = "nah, i can't help with that"
            else:
                reply = " ".join(block["text"] for block in result["content"]
                                 if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
            self.failures = 0
            return Completion(reply, cost, input_tokens, output_tokens)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, ProviderError) as exc:
            self.note_failure()
            code = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            log.warning("Claude request failed: %s", code)
            raise ProviderError(code) from None
