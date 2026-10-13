"""Request limits, protocol isolation, and accounting with an in-memory HTTP transport."""

from dataclasses import replace
from decimal import ROUND_CEILING
import json
import unittest

import httpx

from bot.config import CACHE_READ_RATE, CACHE_WRITE_RATE, Config, INPUT_RATE, MODEL, OUTPUT_RATE
from bot.prompt import build_request
from bot.provider import API_URL, ClaudeClient, MAX_RESPONSE_BYTES, ProviderError, reserve_cost, usage_cost


def config(**env):
    return Config.from_env({
        "DISCORD_TOKEN": "discord-unit-test-token-never-valid",
        "ANTHROPIC_API_KEY": "sk-ant-fake-unit-test-key-never-valid",
        "ALLOWED_CHANNEL_IDS": "111111111111111111",
        **env,
    })


def request_payload(settings=None):
    return build_request(settings or config(), current="hey there", recent=[], memories=[], style={})


class RequestTests(unittest.TestCase):
    def test_cooldown_configuration_defaults_to_fifteen_and_accepts_zero_or_custom(self):
        self.assertEqual(config().cooldown_seconds, 15)
        for value in (0, 30, 86400):
            with self.subTest(value=value):
                self.assertEqual(config(COOLDOWN_SECONDS=str(value)).cooldown_seconds, value)
        for invalid in ("-1", "86401", "1.5", "not-a-number"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                config(COOLDOWN_SECONDS=invalid)

    def test_default_is_short_low_effort_nonthinking_and_tool_free(self):
        payload = request_payload()
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["model"], "claude-haiku-5-5")
        self.assertEqual(payload["max_tokens"], 120)
        self.assertEqual(payload["output_config"], {"effort": "low"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertNotIn("cache_control", payload["system"][0])

    def test_config_rejects_above_twenty_thousand_and_request_clamps_bypassed_config(self):
        with self.assertRaises(ValueError):
            config(MAX_OUTPUT_TOKENS="20001")
        self.assertEqual(request_payload(config(MAX_OUTPUT_TOKENS="20000"))["max_tokens"], 20000)
        manipulated = replace(config(), max_output_tokens=1_000_000)
        self.assertEqual(request_payload(manipulated)["max_tokens"], 20000)

    def test_context_byte_limit_handles_large_unicode_and_escaped_history(self):
        history = [{"speaker": "person1", "text": "漢\\\"" * 800} for _ in range(8)]
        payload = build_request(config(), current="日" * 2000, recent=history,
                                memories=["語" * 160] * 3, style={"lowercase": 0.5})
        content = payload["messages"][0]["content"]
        self.assertLessEqual(len(content.encode("utf-8")), 9000)
        self.assertEqual(json.loads(content)["current_message"], "日" * 2000)

    def test_cache_opt_in_changes_only_static_system_block_and_reserves_write_rate(self):
        payload = request_payload(config(CACHE_SYSTEM_PROMPT="true"))
        self.assertEqual(payload["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", json.dumps(payload["messages"]))
        byte_bound = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 2048
        expected = int((byte_bound * CACHE_WRITE_RATE + 120 * OUTPUT_RATE).to_integral_value(rounding=ROUND_CEILING))
        self.assertEqual(reserve_cost(payload), expected)

    def test_usage_accounts_for_normal_cache_creation_and_cache_read_tokens(self):
        usage = {"input_tokens": 111, "output_tokens": 23,
                 "cache_creation_input_tokens": 101, "cache_read_input_tokens": 87}
        expected = (111 * INPUT_RATE + 23 * OUTPUT_RATE + 101 * CACHE_WRITE_RATE + 87 * CACHE_READ_RATE)
        self.assertEqual(usage_cost(usage), (int(expected.to_integral_value(rounding=ROUND_CEILING)), 299, 23))
        self.assertEqual(usage_cost({"input_tokens": 1, "output_tokens": 1}),
                         (int((INPUT_RATE + OUTPUT_RATE).to_integral_value(rounding=ROUND_CEILING)), 1, 1))

    def test_untrustworthy_usage_is_rejected_instead_of_refunding_budget(self):
        for usage in (
            {}, {"input_tokens": 10}, {"input_tokens": -1, "output_tokens": 1},
            {"input_tokens": True, "output_tokens": 1}, {"input_tokens": 1, "output_tokens": 1.1},
            {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": "10"},
        ):
            with self.subTest(usage=usage), self.assertRaises(ProviderError):
                usage_cost(usage)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, handler):
        client = ClaudeClient("sk-ant-fake-unit-test-key-never-valid", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    async def test_one_fixed_endpoint_post_parses_only_text_blocks(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "name": "dangerous", "input": {"text": "not spoken"}},
                    {"type": "thinking", "thinking": "not spoken"},
                    {"type": "text", "text": "friend"},
                    {"type": "text", "text": {"bad": "value"}},
                    "unexpected block",
                ],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            })

        client = await self.client(handler)
        result = await client.complete(request_payload())
        self.assertEqual(result.text, "hello friend")
        self.assertEqual((result.input_tokens, result.output_tokens), (10, 2))
        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0].url), API_URL)
        self.assertEqual(seen[0].method, "POST")
        self.assertNotIn("tools", json.loads(seen[0].content))

    async def test_http_errors_are_not_retried_and_do_not_expose_bodies(self):
        for status in (500, 429, 401):
            with self.subTest(status=status):
                seen = []

                def handler(request):
                    seen.append(request)
                    return httpx.Response(status, text="private provider body and fake api secret")

                client = await self.client(handler)
                with self.assertRaises(ProviderError) as error:
                    await client.complete(request_payload())
                self.assertEqual(len(seen), 1)
                self.assertEqual(str(error.exception), f"http_{status}")
                self.assertNotIn("private", str(error.exception))
                if status in (429, 401):
                    self.assertFalse(client.available())
                    with self.assertRaises(ProviderError):
                        await client.complete(request_payload())
                    self.assertEqual(len(seen), 1)

    async def test_network_timeout_makes_one_attempt(self):
        seen = []

        def handler(request):
            seen.append(request)
            raise httpx.ReadTimeout("private transport details", request=request)

        client = await self.client(handler)
        with self.assertRaisesRegex(ProviderError, "^ReadTimeout$"):
            await client.complete(request_payload())
        self.assertEqual(len(seen), 1)

    async def test_three_failures_open_circuit_and_fourth_does_not_send(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(503)

        client = await self.client(handler)
        for _ in range(3):
            with self.assertRaises(ProviderError):
                await client.complete(request_payload())
        self.assertFalse(client.available())
        with self.assertRaisesRegex(ProviderError, "backoff"):
            await client.complete(request_payload())
        self.assertEqual(len(seen), 3)

    async def test_oversized_or_missing_usage_responses_fail_closed(self):
        responses = [
            httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
            httpx.Response(200, json={"content": [{"type": "text", "text": "hello"}]}),
        ]
        for response in responses:
            with self.subTest(response_length=len(response.content)):
                client = await self.client(lambda request: response)
                with self.assertRaises(ProviderError):
                    await client.complete(request_payload())


if __name__ == "__main__":
    unittest.main()
