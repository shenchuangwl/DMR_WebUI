"""AiohttpListClient：用假会话测（测试进程不能开套接字）；长连接复用、超时、不带参数、错误转成 ListFetchError。"""
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from dmr_kernel.listsync import AiohttpListClient, ListFetchError, ListSync
from listsync_world import URL


class FakeResponse:
    def __init__(self, status: int, raw: bytes):
        self.status, self._raw = status, raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self) -> bytes:
        return self._raw


class FakeSession:
    closed = False

    def __init__(self, behaviour):
        self.behaviour, self.requests, self.close_calls = behaviour, [], 0

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        result = self.behaviour()
        if isinstance(result, BaseException):
            raise result
        return result

    async def close(self):
        self.closed = True
        self.close_calls += 1


def client_with(behaviour):
    made: list[FakeSession] = []

    def factory():
        made.append(FakeSession(behaviour))
        return made[-1]

    return AiohttpListClient(session_factory=factory), made


async def test_get_returns_status_and_json_reusing_one_session_without_params():
    client, made = client_with(lambda: FakeResponse(200, json.dumps({"valid": True}).encode()))
    assert await client.get_json(URL, timeout_s=10) == (200, {"valid": True})
    assert await client.get_json(URL, timeout_s=10) == (200, {"valid": True})
    assert len(made) == 1 and [u for u, _ in made[0].requests] == [URL, URL]  # 同一个会话 = 长连接复用
    assert set(made[0].requests[0][1]) == {"timeout", "allow_redirects"} and made[0].requests[0][1]["timeout"].total == 10
    await client.close()
    assert made[0].close_calls == 1


async def test_a_non_200_status_and_a_non_json_body_are_returned_not_raised():
    client, _ = client_with(lambda: FakeResponse(503, b"oops"))
    assert await client.get_json(URL, timeout_s=5) == (503, None)
    client, _ = client_with(lambda: FakeResponse(200, b"<html>"))
    assert await client.get_json(URL, timeout_s=5) == (200, None)


async def test_timeout_raises_list_fetch_error():
    class Slow(FakeResponse):
        async def __aenter__(self):
            await asyncio.sleep(5)
            return self

    client, _ = client_with(lambda: Slow(200, b"{}"))
    with pytest.raises(ListFetchError, match="超过"):
        await client.get_json(URL, timeout_s=0.05)


async def test_connection_errors_raise_list_fetch_error():
    client, _ = client_with(lambda: aiohttp.ClientConnectionError("boom"))
    with pytest.raises(ListFetchError, match="连接失败"):
        await client.get_json(URL, timeout_s=2)
    client, _ = client_with(lambda: ConnectionRefusedError("refused"))
    with pytest.raises(ListFetchError):
        await client.get_json(URL, timeout_s=2)


def test_the_url_must_not_carry_a_query(ledger, bus, clock, config, rules, notifier):
    with pytest.raises(ValueError):
        ListSync(url="http://127.0.0.1:18080/api/v1/onlycoin/live?x=1", ledger=ledger, bus=bus, clock=clock,
                 config_get=config.__getitem__, rules=rules, notifier=notifier)
