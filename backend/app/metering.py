"""Token usage extraction and deterministic micro-CNY pricing.

One CNY equals one million micro-CNY. Provider prices are quoted per million
tokens, so ``tokens * price_per_million`` directly yields micro-CNY. Keeping
that value as an integer lets the ledger record calls that cost far below one
fen without using floating-point money.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any


CURRENCY_SCALE = 1_000_000


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reported: bool = True
    valid: bool = True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class PricePlan:
    buyer_input_cny_per_million: Decimal
    buyer_output_cny_per_million: Decimal
    seller_input_cny_per_million: Decimal
    seller_output_cny_per_million: Decimal
    buyer_cache_hit_cny_per_million: Decimal | None = None
    seller_cache_hit_cny_per_million: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Charge:
    buyer_micro_cny: int
    seller_micro_cny: int
    platform_micro_cny: int


def _nonnegative_int(value: Any) -> tuple[int, bool]:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0, False
    return max(0, value), value >= 0


def _text_bytes(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, list):
        return sum(_content_part_bytes(item) for item in value)
    return 0


def _content_part_bytes(value: Any) -> int:
    if isinstance(value, str):
        return _text_bytes(value)
    if not isinstance(value, dict):
        return 0
    return sum(
        _text_bytes(value.get(key))
        for key in (
            "text",
            "output_text",
            "reasoning_text",
            "summary_text",
            "refusal",
        )
    )


def _message_output_bytes(message: Any) -> int:
    if not isinstance(message, dict):
        return 0
    total = sum(
        _text_bytes(message.get(key))
        for key in ("content", "reasoning_content", "refusal")
    )
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if isinstance(function, dict):
                total += _text_bytes(function.get("arguments"))
    function_call = message.get("function_call")
    if isinstance(function_call, dict):
        total += _text_bytes(function_call.get("arguments"))
    return total


def _responses_output_bytes(response: Any) -> int:
    if not isinstance(response, dict):
        return 0
    output = response.get("output")
    if not isinstance(output, list):
        return 0
    total = 0
    for item in output:
        if not isinstance(item, dict):
            continue
        total += _text_bytes(item.get("arguments"))
        content = item.get("content")
        if isinstance(content, list):
            total += sum(_content_part_bytes(part) for part in content)
        summary = item.get("summary")
        if isinstance(summary, list):
            total += sum(_content_part_bytes(part) for part in summary)
    if total == 0:
        total += _text_bytes(response.get("output_text"))
    return total


def semantic_output_bytes(payload: Any, *, protocol: str | None = None) -> int:
    """Count only model text/tool arguments, excluding provider metadata/padding."""

    if not isinstance(payload, dict):
        return 0
    total = 0
    if protocol in (None, "chat"):
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    total += _message_output_bytes(
                        choice.get("message", choice.get("delta"))
                    )
    if protocol in (None, "responses"):
        total += _responses_output_bytes(payload)
        nested_response = payload.get("response")
        if isinstance(nested_response, dict):
            total += _responses_output_bytes(nested_response)
    return total


def extract_usage(payload: Any) -> TokenUsage:
    """Extract usage from Chat Completions or Responses API JSON."""

    if not isinstance(payload, dict):
        return TokenUsage(reported=False, valid=False)
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        response = payload.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return TokenUsage(reported=False, valid=False)

    input_raw = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_raw = usage.get("completion_tokens", usage.get("output_tokens"))
    input_tokens, input_valid = _nonnegative_int(input_raw)
    output_tokens, output_valid = _nonnegative_int(output_raw)
    details = usage.get("input_tokens_details")
    cached_raw = (
        details.get("cached_tokens", 0)
        if isinstance(details, dict)
        else 0
    )
    cache_hit, cache_hit_valid = _nonnegative_int(
        usage.get("prompt_cache_hit_tokens", cached_raw)
    )
    cache_miss_reported = "prompt_cache_miss_tokens" in usage
    cache_miss, cache_miss_valid = _nonnegative_int(
        usage.get("prompt_cache_miss_tokens", 0)
    )
    valid = (
        input_raw is not None
        and output_raw is not None
        and input_valid
        and output_valid
        and cache_hit_valid
        and cache_miss_valid
    )
    if cache_hit > input_tokens:
        cache_hit = input_tokens
        valid = False
    if cache_miss_reported:
        if cache_hit + cache_miss != input_tokens:
            valid = False
    else:
        cache_miss = max(0, input_tokens - cache_hit)

    total_raw = usage.get("total_tokens")
    if total_raw is not None:
        total_tokens, total_valid = _nonnegative_int(total_raw)
        valid = (
            valid
            and total_valid
            and total_tokens == input_tokens + output_tokens
        )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
        reported=True,
        valid=valid,
    )


class SSEUsageAccumulator:
    """Read final usage objects from chunked SSE without retaining content."""

    def __init__(self, protocol: str | None = None) -> None:
        if protocol not in (None, "chat", "responses"):
            raise ValueError("protocol must be 'chat', 'responses', or None")
        self._protocol = protocol
        self._buffer = b""
        self.usage = TokenUsage(reported=False, valid=False)
        self.observed_output_bytes = 0
        self._saw_output_delta = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            raw_line, self._buffer = self._buffer.split(b"\n", 1)
            self._consume_line(raw_line.rstrip(b"\r"))

    def finish(self) -> TokenUsage:
        if self._buffer:
            self._consume_line(self._buffer.rstrip(b"\r"))
            self._buffer = b""
        return self.usage

    def _consume_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        self._record_output(payload)
        usage = extract_usage(payload)
        if usage.reported:
            self.usage = usage

    def _record_output(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return

        choices = payload.get("choices")
        if self._protocol in (None, "chat") and isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    observed = _message_output_bytes(delta)
                    if observed:
                        self._saw_output_delta = True
                        self.observed_output_bytes += observed

        event_type = payload.get("type")
        if (
            self._protocol in (None, "responses")
            and isinstance(event_type, str)
            and event_type.endswith(".delta")
        ):
            observed = sum(
                _text_bytes(payload.get(key))
                for key in ("delta", "text", "arguments")
            )
            if observed:
                self._saw_output_delta = True
                self.observed_output_bytes += observed

        if (
            self._protocol in (None, "responses")
            and not self._saw_output_delta
            and event_type
            in {
                "response.completed",
                "response.incomplete",
                "response.failed",
            }
        ):
            self.observed_output_bytes = max(
                self.observed_output_bytes,
                semantic_output_bytes(payload, protocol="responses"),
            )


def _ceil_micro_cny(tokens: int, rate: Decimal) -> int:
    return int((Decimal(tokens) * rate).to_integral_value(rounding=ROUND_CEILING))


def calculate_charge(usage: TokenUsage, plan: PricePlan) -> Charge:
    buyer_hit_rate = (
        plan.buyer_cache_hit_cny_per_million
        if plan.buyer_cache_hit_cny_per_million is not None
        else plan.buyer_input_cny_per_million
    )
    seller_hit_rate = (
        plan.seller_cache_hit_cny_per_million
        if plan.seller_cache_hit_cny_per_million is not None
        else plan.seller_input_cny_per_million
    )
    miss_tokens = usage.cache_miss_tokens or max(
        0, usage.input_tokens - usage.cache_hit_tokens
    )
    buyer = (
        _ceil_micro_cny(usage.cache_hit_tokens, buyer_hit_rate)
        + _ceil_micro_cny(miss_tokens, plan.buyer_input_cny_per_million)
        + _ceil_micro_cny(usage.output_tokens, plan.buyer_output_cny_per_million)
    )
    seller = (
        _ceil_micro_cny(usage.cache_hit_tokens, seller_hit_rate)
        + _ceil_micro_cny(miss_tokens, plan.seller_input_cny_per_million)
        + _ceil_micro_cny(usage.output_tokens, plan.seller_output_cny_per_million)
    )
    seller = min(seller, buyer)
    return Charge(
        buyer_micro_cny=buyer,
        seller_micro_cny=seller,
        platform_micro_cny=buyer - seller,
    )


def micro_cny_to_cny(value: int) -> float:
    return round(value / CURRENCY_SCALE, 6)
