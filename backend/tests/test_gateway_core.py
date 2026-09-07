from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

import httpx

from backend.app.providers import (
    ProviderRegistry,
    ProviderTransport,
    Supply,
    UpstreamRejected,
)
from backend.app.router import GatewayRouter, NoSupplyAvailable


MODEL = "deepseek-v4-flash"


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class FailBeforeFirstByte(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise httpx.ReadError("connection closed before the first byte")
        yield b""  # pragma: no cover - makes this an async generator


class DenyAllHealth:
    async def allow_request(self, _source_id: str):
        return SimpleNamespace(allowed=False, generation=7)


def supplies() -> tuple[list[Supply], str, str]:
    primary_key = "test-primary-private-value"
    backup_key = "test-backup-private-value"
    return (
        [
            Supply(
                id="primary",
                name="Primary",
                provider="deepseek",
                base_url="https://primary.test",
                api_key=primary_key,
                models=frozenset({MODEL}),
                priority=1,
            ),
            Supply(
                id="backup",
                name="Backup",
                provider="deepseek",
                base_url="https://backup.test",
                api_key=backup_key,
                models=frozenset({MODEL}),
                priority=2,
            ),
        ],
        primary_key,
        backup_key,
    )


class GatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unstarted_stream_can_be_aborted_without_leaking_attempt(self) -> None:
        configured, _primary_key, _backup_key = supplies()

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream(
                    [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n']
                ),
            )

        registry = ProviderRegistry(
            configured,
            failure_threshold=2,
            cooldown_seconds=30,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = GatewayRouter(
                registry,
                ProviderTransport(
                    client,
                    registry,
                    first_byte_timeout_seconds=1,
                ),
            )
            stream = await gateway.open_stream(
                model=MODEL,
                path="chat/completions",
                payload={"model": MODEL, "stream": True},
            )
            await gateway.abort_unstarted_stream(stream, model=MODEL)

        status, summary = await registry.snapshot()
        primary = next(item for item in status if item["id"] == "primary")
        self.assertEqual(primary["inFlight"], 0)
        self.assertEqual(summary, {"totalRequests": 1, "successRate": 0.0})

    async def test_non_stream_retries_retryable_failure_on_backup(self) -> None:
        configured, primary_key, backup_key = supplies()
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.host)
            expected_key = primary_key if request.url.host == "primary.test" else backup_key
            self.assertEqual(request.headers["authorization"], f"Bearer {expected_key}")
            self.assertNotIn(expected_key, request.content.decode("utf-8"))
            if request.url.host == "primary.test":
                return httpx.Response(
                    503,
                    json={"error": {"message": "primary unavailable"}},
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 2,
                        "total_tokens": 13,
                    },
                },
            )

        registry = ProviderRegistry(
            configured,
            failure_threshold=2,
            cooldown_seconds=30,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            gateway = GatewayRouter(
                registry,
                ProviderTransport(
                    client,
                    registry,
                    first_byte_timeout_seconds=1,
                ),
            )
            response = await gateway.request(
                model=MODEL,
                path="chat/completions",
                payload={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": False,
                },
            )

        self.assertEqual(calls, ["primary.test", "backup.test"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["id"], "chatcmpl-test")
        status, summary = await registry.snapshot()
        primary = next(item for item in status if item["id"] == "primary")
        backup = next(item for item in status if item["id"] == "backup")
        self.assertEqual(primary["consecutiveFailures"], 1)
        self.assertEqual(backup["successRate"], 100.0)
        self.assertEqual(summary, {"totalRequests": 1, "successRate": 100.0})

    async def test_stream_fails_over_before_first_byte_and_redacts_split_key(
        self,
    ) -> None:
        configured, _primary_key, backup_key = supplies()
        calls: list[str] = []
        encoded_key = backup_key.encode("utf-8")
        split_at = 9

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.host)
            if request.url.host == "primary.test":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=FailBeforeFirstByte(),
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream(
                    [
                        b'data: {"choices":[{"delta":{"content":"before ',
                        encoded_key[:split_at],
                        encoded_key[split_at:] + b' after"}}]}\n\n',
                        b'data: {"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                ),
            )

        registry = ProviderRegistry(
            configured,
            failure_threshold=2,
            cooldown_seconds=30,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            gateway = GatewayRouter(
                registry,
                ProviderTransport(
                    client,
                    registry,
                    first_byte_timeout_seconds=1,
                ),
            )
            stream = await gateway.open_stream(
                model=MODEL,
                path="chat/completions",
                payload={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )
            body = b"".join([chunk async for chunk in stream.body])

        self.assertEqual(calls, ["primary.test", "backup.test"])
        self.assertNotIn(encoded_key, body)
        self.assertIn(b"*" * len(encoded_key), body)
        self.assertIn(b"data: [DONE]\n\n", body)
        status, summary = await registry.snapshot()
        primary = next(item for item in status if item["id"] == "primary")
        backup = next(item for item in status if item["id"] == "backup")
        self.assertEqual(primary["consecutiveFailures"], 1)
        self.assertEqual(backup["inFlight"], 0)
        self.assertEqual(summary, {"totalRequests": 1, "successRate": 100.0})

    async def test_sse_keepalive_without_data_does_not_commit_the_source(self) -> None:
        configured, _primary_key, _backup_key = supplies()
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.host)
            if request.url.host == "primary.test":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=ChunkStream([b": keep-alive\n\n"]),
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream(
                    [
                        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                ),
            )

        registry = ProviderRegistry(
            configured,
            failure_threshold=2,
            cooldown_seconds=30,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = GatewayRouter(
                registry,
                ProviderTransport(
                    client,
                    registry,
                    first_byte_timeout_seconds=1,
                ),
            )
            stream = await gateway.open_stream(
                model=MODEL,
                path="chat/completions",
                payload={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
            body = b"".join([chunk async for chunk in stream.body])

        self.assertEqual(calls, ["primary.test", "backup.test"])
        self.assertIn(b'"content":"ok"', body)

    async def test_non_stream_protocol_violations_fail_over(self) -> None:
        cases = {
            "redirect": lambda: httpx.Response(
                302,
                headers={"location": "https://private-upstream.test"},
            ),
            "no_content": lambda: httpx.Response(204),
            "wrong_content_type": lambda: httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b'{"choices":[]}',
            ),
            "top_level_error": lambda: httpx.Response(
                200,
                json={"error": {"message": "not a completion"}},
            ),
            "non_object_json": lambda: httpx.Response(200, json=[]),
        }

        for name, bad_response in cases.items():
            with self.subTest(name=name):
                configured, _primary_key, _backup_key = supplies()
                calls: list[str] = []

                async def handler(request: httpx.Request) -> httpx.Response:
                    calls.append(request.url.host)
                    if request.url.host == "primary.test":
                        return bad_response()
                    return httpx.Response(
                        200,
                        json={"id": "response-backup", "choices": [], "error": None},
                    )

                registry = ProviderRegistry(
                    configured,
                    failure_threshold=2,
                    cooldown_seconds=30,
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as client:
                    gateway = GatewayRouter(
                        registry,
                        ProviderTransport(
                            client,
                            registry,
                            first_byte_timeout_seconds=1,
                        ),
                    )
                    response = await gateway.request(
                        model=MODEL,
                        path="chat/completions",
                        payload={"model": MODEL, "stream": False},
                    )

                self.assertEqual(calls, ["primary.test", "backup.test"])
                self.assertEqual(response.supply.id, "backup")

    async def test_stream_protocol_violations_fail_over_before_commit(self) -> None:
        cases = {
            "wrong_content_type": lambda: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"choices":[]}',
            ),
            "done_is_first_event": lambda: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream([b"data: [DONE]\n\n"]),
            ),
            "malformed_json": lambda: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream([b'data: {"choices":\n\n']),
            ),
            "top_level_error": lambda: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream(
                    [b'data: {"error":{"message":"bad source"}}\n\n']
                ),
            ),
            "typed_error_event": lambda: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream([b'data: {"type":"error","error":null}\n\n']),
            ),
            "incomplete_event_at_eof": lambda: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream([b'data: {"choices":[]']),
            ),
        }

        for name, bad_response in cases.items():
            with self.subTest(name=name):
                configured, _primary_key, _backup_key = supplies()
                calls: list[str] = []

                async def handler(request: httpx.Request) -> httpx.Response:
                    calls.append(request.url.host)
                    if request.url.host == "primary.test":
                        return bad_response()
                    return httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        stream=ChunkStream(
                            [
                                b'data: {"choices":[{"delta":{"content":"ok"}}],"error":null}\n\n',
                                b"data: [DONE]\n\n",
                            ]
                        ),
                    )

                registry = ProviderRegistry(
                    configured,
                    failure_threshold=2,
                    cooldown_seconds=30,
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as client:
                    gateway = GatewayRouter(
                        registry,
                        ProviderTransport(
                            client,
                            registry,
                            first_byte_timeout_seconds=1,
                        ),
                    )
                    stream = await gateway.open_stream(
                        model=MODEL,
                        path="chat/completions",
                        payload={"model": MODEL, "stream": True},
                    )
                    body = b"".join([chunk async for chunk in stream.body])

                self.assertEqual(calls, ["primary.test", "backup.test"])
                self.assertEqual(stream.upstream.supply.id, "backup")
                self.assertIn(b'"content":"ok"', body)

    async def test_protocol_error_is_sanitized_and_retryable(self) -> None:
        configured, primary_key, _backup_key = supplies()
        primary = configured[0]

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"error": {"message": f"credential={primary_key}"}},
            )

        registry = ProviderRegistry(
            [primary],
            failure_threshold=2,
            cooldown_seconds=30,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = ProviderTransport(
                client,
                registry,
                first_byte_timeout_seconds=1,
            )
            with self.assertRaises(UpstreamRejected) as raised:
                await transport.request(
                    primary,
                    path="chat/completions",
                    payload={"model": MODEL},
                )

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn(primary_key.encode("utf-8"), raised.exception.content)
        self.assertEqual(
            json.loads(raised.exception.content)["error"]["code"],
            "invalid_upstream_response",
        )

    async def test_persistent_health_denial_returns_configured_unavailable(self) -> None:
        configured, _primary_key, _backup_key = supplies()

        async def handler(_request: httpx.Request) -> httpx.Response:
            self.fail("transport must not run while persistent circuits deny all sources")

        registry = ProviderRegistry(
            configured,
            failure_threshold=2,
            cooldown_seconds=30,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = GatewayRouter(
                registry,
                ProviderTransport(
                    client,
                    registry,
                    first_byte_timeout_seconds=1,
                ),
                health=DenyAllHealth(),  # type: ignore[arg-type]
            )
            for stream in (False, True):
                with self.subTest(stream=stream):
                    with self.assertRaises(NoSupplyAvailable) as raised:
                        if stream:
                            await gateway.open_stream(
                                model=MODEL,
                                path="chat/completions",
                                payload={"model": MODEL, "stream": True},
                            )
                        else:
                            await gateway.request(
                                model=MODEL,
                                path="chat/completions",
                                payload={"model": MODEL, "stream": False},
                            )
                    self.assertTrue(raised.exception.configured)


if __name__ == "__main__":
    unittest.main()
