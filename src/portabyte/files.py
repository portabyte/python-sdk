"""File upload, delivery, and management operations."""

import mimetypes
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Union, cast
from urllib.parse import quote

from .client import _Http
from .errors import PortabyteError
from .models import (Asset, AssetDeliveryURL, BrowserUploadSession, CreateSession,
                     ListAssetsResult)

PathLike = Union[str, Path]
StateCallback = Callable[[Dict[str, Any]], None]


def _required(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise PortabyteError(f"Response is missing {key}.", code="invalid_response")
    return value


def _asset_path(asset_id: str) -> str:
    if not isinstance(asset_id, str) or not asset_id:
        raise ValueError("An asset ID is required.")
    return "assets/" + quote(asset_id, safe="")


class Files:
    """Operations available as ``portabyte.files``."""

    def __init__(self, http: _Http) -> None:
        self._http = http

    def create(self, name: str, content_type: str, size_bytes: int, *,
               path: Optional[str] = None, visibility: Optional[str] = None,
               cors_origin: Optional[str] = None) -> CreateSession:
        """Create a signed upload session. Use ``upload`` for the full flow."""
        if not isinstance(name, str) or not name or not isinstance(content_type, str) or not content_type:
            raise ValueError("A name and content type are required.")
        if "\r" in content_type or "\n" in content_type or isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError("A valid content type and nonnegative size are required.")
        body = {"name": name, "contentType": content_type, "sizeBytes": size_bytes}
        for key, value in (("path", path), ("visibility", visibility), ("corsOrigin", cors_origin)):
            if value is not None:
                body[key] = value
        return cast(CreateSession, self._http.api("POST", "assets", body=body))

    def upload(self, file_path: PathLike, *, name: Optional[str] = None,
               content_type: Optional[str] = None, path: Optional[str] = None,
               visibility: Optional[str] = None) -> Asset:
        """Upload a local file, then confirm it. Large files use multipart."""
        file = Path(file_path)
        if not file.is_file():
            raise ValueError("Upload file must be a readable local file.")
        media_type = content_type or mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        session = self.create(name or file.name, media_type, file.stat().st_size,
                              path=path, visibility=visibility)
        return self.resume(session, file)

    def prepare_browser_upload(self, name: str, content_type: str, size_bytes: int, *,
                               path: Optional[str] = None, visibility: Optional[str] = None,
                               cors_origin: Optional[str] = None) -> BrowserUploadSession:
        """Return only session fields safe to send to a browser."""
        session = self.create(name, content_type, size_bytes, path=path,
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

    def confirm(self, asset_id: str) -> Asset:
        """Confirm bytes uploaded to a signed URL from your server."""
        return cast(Asset, self._http.api("POST", _asset_path(asset_id) + "/uploaded"))

    def get(self, asset_id: str) -> Asset:
        return cast(Asset, self._http.api("GET", _asset_path(asset_id)))

    def list(self, *, cursor: Optional[str] = None,
             limit: Optional[int] = None) -> ListAssetsResult:
        params: Dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return cast(ListAssetsResult, self._http.api("GET", "assets", params=params))

    def remove(self, asset_id: str) -> None:
        self._http.api("DELETE", _asset_path(asset_id))

    def url(self, asset_id: str) -> AssetDeliveryURL:
        """Get a stable public URL or a short-lived private URL."""
        return cast(AssetDeliveryURL, self._http.api("GET", _asset_path(asset_id) + "/url"))

    def resume(self, session: Mapping[str, Any], file_path: PathLike, *,
               state: Optional[Mapping[str, Any]] = None,
               on_state_change: Optional[StateCallback] = None) -> Asset:
        """Finish an existing upload session and persist multipart state as it changes."""
        file = Path(file_path)
        if not file.is_file() or file.stat().st_size != session.get("sizeBytes"):
            raise ValueError("The selected file does not match the upload session size.")
        url = _required(session, "uploadUrl")
        content_type = _required(session, "contentType")
        if session.get("uploadMode") == "single":
            with file.open("rb") as source:
                self._http.put_bytes(url, content_type, source.read())
        elif session.get("uploadMode") == "multipart":
            self._multipart(session, file, state or {}, on_state_change)
        else:
            raise PortabyteError("Upload session has an invalid mode.", code="invalid_upload")
        return self.confirm(_required(session, "id"))

    def cancel(self, session: Mapping[str, Any], *,
               state: Optional[Mapping[str, Any]] = None) -> None:
        """Abort a multipart transfer if started, then remove its pending asset."""
        upload_id = (state or {}).get("uploadId")
        if session.get("uploadMode") == "multipart" and isinstance(upload_id, str) and upload_id:
            url = _required(session, "uploadUrl")
            try:
                self._http.signed_json("DELETE", url + "/multipart/" + quote(upload_id, safe=""))
            except PortabyteError:
                # The signed URL may have expired; the asset still needs removal.
                pass
        self.remove(_required(session, "id"))

    def _multipart(self, session: Mapping[str, Any], file: Path,
                   state: Mapping[str, Any], on_state_change: Optional[StateCallback]) -> None:
        url = _required(session, "uploadUrl")
        content_type = _required(session, "contentType")
        part_size = session.get("partSize")
        if isinstance(part_size, bool) or not isinstance(part_size, int) or part_size < 5 * 1024 * 1024:
            raise PortabyteError("Multipart session has an invalid part size.", code="invalid_upload")
        upload_id = state.get("uploadId")
        if not isinstance(upload_id, str) or not upload_id:
            started = self._http.signed_json("POST", url + "/multipart", {})
            upload_id = _required(started, "uploadId")
            state = {"uploadId": upload_id, "parts": []}
            if on_state_change:
                on_state_change(dict(state))
        size = file.stat().st_size
        count = (size + part_size - 1) // part_size
        parts: Dict[int, Dict[str, Any]] = {}
        for part in state.get("parts", []):
            if (not isinstance(part, dict) or isinstance(part.get("partNumber"), bool)
                    or not isinstance(part.get("partNumber"), int)
                    or not 1 <= part["partNumber"] <= count
                    or not isinstance(part.get("etag"), str) or not part["etag"]):
                raise ValueError("Multipart state contains an invalid part.")
            parts[part["partNumber"]] = part
        with file.open("rb") as source:
            for number in range(1, count + 1):
                if number in parts:
                    continue
                source.seek((number - 1) * part_size)
                length = min(part_size, size - (number - 1) * part_size)
                data = source.read(length)
                if len(data) != length:
                    raise PortabyteError("Could not read upload part.", code="invalid_upload")
                part = self._http.put_bytes(url + "/multipart/" + quote(upload_id, safe="") +
                                            "/parts/" + str(number), content_type, data)
                if part.get("partNumber") != number or not isinstance(part.get("etag"), str):
                    raise PortabyteError("Upload gateway returned an invalid part.", code="invalid_response")
                parts[number] = part
                if on_state_change:
                    on_state_change({"uploadId": upload_id, "parts": [parts[n] for n in sorted(parts)]})
        ordered = [parts[n] for n in sorted(parts)]
        self._http.signed_json("POST", url + "/multipart/" + quote(upload_id, safe="") +
                               "/complete", {"parts": ordered})
