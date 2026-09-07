from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.app import config as config_module


TEST_BUYER_KEY = "tc_test_buyer_key_only"
TEST_ADMIN_KEY = "tca_test_operator_key_only"

# main.py builds its immutable settings object at import time. Supply every
# credential-related setting explicitly so this test never reads or calls the
# developer's real provider configuration.
with (
    patch.dict(
        os.environ,
        {
            "TOKENCOIN_BUYER_API_KEY": TEST_BUYER_KEY,
            "TOKENCOIN_ADMIN_API_KEY": TEST_ADMIN_KEY,
            "DEEPSEEK_API_KEY": "",
            "DEEPSEEK_BASE_URL": "https://provider.invalid",
            "TOKENCOIN_SUPPLIES_JSON": "",
            "TOKENCOIN_SCHEDULED_PROBES_ENABLED": "false",
            "TOKENCOIN_PROBE_ON_STARTUP": "false",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "",
            "TOKENCOIN_OTEL_CONSOLE": "false",
        },
        clear=False,
    ),
    patch.object(
        config_module,
        "DOTENV_PATH",
        Path(__file__).with_name("_intentionally_missing.env"),
    ),
):
    config_module.get_settings.cache_clear()
    from backend.app import main as main_module


class FakeRegistry:
    async def supported_models(self) -> list[str]:
        return ["deepseek-v4-flash"]

    async def source_ids(self) -> list[str]:
        return []


class BuyerAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_models_rejects_missing_and_wrong_key_then_accepts_bearer_key(
        self,
    ) -> None:
        main_module.app.state.registry = FakeRegistry()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app),
            base_url="http://test",
        ) as client:
            missing = await client.get("/v1/models")
            wrong = await client.get(
                "/v1/models",
                headers={"authorization": "Bearer tc_wrong_key"},
            )
            correct = await client.get(
                "/v1/models",
                headers={"authorization": f"Bearer {TEST_BUYER_KEY}"},
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(missing.json()["error"]["code"], "invalid_api_key")
        self.assertEqual(wrong.json()["error"]["type"], "authentication_error")
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(correct.json()["data"][0]["id"], "deepseek-v4-flash")
        combined = missing.text + wrong.text + correct.text
        self.assertNotIn(TEST_BUYER_KEY, combined)
        self.assertNotIn("tc_wrong_key", combined)

    async def test_manual_probe_requires_operator_key_and_is_rate_limited(
        self,
    ) -> None:
        main_module.app.state.registry = FakeRegistry()
        main_module.app.state.manual_probe_lock = main_module.asyncio.Lock()
        main_module.app.state.last_manual_probe_at = 0.0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_module.app),
            base_url="http://test",
        ) as client:
            buyer_only = await client.post(
                "/api/probes/run",
                headers={"authorization": f"Bearer {TEST_BUYER_KEY}"},
            )
            operator = await client.post(
                "/api/probes/run",
                headers={"authorization": f"Bearer {TEST_ADMIN_KEY}"},
            )
            limited = await client.post(
                "/api/probes/run",
                headers={"authorization": f"Bearer {TEST_ADMIN_KEY}"},
            )

        self.assertEqual(buyer_only.status_code, 401)
        self.assertEqual(
            buyer_only.json()["error"]["code"], "invalid_admin_api_key"
        )
        self.assertEqual(operator.status_code, 200)
        self.assertEqual(operator.json(), {"data": []})
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry-after", limited.headers)


if __name__ == "__main__":
    unittest.main()
