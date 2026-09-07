"""Manual and scheduled supply probes with a rolling quality score.

Probe callables receive only a ``source_id``.  Credentials remain inside the
caller's configured adapter.  This module deliberately persists no request
payloads, response bodies, exception messages, headers, URLs, or API keys.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from .health import HealthMonitor, HealthState, RequestPermission


class AuditError(RuntimeError):
    """Base error for audit operations."""


class ProbeKind(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class QualityBand(str, Enum):
    UNVERIFIED = "unverified"
    HIGH = "high"
    ACCEPTABLE = "acceptable"
    RISKY = "risky"


@dataclass(frozen=True, slots=True)
class AuditPolicy:
    window_size: int = 20
    recency_decay: float = 0.90
    target_latency_ms: float = 800.0
    maximum_latency_ms: float = 5_000.0
    high_score: float = 85.0
    acceptable_score: float = 65.0

    def __post_init__(self) -> None:
        if not 1 <= self.window_size <= 1000:
            raise ValueError("window_size must be between 1 and 1000")
        if not 0 < self.recency_decay <= 1:
            raise ValueError("recency_decay must be in (0, 1]")
        if self.target_latency_ms < 0:
            raise ValueError("target_latency_ms must be non-negative")
        if self.maximum_latency_ms <= self.target_latency_ms:
            raise ValueError("maximum_latency_ms must exceed target_latency_ms")
        if not 0 < self.acceptable_score <= self.high_score <= 100:
            raise ValueError("quality thresholds must satisfy 0 < acceptable <= high <= 100")


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    """A sanitized observation returned by a provider-specific probe adapter."""

    success: bool
    latency_ms: float | None = None
    model_match: bool | None = None
    capability_match: bool | None = None
    billing_consistent: bool | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    probe_id: str
    source_id: str
    kind: ProbeKind
    passed: bool
    score: float
    latency_ms: float | None
    transport_success: bool
    model_match: bool | None
    capability_match: bool | None
    billing_consistent: bool | None
    error_code: str | None
    created_at: float


@dataclass(frozen=True, slots=True)
class QualitySnapshot:
    source_id: str
    score: float
    band: QualityBand
    sample_size: int
    total_probes: int
    pass_rate: float
    model_match_rate: float | None
    average_latency_ms: float | None
    last_probe_at: float | None


ProbeReturn: TypeAlias = ProbeObservation | Awaitable[ProbeObservation]
ProbeCallable: TypeAlias = Callable[[str], ProbeReturn]
SourceProviderReturn: TypeAlias = Iterable[str] | Awaitable[Iterable[str]]
SourceProvider: TypeAlias = Callable[[], SourceProviderReturn]


_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _require_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{name} must be a non-empty string of at most 200 characters")
    return value


def _safe_error_code(value: str | None, default: str | None = None) -> str | None:
    candidate = value or default
    if candidate is None:
        return None
    if not _CODE_PATTERN.fullmatch(candidate):
        return "invalid_error_code"
    return candidate


class AuditService:
    """Runs probes, updates circuit health, and stores rolling quality metrics."""

    def __init__(
        self,
        db_path: str | Path = "token_market.db",
        health: HealthMonitor | None = None,
        policy: AuditPolicy | None = None,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        database = str(db_path)
        self.policy = policy or AuditPolicy()
        self.health = health or HealthMonitor(db_path)
        self._owns_health = health is None
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            database,
            uri=database.startswith("file:"),
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        self._connection.row_factory = sqlite3.Row

    async def initialize(self) -> None:
        if self._owns_health:
            await self.health.initialize()
        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)
        if self._owns_health:
            await self.health.close()

    async def run_manual_probe(
        self,
        source_id: str,
        probe: ProbeCallable,
        *,
        probe_id: str | None = None,
    ) -> ProbeResult:
        """Run an operator-requested probe, even while a circuit is open."""

        return await self._run_probe(
            _require_id(source_id, "source_id"),
            probe,
            ProbeKind.MANUAL,
            probe_id=probe_id,
            respect_circuit=False,
        )

    async def run_scheduled_probe(
        self,
        source_id: str,
        probe: ProbeCallable,
        *,
        probe_id: str | None = None,
    ) -> ProbeResult | None:
        """Run a background probe unless its source is still cooling down."""

        return await self._run_probe(
            _require_id(source_id, "source_id"),
            probe,
            ProbeKind.SCHEDULED,
            probe_id=probe_id,
            respect_circuit=True,
        )

    async def run_scheduled_batch(
        self,
        source_ids: Iterable[str],
        probe: ProbeCallable,
        *,
        concurrency: int = 8,
    ) -> list[ProbeResult]:
        """Probe a snapshot of sources with bounded concurrency."""

        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        unique_ids = list(dict.fromkeys(_require_id(item, "source_id") for item in source_ids))
        semaphore = asyncio.Semaphore(concurrency)

        async def run_one(source_id: str) -> ProbeResult | None:
            async with semaphore:
                return await self.run_scheduled_probe(source_id, probe)

        results = await asyncio.gather(*(run_one(source_id) for source_id in unique_ids))
        return [result for result in results if result is not None]

    async def serve_schedule(
        self,
        source_provider: SourceProvider,
        probe: ProbeCallable,
        *,
        interval_seconds: float,
        stop_event: asyncio.Event,
        concurrency: int = 8,
    ) -> None:
        """Run scheduled batches until ``stop_event`` is set.

        Intended for a FastAPI lifespan task.  The first batch runs immediately.
        """

        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while not stop_event.is_set():
            supplied = source_provider()
            source_ids = await supplied if inspect.isawaitable(supplied) else supplied
            await self.run_scheduled_batch(source_ids, probe, concurrency=concurrency)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue

    async def get_result(self, probe_id: str) -> ProbeResult | None:
        return await asyncio.to_thread(
            self._get_result_sync, _require_id(probe_id, "probe_id")
        )

    async def list_results(
        self,
        source_id: str | None = None,
        *,
        kind: ProbeKind | str | None = None,
        limit: int = 50,
    ) -> list[ProbeResult]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        normalized_kind = None if kind is None else ProbeKind(kind)
        if source_id is not None:
            _require_id(source_id, "source_id")
        return await asyncio.to_thread(
            self._list_results_sync,
            source_id,
            normalized_kind,
            limit,
        )

    async def quality(self, source_id: str) -> QualitySnapshot:
        return await asyncio.to_thread(
            self._quality_sync, _require_id(source_id, "source_id")
        )

    async def _run_probe(
        self,
        source_id: str,
        probe: ProbeCallable,
        kind: ProbeKind,
        *,
        probe_id: str | None,
        respect_circuit: bool,
    ) -> ProbeResult | None:
        normalized_probe_id = probe_id or uuid.uuid4().hex
        _require_id(normalized_probe_id, "probe_id")
        existing = await self.get_result(normalized_probe_id)
        if existing is not None:
            if existing.source_id != source_id or existing.kind is not kind:
                raise AuditError("probe_id was reused for a different source or probe kind")
            return existing

        permission: RequestPermission | None = None
        if respect_circuit:
            permission = await self.health.allow_request(source_id)
            if not permission.allowed:
                return None

        try:
            started = self._monotonic()
            try:
                returned = probe(source_id)
                observation = await returned if inspect.isawaitable(returned) else returned
                if not isinstance(observation, ProbeObservation):
                    raise TypeError("probe must return ProbeObservation")
            except Exception as exc:
                # Store only the exception class, never its message (which could
                # contain a URL, header, credential, prompt, or provider response).
                observation = ProbeObservation(
                    success=False,
                    error_code=_safe_error_code(type(exc).__name__, "probe_exception"),
                )

            measured_latency = max(0.0, (self._monotonic() - started) * 1000.0)
            latency_ms = (
                measured_latency
                if observation.latency_ms is None
                else float(observation.latency_ms)
            )
            if latency_ms < 0:
                observation = ProbeObservation(
                    success=False,
                    latency_ms=measured_latency,
                    error_code="invalid_latency",
                )
                latency_ms = measured_latency

            normalized = ProbeObservation(
                success=bool(observation.success),
                latency_ms=latency_ms,
                model_match=observation.model_match,
                capability_match=observation.capability_match,
                billing_consistent=observation.billing_consistent,
                error_code=_safe_error_code(observation.error_code),
            )
            passed = self._is_passed(normalized)
            score = self._score_observation(normalized)
            result = ProbeResult(
                probe_id=normalized_probe_id,
                source_id=source_id,
                kind=kind,
                passed=passed,
                score=score,
                latency_ms=normalized.latency_ms,
                transport_success=normalized.success,
                model_match=normalized.model_match,
                capability_match=normalized.capability_match,
                billing_consistent=normalized.billing_consistent,
                error_code=normalized.error_code,
                created_at=self._clock(),
            )
            persisted, inserted = await asyncio.to_thread(
                self._insert_result_sync, result
            )

            if inserted:
                generation = None if permission is None else permission.generation
                if persisted.passed:
                    await self.health.record_success(
                        source_id,
                        latency_ms=persisted.latency_ms,
                        generation=generation,
                        now=persisted.created_at,
                    )
                else:
                    await self.health.record_failure(
                        source_id,
                        reason_code=self._failure_reason(persisted),
                        generation=generation,
                        now=persisted.created_at,
                    )
            return persisted
        finally:
            if permission is not None and permission.state is HealthState.HALF_OPEN:
                await asyncio.shield(
                    self.health.release_permission(
                        source_id,
                        generation=permission.generation,
                    )
                )

    @staticmethod
    def _is_passed(observation: ProbeObservation) -> bool:
        explicit_checks = (
            observation.model_match,
            observation.capability_match,
            observation.billing_consistent,
        )
        return observation.success and all(value is not False for value in explicit_checks)

    def _score_observation(self, observation: ProbeObservation) -> float:
        if not observation.success:
            return 0.0

        components: list[tuple[float, float]] = [(0.50, 1.0)]
        if observation.latency_ms is not None:
            latency = observation.latency_ms
            if latency <= self.policy.target_latency_ms:
                latency_score = 1.0
            elif latency >= self.policy.maximum_latency_ms:
                latency_score = 0.0
            else:
                span = self.policy.maximum_latency_ms - self.policy.target_latency_ms
                latency_score = 1.0 - (latency - self.policy.target_latency_ms) / span
            components.append((0.20, latency_score))
        if observation.model_match is not None:
            components.append((0.15, float(observation.model_match)))
        if observation.capability_match is not None:
            components.append((0.10, float(observation.capability_match)))
        if observation.billing_consistent is not None:
            components.append((0.05, float(observation.billing_consistent)))
        weight = sum(item_weight for item_weight, _ in components)
        value = sum(item_weight * item_score for item_weight, item_score in components)
        score = round(100.0 * value / weight, 2)
        if not self._is_passed(observation):
            failure_cap = max(0.0, self.policy.acceptable_score - 1.0)
            score = min(score, failure_cap)
        return round(score, 2)

    @staticmethod
    def _failure_reason(result: ProbeResult) -> str:
        if not result.transport_success:
            return result.error_code or "probe_transport_failed"
        if result.model_match is False:
            return "model_mismatch"
        if result.capability_match is False:
            return "capability_mismatch"
        if result.billing_consistent is False:
            return "billing_mismatch"
        return result.error_code or "probe_failed"

    def _initialize_sync(self) -> None:
        with self._lock:
            self._ensure_open()
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS probe_results (
                    probe_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('manual', 'scheduled')),
                    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
                    score REAL NOT NULL CHECK (score >= 0 AND score <= 100),
                    latency_ms REAL,
                    transport_success INTEGER NOT NULL CHECK (transport_success IN (0, 1)),
                    model_match INTEGER CHECK (model_match IN (0, 1)),
                    capability_match INTEGER CHECK (capability_match IN (0, 1)),
                    billing_consistent INTEGER CHECK (billing_consistent IN (0, 1)),
                    error_code TEXT,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_probe_results_source_created
                    ON probe_results(source_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS source_quality (
                    source_id TEXT PRIMARY KEY,
                    score REAL NOT NULL,
                    sample_size INTEGER NOT NULL,
                    total_probes INTEGER NOT NULL,
                    pass_rate REAL NOT NULL,
                    model_match_rate REAL,
                    average_latency_ms REAL,
                    last_probe_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def _insert_result_sync(self, result: ProbeResult) -> tuple[ProbeResult, bool]:
        with self._transaction() as cursor:
            existing = cursor.execute(
                "SELECT * FROM probe_results WHERE probe_id = ?", (result.probe_id,)
            ).fetchone()
            if existing is not None:
                parsed = self._result_from_row(existing)
                if parsed.source_id != result.source_id or parsed.kind is not result.kind:
                    raise AuditError("probe_id was reused for a different source or probe kind")
                return parsed, False
            cursor.execute(
                """
                INSERT INTO probe_results(
                    probe_id, source_id, kind, passed, score, latency_ms,
                    transport_success, model_match, capability_match,
                    billing_consistent, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.probe_id,
                    result.source_id,
                    result.kind.value,
                    int(result.passed),
                    result.score,
                    result.latency_ms,
                    int(result.transport_success),
                    self._nullable_bool(result.model_match),
                    self._nullable_bool(result.capability_match),
                    self._nullable_bool(result.billing_consistent),
                    result.error_code,
                    result.created_at,
                ),
            )
            self._recalculate_quality(cursor, result.source_id, result.created_at)
            return result, True

    def _recalculate_quality(
        self, cursor: sqlite3.Cursor, source_id: str, now: float
    ) -> None:
        rows = cursor.execute(
            """
            SELECT * FROM probe_results
            WHERE source_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (source_id, self.policy.window_size),
        ).fetchall()
        weights = [self.policy.recency_decay**index for index in range(len(rows))]
        weight_total = sum(weights)
        score = sum(float(row["score"]) * weight for row, weight in zip(rows, weights)) / weight_total
        pass_rate = sum(int(row["passed"]) for row in rows) / len(rows)
        latencies = [float(row["latency_ms"]) for row in rows if row["latency_ms"] is not None]
        model_checks = [int(row["model_match"]) for row in rows if row["model_match"] is not None]
        total_probes = int(
            cursor.execute(
                "SELECT COUNT(*) AS count FROM probe_results WHERE source_id = ?",
                (source_id,),
            ).fetchone()["count"]
        )
        cursor.execute(
            """
            INSERT INTO source_quality(
                source_id, score, sample_size, total_probes, pass_rate,
                model_match_rate, average_latency_ms, last_probe_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                score = excluded.score,
                sample_size = excluded.sample_size,
                total_probes = excluded.total_probes,
                pass_rate = excluded.pass_rate,
                model_match_rate = excluded.model_match_rate,
                average_latency_ms = excluded.average_latency_ms,
                last_probe_at = excluded.last_probe_at,
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                round(score, 2),
                len(rows),
                total_probes,
                pass_rate,
                None if not model_checks else sum(model_checks) / len(model_checks),
                None if not latencies else sum(latencies) / len(latencies),
                float(rows[0]["created_at"]),
                now,
            ),
        )

    def _get_result_sync(self, probe_id: str) -> ProbeResult | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM probe_results WHERE probe_id = ?", (probe_id,)
            ).fetchone()
            return None if row is None else self._result_from_row(row)

    def _list_results_sync(
        self, source_id: str | None, kind: ProbeKind | None, limit: int
    ) -> list[ProbeResult]:
        with self._lock:
            self._ensure_open()
            if source_id is None and kind is None:
                rows = self._connection.execute(
                    "SELECT * FROM probe_results ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            elif source_id is None:
                rows = self._connection.execute(
                    """
                    SELECT * FROM probe_results
                    WHERE kind = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT ?
                    """,
                    (kind.value, limit),
                ).fetchall()
            elif kind is None:
                rows = self._connection.execute(
                    """
                    SELECT * FROM probe_results
                    WHERE source_id = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT ?
                    """,
                    (source_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM probe_results
                    WHERE source_id = ? AND kind = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT ?
                    """,
                    (source_id, kind.value, limit),
                ).fetchall()
            return [self._result_from_row(row) for row in rows]

    def _quality_sync(self, source_id: str) -> QualitySnapshot:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM source_quality WHERE source_id = ?", (source_id,)
            ).fetchone()
            if row is None:
                return QualitySnapshot(
                    source_id=source_id,
                    score=0.0,
                    band=QualityBand.UNVERIFIED,
                    sample_size=0,
                    total_probes=0,
                    pass_rate=0.0,
                    model_match_rate=None,
                    average_latency_ms=None,
                    last_probe_at=None,
                )
            score = float(row["score"])
            if score >= self.policy.high_score:
                band = QualityBand.HIGH
            elif score >= self.policy.acceptable_score:
                band = QualityBand.ACCEPTABLE
            else:
                band = QualityBand.RISKY
            return QualitySnapshot(
                source_id=source_id,
                score=score,
                band=band,
                sample_size=int(row["sample_size"]),
                total_probes=int(row["total_probes"]),
                pass_rate=float(row["pass_rate"]),
                model_match_rate=(
                    None
                    if row["model_match_rate"] is None
                    else float(row["model_match_rate"])
                ),
                average_latency_ms=(
                    None
                    if row["average_latency_ms"] is None
                    else float(row["average_latency_ms"])
                ),
                last_probe_at=float(row["last_probe_at"]),
            )

    @staticmethod
    def _nullable_bool(value: bool | None) -> int | None:
        return None if value is None else int(value)

    @staticmethod
    def _result_from_row(row: sqlite3.Row) -> ProbeResult:
        def optional_bool(column: str) -> bool | None:
            return None if row[column] is None else bool(row[column])

        return ProbeResult(
            probe_id=row["probe_id"],
            source_id=row["source_id"],
            kind=ProbeKind(row["kind"]),
            passed=bool(row["passed"]),
            score=float(row["score"]),
            latency_ms=None if row["latency_ms"] is None else float(row["latency_ms"]),
            transport_success=bool(row["transport_success"]),
            model_match=optional_bool("model_match"),
            capability_match=optional_bool("capability_match"),
            billing_consistent=optional_bool("billing_consistent"),
            error_code=row["error_code"],
            created_at=float(row["created_at"]),
        )

    def _transaction(self):
        return _AuditTransaction(self)

    def _ensure_open(self) -> None:
        if self._closed:
            raise AuditError("audit service is closed")

    def _close_sync(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True


class _AuditTransaction:
    def __init__(self, service: AuditService) -> None:
        self._service = service
        self._cursor: sqlite3.Cursor | None = None

    def __enter__(self) -> sqlite3.Cursor:
        self._service._lock.acquire()
        try:
            self._service._ensure_open()
            self._service._connection.execute("BEGIN IMMEDIATE")
            self._cursor = self._service._connection.cursor()
            return self._cursor
        except BaseException:
            self._service._lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is None:
                self._service._connection.commit()
            else:
                self._service._connection.rollback()
        finally:
            if self._cursor is not None:
                self._cursor.close()
            self._service._lock.release()
        return False


ProbeAuditor = AuditService

__all__ = (
    "AuditError",
    "AuditPolicy",
    "AuditService",
    "ProbeAuditor",
    "ProbeCallable",
    "ProbeKind",
    "ProbeObservation",
    "ProbeResult",
    "QualityBand",
    "QualitySnapshot",
    "SourceProvider",
)
