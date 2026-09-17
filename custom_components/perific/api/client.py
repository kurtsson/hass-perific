"""HTTP client for the Enegic API.

Endpoint reference: ``docs/api/enegic.md``. Verbs are not consistent across the API
and are not inferrable — ``/getlatestpackets`` is a PUT despite being a read.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout

from .exceptions import (
    PerificAuthError,
    PerificConnectionError,
    PerificRateLimitError,
    PerificResponseError,
)
from .models import TokenInfo, parse_items, parse_latest_packets

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from .models import Item, ItemPackets

BASE_URL = "https://api.enegic.com"
DEFAULT_TIMEOUT = ClientTimeout(total=30)

_BODYLESS_METHODS = ("PUT", "POST")


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _raise_for_status(status: int, path: str, retry_after: str | None) -> None:
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        raise PerificAuthError(f"{path} rejected the credentials (HTTP {status})")
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        raise PerificRateLimitError(
            f"{path} is rate limited", retry_after=_retry_after(retry_after)
        )
    if status >= HTTPStatus.BAD_REQUEST:
        raise PerificResponseError(f"{path} answered HTTP {status}", status=status)


class EnegicClient:
    """Talks to api.enegic.com on behalf of one account."""

    def __init__(
        self,
        session: ClientSession,
        *,
        token: str | None = None,
        base_url: str = BASE_URL,
        timeout: ClientTimeout = DEFAULT_TIMEOUT,
    ) -> None:
        """Take an externally owned session; this class never creates or closes one."""
        self._session = session
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @property
    def token(self) -> str | None:
        """The token in use, once logged in."""
        return self._token

    async def async_login(self, username: str, password: str) -> TokenInfo:
        """Mint a token and keep it for subsequent calls."""
        payload = await self._request(
            "PUT",
            "/createtoken",
            body={"username": username, "password": password},
            authenticated=False,
        )
        info = TokenInfo.from_api(payload)
        self._token = info.token
        return info

    async def async_get_items(self) -> list[Item]:
        """Every device on the account, meters and otherwise."""
        return parse_items(await self._request("GET", "/getaccountoverview"))

    async def async_get_meters(self) -> list[Item]:
        """Only the phase meters. Accounts also hold chargers and stub items."""
        return [item for item in await self.async_get_items() if item.is_meter]

    async def async_get_latest_packets(self) -> dict[int, ItemPackets]:
        """Fetch current readings, keyed by item id."""
        return parse_latest_packets(await self._request("PUT", "/getlatestpackets"))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            if self._token is None:
                raise PerificAuthError(f"{path} needs a token; log in first")
            # Bare token, and X-Authorization rather than Authorization.
            headers["X-Authorization"] = self._token

        if body is not None:
            kwargs: dict[str, Any] = {"json": body}
        elif method in _BODYLESS_METHODS:
            # An explicit empty body sends Content-Length: 0, which the bodyless
            # PUT endpoints require.
            kwargs = {"data": b""}
        else:
            kwargs = {}

        payload: Any = None
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                timeout=self._timeout,
                **kwargs,
            ) as response:
                status = response.status
                retry_after = response.headers.get("Retry-After")
                if status < HTTPStatus.BAD_REQUEST:
                    payload = await response.json(content_type=None)
        except TimeoutError as err:
            raise PerificConnectionError(f"{path} timed out") from err
        except ValueError as err:
            raise PerificResponseError(f"{path} did not answer with JSON") from err
        except ClientError as err:
            raise PerificConnectionError(f"{path} could not be reached: {err}") from err

        _raise_for_status(status, path, retry_after)
        return payload
