from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.app.main import (
    PRICE_PLAN,
    _metering_guard,
    _normalize_output_limit,
    _provisional_usage,
    _ledger_recovery_loop,
    _reservation_heartbeat,
    _reserve_with_cancellation_cleanup,
    _settle_usage,
    _usage_within_authorization,
)
from backend.app.ledger import EarningStatus, Ledger, RequestStatus
from backend.app.metering import TokenUsage, calculate_charge
from backend.app.providers import Supply


class BillingGuardTests(unittest.TestCase):
    def test_provider_usage_must_fit_request_and_observed_response(self) -> None:
        guard = _metering_guard(
            {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 32,
            }
        )
        plausible = TokenUsage(
            input_tokens=20,
            output_tokens=5,
            cache_miss_tokens=20,
        )
        inflated = TokenUsage(
            input_tokens=20,
            output_tokens=1_000_000,
            cache_miss_tokens=20,
        )

        self.assertTrue(_usage_within_authorization(plausible, guard, 200))
        self.assertFalse(_usage_within_authorization(inflated, guard, 200))

    def test_output_limit_is_rejected_instead_of_silently_under_reserved(self) -> None:
        with self.assertRaises(ValueError):
            _metering_guard(
                {
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 1_000_000,
                }
            )

        for invalid in (True, "32", 3.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _metering_guard(
                    {
                        "model": "deepseek-v4-flash",
                        "messages": [{"role": "user", "content": "hello"}],
                        "max_tokens": invalid,
                    }
                )

    def test_output_limit_aliases_use_the_largest_authorization(self) -> None:
        payload = {
            "model": "deepseek-v4-flash",
            "input": "hello",
            "max_tokens": 1,
            "max_output_tokens": 64,
        }
        guard = _metering_guard(payload)
        normalized = _normalize_output_limit(payload, upstream_path="responses")

        self.assertEqual(guard.output_token_ceiling, 64)
        self.assertNotIn("max_tokens", normalized)
        self.assertEqual(normalized["max_output_tokens"], 64)

    def test_interrupted_stream_estimate_never_exceeds_authorization(self) -> None:
        guard = _metering_guard(
            {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            }
        )
        usage = _provisional_usage(guard, observed_output_bytes=512)
        charge = calculate_charge(usage, PRICE_PLAN)

        self.assertLessEqual(usage.output_tokens, guard.output_token_ceiling)
        self.assertLessEqual(charge.buyer_micro_cny, guard.reservation_micro_cny)

    def test_interrupted_stream_estimate_does_not_divide_observed_bytes(self) -> None:
        guard = _metering_guard(
            {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 32,
            }
        )

        usage = _provisional_usage(guard, observed_output_bytes=4)

        self.assertEqual(usage.output_tokens, 12)


class BackgroundLeaseResilienceTests(unittest.IsolatedAsyncioTestCase):
    class TwoPassStop:
        def __init__(self, passes: int) -> None:
            self.passes = passes
            self.checks = 0

        def is_set(self) -> bool:
            self.checks += 1
            return self.checks > self.passes

        async def wait(self) -> None:
            raise TimeoutError

    async def test_recovery_loop_retries_after_transient_database_error(self) -> None:
        class FlakyLedger:
            reservation_lease_seconds = 30

            def __init__(self) -> None:
                self.calls = 0

            async def recover_reserved(self) -> None:
                self.calls += 1
                raise OSError("database temporarily busy")

        ledger = FlakyLedger()
        app = SimpleNamespace(state=SimpleNamespace(ledger=ledger))

        with self.assertLogs("backend.app.main", level="ERROR"):
            await _ledger_recovery_loop(app, self.TwoPassStop(1))

        self.assertEqual(ledger.calls, 1)

    async def test_heartbeat_retries_after_transient_database_error(self) -> None:
        class FlakyLedger:
            reservation_lease_seconds = 30

            def __init__(self) -> None:
                self.calls = 0

            async def renew_reservation(self, _request_id: str) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise OSError("database temporarily busy")

        ledger = FlakyLedger()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ledger=ledger)))

        with self.assertLogs("backend.app.main", level="ERROR"):
            await _reservation_heartbeat(request, "request-1", self.TwoPassStop(2))

        self.assertEqual(ledger.calls, 2)


class FakeHealth:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    async def force_open(self, source_id: str, *, reason_code: str):
        self.opened.append((source_id, reason_code))


class BillingSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_untrusted_usage_charges_local_estimate_and_withholds_seller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "billing.sqlite3")
            await ledger.initialize()
            await ledger.credit_buyer("local-buyer", 1_000_000, "deposit")
            guard = _metering_guard(
                {
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 32,
                }
            )
            await ledger.reserve(
                "untrusted",
                "local-buyer",
                "routing-pool",
                guard.reservation_micro_cny,
                require_new=True,
            )
            health = FakeHealth()
            request = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(ledger=ledger, health=health)
                )
            )
            supply = Supply(
                id="seller-1",
                name="Seller",
                provider="test",
                base_url="https://provider.invalid",
                api_key="test-only-secret",
                models=frozenset({"deepseek-v4-flash"}),
            )

            metering, charged = await _settle_usage(
                request,
                request_id="untrusted",
                supply=supply,
                model="deepseek-v4-flash",
                usage=TokenUsage(
                    input_tokens=10,
                    output_tokens=32,
                    cache_miss_tokens=10,
                ),
                guard=guard,
                observed_output_bytes=2,
            )

            settled = await ledger.get_request("untrusted")
            earning = await ledger.get_earning("untrusted")
            self.assertEqual(metering, "provisional_untrusted")
            self.assertGreater(charged, 0)
            self.assertEqual(settled.status, RequestStatus.SETTLED)
            self.assertEqual(earning.amount_minor, 0)
            self.assertEqual(earning.status, EarningStatus.DISPUTED)
            self.assertEqual(health.opened, [("seller-1", "usage_untrusted")])
            await ledger.close()

    async def test_cancellation_waits_for_reservation_then_refunds(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class DelayedLedger:
            def __init__(self) -> None:
                self.refunded = False

            async def reserve(self, *_args, **_kwargs):
                started.set()
                await release.wait()

            async def refund(self, *_args, **_kwargs):
                self.refunded = True

        ledger = DelayedLedger()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    ledger=ledger,
                    idempotency_lock=asyncio.Lock(),
                )
            )
        )
        task = asyncio.create_task(
            _reserve_with_cancellation_cleanup(request, "cancelled", 100)
        )
        await started.wait()
        task.cancel()
        release.set()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(ledger.refunded)


if __name__ == "__main__":
    unittest.main()
