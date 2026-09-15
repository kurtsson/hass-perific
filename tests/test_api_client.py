"""Client behaviour, exercised against a local stand-in for api.enegic.com.

No Home Assistant runtime is involved — the client is standalone by design.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from aiohttp import ClientSession, ClientTimeout, web

from custom_components.perific.api import (
    BUCKET_MINUTE,
    EnegicClient,
    PerificAuthError,
    PerificConnectionError,
    PerificRateLimitError,
    PerificResponseError,
)

from .conftest import FakeApi

METER_ITEM_ID = 10004
TOKEN = "a-token-from-createtoken"


class TestLogin:
    """``PUT /createtoken``."""

    async def test_sends_credentials_and_keeps_the_token(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        createtoken: Any,
    ) -> None:
        fake_api.respond_json("PUT", "/createtoken", createtoken)
        client = make_client()

        info = await client.async_login("user@example.com", "secret")

        assert info.token == createtoken["TokenInfo"]["Token"]
        assert client.token == info.token
        recorded = fake_api.request_for("/createtoken")
        assert recorded.method == "PUT"
        assert (
            recorded.body == b'{"username": "user@example.com", "password": "secret"}'
        )

    async def test_does_not_send_a_token_it_does_not_have(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        createtoken: Any,
    ) -> None:
        fake_api.respond_json("PUT", "/createtoken", createtoken)
        await make_client().async_login("user@example.com", "secret")
        assert "X-Authorization" not in fake_api.request_for("/createtoken").headers

    async def test_bad_credentials_raise_auth_error(
        self, fake_api: FakeApi, make_client: Callable[..., EnegicClient]
    ) -> None:
        fake_api.respond_json("PUT", "/createtoken", {}, status=401)
        with pytest.raises(PerificAuthError):
            await make_client().async_login("user@example.com", "wrong")


class TestAuthHeader:
    """The header shape is easy to get wrong and fails as a 401 at runtime."""

    async def test_token_is_sent_bare_in_x_authorization(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        overview: Any,
    ) -> None:
        fake_api.respond_json("GET", "/getaccountoverview", overview)
        await make_client(token=TOKEN).async_get_items()

        headers = fake_api.request_for("/getaccountoverview").headers
        assert headers["X-Authorization"] == TOKEN
        assert "Authorization" not in headers

    async def test_without_a_token_nothing_is_sent_at_all(
        self, fake_api: FakeApi, make_client: Callable[..., EnegicClient]
    ) -> None:
        with pytest.raises(PerificAuthError):
            await make_client().async_get_items()
        assert fake_api.requests == []


class TestBodylessPut:
    """``/getlatestpackets`` is a PUT with no body and needs Content-Length: 0."""

    async def test_sends_an_empty_body_with_an_explicit_length(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        packets_t0: Any,
    ) -> None:
        fake_api.respond_json("PUT", "/getlatestpackets", packets_t0)
        await make_client(token=TOKEN).async_get_latest_packets()

        recorded = fake_api.request_for("/getlatestpackets")
        assert recorded.method == "PUT"
        assert recorded.body == b""
        assert recorded.headers["Content-Length"] == "0"


class TestGetItems:
    """``GET /getaccountoverview``."""

    async def test_returns_every_item(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        overview: Any,
    ) -> None:
        fake_api.respond_json("GET", "/getaccountoverview", overview)
        items = await make_client(token=TOKEN).async_get_items()
        assert [item.item_id for item in items] == [10001, 10002, 10003, METER_ITEM_ID]

    async def test_meters_filters_out_chargers_and_stubs(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        overview: Any,
    ) -> None:
        fake_api.respond_json("GET", "/getaccountoverview", overview)
        meters = await make_client(token=TOKEN).async_get_meters()
        assert [item.item_id for item in meters] == [METER_ITEM_ID]

    async def test_one_bad_item_does_not_cost_the_others(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        overview: Any,
    ) -> None:
        payload = {"Items": [{"Name": "no id here"}, *overview["Items"]]}
        fake_api.respond_json("GET", "/getaccountoverview", payload)
        items = await make_client(token=TOKEN).async_get_items()
        assert len(items) == len(overview["Items"])

    @pytest.mark.parametrize("payload", [{}, {"Items": None}, [], "nonsense"])
    async def test_a_response_without_items_is_an_error(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        payload: Any,
    ) -> None:
        fake_api.respond_json("GET", "/getaccountoverview", payload)
        with pytest.raises(PerificResponseError):
            await make_client(token=TOKEN).async_get_items()


class TestGetLatestPackets:
    """``PUT /getlatestpackets``."""

    async def test_keys_by_item_id(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        packets_t0: Any,
    ) -> None:
        fake_api.respond_json("PUT", "/getlatestpackets", packets_t0)
        packets = await make_client(token=TOKEN).async_get_latest_packets()

        assert set(packets) == {METER_ITEM_ID}
        minute = packets[METER_ITEM_ID].packets[BUCKET_MINUTE]
        assert minute.data.energy_import == 248718.155

    async def test_one_bad_entry_does_not_cost_the_others(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        packets_t0: Any,
    ) -> None:
        fake_api.respond_json(
            "PUT", "/getlatestpackets", [{"LatestPackets": {}}, *packets_t0]
        )
        packets = await make_client(token=TOKEN).async_get_latest_packets()
        assert set(packets) == {METER_ITEM_ID}

    async def test_a_non_list_response_is_an_error(
        self, fake_api: FakeApi, make_client: Callable[..., EnegicClient]
    ) -> None:
        fake_api.respond_json("PUT", "/getlatestpackets", {"unexpected": True})
        with pytest.raises(PerificResponseError):
            await make_client(token=TOKEN).async_get_latest_packets()


class TestFailureMapping:
    """Every failure the integration has to distinguish."""

    @pytest.mark.parametrize("status", [401, 403])
    async def test_rejected_credentials(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        status: int,
    ) -> None:
        fake_api.respond_json("GET", "/getaccountoverview", {}, status=status)
        with pytest.raises(PerificAuthError):
            await make_client(token=TOKEN).async_get_items()

    async def test_rate_limited_carries_the_retry_hint(
        self, fake_api: FakeApi, make_client: Callable[..., EnegicClient]
    ) -> None:
        fake_api.respond_json(
            "GET",
            "/getaccountoverview",
            {},
            status=429,
            headers={"Retry-After": "120"},
        )
        with pytest.raises(PerificRateLimitError) as caught:
            await make_client(token=TOKEN).async_get_items()
        assert caught.value.retry_after == 120.0

    async def test_rate_limited_without_a_usable_hint(
        self, fake_api: FakeApi, make_client: Callable[..., EnegicClient]
    ) -> None:
        fake_api.respond_json(
            "GET",
            "/getaccountoverview",
            {},
            status=429,
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
        )
        with pytest.raises(PerificRateLimitError) as caught:
            await make_client(token=TOKEN).async_get_items()
        assert caught.value.retry_after is None

    async def test_server_error_records_the_status(
        self, fake_api: FakeApi, make_client: Callable[..., EnegicClient]
    ) -> None:
        fake_api.respond_json("GET", "/getaccountoverview", {}, status=500)
        with pytest.raises(PerificResponseError) as caught:
            await make_client(token=TOKEN).async_get_items()
        assert caught.value.status == 500

    async def test_a_body_that_is_not_json(
        self, fake_api: FakeApi, make_client: Callable[..., EnegicClient]
    ) -> None:
        fake_api.respond(
            "GET",
            "/getaccountoverview",
            lambda: web.Response(text="<html>gateway</html>", content_type="text/html"),
        )
        with pytest.raises(PerificResponseError):
            await make_client(token=TOKEN).async_get_items()

    async def test_an_unreachable_api(
        self, socket_enabled: None, session: ClientSession
    ) -> None:
        client = EnegicClient(session, token=TOKEN, base_url="http://127.0.0.1:1")
        with pytest.raises(PerificConnectionError):
            await client.async_get_items()

    async def test_a_response_that_never_arrives(
        self, fake_api: FakeApi, session: ClientSession
    ) -> None:
        async def stall() -> web.StreamResponse:
            await asyncio.sleep(1)
            return web.Response()

        fake_api.respond("GET", "/getaccountoverview", stall)
        client = EnegicClient(
            session,
            token=TOKEN,
            base_url=fake_api.base_url,
            timeout=ClientTimeout(total=0.1),
        )
        with pytest.raises(PerificConnectionError):
            await client.async_get_items()


class TestSessionOwnership:
    """The session is injected, so the client must never close it."""

    async def test_the_session_survives_the_client(
        self,
        fake_api: FakeApi,
        session: ClientSession,
        overview: Any,
    ) -> None:
        fake_api.respond_json("GET", "/getaccountoverview", overview)
        client = EnegicClient(session, token=TOKEN, base_url=fake_api.base_url)
        await client.async_get_items()
        del client
        assert not session.closed


class TestBaseUrl:
    """A trailing slash on the base URL must not produce a double slash."""

    async def test_trailing_slash_is_trimmed(
        self, fake_api: FakeApi, session: ClientSession, overview: Any
    ) -> None:
        fake_api.respond_json("GET", "/getaccountoverview", overview)
        client = EnegicClient(session, token=TOKEN, base_url=f"{fake_api.base_url}/")
        await client.async_get_items()
        assert fake_api.request_for("/getaccountoverview").method == "GET"
