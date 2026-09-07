"""Idempotent SQLite ledger for buyer reservations and seller settlement.

Every integer amount is stored in micro-CNY: 1 CNY equals 1,000,000 units.
The generic ``*_minor`` names let the ledger later support other currencies
without floating-point arithmetic. The public API is asynchronous so SQLite
work does not block the event loop.
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
from typing import Callable, Iterable


class LedgerError(RuntimeError):
    """Base error for ledger operations."""


class InsufficientFunds(LedgerError):
    """The buyer does not have enough available balance to reserve a request."""


class IdempotencyConflict(LedgerError):
    """An idempotency key was reused with a different payload."""


class InvalidLedgerTransition(LedgerError):
    """The requested state transition is not allowed."""


class ReservationNotFound(LedgerError):
    """No request exists for the supplied request id."""


class EarningNotFound(LedgerError):
    """No seller earning exists for the supplied request id."""


class RequestStatus(str, Enum):
    RESERVED = "reserved"
    SETTLED = "settled"
    REFUNDED = "refunded"


class EarningStatus(str, Enum):
    PENDING = "pending"
    CLEARED = "cleared"
    DISPUTED = "disputed"


@dataclass(frozen=True, slots=True)
class BuyerBalance:
    buyer_id: str
    available_minor: int
    reserved_minor: int

    @property
    def total_minor(self) -> int:
        return self.available_minor + self.reserved_minor


@dataclass(frozen=True, slots=True)
class LedgerRequest:
    request_id: str
    buyer_id: str
    seller_id: str
    reserved_minor: int
    charged_minor: int
    seller_amount_minor: int
    platform_fee_minor: int
    status: RequestStatus
    failure_reason_code: str | None
    created_at: float
    updated_at: float
    expires_at: float | None


@dataclass(frozen=True, slots=True)
class SellerEarning:
    request_id: str
    seller_id: str
    amount_minor: int
    status: EarningStatus
    dispute_reason_code: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class SellerBalances:
    seller_id: str
    pending_minor: int
    cleared_minor: int
    disputed_minor: int

    @property
    def total_minor(self) -> int:
        return self.pending_minor + self.cleared_minor + self.disputed_minor


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    entry_id: int
    request_id: str
    entry_type: str
    buyer_id: str | None
    seller_id: str | None
    amount_minor: int
    created_at: float


_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _require_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{name} must be a non-empty string of at most 200 characters")
    return value


def _require_minor(value: int, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer minor-unit amount")
    minimum = 1 if positive else 0
    if value < minimum:
        comparator = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be {comparator}")
    return value


def _safe_code(value: str | None, default: str) -> str:
    candidate = value or default
    if not _CODE_PATTERN.fullmatch(candidate):
        raise ValueError("reason_code must contain only letters, numbers, '.', '_', ':', or '-'")
    return candidate


class Ledger:
    """Small, transactional ledger suitable for importing from FastAPI.

    Call :meth:`initialize` once during application startup and :meth:`close`
    during shutdown.  A single instance may safely serve concurrent coroutines.
    """

    def __init__(
        self,
        db_path: str | Path = "token_market.db",
        *,
        clock: Callable[[], float] = time.time,
        reservation_lease_seconds: float = 300.0,
    ) -> None:
        if reservation_lease_seconds <= 0:
            raise ValueError("reservation_lease_seconds must be positive")
        database = str(db_path)
        self._clock = clock
        self._reservation_lease_seconds = float(reservation_lease_seconds)
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

    @property
    def reservation_lease_seconds(self) -> float:
        return self._reservation_lease_seconds

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    async def credit_buyer(
        self,
        buyer_id: str,
        amount_minor: int,
        idempotency_key: str,
    ) -> BuyerBalance:
        """Credit a buyer once; retries with the same payload are harmless."""

        return await asyncio.to_thread(
            self._credit_buyer_sync,
            _require_id(buyer_id, "buyer_id"),
            _require_minor(amount_minor, "amount_minor", positive=True),
            _require_id(idempotency_key, "idempotency_key"),
        )

    async def get_buyer_balance(self, buyer_id: str) -> BuyerBalance:
        return await asyncio.to_thread(
            self._get_buyer_balance_sync, _require_id(buyer_id, "buyer_id")
        )

    async def reserve(
        self,
        request_id: str,
        buyer_id: str,
        seller_id: str,
        amount_minor: int,
        *,
        require_new: bool = False,
    ) -> LedgerRequest:
        """Move buyer funds from available to reserved exactly once."""

        return await asyncio.to_thread(
            self._reserve_sync,
            _require_id(request_id, "request_id"),
            _require_id(buyer_id, "buyer_id"),
            _require_id(seller_id, "seller_id"),
            _require_minor(amount_minor, "amount_minor", positive=True),
            bool(require_new),
        )

    async def settle(
        self,
        request_id: str,
        actual_amount_minor: int | None = None,
        seller_amount_minor: int | None = None,
        seller_id: str | None = None,
    ) -> LedgerRequest:
        """Settle a successful request and create pending seller income.

        ``actual_amount_minor`` defaults to the full reservation.  Any unused
        reservation is immediately returned to the buyer.  The seller amount
        defaults to the actual charge; the difference is the platform fee.
        ``seller_id`` may identify the source selected after a reservation was
        placed against a routing pool.  Only a still-reserved request can have
        its seller replaced.
        """

        if actual_amount_minor is not None:
            _require_minor(actual_amount_minor, "actual_amount_minor")
        if seller_amount_minor is not None:
            _require_minor(seller_amount_minor, "seller_amount_minor")
        if seller_id is not None:
            _require_id(seller_id, "seller_id")
        return await asyncio.to_thread(
            self._settle_sync,
            _require_id(request_id, "request_id"),
            actual_amount_minor,
            seller_amount_minor,
            seller_id,
        )

    async def refund(
        self,
        request_id: str,
        reason_code: str = "upstream_failed",
    ) -> LedgerRequest:
        """Return a failed request's full reservation exactly once."""

        return await asyncio.to_thread(
            self._refund_sync,
            _require_id(request_id, "request_id"),
            _safe_code(reason_code, "upstream_failed"),
        )

    async def renew_reservation(self, request_id: str) -> LedgerRequest:
        """Extend a live request lease while an upstream call is in progress."""

        return await asyncio.to_thread(
            self._renew_reservation_sync,
            _require_id(request_id, "request_id"),
        )

    async def recover_reserved(
        self,
        reason_code: str = "startup_recovery",
    ) -> int:
        """Refund only reservations whose processing lease has expired."""

        return await asyncio.to_thread(
            self._recover_reserved_sync,
            _safe_code(reason_code, "startup_recovery"),
        )

    async def get_request(self, request_id: str) -> LedgerRequest:
        return await asyncio.to_thread(
            self._get_request_sync, _require_id(request_id, "request_id")
        )

    async def get_earning(self, request_id: str) -> SellerEarning:
        return await asyncio.to_thread(
            self._get_earning_sync, _require_id(request_id, "request_id")
        )

    async def get_seller_balances(self, seller_id: str) -> SellerBalances:
        return await asyncio.to_thread(
            self._get_seller_balances_sync, _require_id(seller_id, "seller_id")
        )

    async def clear_earning(self, request_id: str) -> SellerEarning:
        """Move one pending earning to cleared; repeated calls are idempotent."""

        return await asyncio.to_thread(
            self._clear_earning_sync, _require_id(request_id, "request_id")
        )

    async def clear_pending_earnings(
        self,
        *,
        seller_id: str | None = None,
        before: float | None = None,
    ) -> int:
        """Clear pending rows created at or before ``before`` and return a count."""

        if seller_id is not None:
            _require_id(seller_id, "seller_id")
        cutoff = self._clock() if before is None else float(before)
        return await asyncio.to_thread(
            self._clear_pending_earnings_sync, seller_id, cutoff
        )

    async def dispute_earning(
        self,
        request_id: str,
        reason_code: str = "quality_review",
    ) -> SellerEarning:
        """Mark pending or cleared seller income as disputed."""

        return await asyncio.to_thread(
            self._dispute_earning_sync,
            _require_id(request_id, "request_id"),
            _safe_code(reason_code, "quality_review"),
        )

    async def list_entries(
        self, request_id: str | None = None, *, limit: int = 100
    ) -> list[LedgerEntry]:
        if request_id is not None:
            _require_id(request_id, "request_id")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await asyncio.to_thread(self._list_entries_sync, request_id, limit)

    def _initialize_sync(self) -> None:
        with self._lock:
            self._ensure_open()
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS buyer_accounts (
                    buyer_id TEXT PRIMARY KEY,
                    available_minor INTEGER NOT NULL DEFAULT 0 CHECK (available_minor >= 0),
                    reserved_minor INTEGER NOT NULL DEFAULT 0 CHECK (reserved_minor >= 0),
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS buyer_credits (
                    idempotency_key TEXT PRIMARY KEY,
                    buyer_id TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ledger_requests (
                    request_id TEXT PRIMARY KEY,
                    buyer_id TEXT NOT NULL,
                    seller_id TEXT NOT NULL,
                    reserved_minor INTEGER NOT NULL CHECK (reserved_minor > 0),
                    charged_minor INTEGER NOT NULL DEFAULT 0 CHECK (charged_minor >= 0),
                    seller_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK (seller_amount_minor >= 0),
                    platform_fee_minor INTEGER NOT NULL DEFAULT 0 CHECK (platform_fee_minor >= 0),
                    status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'refunded')),
                    failure_reason_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    settled_at REAL,
                    refunded_at REAL,
                    expires_at REAL,
                    FOREIGN KEY (buyer_id) REFERENCES buyer_accounts(buyer_id)
                );

                CREATE TABLE IF NOT EXISTS seller_earnings (
                    request_id TEXT PRIMARY KEY,
                    seller_id TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'cleared', 'disputed')),
                    dispute_reason_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    cleared_at REAL,
                    disputed_at REAL,
                    FOREIGN KEY (request_id) REFERENCES ledger_requests(request_id)
                );

                CREATE INDEX IF NOT EXISTS idx_seller_earnings_seller_status
                    ON seller_earnings(seller_id, status, created_at);

                CREATE TABLE IF NOT EXISTS ledger_entries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    buyer_id TEXT,
                    seller_id TEXT,
                    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
                    created_at REAL NOT NULL,
                    UNIQUE (request_id, entry_type)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(ledger_requests)"
                ).fetchall()
            }
            if "expires_at" not in columns:
                self._connection.execute(
                    "ALTER TABLE ledger_requests ADD COLUMN expires_at REAL"
                )
            self._connection.execute(
                """
                UPDATE ledger_requests
                SET expires_at = created_at
                WHERE status = 'reserved' AND expires_at IS NULL
                """
            )

    def _credit_buyer_sync(
        self, buyer_id: str, amount_minor: int, idempotency_key: str
    ) -> BuyerBalance:
        now = self._clock()
        with self._transaction() as cursor:
            existing = cursor.execute(
                "SELECT buyer_id, amount_minor FROM buyer_credits WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["buyer_id"] != buyer_id or existing["amount_minor"] != amount_minor:
                    raise IdempotencyConflict(
                        "credit idempotency key was reused with a different payload"
                    )
                return self._balance_from_cursor(cursor, buyer_id)

            cursor.execute(
                """
                INSERT INTO buyer_accounts(buyer_id, available_minor, reserved_minor, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(buyer_id) DO UPDATE SET
                    available_minor = available_minor + excluded.available_minor,
                    updated_at = excluded.updated_at
                """,
                (buyer_id, amount_minor, now),
            )
            cursor.execute(
                "INSERT INTO buyer_credits(idempotency_key, buyer_id, amount_minor, created_at) VALUES (?, ?, ?, ?)",
                (idempotency_key, buyer_id, amount_minor, now),
            )
            cursor.execute(
                """
                INSERT INTO ledger_entries(request_id, entry_type, buyer_id, seller_id, amount_minor, created_at)
                VALUES (?, 'buyer_credit', ?, NULL, ?, ?)
                """,
                (f"credit:{idempotency_key}", buyer_id, amount_minor, now),
            )
            return self._balance_from_cursor(cursor, buyer_id)

    def _get_buyer_balance_sync(self, buyer_id: str) -> BuyerBalance:
        with self._lock:
            self._ensure_open()
            return self._balance_from_cursor(self._connection, buyer_id)

    def _reserve_sync(
        self,
        request_id: str,
        buyer_id: str,
        seller_id: str,
        amount_minor: int,
        require_new: bool,
    ) -> LedgerRequest:
        now = self._clock()
        with self._transaction() as cursor:
            existing = cursor.execute(
                "SELECT * FROM ledger_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                if require_new:
                    raise IdempotencyConflict("request id has already been reserved")
                reservation_entry = cursor.execute(
                    """
                    SELECT seller_id FROM ledger_entries
                    WHERE request_id = ? AND entry_type = 'reservation'
                    """,
                    (request_id,),
                ).fetchone()
                reserved_seller_id = (
                    existing["seller_id"]
                    if reservation_entry is None
                    else reservation_entry["seller_id"]
                )
                if (
                    existing["buyer_id"] != buyer_id
                    or reserved_seller_id != seller_id
                    or existing["reserved_minor"] != amount_minor
                ):
                    raise IdempotencyConflict(
                        "request id was reused with a different reservation payload"
                    )
                return self._request_from_row(existing)

            account = cursor.execute(
                "SELECT available_minor FROM buyer_accounts WHERE buyer_id = ?",
                (buyer_id,),
            ).fetchone()
            available = 0 if account is None else int(account["available_minor"])
            if available < amount_minor:
                raise InsufficientFunds(
                    f"buyer has {available} minor units available; {amount_minor} required"
                )

            cursor.execute(
                """
                UPDATE buyer_accounts
                SET available_minor = available_minor - ?,
                    reserved_minor = reserved_minor + ?,
                    updated_at = ?
                WHERE buyer_id = ?
                """,
                (amount_minor, amount_minor, now, buyer_id),
            )
            cursor.execute(
                """
                INSERT INTO ledger_requests(
                    request_id, buyer_id, seller_id, reserved_minor, status,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)
                """,
                (
                    request_id,
                    buyer_id,
                    seller_id,
                    amount_minor,
                    now,
                    now,
                    now + self._reservation_lease_seconds,
                ),
            )
            cursor.execute(
                """
                INSERT INTO ledger_entries(request_id, entry_type, buyer_id, seller_id, amount_minor, created_at)
                VALUES (?, 'reservation', ?, ?, ?, ?)
                """,
                (request_id, buyer_id, seller_id, amount_minor, now),
            )
            row = cursor.execute(
                "SELECT * FROM ledger_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return self._request_from_row(row)

    def _settle_sync(
        self,
        request_id: str,
        actual_amount_minor: int | None,
        seller_amount_minor: int | None,
        seller_id: str | None,
    ) -> LedgerRequest:
        now = self._clock()
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM ledger_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise ReservationNotFound(request_id)

            if row["status"] == RequestStatus.SETTLED.value:
                expected_actual = row["charged_minor"]
                expected_seller = row["seller_amount_minor"]
                if (
                    actual_amount_minor is not None
                    and actual_amount_minor != expected_actual
                ) or (
                    seller_amount_minor is not None
                    and seller_amount_minor != expected_seller
                ) or (
                    seller_id is not None and seller_id != row["seller_id"]
                ):
                    raise IdempotencyConflict(
                        "settlement retry used amounts different from the original settlement"
                    )
                return self._request_from_row(row)
            if row["status"] == RequestStatus.REFUNDED.value:
                raise InvalidLedgerTransition("a refunded request cannot be settled")

            reserved = int(row["reserved_minor"])
            actual = reserved if actual_amount_minor is None else actual_amount_minor
            seller = actual if seller_amount_minor is None else seller_amount_minor
            _require_minor(actual, "actual_amount_minor")
            _require_minor(seller, "seller_amount_minor")
            if actual > reserved:
                raise ValueError("actual_amount_minor cannot exceed the reservation")
            if seller > actual:
                raise ValueError("seller_amount_minor cannot exceed the buyer charge")
            release = reserved - actual
            platform_fee = actual - seller
            settled_seller_id = row["seller_id"] if seller_id is None else seller_id

            cursor.execute(
                """
                UPDATE buyer_accounts
                SET available_minor = available_minor + ?,
                    reserved_minor = reserved_minor - ?,
                    updated_at = ?
                WHERE buyer_id = ?
                """,
                (release, reserved, now, row["buyer_id"]),
            )
            cursor.execute(
                """
                UPDATE ledger_requests
                SET seller_id = ?, charged_minor = ?, seller_amount_minor = ?, platform_fee_minor = ?,
                    status = 'settled', updated_at = ?, settled_at = ?, expires_at = NULL
                WHERE request_id = ?
                """,
                (
                    settled_seller_id,
                    actual,
                    seller,
                    platform_fee,
                    now,
                    now,
                    request_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO seller_earnings(
                    request_id, seller_id, amount_minor, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (request_id, settled_seller_id, seller, now, now),
            )
            cursor.execute(
                """
                INSERT INTO ledger_entries(request_id, entry_type, buyer_id, seller_id, amount_minor, created_at)
                VALUES (?, 'settlement', ?, ?, ?, ?)
                """,
                (request_id, row["buyer_id"], settled_seller_id, actual, now),
            )
            if release:
                cursor.execute(
                    """
                    INSERT INTO ledger_entries(request_id, entry_type, buyer_id, seller_id, amount_minor, created_at)
                    VALUES (?, 'reservation_release', ?, ?, ?, ?)
                    """,
                    (request_id, row["buyer_id"], settled_seller_id, release, now),
                )
            updated = cursor.execute(
                "SELECT * FROM ledger_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return self._request_from_row(updated)

    def _refund_sync(self, request_id: str, reason_code: str) -> LedgerRequest:
        now = self._clock()
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM ledger_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise ReservationNotFound(request_id)
            if row["status"] == RequestStatus.REFUNDED.value:
                return self._request_from_row(row)
            if row["status"] == RequestStatus.SETTLED.value:
                raise InvalidLedgerTransition(
                    "a settled request cannot be refunded; dispute its seller earning instead"
                )

            reserved = int(row["reserved_minor"])
            cursor.execute(
                """
                UPDATE buyer_accounts
                SET available_minor = available_minor + ?,
                    reserved_minor = reserved_minor - ?,
                    updated_at = ?
                WHERE buyer_id = ?
                """,
                (reserved, reserved, now, row["buyer_id"]),
            )
            cursor.execute(
                """
                UPDATE ledger_requests
                SET status = 'refunded', failure_reason_code = ?, updated_at = ?,
                    refunded_at = ?, expires_at = NULL
                WHERE request_id = ?
                """,
                (reason_code, now, now, request_id),
            )
            cursor.execute(
                """
                INSERT INTO ledger_entries(request_id, entry_type, buyer_id, seller_id, amount_minor, created_at)
                VALUES (?, 'refund', ?, ?, ?, ?)
                """,
                (request_id, row["buyer_id"], row["seller_id"], reserved, now),
            )
            updated = cursor.execute(
                "SELECT * FROM ledger_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return self._request_from_row(updated)

    def _renew_reservation_sync(self, request_id: str) -> LedgerRequest:
        now = self._clock()
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM ledger_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise ReservationNotFound(request_id)
            if row["status"] != RequestStatus.RESERVED.value:
                raise InvalidLedgerTransition("only a reserved request can renew its lease")
            cursor.execute(
                """
                UPDATE ledger_requests
                SET updated_at = ?, expires_at = ?
                WHERE request_id = ? AND status = 'reserved'
                """,
                (now, now + self._reservation_lease_seconds, request_id),
            )
            updated = cursor.execute(
                "SELECT * FROM ledger_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return self._request_from_row(updated)

    def _recover_reserved_sync(self, reason_code: str) -> int:
        now = self._clock()
        with self._transaction() as cursor:
            rows = cursor.execute(
                """
                SELECT * FROM ledger_requests
                WHERE status = 'reserved' AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                reserved = int(row["reserved_minor"])
                cursor.execute(
                    """
                    UPDATE buyer_accounts
                    SET available_minor = available_minor + ?,
                        reserved_minor = reserved_minor - ?, updated_at = ?
                    WHERE buyer_id = ?
                    """,
                    (reserved, reserved, now, row["buyer_id"]),
                )
                cursor.execute(
                    """
                    UPDATE ledger_requests
                    SET status = 'refunded', failure_reason_code = ?,
                        updated_at = ?, refunded_at = ?, expires_at = NULL
                    WHERE request_id = ?
                    """,
                    (reason_code, now, now, row["request_id"]),
                )
                cursor.execute(
                    """
                    INSERT INTO ledger_entries(
                        request_id, entry_type, buyer_id, seller_id,
                        amount_minor, created_at
                    ) VALUES (?, 'refund', ?, ?, ?, ?)
                    """,
                    (
                        row["request_id"],
                        row["buyer_id"],
                        row["seller_id"],
                        reserved,
                        now,
                    ),
                )
            return len(rows)

    def _get_request_sync(self, request_id: str) -> LedgerRequest:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM ledger_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise ReservationNotFound(request_id)
            return self._request_from_row(row)

    def _get_earning_sync(self, request_id: str) -> SellerEarning:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM seller_earnings WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise EarningNotFound(request_id)
            return self._earning_from_row(row)

    def _get_seller_balances_sync(self, seller_id: str) -> SellerBalances:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT status, COALESCE(SUM(amount_minor), 0) AS amount
                FROM seller_earnings
                WHERE seller_id = ?
                GROUP BY status
                """,
                (seller_id,),
            ).fetchall()
            totals = {row["status"]: int(row["amount"]) for row in rows}
            return SellerBalances(
                seller_id=seller_id,
                pending_minor=totals.get(EarningStatus.PENDING.value, 0),
                cleared_minor=totals.get(EarningStatus.CLEARED.value, 0),
                disputed_minor=totals.get(EarningStatus.DISPUTED.value, 0),
            )

    def _clear_earning_sync(self, request_id: str) -> SellerEarning:
        now = self._clock()
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM seller_earnings WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise EarningNotFound(request_id)
            if row["status"] == EarningStatus.CLEARED.value:
                return self._earning_from_row(row)
            if row["status"] == EarningStatus.DISPUTED.value:
                raise InvalidLedgerTransition("a disputed earning cannot be cleared")
            cursor.execute(
                """
                UPDATE seller_earnings
                SET status = 'cleared', updated_at = ?, cleared_at = ?
                WHERE request_id = ?
                """,
                (now, now, request_id),
            )
            updated = cursor.execute(
                "SELECT * FROM seller_earnings WHERE request_id = ?", (request_id,)
            ).fetchone()
            return self._earning_from_row(updated)

    def _clear_pending_earnings_sync(
        self, seller_id: str | None, before: float
    ) -> int:
        now = self._clock()
        with self._transaction() as cursor:
            if seller_id is None:
                result = cursor.execute(
                    """
                    UPDATE seller_earnings
                    SET status = 'cleared', updated_at = ?, cleared_at = ?
                    WHERE status = 'pending' AND created_at <= ?
                    """,
                    (now, now, before),
                )
            else:
                result = cursor.execute(
                    """
                    UPDATE seller_earnings
                    SET status = 'cleared', updated_at = ?, cleared_at = ?
                    WHERE status = 'pending' AND seller_id = ? AND created_at <= ?
                    """,
                    (now, now, seller_id, before),
                )
            return result.rowcount

    def _dispute_earning_sync(
        self, request_id: str, reason_code: str
    ) -> SellerEarning:
        now = self._clock()
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM seller_earnings WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise EarningNotFound(request_id)
            if row["status"] == EarningStatus.DISPUTED.value:
                if row["dispute_reason_code"] != reason_code:
                    raise IdempotencyConflict(
                        "dispute retry used a reason different from the original dispute"
                    )
                return self._earning_from_row(row)
            cursor.execute(
                """
                UPDATE seller_earnings
                SET status = 'disputed', dispute_reason_code = ?, updated_at = ?, disputed_at = ?
                WHERE request_id = ?
                """,
                (reason_code, now, now, request_id),
            )
            updated = cursor.execute(
                "SELECT * FROM seller_earnings WHERE request_id = ?", (request_id,)
            ).fetchone()
            return self._earning_from_row(updated)

    def _list_entries_sync(
        self, request_id: str | None, limit: int
    ) -> list[LedgerEntry]:
        with self._lock:
            self._ensure_open()
            if request_id is None:
                rows = self._connection.execute(
                    "SELECT * FROM ledger_entries ORDER BY entry_id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM ledger_entries
                    WHERE request_id = ? ORDER BY entry_id ASC LIMIT ?
                    """,
                    (request_id, limit),
                ).fetchall()
            return [
                LedgerEntry(
                    entry_id=int(row["entry_id"]),
                    request_id=row["request_id"],
                    entry_type=row["entry_type"],
                    buyer_id=row["buyer_id"],
                    seller_id=row["seller_id"],
                    amount_minor=int(row["amount_minor"]),
                    created_at=float(row["created_at"]),
                )
                for row in rows
            ]

    def _balance_from_cursor(
        self, cursor: sqlite3.Connection | sqlite3.Cursor, buyer_id: str
    ) -> BuyerBalance:
        row = cursor.execute(
            "SELECT available_minor, reserved_minor FROM buyer_accounts WHERE buyer_id = ?",
            (buyer_id,),
        ).fetchone()
        if row is None:
            return BuyerBalance(buyer_id, 0, 0)
        return BuyerBalance(
            buyer_id=buyer_id,
            available_minor=int(row["available_minor"]),
            reserved_minor=int(row["reserved_minor"]),
        )

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> LedgerRequest:
        return LedgerRequest(
            request_id=row["request_id"],
            buyer_id=row["buyer_id"],
            seller_id=row["seller_id"],
            reserved_minor=int(row["reserved_minor"]),
            charged_minor=int(row["charged_minor"]),
            seller_amount_minor=int(row["seller_amount_minor"]),
            platform_fee_minor=int(row["platform_fee_minor"]),
            status=RequestStatus(row["status"]),
            failure_reason_code=row["failure_reason_code"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            expires_at=(
                None if row["expires_at"] is None else float(row["expires_at"])
            ),
        )

    @staticmethod
    def _earning_from_row(row: sqlite3.Row) -> SellerEarning:
        return SellerEarning(
            request_id=row["request_id"],
            seller_id=row["seller_id"],
            amount_minor=int(row["amount_minor"]),
            status=EarningStatus(row["status"]),
            dispute_reason_code=row["dispute_reason_code"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def _transaction(self):
        return _LedgerTransaction(self)

    def _ensure_open(self) -> None:
        if self._closed:
            raise LedgerError("ledger is closed")

    def _close_sync(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True


class _LedgerTransaction:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._cursor: sqlite3.Cursor | None = None

    def __enter__(self) -> sqlite3.Cursor:
        self._ledger._lock.acquire()
        try:
            self._ledger._ensure_open()
            self._ledger._connection.execute("BEGIN IMMEDIATE")
            self._cursor = self._ledger._connection.cursor()
            return self._cursor
        except BaseException:
            self._ledger._lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is None:
                self._ledger._connection.commit()
            else:
                self._ledger._connection.rollback()
        finally:
            if self._cursor is not None:
                self._cursor.close()
            self._ledger._lock.release()
        return False


# Explicit alias for callers that prefer the implementation-specific name.
SQLiteLedger = Ledger


__all__: Iterable[str] = (
    "BuyerBalance",
    "EarningNotFound",
    "EarningStatus",
    "IdempotencyConflict",
    "InsufficientFunds",
    "InvalidLedgerTransition",
    "Ledger",
    "LedgerEntry",
    "LedgerError",
    "LedgerRequest",
    "RequestStatus",
    "ReservationNotFound",
    "SQLiteLedger",
    "SellerBalances",
    "SellerEarning",
)
