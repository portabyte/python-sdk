import asyncio
import json

import httpx
import pytest

from portabyte import AsyncPortabyte, PortabyteError


def session(mode="single", size=5):
    value = {
        "id": "asset-1",
        "name": "sample.txt",
        "contentType": "text/plain",
        "sizeBytes": size,
        "uploadUrl": "https://gateway.test/v1/token",
        "uploadExpiresAt": "2026-10-03T12:00:00Z",
        "uploadMode": mode,
        "serverOnly": "secret",
    }
    if mode == "multipart":
        value["partSize"] = 5 * 1024 * 1024
    return value


def test_async_single_upload_and_auth_boundary(tmp_path):
    file = tmp_path / "sample.txt"
    file.write_bytes(b"hello")
    calls = []

    async def handler(request):
        calls.append(request.method)
        if request.url.path == "/v1/assets":
            assert request.headers["authorization"] == "Bearer pbt_sk_live_test"
            return httpx.Response(201, json=session())
        if request.url.host == "gateway.test":
            assert "authorization" not in request.headers
            assert request.content == b"hello"
            return httpx.Response(201)
        assert request.url.path == "/v1/assets/asset-1/uploaded"
        return httpx.Response(200, json={"id": "asset-1", "status": "ready"})

    async def run():
        async with AsyncPortabyte(
            "pbt_sk_live_test", _transport=httpx.MockTransport(handler)
        ) as sdk:
            assert (await sdk.files.upload(file))["status"] == "ready"

    asyncio.run(run())
    assert calls == ["POST", "PUT", "POST"]


def test_async_browser_safe_fields_and_errors():
    async def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json=session())
        return httpx.Response(
            404, json={"code": "not_found", "message": "Missing", "requestId": "req-1"}
        )

    async def run():
        async with AsyncPortabyte(
            "pbt_sk_live_test", _transport=httpx.MockTransport(handler)
        ) as sdk:
            browser = await sdk.files.prepare_browser_upload(
                "sample.txt", "text/plain", 5
            )
            assert "serverOnly" not in browser
            assert browser["assetId"] == "asset-1"
            with pytest.raises(PortabyteError) as caught:
                await sdk.files.get("missing")
            assert (
                caught.value.status,
                caught.value.code,
                caught.value.request_id,
            ) == (404, "not_found", "req-1")

    asyncio.run(run())


def test_async_multipart_resume_awaits_state_callback(tmp_path):
    part_size = 5 * 1024 * 1024
    file = tmp_path / "sample.txt"
    file.write_bytes(b"a" * part_size + b"z")
    saved = []

    async def handler(request):
        if request.url.path.endswith("/multipart"):
            return httpx.Response(200, json={"uploadId": "upload-1"})
        if "/parts/" in request.url.path:
            number = int(request.url.path.rsplit("/", 1)[1])
            assert "authorization" not in request.headers
            return httpx.Response(
                200, json={"partNumber": number, "etag": f"etag-{number}"}
            )
        if request.url.path.endswith("/complete"):
            assert [p["partNumber"] for p in json.loads(request.content)["parts"]] == [
                1,
                2,
            ]
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": "asset-1"})

    async def save(state):
        await asyncio.sleep(0)
        saved.append(state)

    async def run():
        async with AsyncPortabyte(
            "pbt_sk_live_test", _transport=httpx.MockTransport(handler)
        ) as sdk:
            await sdk.files.resume(
                session("multipart", part_size + 1), file, on_state_change=save
            )

    asyncio.run(run())
    assert len(saved) == 3
    assert len(saved[-1]["parts"]) == 2


def test_async_get_retries_and_create_does_not():
    calls = []

    async def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(
                503, headers={"retry-after": "0"}, json={"code": "unavailable"}
            )
        return httpx.Response(200, json={"id": "asset-1"})

    async def run():
        async with AsyncPortabyte(
            "pbt_sk_live_test", _transport=httpx.MockTransport(handler)
        ) as sdk:
            assert (await sdk.files.get("asset-1"))["id"] == "asset-1"

    asyncio.run(run())
    assert calls == ["GET", "GET"]


def test_async_cancel_removes_asset_after_expired_abort():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if request.url.host == "gateway.test":
            return httpx.Response(403, json={"code": "expired"})
        return httpx.Response(204)

    async def run():
        async with AsyncPortabyte(
            "pbt_sk_live_test", _transport=httpx.MockTransport(handler)
        ) as sdk:
            await sdk.files.cancel(session("multipart"), state={"uploadId": "upload-1"})

    asyncio.run(run())
    assert calls[-1] == "/v1/assets/asset-1"
