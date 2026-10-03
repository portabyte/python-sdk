"""HTTP client and public entry point."""

import time
from typing import Any, Dict, Optional

import httpx

from .errors import PortabyteError

_API_URL = "https://api.portabyte.dev/v1/"
_SDK_VERSION = "0.1.0"


class _Http:
    def __init__(self, api_key: str, client: httpx.Client, max_retries: int) -> None:
        self.api_key = api_key
        self.client = client
        self.max_retries = max_retries

    def api(
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
        return self._request(
            method,
            _API_URL + path,
            headers=headers,
            json=body,
            params=params,
            retryable=method == "GET",
        )

    def signed_json(self, method: str, url: str, body: Any = None) -> Dict[str, Any]:
        _check_signed_url(url)
        return self._request(
            method,
            url,
            headers={"Content-Type": "application/json"},
            json=body,
            retryable=False,
        )

    def put_bytes(self, url: str, content_type: str, data: bytes) -> Dict[str, Any]:
        _check_signed_url(url)
        if not content_type or "\r" in content_type or "\n" in content_type:
            raise ValueError("Invalid upload content type.")
        return self._request(
            "PUT",
            url,
            headers={"Content-Type": content_type},
            content=data,
            retryable=True,
        )

    def _request(
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
                response = self.client.request(
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
                time.sleep(min(0.5 * (2**attempt), 8.0))
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
                    time.sleep(delay)
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


def _check_signed_url(url: str) -> None:
    parsed = httpx.URL(url)
    if parsed.scheme != "https" or not parsed.host or parsed.userinfo:
        raise ValueError("Upload session contains an invalid signed URL.")


def _response_error(response: httpx.Response) -> PortabyteError:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    message = body.get("message")
    code = body.get("code")
    request_id = body.get("requestId") or response.headers.get("x-request-id")
    return PortabyteError(
        message
        if isinstance(message, str)
        else f"Request failed with status {response.status_code}.",
        response.status_code,
        code if isinstance(code, str) else "request_failed",
        request_id if isinstance(request_id, str) else None,
    )


class Portabyte:
    """Portabyte server SDK. Keep the API key on your server.

    Usage::

        with Portabyte("pbt_sk_live_...") as portabyte:
            asset = portabyte.files.upload("photo.jpg")
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 300.0,
        max_retries: int = 2,
        _transport: Optional[httpx.BaseTransport] = None,
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
        self._client = httpx.Client(
            timeout=timeout, follow_redirects=False, transport=_transport
        )
        self._http = _Http(api_key, self._client, max_retries)
        from .files import Files

        self.files = Files(self._http)

    def close(self) -> None:
        """Release HTTP connections."""
        self._client.close()

    def __enter__(self) -> "Portabyte":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
