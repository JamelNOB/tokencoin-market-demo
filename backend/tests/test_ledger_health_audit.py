from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.app.audit import (
    AuditPolicy,
    AuditService,
    ProbeKind,
    ProbeObservation,
    QualityBand,
)
from backend.app.health import HealthMonitor, HealthPolicy, HealthState
from backend.app.ledger import (
    EarningStatus,
    IdempotencyConflict,
    InsufficientFunds,
    InvalidLedgerTransition,
    Ledger,
    RequestStatus,
)


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.db_path = Path(self.tempdir.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.db_path, clock=self.clock)
        await self.ledger.initialize()

    async def asyncTearDown(self) -> None:
        await self.ledger.close()
        self.tempdir.cleanup()

    async def test_reserve_and_settle_are_idempotent(self) -> None:
        credited = await self.ledger.credit_buyer("buyer-1", 1_000, "deposit-1")
        self.assertEqual(credited.available_minor, 1_000)

        repeated_credit = await self.ledger.credit_buyer(
            "buyer-1", 1_000, "deposit-1"
        )
        self.assertEqual(repeated_credit.available_minor, 1_000)
        with self.assertRaises(IdempotencyConflict):
            await self.ledger.credit_buyer("buyer-1", 2_000, "deposit-1")

        first = await self.ledger.reserve("req-1", "buyer-1", "seller-1", 600)
        repeated = await self.ledger.reserve(
            "req-1", "buyer-1", "seller-1", 600
        )
        self.assertEqual(first, repeated)
        self.assertEqual(first.status, RequestStatus.RESERVED)
        self.assertEqual(
            await self.ledger.get_buyer_balance("buyer-1"),
            credited.__class__("buyer-1", available_minor=400, reserved_minor=600),
        )

        with self.assertRaises(IdempotencyConflict):
            await self.ledger.reserve("req-1", "buyer-1", "seller-2", 600)

        settled = await self.ledger.settle(
            "req-1", actual_amount_minor=500, seller_amount_minor=450
        )
        repeated_settlement = await self.ledger.settle(
            "req-1", actual_amount_minor=500, seller_amount_minor=450
        )
        self.assertEqual(settled, repeated_settlement)
        self.assertEqual(settled.status, RequestStatus.SETTLED)
        self.assertEqual(settled.platform_fee_minor, 50)
        self.assertEqual(
            (await self.ledger.get_buyer_balance("buyer-1")).available_minor, 500
        )

        earning = await self.ledger.get_earning("req-1")
        self.assertEqual(earning.status, EarningStatus.PENDING)
        self.assertEqual(earning.amount_minor, 450)
        balances = await self.ledger.get_seller_balances("seller-1")
        self.assertEqual(balances.pending_minor, 450)

        entries = await self.ledger.list_entries("req-1")
        self.assertEqual(
            [entry.entry_type for entry in entries],
            ["reservation", "settlement", "reservation_release"],
        )
        with self.assertRaises(IdempotencyConflict):
            await self.ledger.settle(
                "req-1", actual_amount_minor=499, seller_amount_minor=450
            )
        with self.assertRaises(InvalidLedgerTransition):
            await self.ledger.refund("req-1")

    async def test_failed_request_refunds_once_and_income_can_be_held(self) -> None:
        await self.ledger.credit_buyer("buyer-1", 1_000, "deposit-1")
        await self.ledger.reserve("failed", "buyer-1", "seller-1", 300)
        refunded = await self.ledger.refund("failed", "timeout")
        repeated = await self.ledger.refund("failed", "timeout")
        self.assertEqual(refunded, repeated)
        self.assertEqual(refunded.status, RequestStatus.REFUNDED)
        balance = await self.ledger.get_buyer_balance("buyer-1")
        self.assertEqual((balance.available_minor, balance.reserved_minor), (1_000, 0))

        await self.ledger.reserve("paid", "buyer-1", "seller-1", 400)
        await self.ledger.settle(
            "paid", actual_amount_minor=400, seller_amount_minor=360
        )
        cleared = await self.ledger.clear_earning("paid")
        self.assertEqual(cleared.status, EarningStatus.CLEARED)
        self.assertEqual(await self.ledger.clear_earning("paid"), cleared)

        disputed = await self.ledger.dispute_earning("paid", "model_mismatch")
        self.assertEqual(disputed.status, EarningStatus.DISPUTED)
        totals = await self.ledger.get_seller_balances("seller-1")
        self.assertEqual(totals.disputed_minor, 360)
        self.assertEqual(totals.pending_minor, 0)
        with self.assertRaises(IdempotencyConflict):
            await self.ledger.dispute_earning("paid", "different_reason")

        with self.assertRaises(InsufficientFunds):
            await self.ledger.reserve("too-large", "buyer-1", "seller-1", 601)

    async def test_settlement_can_replace_routing_pool_with_actual_seller(self) -> None:
        await self.ledger.credit_buyer("buyer-1", 1_000, "deposit-1")
        await self.ledger.reserve("routed", "buyer-1", "routing-pool", 500)

        settled = await self.ledger.settle(
            "routed",
            actual_amount_minor=400,
            seller_amount_minor=360,
            seller_id="supply-deepseek-7",
        )
        self.assertEqual(settled.seller_id, "supply-deepseek-7")
        earning = await self.ledger.get_earning("routed")
        self.assertEqual(earning.seller_id, "supply-deepseek-7")
        self.assertEqual(
            (await self.ledger.get_seller_balances("supply-deepseek-7")).pending_minor,
            360,
        )
        self.assertEqual(
            (await self.ledger.get_seller_balances("routing-pool")).pending_minor,
            0,
        )

        repeated = await self.ledger.settle(
            "routed",
            actual_amount_minor=400,
            seller_amount_minor=360,
            seller_id="supply-deepseek-7",
        )
        self.assertEqual(repeated, settled)

        # A late retry of the original reservation remains idempotent even
        # though the request now records the actual selected seller.
        retried_reservation = await self.ledger.reserve(
            "routed", "buyer-1", "routing-pool", 500
        )
        self.assertEqual(retried_reservation, settled)

        with self.assertRaises(IdempotencyConflict):
            await self.ledger.settle(
                "routed",
                actual_amount_minor=400,
                seller_amount_minor=360,
                seller_id="different-supply",
            )

    async def test_database_atomically_rejects_duplicate_request_owner(self) -> None:
        await self.ledger.credit_buyer("buyer-1", 1_000, "deposit-1")
        other_process = Ledger(self.db_path, clock=self.clock)
        await other_process.initialize()
        try:
            results = await asyncio.gather(
                self.ledger.reserve(
                    "owned-once",
                    "buyer-1",
                    "routing-pool",
                    300,
                    require_new=True,
                ),
                other_process.reserve(
                    "owned-once",
                    "buyer-1",
                    "routing-pool",
                    300,
                    require_new=True,
                ),
                return_exceptions=True,
            )
        finally:
            await other_process.close()

        self.assertEqual(
            sum(isinstance(item, IdempotencyConflict) for item in results), 1
        )
        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)

        balance = await self.ledger.get_buyer_balance("buyer-1")
        self.assertEqual((balance.available_minor, balance.reserved_minor), (700, 300))

    async def test_startup_recovery_refunds_abandoned_reservations(self) -> None:
        await self.ledger.credit_buyer("buyer-1", 1_000, "deposit-recovery")
        await self.ledger.reserve("abandoned", "buyer-1", "routing-pool", 300)

        active_recovery = await self.ledger.recover_reserved()
        self.assertEqual(active_recovery, 0)
        self.clock.advance(301)
        recovered = await self.ledger.recover_reserved()

        self.assertEqual(recovered, 1)
        request = await self.ledger.get_request("abandoned")
        self.assertEqual(request.status, RequestStatus.REFUNDED)
        self.assertEqual(request.failure_reason_code, "startup_recovery")
        balance = await self.ledger.get_buyer_balance("buyer-1")
        self.assertEqual((balance.available_minor, balance.reserved_minor), (1_000, 0))

    async def test_renewed_reservation_is_not_recovered_while_active(self) -> None:
        await self.ledger.credit_buyer("buyer-1", 1_000, "deposit-renewal")
        reservation = await self.ledger.reserve(
            "long-running", "buyer-1", "routing-pool", 300
        )
        self.clock.advance(200)
        renewed = await self.ledger.renew_reservation("long-running")

        self.assertGreater(renewed.expires_at or 0, reservation.expires_at or 0)
        self.clock.advance(150)
        self.assertEqual(await self.ledger.recover_reserved(), 0)
        self.assertEqual(
            (await self.ledger.get_request("long-running")).status,
            RequestStatus.RESERVED,
        )


class HealthMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.monitor = HealthMonitor(
            Path(self.tempdir.name) / "health.sqlite3",
            HealthPolicy(failure_threshold=3, cooldown_seconds=10),
            clock=self.clock,
        )
        await self.monitor.initialize()

    async def asyncTearDown(self) -> None:
        await self.monitor.close()
        self.tempdir.cleanup()

    async def test_degraded_open_half_open_and_recovery(self) -> None:
        self.assertEqual((await self.monitor.get("source-1")).state, HealthState.HEALTHY)
        self.assertEqual(
            (await self.monitor.record_failure("source-1", reason_code="timeout")).state,
            HealthState.DEGRADED,
        )
        await self.monitor.record_failure("source-1", reason_code="timeout")
        opened = await self.monitor.record_failure("source-1", reason_code="timeout")
        self.assertEqual(opened.state, HealthState.OPEN)

        denied = await self.monitor.allow_request("source-1", now=self.clock() + 5)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.retry_after_seconds, 5)

        half_open = await self.monitor.get("source-1", now=self.clock() + 10)
        self.assertEqual(half_open.state, HealthState.HALF_OPEN)
        trial = await self.monitor.allow_request("source-1", now=self.clock() + 10)
        second_trial = await self.monitor.allow_request("source-1", now=self.clock() + 10)
        self.assertTrue(trial.allowed)
        self.assertFalse(second_trial.allowed)

        recovered = await self.monitor.record_success(
            "source-1", latency_ms=120, now=self.clock() + 10
        )
        self.assertEqual(recovered.state, HealthState.HEALTHY)
        self.assertEqual(recovered.consecutive_failures, 0)
        self.assertEqual(recovered.latency_ewma_ms, 120)

    async def test_failed_half_open_trial_reopens_circuit(self) -> None:
        for _ in range(3):
            await self.monitor.record_failure("source-2", reason_code="upstream_5xx")
        self.clock.advance(10)
        permission = await self.monitor.allow_request("source-2")
        self.assertTrue(permission.allowed)
        self.assertEqual(permission.state, HealthState.HALF_OPEN)
        reopened = await self.monitor.record_failure(
            "source-2", reason_code="probe_failed"
        )
        self.assertEqual(reopened.state, HealthState.OPEN)
        self.assertEqual(reopened.opened_at, self.clock())

    async def test_stale_generation_cannot_recover_open_circuit(self) -> None:
        permissions = [
            await self.monitor.allow_request("source-stale") for _ in range(3)
        ]
        self.assertEqual({item.generation for item in permissions}, {0})

        for permission in permissions:
            opened = await self.monitor.record_failure(
                "source-stale",
                reason_code="timeout",
                generation=permission.generation,
            )
        self.assertEqual(opened.state, HealthState.OPEN)
        self.assertEqual(opened.generation, 1)

        stale = await self.monitor.record_success(
            "source-stale",
            latency_ms=10,
            generation=permissions[0].generation,
        )
        self.assertEqual(stale.state, HealthState.OPEN)
        self.assertEqual(stale.generation, 1)
        self.assertEqual(stale.total_successes, 0)
        self.assertEqual(stale.total_failures, 3)

    async def test_half_open_lease_expires_and_can_be_released(self) -> None:
        permissions = [
            await self.monitor.allow_request("source-lease") for _ in range(3)
        ]
        for permission in permissions:
            await self.monitor.record_failure(
                "source-lease",
                reason_code="timeout",
                generation=permission.generation,
            )

        trial = await self.monitor.allow_request(
            "source-lease", now=self.clock() + 10
        )
        self.assertTrue(trial.allowed)
        self.assertEqual(trial.state, HealthState.HALF_OPEN)
        self.assertEqual(trial.generation, 1)
        self.assertEqual(trial.lease_expires_at, self.clock() + 40)

        blocked = await self.monitor.allow_request(
            "source-lease", now=self.clock() + 39
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.retry_after_seconds, 1)

        replacement = await self.monitor.allow_request(
            "source-lease", now=self.clock() + 40
        )
        self.assertTrue(replacement.allowed)
        self.assertEqual(replacement.generation, 2)

        still_reserved = await self.monitor.release_permission(
            "source-lease",
            generation=trial.generation,
            now=self.clock() + 40,
        )
        self.assertTrue(still_reserved.half_open_in_flight)

        released = await self.monitor.release_permission(
            "source-lease",
            generation=replacement.generation,
            now=self.clock() + 40,
        )
        self.assertFalse(released.half_open_in_flight)
        self.assertIsNone(released.half_open_lease_expires_at)
        self.assertTrue(
            (
                await self.monitor.allow_request(
                    "source-lease", now=self.clock() + 40
                )
            ).allowed
        )


class AuditServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        db_path = Path(self.tempdir.name) / "audit.sqlite3"
        self.health = HealthMonitor(
            db_path,
            HealthPolicy(failure_threshold=2, cooldown_seconds=60),
            clock=self.clock,
        )
        self.audit = AuditService(
            db_path,
            self.health,
            AuditPolicy(window_size=4, recency_decay=0.9),
            clock=self.clock,
        )
        await self.health.initialize()
        await self.audit.initialize()

    async def asyncTearDown(self) -> None:
        await self.audit.close()
        await self.health.close()
        self.tempdir.cleanup()

    async def test_manual_and_scheduled_probes_update_quality_and_health(self) -> None:
        calls = 0

        async def good_probe(source_id: str) -> ProbeObservation:
            nonlocal calls
            calls += 1
            self.assertEqual(source_id, "source-1")
            return ProbeObservation(
                success=True,
                latency_ms=100,
                model_match=True,
                capability_match=True,
                billing_consistent=True,
            )

        first = await self.audit.run_manual_probe(
            "source-1", good_probe, probe_id="manual-1"
        )
        repeated = await self.audit.run_manual_probe(
            "source-1", good_probe, probe_id="manual-1"
        )
        self.assertEqual(first, repeated)
        self.assertEqual(calls, 1)
        self.assertTrue(first.passed)
        self.assertEqual(first.score, 100)
        quality = await self.audit.quality("source-1")
        self.assertEqual(quality.band, QualityBand.HIGH)
        self.assertEqual(quality.sample_size, 1)

        async def diluted_probe(_: str) -> ProbeObservation:
            return ProbeObservation(
                success=True,
                latency_ms=100,
                model_match=False,
                capability_match=True,
                billing_consistent=True,
            )

        first_failure = await self.audit.run_scheduled_probe(
            "source-1", diluted_probe, probe_id="scheduled-1"
        )
        second_failure = await self.audit.run_scheduled_probe(
            "source-1", diluted_probe, probe_id="scheduled-2"
        )
        self.assertIsNotNone(first_failure)
        self.assertFalse(first_failure.passed)
        self.assertEqual(first_failure.kind, ProbeKind.SCHEDULED)
        self.assertEqual(
            (await self.health.get("source-1")).state,
            HealthState.OPEN,
        )

        skipped = await self.audit.run_scheduled_probe(
            "source-1", diluted_probe, probe_id="scheduled-skipped"
        )
        self.assertIsNone(skipped)
        self.assertIsNone(await self.audit.get_result("scheduled-skipped"))

        manual_recovery = await self.audit.run_manual_probe(
            "source-1", good_probe, probe_id="manual-recovery"
        )
        self.assertTrue(manual_recovery.passed)
        self.assertEqual(
            (await self.health.get("source-1")).state,
            HealthState.HEALTHY,
        )

        quality = await self.audit.quality("source-1")
        self.assertEqual(quality.sample_size, 4)
        self.assertEqual(quality.total_probes, 4)
        self.assertGreater(quality.score, 0)
        self.assertLess(quality.score, 100)
        self.assertEqual(quality.model_match_rate, 0.5)

    async def test_probe_exception_persists_only_exception_type(self) -> None:
        async def exploding_probe(_: str) -> ProbeObservation:
            raise RuntimeError("provider rejected sk-fake-secret-value")

        result = await self.audit.run_manual_probe(
            "source-2", exploding_probe, probe_id="exception-1"
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.error_code, "RuntimeError")
        stored = await self.audit.get_result("exception-1")
        self.assertNotIn("sk-fake-secret-value", repr(stored))

    async def test_unverified_quality_and_batch_deduplication(self) -> None:
        quality = await self.audit.quality("never-seen")
        self.assertEqual(quality.band, QualityBand.UNVERIFIED)

        async def probe(_: str) -> ProbeObservation:
            return ProbeObservation(success=True, latency_ms=200)

        results = await self.audit.run_scheduled_batch(
            ["source-a", "source-a", "source-b"], probe, concurrency=2
        )
        self.assertEqual({item.source_id for item in results}, {"source-a", "source-b"})
        self.assertEqual(len(results), 2)
        self.assertEqual(len(await self.audit.list_results(limit=10)), 2)

    async def test_failed_probe_score_is_always_risky(self) -> None:
        async def wrong_model(_: str) -> ProbeObservation:
            return ProbeObservation(
                success=True,
                latency_ms=20,
                model_match=False,
                capability_match=True,
                billing_consistent=True,
            )

        result = await self.audit.run_manual_probe(
            "source-risky", wrong_model, probe_id="risky-1"
        )
        self.assertFalse(result.passed)
        self.assertLess(result.score, self.audit.policy.acceptable_score)
        self.assertEqual(
            (await self.audit.quality("source-risky")).band,
            QualityBand.RISKY,
        )

    async def test_concurrent_duplicate_probe_updates_health_once(self) -> None:
        arrived = 0
        both_arrived = asyncio.Event()

        async def synchronized_probe(_: str) -> ProbeObservation:
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await both_arrived.wait()
            return ProbeObservation(success=True, latency_ms=25)

        first, second = await asyncio.gather(
            self.audit.run_manual_probe(
                "source-duplicate", synchronized_probe, probe_id="duplicate-1"
            ),
            self.audit.run_manual_probe(
                "source-duplicate", synchronized_probe, probe_id="duplicate-1"
            ),
        )
        self.assertEqual(first, second)
        self.assertEqual(arrived, 2)
        self.assertEqual(
            (await self.health.get("source-duplicate")).total_successes,
            1,
        )
        self.assertEqual(
            (await self.audit.quality("source-duplicate")).total_probes,
            1,
        )

    async def test_cancelled_half_open_probe_releases_permission(self) -> None:
        permissions = [
            await self.health.allow_request("source-cancel") for _ in range(2)
        ]
        for permission in permissions:
            await self.health.record_failure(
                "source-cancel",
                reason_code="timeout",
                generation=permission.generation,
            )
        self.clock.advance(60)

        started = asyncio.Event()

        async def hanging_probe(_: str) -> ProbeObservation:
            started.set()
            await asyncio.Event().wait()
            return ProbeObservation(success=True)  # pragma: no cover

        task = asyncio.create_task(
            self.audit.run_scheduled_probe(
                "source-cancel", hanging_probe, probe_id="cancelled-probe"
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        during_trial = await self.health.get("source-cancel")
        self.assertTrue(during_trial.half_open_in_flight)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        after_cancel = await self.health.get("source-cancel")
        self.assertEqual(after_cancel.state, HealthState.HALF_OPEN)
        self.assertFalse(after_cancel.half_open_in_flight)
        next_trial = await self.health.allow_request("source-cancel")
        self.assertTrue(next_trial.allowed)
        await self.health.release_permission(
            "source-cancel", generation=next_trial.generation
        )


if __name__ == "__main__":
    unittest.main()
