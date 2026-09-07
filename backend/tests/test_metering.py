from __future__ import annotations

import unittest
from decimal import Decimal

from backend.app.metering import (
    PricePlan,
    SSEUsageAccumulator,
    TokenUsage,
    calculate_charge,
    extract_usage,
    semantic_output_bytes,
)


class UsageExtractionTests(unittest.TestCase):
    def test_missing_or_malformed_usage_is_not_billable(self) -> None:
        missing = extract_usage({"choices": []})
        malformed = extract_usage(
            {"usage": {"prompt_tokens": "bad", "completion_tokens": -1}}
        )

        self.assertFalse(missing.reported)
        self.assertFalse(missing.valid)
        self.assertTrue(malformed.reported)
        self.assertFalse(malformed.valid)

        numeric_string = extract_usage(
            {"usage": {"prompt_tokens": "10", "completion_tokens": 2}}
        )
        boolean_count = extract_usage(
            {"usage": {"prompt_tokens": True, "completion_tokens": 2}}
        )
        self.assertFalse(numeric_string.valid)
        self.assertFalse(boolean_count.valid)

    def test_extracts_non_stream_chat_completion_usage(self) -> None:
        usage = extract_usage(
            {
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "prompt_cache_hit_tokens": 40,
                    "prompt_cache_miss_tokens": 80,
                    "total_tokens": 150,
                }
            }
        )

        self.assertEqual(
            usage,
            TokenUsage(
                input_tokens=120,
                output_tokens=30,
                cache_hit_tokens=40,
                cache_miss_tokens=80,
            ),
        )

    def test_extracts_non_stream_responses_usage(self) -> None:
        usage = extract_usage(
            {
                "usage": {
                    "input_tokens": 90,
                    "input_tokens_details": {"cached_tokens": 15},
                    "output_tokens": 10,
                    "total_tokens": 100,
                }
            }
        )

        self.assertEqual(
            usage,
            TokenUsage(
                input_tokens=90,
                output_tokens=10,
                cache_hit_tokens=15,
                cache_miss_tokens=75,
            ),
        )

    def test_rejects_inconsistent_cache_and_total_counts(self) -> None:
        bad_cache = extract_usage(
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "prompt_cache_hit_tokens": 4,
                    "prompt_cache_miss_tokens": 9,
                    "total_tokens": 12,
                }
            }
        )
        bad_total = extract_usage(
            {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 999,
                }
            }
        )

        self.assertTrue(bad_cache.reported)
        self.assertFalse(bad_cache.valid)
        self.assertFalse(bad_total.valid)

    def test_accumulates_chunked_chat_sse_usage(self) -> None:
        accumulator = SSEUsageAccumulator()
        for chunk in (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":1',
            b'2,"completion_tokens":4,"prompt_cache_hit_tokens":2}}\n\n',
            b"data: [DONE]\n\n",
        ):
            accumulator.feed(chunk)

        self.assertEqual(
            accumulator.finish(),
            TokenUsage(
                input_tokens=12,
                output_tokens=4,
                cache_hit_tokens=2,
                cache_miss_tokens=10,
            ),
        )

    def test_accumulates_responses_completed_event_without_done_marker(self) -> None:
        accumulator = SSEUsageAccumulator()
        chunks = (
            b"event: response.completed\n",
            b'data: {"type":"response.completed","response":{"usage":',
            b'{"input_tokens":21,"input_tokens_details":{"cached_tokens":5},',
            b'"output_tokens":9,"total_tokens":30}}}\n\n',
        )
        for chunk in chunks:
            accumulator.feed(chunk)

        self.assertEqual(
            accumulator.finish(),
            TokenUsage(
                input_tokens=21,
                output_tokens=9,
                cache_hit_tokens=5,
                cache_miss_tokens=16,
            ),
        )

    def test_semantic_output_ignores_provider_padding_and_sse_metadata(self) -> None:
        payload = {
            "id": "x" * 10_000,
            "padding": "y" * 10_000,
            "choices": [{"message": {"content": "OK"}}],
        }
        self.assertEqual(semantic_output_bytes(payload), 2)

        accumulator = SSEUsageAccumulator()
        accumulator.feed(
            b'data: {"padding":"xxxxxxxx","choices":[{"delta":{"content":"OK"}}]}\n\n'
        )
        accumulator.feed(b': ' + b'x' * 2_000 + b'\n\n')
        accumulator.feed(
            b'data: {"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n'
        )
        self.assertEqual(accumulator.observed_output_bytes, 2)

    def test_calculates_buyer_seller_and_platform_amounts(self) -> None:
        charge = calculate_charge(
            TokenUsage(
                input_tokens=100,
                output_tokens=20,
                cache_hit_tokens=25,
                cache_miss_tokens=75,
            ),
            PricePlan(
                buyer_input_cny_per_million=Decimal("2.40"),
                buyer_output_cny_per_million=Decimal("7.20"),
                buyer_cache_hit_cny_per_million=Decimal("0.08"),
                seller_input_cny_per_million=Decimal("2.22"),
                seller_output_cny_per_million=Decimal("6.66"),
                seller_cache_hit_cny_per_million=Decimal("0.074"),
            ),
        )

        self.assertEqual(charge.buyer_micro_cny, 326)
        self.assertEqual(charge.seller_micro_cny, 303)
        self.assertEqual(charge.platform_micro_cny, 23)


if __name__ == "__main__":
    unittest.main()
