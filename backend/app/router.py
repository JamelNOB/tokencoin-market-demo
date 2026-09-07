"""Request routing, pre-first-byte failover, and stream lifecycle handling."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from .health import HealthMonitor

from .providers import (
    OpenUpstreamStream,
    ProviderRegistry,
    ProviderTransport,
    Supply,
    UpstreamRejected,
    UpstreamResponse,
)
from .telemetry import (
    FAILOVERS,
    TIME_TO_FIRST_TOKEN,
    UPSTREAM_ATTEMPTS,
    UPSTREAM_LATENCY,
    set_circuit_state,
)


class NoSupplyAvailable(Exception):
    def __init__(self, model: str, *, configured: bool) -> None:
        super().__init__(f"No supply available for model {model}")
        self.model = model
        self.configured = configured


class AllSuppliesFailed(Exception):
    def __init__(self, model: str) -> None:
        super().__init__(f"All supplies failed for model {model}")
        self.model = model


@dataclass(slots=True)
class RoutedResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]
    supply: Supply
    attempts: int
    latency_seconds: float


@dataclass(slots=True)
class RoutedStream:
    upstream: OpenUpstreamStream
    body: AsyncIterator[bytes]
    headers: dict[str, str]
    attempts: int
    time_to_first_token_seconds: float
    generation: int | None


class GatewayRouter:
    def __init__(
        self,
        registry: ProviderRegistry,
        transport: ProviderTransport,
        health: HealthMonitor | None = None,
    ) -> None:
        self.registry = registry
        self.transport = transport
        self.health = health

    async def _allow(self, supply: Supply) -> tuple[bool, int | None]:
        if self.health is None:
            return True, None
        permission = await self.health.allow_request(supply.id)
        return permission.allowed, getattr(permission, "generation", None)

    async def _health_success(
        self,
        supply: Supply,
        latency_ms: float,
        generation: int | None,
    ) -> None:
        if self.health is None:
            return
        try:
            state = await self.health.record_success(
                supply.id,
                latency_ms=latency_ms,
                generation=generation,
            )
            set_circuit_state(supply.id, state.state.value)
        except Exception:
            return

    async def _health_failure(
        self,
        supply: Supply,
        reason_code: str,
        generation: int | None,
    ) -> None:
        if self.health is None:
            return
        try:
            state = await self.health.record_failure(
                supply.id,
                reason_code=reason_code,
                generation=generation,
            )
            set_circuit_state(supply.id, state.state.value)
        except Exception:
            return

    async def _health_release(
        self,
        supply: Supply,
        generation: int | None,
    ) -> None:
        if self.health is None:
            return
        method = getattr(self.health, "release_permission", None)
        if not callable(method):
            return
        try:
            await method(supply.id, generation=generation)
        except Exception:
            return

    async def _candidates(self, model: str) -> list[Supply]:
        candidates = await self.registry.candidates(model)
        if candidates:
            return candidates
        raise NoSupplyAvailable(
            model,
            configured=await self.registry.has_model(model),
        )

    async def request(
        self,
        *,
        model: str,
        path: str,
        payload: dict[str, Any],
    ) -> RoutedResponse:
        await self.registry.record_gateway_request()
        candidates = await self._candidates(model)
        attempts = 0
        previous_failure = "unavailable"

        for supply in candidates:
            allowed, generation = await self._allow(supply)
            if not allowed:
                continue
            if attempts:
                FAILOVERS.labels(model=model, reason=previous_failure).inc()
            attempts += 1
            await self.registry.begin_attempt(supply)
            started = time.perf_counter()
            try:
                response = await self.transport.request(
                    supply,
                    path=path,
                    payload=payload,
                )
            except UpstreamRejected as exc:
                if exc.retryable:
                    await self.registry.record_failure(supply)
                    await self._health_failure(
                        supply, f"http_{exc.status_code}", generation
                    )
                    UPSTREAM_ATTEMPTS.labels(
                        source_id=supply.id, model=model, outcome="retryable_error"
                    ).inc()
                    previous_failure = f"http_{exc.status_code}"
                    continue
                await self.registry.release_attempt(supply)
                latency = time.perf_counter() - started
                await self._health_success(supply, latency * 1000, generation)
                UPSTREAM_ATTEMPTS.labels(
                    source_id=supply.id, model=model, outcome="rejected"
                ).inc()
                raise
            except httpx.HTTPError:
                await self.registry.record_failure(supply)
                await self._health_failure(supply, "transport_error", generation)
                UPSTREAM_ATTEMPTS.labels(
                    source_id=supply.id, model=model, outcome="transport_error"
                ).inc()
                previous_failure = "transport_error"
                continue
            except BaseException:
                await self.registry.release_attempt(supply)
                await self._health_release(supply, generation)
                raise

            latency = time.perf_counter() - started
            await self.registry.record_success(supply)
            await self._health_success(supply, latency * 1000, generation)
            await self.registry.record_gateway_success()
            UPSTREAM_ATTEMPTS.labels(
                source_id=supply.id, model=model, outcome="success"
            ).inc()
            UPSTREAM_LATENCY.labels(source_id=supply.id, model=model).observe(latency)
            return RoutedResponse(
                status_code=response.status_code,
                content=response.content,
                headers=response.headers,
                supply=supply,
                attempts=attempts,
                latency_seconds=latency,
            )

        if attempts == 0:
            # The registry had matching candidates, but the persistent health
            # monitor denied every one (open circuit or occupied half-open
            # lease). This is temporary unavailability, not an upstream 502.
            raise NoSupplyAvailable(model, configured=True)
        raise AllSuppliesFailed(model)

    async def open_stream(
        self,
        *,
        model: str,
        path: str,
        payload: dict[str, Any],
    ) -> RoutedStream:
        """Open and prime a stream before FastAPI sends response headers.

        Retrying is safe until this method returns because no upstream response
        byte has reached the buyer. Once streaming begins, the selected source
        remains fixed to avoid duplicate or interleaved model output.
        """

        await self.registry.record_gateway_request()
        candidates = await self._candidates(model)
        attempts = 0
        previous_failure = "unavailable"

        for supply in candidates:
            allowed, generation = await self._allow(supply)
            if not allowed:
                continue
            if attempts:
                FAILOVERS.labels(model=model, reason=previous_failure).inc()
            attempts += 1
            await self.registry.begin_attempt(supply)
            started = time.perf_counter()
            try:
                upstream = await self.transport.open_stream(
                    supply,
                    path=path,
                    payload=payload,
                )
            except UpstreamRejected as exc:
                if exc.retryable:
                    await self.registry.record_failure(supply)
                    await self._health_failure(
                        supply, f"http_{exc.status_code}", generation
                    )
                    UPSTREAM_ATTEMPTS.labels(
                        source_id=supply.id, model=model, outcome="retryable_error"
                    ).inc()
                    previous_failure = f"http_{exc.status_code}"
                    continue
                await self.registry.release_attempt(supply)
                latency = time.perf_counter() - started
                await self._health_success(supply, latency * 1000, generation)
                UPSTREAM_ATTEMPTS.labels(
                    source_id=supply.id, model=model, outcome="rejected"
                ).inc()
                raise
            except httpx.HTTPError:
                await self.registry.record_failure(supply)
                await self._health_failure(supply, "transport_error", generation)
                UPSTREAM_ATTEMPTS.labels(
                    source_id=supply.id, model=model, outcome="transport_error"
                ).inc()
                previous_failure = "transport_error"
                continue
            except asyncio.CancelledError:
                await self.registry.release_attempt(supply)
                await self._health_release(supply, generation)
                raise
            except BaseException:
                await self.registry.release_attempt(supply)
                await self._health_release(supply, generation)
                raise

            first_token_latency = time.perf_counter() - started
            TIME_TO_FIRST_TOKEN.labels(source_id=supply.id, model=model).observe(
                first_token_latency
            )
            UPSTREAM_ATTEMPTS.labels(
                source_id=supply.id, model=model, outcome="stream_open"
            ).inc()
            return RoutedStream(
                upstream=upstream,
                body=self._stream_body(
                    upstream,
                    model=model,
                    generation=generation,
                    started=started,
                ),
                headers=upstream.headers,
                attempts=attempts,
                time_to_first_token_seconds=first_token_latency,
                generation=generation,
            )

        if attempts == 0:
            raise NoSupplyAvailable(model, configured=True)
        raise AllSuppliesFailed(model)

    async def abort_unstarted_stream(self, routed: RoutedStream, *, model: str) -> None:
        """Release an upstream selected for a response body that never started."""

        await routed.upstream.response.aclose()
        await self.registry.release_attempt(routed.upstream.supply)
        await self._health_release(routed.upstream.supply, routed.generation)
        UPSTREAM_ATTEMPTS.labels(
            source_id=routed.upstream.supply.id,
            model=model,
            outcome="cancelled_before_body",
        ).inc()

    async def _stream_body(
        self,
        upstream: OpenUpstreamStream,
        *,
        model: str,
        generation: int | None,
        started: float,
    ) -> AsyncIterator[bytes]:
        completed = False
        try:
            yield upstream.first_chunk
            async for chunk in upstream.iterator:
                if chunk:
                    redacted = upstream.redactor.feed(chunk)
                    if redacted:
                        yield redacted
            final_chunk = upstream.redactor.flush()
            if final_chunk:
                yield final_chunk
            completed = True
        except asyncio.CancelledError:
            await self.registry.release_attempt(upstream.supply)
            await self._health_release(upstream.supply, generation)
            UPSTREAM_ATTEMPTS.labels(
                source_id=upstream.supply.id, model=model, outcome="cancelled"
            ).inc()
            raise
        except GeneratorExit:
            await self.registry.release_attempt(upstream.supply)
            await self._health_release(upstream.supply, generation)
            raise
        except BaseException:
            await self.registry.record_failure(upstream.supply)
            await self._health_failure(
                upstream.supply, "stream_error", generation
            )
            UPSTREAM_ATTEMPTS.labels(
                source_id=upstream.supply.id, model=model, outcome="stream_error"
            ).inc()
            raise
        finally:
            await upstream.response.aclose()
            if completed:
                latency = time.perf_counter() - started
                await self.registry.record_success(upstream.supply)
                await self._health_success(
                    upstream.supply, latency * 1000, generation
                )
                await self.registry.record_gateway_success()
                UPSTREAM_LATENCY.labels(
                    source_id=upstream.supply.id, model=model
                ).observe(latency)
