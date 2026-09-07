"""In-memory upstream supply registry and HTTP transport.

Only this module holds provider credentials. Public snapshots deliberately omit
credentials and base URLs, and all upstream payloads are redacted before they
can be returned to a caller.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings


_PROTOCOL_ERROR_CONTENT = json.dumps(
    {
        "error": {
            "message": "The upstream returned an invalid response.",
            "type": "upstream_protocol_error",
            "code": "invalid_upstream_response",
        }
    },
    separators=(",", ":"),
).encode("utf-8")
_MAX_FIRST_EVENT_BYTES = 64 * 1024


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


@dataclass(slots=True)
class Supply:
    id: str
    name: str
    provider: str
    base_url: str
    api_key: str = field(repr=False)
    models: frozenset[str] = field(default_factory=frozenset)
    priority: int = 100
    enabled: bool = True

    consecutive_failures: int = 0
    circuit_open_until: float | None = None
    in_flight: int = 0
    total_attempts: int = 0
    successful_attempts: int = 0
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_selected_at: float = 0.0

    def circuit_state(self, threshold: int, now: float | None = None) -> str:
        if not self.enabled:
            return "disabled"
        current = time.time() if now is None else now
        if self.circuit_open_until is not None:
            if current < self.circuit_open_until:
                return "open"
            if self.consecutive_failures >= threshold:
                return "half_open"
        if self.consecutive_failures:
            return "degraded"
        return "healthy"

    def public_status(self, threshold: int, now: float | None = None) -> dict[str, Any]:
        success_rate = (
            self.successful_attempts / self.total_attempts * 100
            if self.total_attempts
            else 0.0
        )
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "models": sorted(self.models),
            "enabled": self.enabled,
            "circuit": self.circuit_state(threshold, now),
            "consecutiveFailures": self.consecutive_failures,
            "inFlight": self.in_flight,
            "totalAttempts": self.total_attempts,
            "successRate": round(success_rate, 2),
            "lastSuccessAt": _timestamp(self.last_success_at),
            "lastFailureAt": _timestamp(self.last_failure_at),
        }


class ProviderRegistry:
    """Process-local supply registry with a small circuit breaker."""

    def __init__(
        self,
        supplies: Iterable[Supply],
        *,
        failure_threshold: int,
        cooldown_seconds: float,
    ) -> None:
        supply_list = list(supplies)
        self._supplies = {supply.id: supply for supply in supply_list}
        if len(self._supplies) != len(supply_list):
            raise ValueError("Supply identifiers must be unique")
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._lock = asyncio.Lock()
        self._total_requests = 0
        self._successful_requests = 0

    @property
    def count(self) -> int:
        return len(self._supplies)

    @property
    def failure_threshold(self) -> int:
        return self._failure_threshold

    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            supply.api_key for supply in self._supplies.values() if supply.api_key
        )

    async def source_ids(self) -> list[str]:
        """Return configured source identifiers without exposing credentials."""

        async with self._lock:
            return list(self._supplies)

    async def get_supply(self, source_id: str) -> Supply | None:
        """Resolve a source for internal routing and probe adapters."""

        async with self._lock:
            return self._supplies.get(source_id)

    async def supported_models(self) -> list[str]:
        async with self._lock:
            return sorted(
                {
                    model
                    for supply in self._supplies.values()
                    if supply.enabled
                    for model in supply.models
                }
            )

    async def has_model(self, model: str) -> bool:
        async with self._lock:
            return any(
                supply.enabled and model in supply.models
                for supply in self._supplies.values()
            )

    async def candidates(self, model: str) -> list[Supply]:
        now = time.time()
        async with self._lock:
            candidates = []
            for supply in self._supplies.values():
                if not supply.enabled or model not in supply.models:
                    continue
                state = supply.circuit_state(self._failure_threshold, now)
                if state == "open":
                    continue
                if state == "half_open" and supply.in_flight:
                    continue
                candidates.append(supply)

            candidates.sort(
                key=lambda item: (
                    item.priority,
                    item.consecutive_failures,
                    item.in_flight,
                    item.last_selected_at,
                )
            )
            return candidates

    async def begin_attempt(self, supply: Supply) -> None:
        async with self._lock:
            supply.in_flight += 1
            supply.total_attempts += 1
            supply.last_selected_at = time.time()

    async def record_success(self, supply: Supply) -> None:
        async with self._lock:
            supply.in_flight = max(0, supply.in_flight - 1)
            supply.successful_attempts += 1
            supply.consecutive_failures = 0
            supply.circuit_open_until = None
            supply.last_success_at = time.time()

    async def record_failure(self, supply: Supply) -> None:
        async with self._lock:
            supply.in_flight = max(0, supply.in_flight - 1)
            supply.consecutive_failures += 1
            supply.last_failure_at = time.time()
            if supply.consecutive_failures >= self._failure_threshold:
                supply.circuit_open_until = time.time() + self._cooldown_seconds

    async def release_attempt(self, supply: Supply) -> None:
        """Finish an attempt that should not affect provider health."""

        async with self._lock:
            supply.in_flight = max(0, supply.in_flight - 1)

    async def record_gateway_request(self) -> None:
        async with self._lock:
            self._total_requests += 1

    async def record_gateway_success(self) -> None:
        async with self._lock:
            self._successful_requests += 1

    async def snapshot(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = time.time()
        async with self._lock:
            supplies = [
                supply.public_status(self._failure_threshold, now)
                for supply in self._supplies.values()
            ]
            success_rate = (
                self._successful_requests / self._total_requests * 100
                if self._total_requests
                else 0.0
            )
            summary = {
                "totalRequests": self._total_requests,
                "successRate": round(success_rate, 2),
            }
            return supplies, summary


def _supply_from_mapping(item: dict[str, Any], index: int) -> Supply | None:
    api_key = str(item.get("api_key") or "").strip()
    if not api_key:
        return None

    raw_models = item.get("models", ("deepseek-v4-flash", "deepseek-v4-pro"))
    if isinstance(raw_models, str):
        models = frozenset(value.strip() for value in raw_models.split(",") if value.strip())
    elif isinstance(raw_models, list):
        models = frozenset(str(value).strip() for value in raw_models if str(value).strip())
    else:
        models = frozenset()
    if not models:
        return None

    supply_id = str(item.get("id") or f"configured-{index + 1}").strip()
    base_url = str(item.get("base_url") or "https://api.deepseek.com").strip()
    if not supply_id or not base_url:
        return None

    try:
        priority = int(item.get("priority", 100 + index))
    except (TypeError, ValueError):
        priority = 100 + index

    return Supply(
        id=supply_id,
        name=str(item.get("name") or supply_id).strip(),
        provider=str(item.get("provider") or "deepseek").strip(),
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        models=models,
        priority=priority,
        enabled=bool(item.get("enabled", True)),
    )


def build_registry(settings: Settings) -> ProviderRegistry:
    supplies: list[Supply] = []
    if settings.deepseek_api_key:
        supplies.append(
            Supply(
                id="deepseek-default",
                name="DeepSeek default",
                provider="deepseek",
                base_url=settings.deepseek_base_url,
                api_key=settings.deepseek_api_key,
                models=frozenset(settings.deepseek_models),
                priority=10,
            )
        )

    if settings.additional_supplies_json:
        try:
            configured = json.loads(settings.additional_supplies_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "TOKENCOIN_SUPPLIES_JSON must be a JSON array"
            ) from None
        if not isinstance(configured, list):
            raise ValueError("TOKENCOIN_SUPPLIES_JSON must be a JSON array")
        for index, item in enumerate(configured):
            if not isinstance(item, dict):
                continue
            supply = _supply_from_mapping(item, index)
            if supply is not None:
                supplies.append(supply)

    ids = [supply.id for supply in supplies]
    if len(ids) != len(set(ids)):
        raise ValueError("Supply identifiers must be unique")

    return ProviderRegistry(
        supplies,
        failure_threshold=settings.circuit_failure_threshold,
        cooldown_seconds=settings.circuit_cooldown_seconds,
    )


def _redact_bytes(content: bytes, secrets: Iterable[str]) -> bytes:
    redacted = content
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret.encode("utf-8"), b"[REDACTED]")
    return redacted


class StreamingSecretRedactor:
    """Redact secrets even when a value spans adjacent network chunks."""

    def __init__(self, secrets: Iterable[str]) -> None:
        self._secrets = tuple(
            secret.encode("utf-8") for secret in secrets if secret
        )
        self._tail = b""
        self._overlap = max((len(secret) - 1 for secret in self._secrets), default=0)

    def feed(self, chunk: bytes) -> bytes:
        content = self._tail + chunk
        for secret in self._secrets:
            content = content.replace(secret, b"*" * len(secret))
        if not self._overlap:
            self._tail = b""
            return content
        if len(content) <= self._overlap:
            self._tail = content
            return b""
        split_at = len(content) - self._overlap
        self._tail = content[split_at:]
        return content[:split_at]

    def flush(self) -> bytes:
        content = self._tail
        self._tail = b""
        for secret in self._secrets:
            content = content.replace(secret, b"*" * len(secret))
        return content


def _safe_response_headers(response: httpx.Response) -> dict[str, str]:
    content_type = response.headers.get("content-type", "").lower()
    if content_type.startswith("text/event-stream"):
        return {"content-type": "text/event-stream; charset=utf-8"}
    if content_type.startswith("application/json"):
        return {"content-type": "application/json"}
    return {"content-type": "application/octet-stream"}


def _retryable_status(status_code: int) -> bool:
    # A different configured source may still serve the same logical model.
    # The router only exposes an error after exhausting every eligible source.
    return status_code != 200


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").partition(";")[0].strip().lower()


def _first_sse_data_event_is_valid(content: bytes) -> bool | None:
    """Validate the first complete SSE event containing a ``data`` field.

    ``None`` means that no complete data event is available yet. Comment-only
    keepalives and other SSE fields are skipped. A complete data event must be
    a JSON object, cannot be ``[DONE]``, and cannot contain a top-level
    ``error`` member.
    """

    normalized = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    complete_events = normalized.split(b"\n\n")[:-1]
    for event in complete_events:
        data_lines: list[bytes] = []
        for line in event.split(b"\n"):
            if line == b"data":
                data_lines.append(b"")
            elif line.startswith(b"data:"):
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                data_lines.append(value)
        if not data_lines:
            continue

        payload = b"\n".join(data_lines)
        if payload.strip() == b"[DONE]":
            return False
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return (
            isinstance(parsed, dict)
            and parsed.get("error") is None
            and parsed.get("type") != "error"
        )
    return None


@dataclass(slots=True)
class UpstreamResponse:
    supply: Supply
    status_code: int
    content: bytes
    headers: dict[str, str]


@dataclass(slots=True)
class OpenUpstreamStream:
    supply: Supply
    response: httpx.Response
    iterator: AsyncIterator[bytes]
    first_chunk: bytes
    headers: dict[str, str]
    redactor: StreamingSecretRedactor


class UpstreamRejected(Exception):
    def __init__(
        self,
        *,
        supply: Supply,
        status_code: int,
        content: bytes,
        headers: dict[str, str],
        retryable: bool,
    ) -> None:
        super().__init__(f"Upstream returned HTTP {status_code}")
        self.supply = supply
        self.status_code = status_code
        self.content = content
        self.headers = headers
        self.retryable = retryable


class ProviderTransport:
    def __init__(
        self,
        client: httpx.AsyncClient,
        registry: ProviderRegistry,
        *,
        first_byte_timeout_seconds: float,
    ) -> None:
        self._client = client
        self._registry = registry
        self._first_byte_timeout_seconds = first_byte_timeout_seconds

    @staticmethod
    def _url(supply: Supply, path: str) -> str:
        return f"{supply.base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _headers(supply: Supply, *, stream: bool) -> dict[str, str]:
        return {
            "authorization": f"Bearer {supply.api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
            "accept-encoding": "identity",
        }

    @staticmethod
    def _protocol_rejection(supply: Supply) -> UpstreamRejected:
        return UpstreamRejected(
            supply=supply,
            status_code=502,
            content=_PROTOCOL_ERROR_CONTENT,
            headers={"content-type": "application/json"},
            retryable=True,
        )

    async def request(
        self,
        supply: Supply,
        *,
        path: str,
        payload: dict[str, Any],
    ) -> UpstreamResponse:
        response = await self._client.post(
            self._url(supply, path),
            headers=self._headers(supply, stream=False),
            json=payload,
        )
        if response.status_code != 200:
            raise UpstreamRejected(
                supply=supply,
                status_code=response.status_code,
                content=_redact_bytes(
                    response.content, self._registry.secret_values()
                ),
                headers=_safe_response_headers(response),
                retryable=_retryable_status(response.status_code),
            )
        if _media_type(response) != "application/json":
            raise self._protocol_rejection(supply)
        try:
            payload_object = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise self._protocol_rejection(supply) from None
        if (
            not isinstance(payload_object, dict)
            or payload_object.get("error") is not None
        ):
            raise self._protocol_rejection(supply)

        content = _redact_bytes(response.content, self._registry.secret_values())
        return UpstreamResponse(
            supply=supply,
            status_code=response.status_code,
            content=content,
            headers={"content-type": "application/json"},
        )

    async def open_stream(
        self,
        supply: Supply,
        *,
        path: str,
        payload: dict[str, Any],
    ) -> OpenUpstreamStream:
        request = self._client.build_request(
            "POST",
            self._url(supply, path),
            headers=self._headers(supply, stream=True),
            json=payload,
        )
        response = await self._client.send(request, stream=True)
        if response.status_code != 200:
            try:
                content = await response.aread()
            finally:
                await response.aclose()
            raise UpstreamRejected(
                supply=supply,
                status_code=response.status_code,
                content=_redact_bytes(content, self._registry.secret_values()),
                headers=_safe_response_headers(response),
                retryable=_retryable_status(response.status_code),
            )

        if _media_type(response) != "text/event-stream":
            await response.aclose()
            raise self._protocol_rejection(supply)

        iterator = response.aiter_raw()
        redactor = StreamingSecretRedactor(self._registry.secret_values())
        buffered = b""
        try:
            deadline = asyncio.get_running_loop().time() + self._first_byte_timeout_seconds
            while True:
                validation = _first_sse_data_event_is_valid(buffered)
                if validation is True:
                    break
                if validation is False or len(buffered) > _MAX_FIRST_EVENT_BYTES:
                    raise self._protocol_rejection(supply)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                raw_chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
                if raw_chunk:
                    buffered += raw_chunk
        except StopAsyncIteration:
            await response.aclose()
            raise self._protocol_rejection(supply) from None
        except UpstreamRejected:
            await response.aclose()
            raise
        except TimeoutError:
            await response.aclose()
            raise httpx.ReadTimeout(
                "Upstream produced no usable response event before the first-byte timeout",
                request=request,
            ) from None
        except BaseException:
            await response.aclose()
            raise

        first_chunk = redactor.feed(buffered)
        return OpenUpstreamStream(
            supply=supply,
            response=response,
            iterator=iterator,
            first_chunk=first_chunk,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            redactor=redactor,
        )
