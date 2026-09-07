"""FastAPI entry point for the TokenCoin OpenAI-compatible gateway."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.background import BackgroundTask

from .audit import AuditService, ProbeObservation
from .config import get_settings
from .health import HealthMonitor, HealthPolicy
from .ledger import (
    IdempotencyConflict,
    InsufficientFunds,
    InvalidLedgerTransition,
    Ledger,
    ReservationNotFound,
)
from .metering import (
    PricePlan,
    SSEUsageAccumulator,
    TokenUsage,
    calculate_charge,
    extract_usage,
    micro_cny_to_cny,
    semantic_output_bytes,
)
from .providers import ProviderTransport, Supply, UpstreamRejected, build_registry
from .router import AllSuppliesFailed, GatewayRouter, NoSupplyAvailable
from .schemas import (
    ChatCompletionRequest,
    GatewayStatus,
    ModelListResponse,
    ModelObject,
    ResponsesRequest,
    StatusResponse,
    StatusSummary,
    SupplyStatus,
)
from .telemetry import GATEWAY_REQUESTS, configure_telemetry, record_tokens, tracer


settings = get_settings()
logger = logging.getLogger(__name__)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _price_plan() -> PricePlan:
    """Build an exact Decimal price plan from human-readable settings."""

    return PricePlan(
        buyer_input_cny_per_million=Decimal(str(settings.buyer_input_cny_per_million)),
        buyer_output_cny_per_million=Decimal(str(settings.buyer_output_cny_per_million)),
        buyer_cache_hit_cny_per_million=Decimal(str(settings.buyer_cache_hit_cny_per_million)),
        seller_input_cny_per_million=Decimal(str(settings.seller_input_cny_per_million)),
        seller_output_cny_per_million=Decimal(str(settings.seller_output_cny_per_million)),
        seller_cache_hit_cny_per_million=Decimal(str(settings.seller_cache_hit_cny_per_million)),
    )


PRICE_PLAN = _price_plan()


@dataclass(frozen=True, slots=True)
class MeteringGuard:
    reservation_micro_cny: int
    input_token_ceiling: int
    output_token_ceiling: int
    payload_bytes: int


def _metering_guard(payload: dict[str, Any]) -> MeteringGuard:
    """Conservatively estimate the maximum charge before calling a provider."""

    encoded_size = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    input_upper_bound = encoded_size + 512
    limits = [
        payload[key]
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
        if key in payload
    ]
    if not limits:
        limits = [8192]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in limits):
        raise ValueError("the output token limit must be a JSON integer")
    output_upper_bound = max(limits)
    if not 1 <= output_upper_bound <= 65_536:
        raise ValueError("the output token limit must be between 1 and 65536")
    estimate = calculate_charge(
        TokenUsage(
            input_tokens=input_upper_bound,
            output_tokens=output_upper_bound,
            cache_miss_tokens=input_upper_bound,
        ),
        PRICE_PLAN,
    ).buyer_micro_cny
    buffered = int(
        (Decimal(estimate) * Decimal("1.20")).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return MeteringGuard(
        reservation_micro_cny=max(settings.request_reserve_micro_cny, buffered),
        input_token_ceiling=input_upper_bound,
        output_token_ceiling=output_upper_bound,
        payload_bytes=encoded_size,
    )


def _normalize_output_limit(
    payload: dict[str, Any], *, upstream_path: str
) -> dict[str, Any]:
    """Collapse compatible output-limit aliases into one provider-facing field."""

    normalized = dict(payload)
    guard = _metering_guard(normalized)
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        normalized.pop(key, None)
    canonical = "max_tokens" if upstream_path == "chat/completions" else "max_output_tokens"
    normalized[canonical] = guard.output_token_ceiling
    return normalized


def _usage_within_authorization(
    usage: TokenUsage,
    guard: MeteringGuard,
    observed_output_bytes: int,
) -> bool:
    platform_output_ceiling = min(
        guard.output_token_ceiling,
        max(8, observed_output_bytes + 8),
    )
    return (
        usage.reported
        and usage.valid
        and usage.input_tokens <= guard.input_token_ceiling
        and usage.output_tokens <= guard.output_token_ceiling
        and usage.output_tokens <= platform_output_ceiling
    )


def _provisional_usage(
    guard: MeteringGuard,
    observed_output_bytes: int,
) -> TokenUsage:
    """Estimate conservatively when a stream ends before final provider usage."""

    # One tokenizer token cannot represent less than one UTF-8 byte. Until a
    # trusted usage record arrives, use the byte ceiling so a late disconnect
    # cannot turn already-delivered output into a predictable underpayment.
    input_tokens = min(guard.input_token_ceiling, max(1, guard.payload_bytes))
    output_tokens = min(
        guard.output_token_ceiling,
        observed_output_bytes + 8 if observed_output_bytes > 0 else 0,
    )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_miss_tokens=input_tokens,
        reported=False,
        valid=True,
    )


async def _probe_source(app: FastAPI, source_id: str) -> ProbeObservation:
    """Run a tiny canary without persisting prompt or response content."""

    supply: Supply | None = await app.state.registry.get_supply(source_id)
    if supply is None or not supply.enabled:
        return ProbeObservation(success=False, error_code="source_unavailable")
    model = (
        settings.probe_model
        if settings.probe_model in supply.models
        else sorted(supply.models)[0]
    )
    left = secrets.randbelow(800) + 100
    right = secrets.randbelow(800) + 100
    nonce = secrets.token_hex(3).upper()
    expected = f"TOKENCOIN_{nonce}_{left + right}"
    challenge = (
        f"Calculate {left} + {right}. Reply with exactly TOKENCOIN_{nonce}_"
        "followed by the integer result, with no other text."
    )
    probe_payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": challenge,
            }
        ],
        "temperature": 0,
        "thinking": {"type": "disabled"},
        "max_tokens": 24,
        "stream": False,
    }
    started = time.perf_counter()
    try:
        response = await app.state.http_client.post(
            f"{supply.base_url.rstrip('/')}/chat/completions",
            headers={
                "authorization": f"Bearer {supply.api_key}",
                "content-type": "application/json",
                "accept": "application/json",
            },
            json=probe_payload,
        )
    except httpx.HTTPError:
        return ProbeObservation(success=False, error_code="transport_error")
    latency_ms = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        return ProbeObservation(
            success=False,
            latency_ms=latency_ms,
            error_code=f"http_{response.status_code}",
        )
    if response.headers.get("content-type", "").partition(";")[0].lower() != "application/json":
        return ProbeObservation(
            success=False,
            latency_ms=latency_ms,
            error_code="invalid_content_type",
        )
    try:
        payload = response.json()
    except ValueError:
        return ProbeObservation(
            success=False,
            latency_ms=latency_ms,
            error_code="invalid_json",
        )

    if not isinstance(payload, dict) or "error" in payload:
        return ProbeObservation(
            success=False,
            latency_ms=latency_ms,
            error_code="invalid_payload",
        )

    choices = payload.get("choices")
    message = choices[0].get("message") if isinstance(choices, list) and choices else None
    content = message.get("content", "") if isinstance(message, dict) else ""
    reported_model = payload.get("model")
    usage = extract_usage(payload)
    probe_guard = _metering_guard(probe_payload)
    return ProbeObservation(
        success=True,
        latency_ms=latency_ms,
        model_match=isinstance(reported_model, str) and reported_model == model,
        capability_match=isinstance(content, str) and content.strip() == expected,
        billing_consistent=(
            usage.total_tokens > 0
            and _usage_within_authorization(
                usage,
                probe_guard,
                observed_output_bytes=semantic_output_bytes(payload, protocol="chat"),
            )
        ),
    )


async def _probe_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    if settings.probe_on_startup:
        await app.state.audit.run_scheduled_batch(
            await app.state.registry.source_ids(),
            lambda source_id: _probe_source(app, source_id),
        )
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.probe_interval_seconds
            )
        except TimeoutError:
            await app.state.audit.run_scheduled_batch(
                await app.state.registry.source_ids(),
                lambda source_id: _probe_source(app, source_id),
            )


async def _ledger_recovery_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    interval = max(
        5.0,
        min(60.0, app.state.ledger.reservation_lease_seconds / 2),
    )
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            try:
                await app.state.ledger.recover_reserved()
            except Exception:
                logger.exception("reservation recovery pass failed; retrying")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    minimum_lease = (
        settings.connect_timeout_seconds
        + settings.read_timeout_seconds
        + settings.write_timeout_seconds
        + settings.pool_timeout_seconds
        + 30.0
    )
    ledger = Ledger(
        settings.database_path,
        reservation_lease_seconds=max(
            settings.reservation_lease_seconds,
            minimum_lease,
        ),
    )
    health = HealthMonitor(
        settings.database_path,
        HealthPolicy(
            failure_threshold=settings.circuit_failure_threshold,
            cooldown_seconds=settings.circuit_cooldown_seconds,
        ),
    )
    audit = AuditService(settings.database_path, health=health)
    await ledger.initialize()
    await ledger.recover_reserved()
    await health.initialize()
    await audit.initialize()
    await ledger.credit_buyer(
        settings.buyer_id,
        settings.buyer_starting_micro_cny,
        "local-demo-opening-balance-v1",
    )

    registry = build_registry(settings)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.write_timeout_seconds,
            pool=settings.pool_timeout_seconds,
        ),
        limits=httpx.Limits(
            max_connections=settings.max_connections,
            max_keepalive_connections=settings.max_keepalive_connections,
        ),
        follow_redirects=False,
    )
    transport = ProviderTransport(
        client,
        registry,
        first_byte_timeout_seconds=settings.first_byte_timeout_seconds,
    )
    app.state.registry = registry
    app.state.http_client = client
    app.state.ledger = ledger
    app.state.health = health
    app.state.audit = audit
    app.state.idempotency_lock = asyncio.Lock()
    app.state.manual_probe_lock = asyncio.Lock()
    app.state.last_manual_probe_at = 0.0
    app.state.gateway = GatewayRouter(registry, transport, health=health)
    stop_event = asyncio.Event()
    probe_task: asyncio.Task[None] | None = None
    recovery_task = asyncio.create_task(_ledger_recovery_loop(app, stop_event))
    if settings.scheduled_probes_enabled and registry.count:
        probe_task = asyncio.create_task(_probe_loop(app, stop_event))

    try:
        yield
    finally:
        stop_event.set()
        if probe_task is not None:
            probe_task.cancel()
            with suppress(asyncio.CancelledError):
                await probe_task
        recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await recovery_task
        await client.aclose()
        await audit.close()
        await health.close()
        await ledger.close()


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["authorization", "content-type", "idempotency-key"],
    expose_headers=[
        "x-tokencoin-request-id",
        "x-tokencoin-source-id",
        "x-tokencoin-attempts",
        "x-tokencoin-metering",
        "x-tokencoin-buyer-charge-micro-cny",
    ],
)
configure_telemetry(app)


def _openai_error(
    message: str,
    *,
    status_code: int,
    error_type: str,
    code: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": code,
            }
        },
    )


def _auth_error(request: Request) -> JSONResponse | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if (
        scheme.lower() != "bearer"
        or not token
        or not secrets.compare_digest(token, settings.buyer_api_key)
    ):
        return _openai_error(
            "A valid TokenCoin buyer API key is required.",
            status_code=401,
            error_type="authentication_error",
            code="invalid_api_key",
        )
    return None


def _admin_auth_error(request: Request) -> JSONResponse | None:
    if settings.admin_api_key is None:
        return _openai_error(
            "The operator API is not configured.",
            status_code=503,
            error_type="service_unavailable_error",
            code="admin_api_not_configured",
        )
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if (
        scheme.lower() != "bearer"
        or not token
        or not secrets.compare_digest(token, settings.admin_api_key)
    ):
        return _openai_error(
            "A valid TokenCoin operator API key is required.",
            status_code=401,
            error_type="authentication_error",
            code="invalid_admin_api_key",
        )
    return None


def _routing_error(exc: Exception) -> Response:
    if isinstance(exc, NoSupplyAvailable):
        if exc.configured:
            return _openai_error(
                "All configured supplies for this model are temporarily unavailable.",
                status_code=503,
                error_type="service_unavailable_error",
                code="supplies_unavailable",
            )
        return _openai_error(
            f"The model '{exc.model}' is not configured on this gateway.",
            status_code=404,
            error_type="invalid_request_error",
            code="model_not_found",
        )
    if isinstance(exc, UpstreamRejected):
        headers = dict(exc.headers)
        headers.setdefault("content-type", "application/json")
        return Response(content=exc.content, status_code=exc.status_code, headers=headers)
    return _openai_error(
        "Every eligible upstream supply failed before a response was available.",
        status_code=502,
        error_type="upstream_error",
        code="all_supplies_failed",
    )


def _request_id(request: Request) -> str | JSONResponse:
    supplied = request.headers.get("idempotency-key", "").strip()
    if not supplied:
        return f"tc_req_{uuid.uuid4().hex}"
    if not _REQUEST_ID_PATTERN.fullmatch(supplied):
        return _openai_error(
            "Idempotency-Key must be 1-128 safe ASCII characters.",
            status_code=400,
            error_type="invalid_request_error",
            code="invalid_idempotency_key",
        )
    return supplied


async def _reserve(
    request: Request,
    request_id: str,
    amount: int,
) -> JSONResponse | None:
    ledger: Ledger = request.app.state.ledger
    async with request.app.state.idempotency_lock:
        try:
            await ledger.reserve(
                request_id,
                settings.buyer_id,
                "routing-pool",
                amount,
                require_new=True,
            )
        except InsufficientFunds:
            return _openai_error(
                "The local demo buyer balance is too low for this request.",
                status_code=402,
                error_type="insufficient_funds_error",
                code="insufficient_balance",
            )
        except IdempotencyConflict:
            return _openai_error(
                "This Idempotency-Key conflicts with an earlier request.",
                status_code=409,
                error_type="conflict_error",
                code="idempotency_conflict",
            )
    return None


async def _reserve_with_cancellation_cleanup(
    request: Request,
    request_id: str,
    amount: int,
) -> JSONResponse | None:
    """Finish the atomic DB claim before honoring cancellation."""

    reserve_task = asyncio.create_task(_reserve(request, request_id, amount))
    try:
        return await asyncio.shield(reserve_task)
    except asyncio.CancelledError:
        reservation_created = False
        try:
            reservation_created = await reserve_task is None
        except Exception:
            pass
        if reservation_created:
            refund_task = asyncio.create_task(
                request.app.state.ledger.refund(
                    request_id, "cancelled_during_reservation"
                )
            )
            try:
                await asyncio.shield(refund_task)
            except asyncio.CancelledError:
                with suppress(asyncio.CancelledError):
                    await refund_task
        raise


async def _reservation_heartbeat(
    request: Request,
    request_id: str,
    stop_event: asyncio.Event,
) -> None:
    ledger: Ledger = request.app.state.ledger
    interval = max(5.0, ledger.reservation_lease_seconds / 3)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            try:
                await ledger.renew_reservation(request_id)
            except (ReservationNotFound, InvalidLedgerTransition):
                return
            except Exception:
                logger.exception(
                    "reservation heartbeat failed for %s; retrying", request_id
                )


async def _stop_reservation_heartbeat(
    stop_event: asyncio.Event,
    task: asyncio.Task[None],
) -> None:
    stop_event.set()
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


async def _settle_usage(
    request: Request,
    *,
    request_id: str,
    supply: Supply,
    model: str,
    usage: TokenUsage,
    guard: MeteringGuard,
    observed_output_bytes: int,
) -> tuple[str, int]:
    ledger: Ledger = request.app.state.ledger
    if not _usage_within_authorization(usage, guard, observed_output_bytes):
        provisional = _provisional_usage(guard, observed_output_bytes)
        charge = calculate_charge(provisional, PRICE_PLAN)
        charged = min(charge.buyer_micro_cny, guard.reservation_micro_cny)
        await ledger.settle(
            request_id,
            actual_amount_minor=charged,
            seller_amount_minor=0,
            seller_id=supply.id,
        )
        with suppress(Exception):
            await ledger.dispute_earning(request_id, "usage_untrusted")
        with suppress(Exception):
            await request.app.state.health.force_open(
                supply.id, reason_code="usage_untrusted"
            )
        record_tokens(
            supply.id,
            model,
            prompt_tokens=provisional.input_tokens,
            completion_tokens=provisional.output_tokens,
        )
        return "provisional_untrusted", charged

    charge = calculate_charge(usage, PRICE_PLAN)
    reservation = await ledger.get_request(request_id)
    if (
        charge.buyer_micro_cny > guard.reservation_micro_cny
        or charge.buyer_micro_cny > reservation.reserved_minor
    ):
        provisional = _provisional_usage(guard, observed_output_bytes)
        fallback_charge = calculate_charge(provisional, PRICE_PLAN)
        charged = min(
            fallback_charge.buyer_micro_cny,
            guard.reservation_micro_cny,
            reservation.reserved_minor,
        )
        await ledger.settle(
            request_id,
            actual_amount_minor=charged,
            seller_amount_minor=0,
            seller_id=supply.id,
        )
        with suppress(Exception):
            await ledger.dispute_earning(
                request_id, "charge_exceeded_authorization"
            )
        with suppress(Exception):
            await request.app.state.health.force_open(
                supply.id, reason_code="charge_exceeded_authorization"
            )
        record_tokens(
            supply.id,
            model,
            prompt_tokens=provisional.input_tokens,
            completion_tokens=provisional.output_tokens,
        )
        return "provisional_untrusted", charged
    await ledger.settle(
        request_id,
        actual_amount_minor=charge.buyer_micro_cny,
        seller_amount_minor=charge.seller_micro_cny,
        seller_id=supply.id,
    )
    record_tokens(
        supply.id,
        model,
        prompt_tokens=usage.input_tokens,
        completion_tokens=usage.output_tokens,
    )
    return "settled", charge.buyer_micro_cny


async def _settle_interrupted_stream(
    request: Request,
    *,
    request_id: str,
    supply: Supply,
    model: str,
    guard: MeteringGuard,
    observed_output_bytes: int,
) -> tuple[str, int]:
    """Charge a bounded local estimate so disconnects cannot create free calls."""

    usage = _provisional_usage(guard, observed_output_bytes)
    charge = calculate_charge(usage, PRICE_PLAN)
    charged = min(charge.buyer_micro_cny, guard.reservation_micro_cny)
    seller_amount = min(charge.seller_micro_cny, charged)
    await request.app.state.ledger.settle(
        request_id,
        actual_amount_minor=charged,
        seller_amount_minor=seller_amount,
        seller_id=supply.id,
    )
    record_tokens(
        supply.id,
        model,
        prompt_tokens=usage.input_tokens,
        completion_tokens=usage.output_tokens,
    )
    return "provisional_interrupted", charged


async def _close_stream_body(body: Any) -> None:
    close = getattr(body, "aclose", None)
    if callable(close):
        await close()


async def _proxy(
    request: Request,
    body: ChatCompletionRequest | ResponsesRequest,
    *,
    upstream_path: str,
) -> Response:
    auth_error = _auth_error(request)
    if auth_error is not None:
        return auth_error
    request_id = _request_id(request)
    if isinstance(request_id, JSONResponse):
        return request_id

    payload = body.upstream_payload()
    if body.stream and upstream_path == "chat/completions":
        stream_options = payload.get("stream_options")
        if not isinstance(stream_options, dict):
            stream_options = {}
        payload["stream_options"] = {**stream_options, "include_usage": True}
    try:
        payload = _normalize_output_limit(payload, upstream_path=upstream_path)
        guard = _metering_guard(payload)
    except ValueError as exc:
        return _openai_error(
            str(exc),
            status_code=400,
            error_type="invalid_request_error",
            code="invalid_output_token_limit",
        )
    reserve_error = await _reserve_with_cancellation_cleanup(
        request, request_id, guard.reservation_micro_cny
    )
    if reserve_error is not None:
        return reserve_error
    lease_stop = asyncio.Event()
    lease_task = asyncio.create_task(
        _reservation_heartbeat(request, request_id, lease_stop)
    )

    gateway: GatewayRouter = request.app.state.gateway
    with tracer().start_as_current_span("tokencoin.proxy") as span:
        span.set_attribute("gen_ai.request.model", body.model)
        span.set_attribute("tokencoin.stream", body.stream)
        try:
            if body.stream:
                routed = await gateway.open_stream(
                    model=body.model,
                    path=upstream_path,
                    payload=payload,
                )
                headers = dict(routed.headers)
                headers.setdefault("content-type", "text/event-stream; charset=utf-8")
                headers["cache-control"] = "no-cache"
                headers["x-accel-buffering"] = "no"
                headers["x-tokencoin-request-id"] = request_id
                headers["x-tokencoin-source-id"] = routed.upstream.supply.id
                headers["x-tokencoin-attempts"] = str(routed.attempts)

                body_started = False
                finalization_task: asyncio.Task[tuple[str, int]] | None = None

                async def finalize_impl(
                    *,
                    completed: bool,
                    usage: TokenUsage,
                    observed_output_bytes: int,
                    emitted_any_event: bool,
                ) -> tuple[str, int]:
                    try:
                        try:
                            if body_started:
                                await _close_stream_body(routed.body)
                            else:
                                await gateway.abort_unstarted_stream(
                                    routed, model=body.model
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            pass

                        if completed or (usage.reported and usage.valid):
                            result = await _settle_usage(
                                request,
                                request_id=request_id,
                                supply=routed.upstream.supply,
                                model=body.model,
                                usage=usage,
                                guard=guard,
                                observed_output_bytes=observed_output_bytes,
                            )
                        elif emitted_any_event:
                            result = await _settle_interrupted_stream(
                                request,
                                request_id=request_id,
                                supply=routed.upstream.supply,
                                model=body.model,
                                guard=guard,
                                observed_output_bytes=observed_output_bytes,
                            )
                        else:
                            await request.app.state.ledger.refund(
                                request_id, "stream_not_started"
                            )
                            result = ("stream_not_started", 0)

                        metering, _ = result
                        GATEWAY_REQUESTS.labels(
                            model=body.model,
                            outcome="success" if metering == "settled" else metering,
                            stream="true",
                        ).inc()
                        return result
                    finally:
                        await _stop_reservation_heartbeat(lease_stop, lease_task)

                async def ensure_finalized(
                    *,
                    completed: bool,
                    usage: TokenUsage,
                    observed_output_bytes: int,
                    emitted_any_event: bool,
                ) -> None:
                    nonlocal finalization_task
                    if finalization_task is None:
                        finalization_task = asyncio.create_task(
                            finalize_impl(
                                completed=completed,
                                usage=usage,
                                observed_output_bytes=observed_output_bytes,
                                emitted_any_event=emitted_any_event,
                            )
                        )
                    try:
                        await asyncio.shield(finalization_task)
                    except asyncio.CancelledError:
                        with suppress(asyncio.CancelledError):
                            await finalization_task
                        raise

                async def metered_body():
                    nonlocal body_started
                    body_started = True
                    accumulator = SSEUsageAccumulator(
                        "chat" if upstream_path == "chat/completions" else "responses"
                    )
                    completed = False
                    emitted_any_event = False
                    try:
                        async for chunk in routed.body:
                            emitted_any_event = emitted_any_event or bool(chunk)
                            accumulator.feed(chunk)
                            yield chunk
                        completed = True
                    finally:
                        usage = accumulator.finish()
                        await ensure_finalized(
                            completed=completed,
                            usage=usage,
                            observed_output_bytes=accumulator.observed_output_bytes,
                            emitted_any_event=emitted_any_event,
                        )

                async def finalize_unstarted_body() -> None:
                    await ensure_finalized(
                        completed=False,
                        usage=TokenUsage(reported=False, valid=False),
                        observed_output_bytes=0,
                        emitted_any_event=False,
                    )

                return StreamingResponse(
                    metered_body(),
                    status_code=200,
                    headers=headers,
                    background=BackgroundTask(finalize_unstarted_body),
                )

            routed_response = await gateway.request(
                model=body.model,
                path=upstream_path,
                payload=payload,
            )
            try:
                response_payload = json.loads(routed_response.content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                response_payload = None
            usage = extract_usage(response_payload)
            metering, charged = await _settle_usage(
                request,
                request_id=request_id,
                supply=routed_response.supply,
                model=body.model,
                usage=usage,
                guard=guard,
                observed_output_bytes=semantic_output_bytes(
                    response_payload,
                    protocol=(
                        "chat" if upstream_path == "chat/completions" else "responses"
                    ),
                ),
            )
            await _stop_reservation_heartbeat(lease_stop, lease_task)
            headers = dict(routed_response.headers)
            headers["x-tokencoin-request-id"] = request_id
            headers["x-tokencoin-source-id"] = routed_response.supply.id
            headers["x-tokencoin-attempts"] = str(routed_response.attempts)
            headers["x-tokencoin-metering"] = metering
            headers["x-tokencoin-buyer-charge-micro-cny"] = str(charged)
            GATEWAY_REQUESTS.labels(
                model=body.model,
                outcome="success" if metering == "settled" else metering,
                stream="false",
            ).inc()
            return Response(
                content=routed_response.content,
                status_code=routed_response.status_code,
                headers=headers,
            )
        except (NoSupplyAvailable, UpstreamRejected, AllSuppliesFailed) as exc:
            await _stop_reservation_heartbeat(lease_stop, lease_task)
            with suppress(Exception):
                await request.app.state.ledger.refund(request_id, "routing_failed")
            GATEWAY_REQUESTS.labels(
                model=body.model, outcome="failed", stream=str(body.stream).lower()
            ).inc()
            return _routing_error(exc)
        except BaseException:
            await _stop_reservation_heartbeat(lease_stop, lease_task)
            with suppress(Exception):
                await request.app.state.ledger.refund(request_id, "gateway_exception")
            GATEWAY_REQUESTS.labels(
                model=body.model, outcome="exception", stream=str(body.stream).lower()
            ).inc()
            raise


@app.get("/v1/models", response_model=None)
async def list_models(request: Request) -> ModelListResponse | Response:
    auth_error = _auth_error(request)
    if auth_error is not None:
        return auth_error
    models = await request.app.state.registry.supported_models()
    created = int(time.time())
    return ModelListResponse(
        data=[
            ModelObject(id=model, created=created, owned_by="tokencoin-market")
            for model in models
        ]
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest) -> Response:
    return await _proxy(request, body, upstream_path="chat/completions")


@app.post("/v1/responses")
async def responses(request: Request, body: ResponsesRequest) -> Response:
    return await _proxy(request, body, upstream_path="responses")


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    return {}


def _get(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _public_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return str(value)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _safe_audit(value: Any) -> dict[str, Any]:
    source = _as_mapping(value)
    passed = bool(_get(source, "passed"))
    reason = _get(source, "reason", "error_code")
    if not passed and reason is None:
        if _get(source, "model_match") is False:
            reason = "model_mismatch"
        elif _get(source, "capability_match") is False:
            reason = "capability_mismatch"
        elif _get(source, "billing_consistent") is False:
            reason = "billing_mismatch"
        elif _get(source, "transport_success") is False:
            reason = "transport_failed"
    return {
        "id": _get(source, "id", "probe_id"),
        "supplyId": _get(source, "supplyId", "supply_id", "source_id"),
        "auditType": _enum_value(_get(source, "auditType", "audit_type", "kind")),
        "status": "passed" if passed else "failed",
        "passed": passed,
        "score": _get(source, "score", "quality_score") or 0,
        "latencyMs": _get(source, "latencyMs", "latency_ms"),
        "reason": reason,
        "detail": "检测通过" if passed else (reason or "检测未通过"),
        "checkedAt": _public_timestamp(
            _get(source, "checkedAt", "checked_at", "created_at")
        ),
    }


async def _recent_audits(request: Request) -> list[dict[str, Any]]:
    rows = await request.app.state.audit.list_results(limit=10)
    return [_safe_audit(row) for row in rows]


async def _pending_cny(request: Request, supply_ids: list[str]) -> float:
    pending = 0
    for supply_id in supply_ids:
        balances = await request.app.state.ledger.get_seller_balances(supply_id)
        pending += balances.pending_minor
    return micro_cny_to_cny(pending)


@app.get("/api/status", response_model=StatusResponse)
async def api_status(request: Request) -> StatusResponse:
    registry = request.app.state.registry
    supplies_raw, counters = await registry.snapshot()
    models = await registry.supported_models()
    audits = await _recent_audits(request)
    last_probe_at = next(
        (item.get("checkedAt") for item in audits if item.get("checkedAt")), None
    )
    enriched: list[dict[str, Any]] = []
    for item in supplies_raw:
        source_id = item["id"]
        health = await request.app.state.health.get(source_id)
        quality = await request.app.state.audit.quality(source_id)
        health_status = health.state.value.replace("-", "_")
        if item["circuit"] == "open":
            health_status = "open"
        enriched.append(
            {
                **item,
                "healthStatus": health_status,
                "reliability": quality.score,
                "latencyMs": health.latency_ewma_ms,
                "available": bool(
                    item["enabled"]
                    and health_status != "open"
                    and not (
                        health_status == "half_open"
                        and (item["inFlight"] > 0 or health.half_open_in_flight)
                    )
                ),
            }
        )
    supply_ids = [item["id"] for item in enriched]
    configured = registry.count > 0
    any_available = any(item["available"] for item in enriched)
    status = "unconfigured" if not configured else ("ok" if any_available else "degraded")
    return StatusResponse(
        status=status,
        gateway=GatewayStatus(
            mode="live" if configured else "unconfigured",
            version=settings.version,
            configuredProviders=registry.count,
            supportedModels=models,
        ),
        summary=StatusSummary(
            totalRequests=counters["totalRequests"],
            successRate=counters["successRate"],
            pendingCny=await _pending_cny(request, supply_ids),
            lastProbeAt=last_probe_at,
        ),
        supplies=[SupplyStatus.model_validate(item) for item in enriched],
        recentAudits=audits,
    )


@app.post("/api/probes/run")
async def run_probe(request: Request, source_id: str | None = None) -> Response:
    auth_error = _admin_auth_error(request)
    if auth_error is not None:
        return auth_error
    async with request.app.state.manual_probe_lock:
        now = time.monotonic()
        elapsed = now - request.app.state.last_manual_probe_at
        if elapsed < settings.manual_probe_cooldown_seconds:
            retry_after = max(1, int(settings.manual_probe_cooldown_seconds - elapsed))
            response = _openai_error(
                "Manual probes are rate limited.",
                status_code=429,
                error_type="rate_limit_error",
                code="manual_probe_rate_limited",
            )
            response.headers["retry-after"] = str(retry_after)
            return response
        request.app.state.last_manual_probe_at = now
    source_ids = await request.app.state.registry.source_ids()
    if source_id is not None:
        if source_id not in source_ids:
            return _openai_error(
                "The requested supply does not exist.",
                status_code=404,
                error_type="invalid_request_error",
                code="supply_not_found",
            )
        source_ids = [source_id]
    results = [
        await request.app.state.audit.run_manual_probe(
            item, lambda selected: _probe_source(request.app, selected)
        )
        for item in source_ids
    ]
    return JSONResponse({"data": [_safe_audit(item) for item in results]})


@app.get("/api/ledger/summary")
async def ledger_summary(request: Request) -> Response:
    auth_error = _auth_error(request)
    if auth_error is not None:
        return auth_error
    balance = await request.app.state.ledger.get_buyer_balance(settings.buyer_id)
    return JSONResponse(
        {
            "buyerId": settings.buyer_id,
            "availableCny": micro_cny_to_cny(balance.available_minor),
            "reservedCny": micro_cny_to_cny(balance.reserved_minor),
        }
    )


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "configuredSupplies": request.app.state.registry.count,
        "version": settings.version,
    }


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
