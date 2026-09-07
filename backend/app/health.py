"""Persistent source health and circuit-breaker state.

The monitor exposes async methods backed by standard-library SQLite.  It does
not know anything about provider credentials and stores only operational codes
and aggregate timing data.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable


class HealthError(RuntimeError):
    """Base error for health-monitor operations."""


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    latency_ewma_alpha: float = 0.25
    half_open_lease_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        if not 0 < self.latency_ewma_alpha <= 1:
            raise ValueError("latency_ewma_alpha must be in (0, 1]")
        if self.half_open_lease_seconds <= 0:
            raise ValueError("half_open_lease_seconds must be positive")


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source_id: str
    state: HealthState
    consecutive_failures: int
    total_successes: int
    total_failures: int
    last_success_at: float | None
    last_failure_at: float | None
    opened_at: float | None
    half_open_in_flight: bool
    latency_ewma_ms: float | None
    last_reason_code: str | None
    updated_at: float
    generation: int = 0
    half_open_lease_expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class RequestPermission:
    allowed: bool
    state: HealthState
    retry_after_seconds: float | None = None
    generation: int = 0
    lease_expires_at: float | None = None


_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _require_source_id(source_id: str) -> str:
    if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200:
        raise ValueError("source_id must be a non-empty string of at most 200 characters")
    return source_id


def _safe_reason_code(reason_code: str | None) -> str:
    candidate = reason_code or "request_failed"
    if not _CODE_PATTERN.fullmatch(candidate):
        raise ValueError("reason_code must contain only letters, numbers, '.', '_', ':', or '-'")
    return candidate


def _optional_generation(generation: int | None) -> int | None:
    if generation is None:
        return None
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("generation must be a non-negative integer")
    return generation


class HealthMonitor:
    """SQLite-backed circuit breaker for independently routed sources."""

    def __init__(
        self,
        db_path: str | Path = "token_market.db",
        policy: HealthPolicy | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        database = str(db_path)
        self.policy = policy or HealthPolicy()
        self._clock = clock
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
        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    async def get(self, source_id: str, *, now: float | None = None) -> SourceHealth:
        """Return state, advancing an expired open circuit to half-open."""

        return await asyncio.to_thread(
            self._get_sync,
            _require_source_id(source_id),
            self._clock() if now is None else float(now),
        )

    async def status(
        self, source_id: str, *, now: float | None = None
    ) -> SourceHealth:
        return await self.get(source_id, now=now)

    async def allow_request(
        self, source_id: str, *, now: float | None = None
    ) -> RequestPermission:
        """Reserve permission for a request or the sole half-open trial."""

        return await asyncio.to_thread(
            self._allow_request_sync,
            _require_source_id(source_id),
            self._clock() if now is None else float(now),
        )

    async def record_success(
        self,
        source_id: str,
        *,
        latency_ms: float | None = None,
        generation: int | None = None,
        now: float | None = None,
    ) -> SourceHealth:
        if latency_ms is not None and latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        return await asyncio.to_thread(
            self._record_success_sync,
            _require_source_id(source_id),
            None if latency_ms is None else float(latency_ms),
            _optional_generation(generation),
            self._clock() if now is None else float(now),
        )

    async def record_failure(
        self,
        source_id: str,
        *,
        reason_code: str = "request_failed",
        generation: int | None = None,
        now: float | None = None,
    ) -> SourceHealth:
        return await asyncio.to_thread(
            self._record_failure_sync,
            _require_source_id(source_id),
            _safe_reason_code(reason_code),
            _optional_generation(generation),
            self._clock() if now is None else float(now),
        )

    async def release_permission(
        self,
        source_id: str,
        *,
        generation: int | None = None,
        now: float | None = None,
    ) -> SourceHealth:
        """Release a half-open trial lease, normally from a request ``finally``."""

        return await asyncio.to_thread(
            self._release_permission_sync,
            _require_source_id(source_id),
            _optional_generation(generation),
            self._clock() if now is None else float(now),
        )

    async def force_open(
        self,
        source_id: str,
        *,
        reason_code: str = "manual_isolation",
        now: float | None = None,
    ) -> SourceHealth:
        return await asyncio.to_thread(
            self._force_open_sync,
            _require_source_id(source_id),
            _safe_reason_code(reason_code),
            self._clock() if now is None else float(now),
        )

    async def reset(
        self, source_id: str, *, now: float | None = None
    ) -> SourceHealth:
        return await asyncio.to_thread(
            self._reset_sync,
            _require_source_id(source_id),
            self._clock() if now is None else float(now),
        )

    async def list_sources(self, *, limit: int = 100) -> list[SourceHealth]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await asyncio.to_thread(self._list_sources_sync, limit)

    def _initialize_sync(self) -> None:
        with self._lock:
            self._ensure_open()
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_health (
                    source_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('healthy', 'degraded', 'open', 'half-open')),
                    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
                    total_successes INTEGER NOT NULL DEFAULT 0 CHECK (total_successes >= 0),
                    total_failures INTEGER NOT NULL DEFAULT 0 CHECK (total_failures >= 0),
                    last_success_at REAL,
                    last_failure_at REAL,
                    opened_at REAL,
                    half_open_in_flight INTEGER NOT NULL DEFAULT 0 CHECK (half_open_in_flight IN (0, 1)),
                    half_open_lease_expires_at REAL,
                    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
                    latency_ewma_ms REAL,
                    last_reason_code TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(source_health)"
                ).fetchall()
            }
            if "half_open_lease_expires_at" not in columns:
                self._connection.execute(
                    "ALTER TABLE source_health ADD COLUMN half_open_lease_expires_at REAL"
                )
            if "generation" not in columns:
                self._connection.execute(
                    "ALTER TABLE source_health ADD COLUMN generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0)"
                )

    def _get_sync(self, source_id: str, now: float) -> SourceHealth:
        with self._transaction() as cursor:
            row = self._ensure_source(cursor, source_id, now)
            row = self._advance_cooldown(cursor, row, now)
            return self._from_row(row)

    def _allow_request_sync(self, source_id: str, now: float) -> RequestPermission:
        with self._transaction() as cursor:
            row = self._ensure_source(cursor, source_id, now)
            row = self._advance_cooldown(cursor, row, now)
            state = HealthState(row["state"])

            if state is HealthState.OPEN:
                opened_at = float(row["opened_at"] or now)
                elapsed = max(0.0, now - opened_at)
                retry_after = max(0.0, self.policy.cooldown_seconds - elapsed)
                return RequestPermission(
                    False,
                    state,
                    retry_after,
                    generation=int(row["generation"]),
                )

            if state is HealthState.HALF_OPEN:
                if bool(row["half_open_in_flight"]):
                    lease_expires_at = row["half_open_lease_expires_at"]
                    if lease_expires_at is not None and now >= float(lease_expires_at):
                        cursor.execute(
                            """
                            UPDATE source_health
                            SET half_open_in_flight = 0,
                                half_open_lease_expires_at = NULL,
                                generation = generation + 1,
                                updated_at = ?
                            WHERE source_id = ?
                            """,
                            (now, source_id),
                        )
                        row = self._select(cursor, source_id)
                    else:
                        retry_after = (
                            None
                            if lease_expires_at is None
                            else max(0.0, float(lease_expires_at) - now)
                        )
                        return RequestPermission(
                            False,
                            state,
                            retry_after,
                            generation=int(row["generation"]),
                            lease_expires_at=(
                                None
                                if lease_expires_at is None
                                else float(lease_expires_at)
                            ),
                        )
                lease_expires_at = now + self.policy.half_open_lease_seconds
                cursor.execute(
                    """
                    UPDATE source_health
                    SET half_open_in_flight = 1,
                        half_open_lease_expires_at = ?, updated_at = ?
                    WHERE source_id = ?
                    """,
                    (lease_expires_at, now, source_id),
                )
                return RequestPermission(
                    True,
                    state,
                    None,
                    generation=int(row["generation"]),
                    lease_expires_at=lease_expires_at,
                )

            return RequestPermission(
                True,
                state,
                None,
                generation=int(row["generation"]),
            )

    def _record_success_sync(
        self,
        source_id: str,
        latency_ms: float | None,
        generation: int | None,
        now: float,
    ) -> SourceHealth:
        with self._transaction() as cursor:
            row = self._ensure_source(cursor, source_id, now)
            if generation is not None and generation != int(row["generation"]):
                return self._from_row(row)
            previous_ewma = row["latency_ewma_ms"]
            if latency_ms is None:
                ewma = previous_ewma
            elif previous_ewma is None:
                ewma = latency_ms
            else:
                alpha = self.policy.latency_ewma_alpha
                ewma = alpha * latency_ms + (1 - alpha) * float(previous_ewma)
            cursor.execute(
                """
                UPDATE source_health
                SET state = 'healthy', consecutive_failures = 0,
                    total_successes = total_successes + 1,
                    last_success_at = ?, opened_at = NULL,
                    half_open_in_flight = 0,
                    half_open_lease_expires_at = NULL, latency_ewma_ms = ?,
                    last_reason_code = NULL, updated_at = ?
                WHERE source_id = ?
                """,
                (now, ewma, now, source_id),
            )
            return self._from_row(self._select(cursor, source_id))

    def _record_failure_sync(
        self,
        source_id: str,
        reason_code: str,
        generation: int | None,
        now: float,
    ) -> SourceHealth:
        with self._transaction() as cursor:
            row = self._ensure_source(cursor, source_id, now)
            if generation is not None and generation != int(row["generation"]):
                return self._from_row(row)
            previous_state = HealthState(row["state"])
            failures = int(row["consecutive_failures"]) + 1
            should_open = (
                previous_state in (HealthState.OPEN, HealthState.HALF_OPEN)
                or failures >= self.policy.failure_threshold
            )
            new_state = HealthState.OPEN if should_open else HealthState.DEGRADED
            opened_at = now if should_open else None
            increment_generation = should_open and previous_state is not HealthState.OPEN
            cursor.execute(
                """
                UPDATE source_health
                SET state = ?, consecutive_failures = ?,
                    total_failures = total_failures + 1,
                    last_failure_at = ?, opened_at = ?,
                    half_open_in_flight = 0,
                    half_open_lease_expires_at = NULL,
                    generation = generation + ?,
                    last_reason_code = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (
                    new_state.value,
                    failures,
                    now,
                    opened_at,
                    int(increment_generation),
                    reason_code,
                    now,
                    source_id,
                ),
            )
            return self._from_row(self._select(cursor, source_id))

    def _release_permission_sync(
        self, source_id: str, generation: int | None, now: float
    ) -> SourceHealth:
        with self._transaction() as cursor:
            row = self._ensure_source(cursor, source_id, now)
            generation_matches = (
                generation is None or generation == int(row["generation"])
            )
            if (
                HealthState(row["state"]) is HealthState.HALF_OPEN
                and bool(row["half_open_in_flight"])
                and generation_matches
            ):
                cursor.execute(
                    """
                    UPDATE source_health
                    SET half_open_in_flight = 0,
                        half_open_lease_expires_at = NULL,
                        updated_at = ?
                    WHERE source_id = ?
                    """,
                    (now, source_id),
                )
                row = self._select(cursor, source_id)
            return self._from_row(row)

    def _force_open_sync(
        self, source_id: str, reason_code: str, now: float
    ) -> SourceHealth:
        with self._transaction() as cursor:
            self._ensure_source(cursor, source_id, now)
            cursor.execute(
                """
                UPDATE source_health
                SET state = 'open', opened_at = ?, half_open_in_flight = 0,
                    half_open_lease_expires_at = NULL,
                    generation = generation + 1,
                    last_reason_code = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (now, reason_code, now, source_id),
            )
            return self._from_row(self._select(cursor, source_id))

    def _reset_sync(self, source_id: str, now: float) -> SourceHealth:
        with self._transaction() as cursor:
            self._ensure_source(cursor, source_id, now)
            cursor.execute(
                """
                UPDATE source_health
                SET state = 'healthy', consecutive_failures = 0,
                    opened_at = NULL, half_open_in_flight = 0,
                    half_open_lease_expires_at = NULL,
                    generation = generation + 1,
                    last_reason_code = NULL, updated_at = ?
                WHERE source_id = ?
                """,
                (now, source_id),
            )
            return self._from_row(self._select(cursor, source_id))

    def _list_sources_sync(self, limit: int) -> list[SourceHealth]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM source_health ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._from_row(row) for row in rows]

    def _ensure_source(
        self, cursor: sqlite3.Cursor, source_id: str, now: float
    ) -> sqlite3.Row:
        cursor.execute(
            """
            INSERT INTO source_health(source_id, state, updated_at)
            VALUES (?, 'healthy', ?)
            ON CONFLICT(source_id) DO NOTHING
            """,
            (source_id, now),
        )
        return self._select(cursor, source_id)

    def _advance_cooldown(
        self, cursor: sqlite3.Cursor, row: sqlite3.Row, now: float
    ) -> sqlite3.Row:
        if row["state"] != HealthState.OPEN.value or row["opened_at"] is None:
            return row
        if now - float(row["opened_at"]) < self.policy.cooldown_seconds:
            return row
        cursor.execute(
            """
            UPDATE source_health
            SET state = 'half-open', half_open_in_flight = 0,
                half_open_lease_expires_at = NULL, updated_at = ?
            WHERE source_id = ?
            """,
            (now, row["source_id"]),
        )
        return self._select(cursor, row["source_id"])

    @staticmethod
    def _select(cursor: sqlite3.Cursor, source_id: str) -> sqlite3.Row:
        row = cursor.execute(
            "SELECT * FROM source_health WHERE source_id = ?", (source_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - protected by _ensure_source
            raise HealthError(f"source health row disappeared: {source_id}")
        return row

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SourceHealth:
        return SourceHealth(
            source_id=row["source_id"],
            state=HealthState(row["state"]),
            consecutive_failures=int(row["consecutive_failures"]),
            total_successes=int(row["total_successes"]),
            total_failures=int(row["total_failures"]),
            last_success_at=(
                None if row["last_success_at"] is None else float(row["last_success_at"])
            ),
            last_failure_at=(
                None if row["last_failure_at"] is None else float(row["last_failure_at"])
            ),
            opened_at=None if row["opened_at"] is None else float(row["opened_at"]),
            half_open_in_flight=bool(row["half_open_in_flight"]),
            latency_ewma_ms=(
                None
                if row["latency_ewma_ms"] is None
                else float(row["latency_ewma_ms"])
            ),
            last_reason_code=row["last_reason_code"],
            updated_at=float(row["updated_at"]),
            generation=int(row["generation"]),
            half_open_lease_expires_at=(
                None
                if row["half_open_lease_expires_at"] is None
                else float(row["half_open_lease_expires_at"])
            ),
        )

    def _transaction(self):
        return _HealthTransaction(self)

    def _ensure_open(self) -> None:
        if self._closed:
            raise HealthError("health monitor is closed")

    def _close_sync(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True


class _HealthTransaction:
    def __init__(self, monitor: HealthMonitor) -> None:
        self._monitor = monitor
        self._cursor: sqlite3.Cursor | None = None

    def __enter__(self) -> sqlite3.Cursor:
        self._monitor._lock.acquire()
        try:
            self._monitor._ensure_open()
            self._monitor._connection.execute("BEGIN IMMEDIATE")
            self._cursor = self._monitor._connection.cursor()
            return self._cursor
        except BaseException:
            self._monitor._lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is None:
                self._monitor._connection.commit()
            else:
                self._monitor._connection.rollback()
        finally:
            if self._cursor is not None:
                self._cursor.close()
            self._monitor._lock.release()
        return False


CircuitBreaker = HealthMonitor

__all__ = (
    "CircuitBreaker",
    "HealthError",
    "HealthMonitor",
    "HealthPolicy",
    "HealthState",
    "RequestPermission",
    "SourceHealth",
)
