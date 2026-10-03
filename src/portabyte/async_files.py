"""Async file upload, delivery, and management operations."""

import asyncio
import inspect
import mimetypes
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Union, cast
from urllib.parse import quote

from .async_client import _AsyncHttp
from .errors import PortabyteError
from .files import PathLike, _asset_path, _required
from .models import (Asset, AssetDeliveryURL, BrowserUploadSession, CreateSession,
                     ListAssetsResult)


def _read_part(source: Any, offset: int, length: int) -> bytes:
    source.seek(offset)
    return source.read(length)


AsyncStateCallback = Callable[[Dict[str, Any]], Union[None, Awaitable[None]]]


async def _emit(callback: Optional[AsyncStateCallback], state: Dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(state)
    if inspect.isawaitable(result):
        await result


class AsyncFiles:
    """Operations available as ``async_portabyte.files``."""

    def __init__(self, http: _AsyncHttp) -> None:
        self._http = http

    async def create(self, name: str, content_type: str, size_bytes: int, *,
                     path: Optional[str] = None, visibility: Optional[str] = None,
                     cors_origin: Optional[str] = None) -> CreateSession:
        if not isinstance(name, str) or not name or not isinstance(content_type, str) or not content_type:
            raise ValueError("A name and content type are required.")
        if "\r" in content_type or "\n" in content_type or isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError("A valid content type and nonnegative size are required.")
        body: Dict[str, Any] = {"name": name, "contentType": content_type, "sizeBytes": size_bytes}
        for key, value in (("path", path), ("visibility", visibility), ("corsOrigin", cors_origin)):
            if value is not None:
                body[key] = value
        return cast(CreateSession, await self._http.api("POST", "assets", body=body))

    async def upload(self, file_path: PathLike, *, name: Optional[str] = None,
                     content_type: Optional[str] = None, path: Optional[str] = None,
                     visibility: Optional[str] = None) -> Asset:
        file = Path(file_path)
        if not await asyncio.to_thread(file.is_file):
            raise ValueError("Upload file must be a readable local file.")
        media_type = content_type or mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        size = (await asyncio.to_thread(file.stat)).st_size
        session = await self.create(name or file.name, media_type, size,
                                    path=path, visibility=visibility)
        return await self.resume(session, file)

    async def prepare_browser_upload(self, name: str, content_type: str, size_bytes: int, *,
                                     path: Optional[str] = None, visibility: Optional[str] = None,
                                     cors_origin: Optional[str] = None) -> BrowserUploadSession:
        session = await self.create(name, content_type, size_bytes, path=path,
                                    visibility=visibility, cors_origin=cors_origin)
        browser: Dict[str, Any] = {
            "assetId": _required(session, "id"),
            "uploadUrl": _required(session, "uploadUrl"),
            "uploadExpiresAt": _required(session, "uploadExpiresAt"),
            "uploadMode": _required(session, "uploadMode"),
        }
        for key in ("partSize", "maxConcurrency"):
            if key in session:
                browser[key] = session[key]
        return cast(BrowserUploadSession, browser)

    async def confirm(self, asset_id: str) -> Asset:
        return cast(Asset, await self._http.api("POST", _asset_path(asset_id) + "/uploaded"))

    async def get(self, asset_id: str) -> Asset:
        return cast(Asset, await self._http.api("GET", _asset_path(asset_id)))

    async def list(self, *, cursor: Optional[str] = None,
                   limit: Optional[int] = None) -> ListAssetsResult:
        params: Dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return cast(ListAssetsResult, await self._http.api("GET", "assets", params=params))

    async def remove(self, asset_id: str) -> None:
        await self._http.api("DELETE", _asset_path(asset_id))

    async def url(self, asset_id: str) -> AssetDeliveryURL:
        return cast(AssetDeliveryURL, await self._http.api("GET", _asset_path(asset_id) + "/url"))

    async def resume(self, session: Mapping[str, Any], file_path: PathLike, *,
                     state: Optional[Mapping[str, Any]] = None,
                     on_state_change: Optional[AsyncStateCallback] = None) -> Asset:
        file = Path(file_path)
        if not await asyncio.to_thread(file.is_file) or (await asyncio.to_thread(file.stat)).st_size != session.get("sizeBytes"):
            raise ValueError("The selected file does not match the upload session size.")
        url = _required(session, "uploadUrl")
        content_type = _required(session, "contentType")
        if session.get("uploadMode") == "single":
            data = await asyncio.to_thread(file.read_bytes)
            await self._http.put_bytes(url, content_type, data)
        elif session.get("uploadMode") == "multipart":
            await self._multipart(session, file, state or {}, on_state_change)
        else:
            raise PortabyteError("Upload session has an invalid mode.", code="invalid_upload")
        return await self.confirm(_required(session, "id"))

    async def cancel(self, session: Mapping[str, Any], *,
                     state: Optional[Mapping[str, Any]] = None) -> None:
        upload_id = (state or {}).get("uploadId")
        if session.get("uploadMode") == "multipart" and isinstance(upload_id, str) and upload_id:
            url = _required(session, "uploadUrl")
            try:
                await self._http.signed_json("DELETE", url + "/multipart/" + quote(upload_id, safe=""))
            except PortabyteError:
                pass
        await self.remove(_required(session, "id"))

    async def _multipart(self, session: Mapping[str, Any], file: Path,
                         state: Mapping[str, Any], on_state_change: Optional[AsyncStateCallback]) -> None:
        url = _required(session, "uploadUrl")
        content_type = _required(session, "contentType")
        part_size = session.get("partSize")
        if isinstance(part_size, bool) or not isinstance(part_size, int) or part_size < 5 * 1024 * 1024:
            raise PortabyteError("Multipart session has an invalid part size.", code="invalid_upload")
        upload_id = state.get("uploadId")
        if not isinstance(upload_id, str) or not upload_id:
            started = await self._http.signed_json("POST", url + "/multipart", {})
            upload_id = _required(started, "uploadId")
            state = {"uploadId": upload_id, "parts": []}
            await _emit(on_state_change, dict(state))
        size = (await asyncio.to_thread(file.stat)).st_size
        count = (size + part_size - 1) // part_size
        parts: Dict[int, Dict[str, Any]] = {}
        for part in state.get("parts", []):
            if (not isinstance(part, dict) or isinstance(part.get("partNumber"), bool)
                    or not isinstance(part.get("partNumber"), int)
                    or not 1 <= part["partNumber"] <= count
                    or not isinstance(part.get("etag"), str) or not part["etag"]):
                raise ValueError("Multipart state contains an invalid part.")
            parts[part["partNumber"]] = part
        source = await asyncio.to_thread(file.open, "rb")
        try:
            for number in range(1, count + 1):
                if number in parts:
                    continue
                length = min(part_size, size - (number - 1) * part_size)
                data = await asyncio.to_thread(_read_part, source, (number - 1) * part_size, length)
                if len(data) != length:
                    raise PortabyteError("Could not read upload part.", code="invalid_upload")
                part = await self._http.put_bytes(url + "/multipart/" + quote(upload_id, safe="") +
                                                  "/parts/" + str(number), content_type, data)
                if part.get("partNumber") != number or not isinstance(part.get("etag"), str):
                    raise PortabyteError("Upload gateway returned an invalid part.", code="invalid_response")
                parts[number] = part
                await _emit(on_state_change, {"uploadId": upload_id,
                                               "parts": [parts[n] for n in sorted(parts)]})
        finally:
            await asyncio.to_thread(source.close)
        await self._http.signed_json("POST", url + "/multipart/" + quote(upload_id, safe="") +
                                     "/complete", {"parts": [parts[n] for n in sorted(parts)]})
