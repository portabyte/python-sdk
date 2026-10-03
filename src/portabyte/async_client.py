"""Async HTTP client and public entry point."""

import asyncio
from typing import Any, Dict, Optional

import httpx

from .client import _API_URL, _SDK_VERSION, _check_signed_url, _response_error
from .errors import PortabyteError


class _AsyncHttp:
    def __init__(
        self, api_key: str, client: httpx.AsyncClient, max_retries: int
    ) -> None:
        self.api_key = api_key
        self.client = client
        self.max_retries = max_retries

    async def api(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "X-Portabyte-SDK": "python/" + _SDK_VERSION,
        }
        return await self._request(
            method,
            _API_URL + path,
            headers=headers,
            json=body,
            params=params,
            retryable=method == "GET",
        )

    async def signed_json(
        self, method: str, url: str, body: Any = None
    ) -> Dict[str, Any]:
        _check_signed_url(url)
        return await self._request(
            method, url, headers={"Content-Type": "application/json"}, json=body
        )

    async def put_bytes(
        self, url: str, content_type: str, data: bytes
    ) -> Dict[str, Any]:
        _check_signed_url(url)
        if not content_type or "\r" in content_type or "\n" in content_type:
            raise ValueError("Invalid upload content type.")
        return await self._request(
            "PUT",
            url,
            headers={"Content-Type": content_type},
            content=data,
            retryable=True,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        content: Optional[bytes] = None,
        retryable: bool = False,
    ) -> Dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    params=params,
                    content=content,
                )
            except httpx.RequestError as exc:
                error = PortabyteError("Network request failed.", code="network_error")
                if not retryable or attempt == self.max_retries:
                    raise error from exc
                await asyncio.sleep(min(0.5 * (2**attempt), 8.0))
                continue

            if not 200 <= response.status_code < 300:
                error = _response_error(response)
                if (
                    retryable
                    and (response.status_code == 429 or response.status_code >= 500)
                    and attempt < self.max_retries
                ):
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay = (
                            max(0.0, min(float(retry_after), 8.0))
                            if retry_after is not None
                            else 0.5 * (2**attempt)
                        )
                    except ValueError:
                        delay = 0.5 * (2**attempt)
                    await asyncio.sleep(delay)
                    continue
                raise error

            if not response.content:
                return {}
            try:
                decoded = response.json()
            except ValueError as exc:
                raise PortabyteError(
                    "Portabyte returned invalid JSON.",
                    response.status_code,
                    "invalid_response",
                ) from exc
            if not isinstance(decoded, dict):
                raise PortabyteError(
                    "Portabyte returned invalid JSON.",
                    response.status_code,
                    "invalid_response",
                )
            return decoded
        raise AssertionError("unreachable")


class AsyncPortabyte:
    """Async Portabyte server SDK for asyncio-based applications.

    Usage::

        async with AsyncPortabyte("pbt_sk_live_...") as portabyte:
            asset = await portabyte.files.upload("photo.jpg")
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 300.0,
        max_retries: int = 2,
        _transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or "\r" in api_key
            or "\n" in api_key
        ):
            raise ValueError("A project API key is required.")
        if timeout <= 0 or max_retries < 0:
            raise ValueError("timeout must be positive and max_retries nonnegative.")
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=_transport
        )
        self._http = _AsyncHttp(api_key, self._client, max_retries)
        from .async_files import AsyncFiles

        self.files = AsyncFiles(self._http)

    async def aclose(self) -> None:
        """Release HTTP connections."""
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncPortabyte":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
