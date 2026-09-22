"""Shared fixtures: the redacted probe captures, and a local stand-in for the API.

The stand-in is a real aiohttp server on 127.0.0.1 rather than a mocked session, so
the two non-obvious wire details — the bare ``X-Authorization`` header and the
``Content-Length: 0`` bodyless PUT — are asserted against what aiohttp actually sends.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.perific.api import (
    EnegicClient,
    Item,
    TokenInfo,
    parse_items,
    parse_latest_packets,
)
from custom_components.perific.const import CONF_TOKEN_VALID_TO, DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"

USERNAME = "user@example.com"
PASSWORD = "correct horse battery staple"

TOKEN = "11111111-2222-3333-4444-555555555555"
# Relative to now, because setup refuses a token that has already expired. A literal
# date here would turn into a test that starts failing on a particular day.
TOKEN_VALID_TO = dt_util.utcnow() + timedelta(days=365)
TOKEN_INFO = TokenInfo(token=TOKEN, valid_to=TOKEN_VALID_TO)

ENTRY_DATA = {
    CONF_USERNAME: USERNAME,
    CONF_TOKEN: TOKEN,
    CONF_TOKEN_VALID_TO: TOKEN_VALID_TO.isoformat(),
}


def load_fixture(name: str) -> Any:
    """Read one redacted capture from ``tests/fixtures``."""
    return json.loads((FIXTURES / name).read_text())


type Responder = Callable[[], web.StreamResponse | Awaitable[web.StreamResponse]]


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """What the stand-in server saw on the wire."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeApi:
    """A local stand-in for api.enegic.com.

    Register responses with :meth:`respond_json` or :meth:`respond`; every request
    that arrives is recorded in :attr:`requests`, stubbed or not.
    """

    requests: list[RecordedRequest] = field(default_factory=list)
    _routes: dict[tuple[str, str], Responder] = field(default_factory=dict)
    _server: TestServer | None = None

    def respond(self, method: str, path: str, build: Responder) -> None:
        """Answer one method and path with a freshly built response each time.

        ``build`` may be a coroutine function, which is how a stalled response is
        simulated.
        """
        self._routes[(method, path)] = build

    def respond_json(
        self,
        method: str,
        path: str,
        payload: Any,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Answer one method and path with a JSON body."""
        body = json.dumps(payload)
        self.respond(
            method,
            path,
            lambda: web.Response(
                status=status,
                body=body.encode(),
                content_type="application/json",
                headers=headers,
            ),
        )

    @property
    def base_url(self) -> str:
        """The server's origin, in the shape ``EnegicClient`` expects."""
        if self._server is None:
            raise RuntimeError("Server not started")
        return str(self._server.make_url("")).rstrip("/")

    def request_for(self, path: str) -> RecordedRequest:
        """The single recorded request for a path, asserting there was exactly one."""
        matches = [record for record in self.requests if record.path == path]
        assert len(matches) == 1, f"expected one request to {path}, saw {len(matches)}"
        return matches[0]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.path,
                headers=dict(request.headers),
                body=await request.read(),
            )
        )
        build = self._routes.get((request.method, request.path))
        if build is None:
            return web.Response(status=404, text="no stub registered")
        built = build()
        if inspect.isawaitable(built):
            return await built
        return built

    async def start(self) -> None:
        """Bind the server to an ephemeral port on the loopback interface."""
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()

    async def stop(self) -> None:
        """Release the port."""
        if self._server is not None:
            await self._server.close()


@pytest.fixture
async def fake_api(socket_enabled: None) -> AsyncIterator[FakeApi]:
    """A running stand-in for the Enegic API.

    ``socket_enabled`` is required: the Home Assistant test plugin blocks socket
    creation outright, so binding a loopback listener needs it lifted.
    """
    api = FakeApi()
    await api.start()
    try:
        yield api
    finally:
        await api.stop()


@pytest.fixture
async def session() -> AsyncIterator[ClientSession]:
    """An externally owned session, as the integration injects one."""
    async with ClientSession() as http:
        yield http


@pytest.fixture
def make_client(
    session: ClientSession, fake_api: FakeApi
) -> Callable[..., EnegicClient]:
    """Build a client pointed at the stand-in, with or without a token."""

    def build(*, token: str | None = None) -> EnegicClient:
        return EnegicClient(session, token=token, base_url=fake_api.base_url)

    return build


@pytest.fixture
def createtoken() -> Any:
    """The captured ``PUT /createtoken`` response."""
    return load_fixture("createtoken.json")


@pytest.fixture
def overview() -> Any:
    """The captured ``GET /getaccountoverview`` response."""
    return load_fixture("getaccountoverview.json")


@pytest.fixture
def packets_t0() -> Any:
    """The first ``PUT /getlatestpackets`` capture."""
    return load_fixture("getlatestpackets_t0.json")


@pytest.fixture
def packets_t1() -> Any:
    """The second ``PUT /getlatestpackets`` capture, taken ~60 s after the first."""
    return load_fixture("getlatestpackets_t1.json")


@pytest.fixture
def phasedata() -> Any:
    """The captured ``PUT /getphasedata`` response.

    Eleven minutes spanning an hour boundary, so the hourly reduction has two
    hours to work with. ``hwo`` is flat across all of them, which is what the
    real export register does overnight.
    """
    return load_fixture("getphasedata.json")


@pytest.fixture
def meters(overview: Any) -> list[Item]:
    """The meters in the capture — one of its four items."""
    return [item for item in parse_items(overview) if item.is_meter]


@pytest.fixture
def mock_client(meters: list[Item], packets_t0: Any) -> AsyncMock:
    """An ``EnegicClient`` stand-in answering from the captures."""
    client = AsyncMock()
    client.async_login.return_value = TOKEN_INFO
    client.async_get_meters.return_value = meters
    client.async_get_latest_packets.return_value = parse_latest_packets(packets_t0)
    # Setting up an entry starts a history import. Without a real list here the
    # importer would walk a Mock, so the quiet default is "nothing to read".
    client.async_get_phase_data.return_value = []
    return client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A config entry holding a token, as the config flow creates one."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME,
        version=2,
        data=dict(ENTRY_DATA),
    )


@pytest.fixture
def legacy_config_entry() -> MockConfigEntry:
    """A version 1 entry, from before the password was traded for a token."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME,
        version=1,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )


async def settle(hass: HomeAssistant) -> None:
    """Run pending work until a history import has finished.

    An import hops to the recorder's executor several times — once per resume
    point, then again per series read — and a drain returns as soon as the loop
    is idle, which it is while an executor job is still out. Each hop therefore
    needs its own drain, and one or two leave the import half-run: the client
    call a test is waiting for has not happened yet, and whatever is left
    finishes after the recorder has been torn down.
    """
    for _ in range(6):
        await hass.async_block_till_done()


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    enable_custom_integrations: None,
) -> MockConfigEntry:
    """Set the integration up against the mocked client and return its entry."""
    config_entry.add_to_hass(hass)
    with patch("custom_components.perific.EnegicClient", return_value=mock_client):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await settle(hass)
    return config_entry
