import json
from importlib.metadata import version
import httpx
import pytest

from portabyte import Portabyte, PortabyteError
from portabyte import __version__


def test_package_version_matches_sdk_version():
    assert version("portabyte") == __version__


def client(handler):
    return Portabyte(
        "pbt_sk_live_test", max_retries=0, _transport=httpx.MockTransport(handler)
    )


def session(mode="single", size=5):
    data = {
        "id": "asset-1",
        "name": "sample.txt",
        "contentType": "text/plain",
        "sizeBytes": size,
        "uploadUrl": "https://gateway.test/v1/token",
        "uploadExpiresAt": "2026-10-02T21:00:00Z",
        "uploadMode": mode,
        "secret": "must-not-reach-browser",
    }
    if mode == "multipart":
        data["partSize"] = 5 * 1024 * 1024
        data["maxConcurrency"] = 3
    return data


def test_single_upload_uses_key_only_on_api(tmp_path):
    file = tmp_path / "sample.txt"
    file.write_bytes(b"hello")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/assets":
            assert request.headers["authorization"] == "Bearer pbt_sk_live_test"
            assert json.loads(request.content) == {
                "name": "sample.txt",
                "contentType": "text/plain",
                "sizeBytes": 5,
                "visibility": "public",
            }
            return httpx.Response(201, json=session())
        if request.url.host == "gateway.test":
            assert "authorization" not in request.headers
            assert "x-portabyte-sdk" not in request.headers
            assert request.headers["content-type"] == "text/plain"
            assert request.content == b"hello"
            return httpx.Response(201)
        assert request.url.path == "/v1/assets/asset-1/uploaded"
        return httpx.Response(200, json={"id": "asset-1", "status": "ready"})

    with client(handler) as sdk:
        assert sdk.files.upload(file, visibility="public")["status"] == "ready"
    assert [r.method for r in requests] == ["POST", "PUT", "POST"]


def test_browser_session_omits_server_fields():
    with client(
        lambda request: httpx.Response(201, json=session("multipart", 8_000_000))
    ) as sdk:
        browser = sdk.files.prepare_browser_upload(
            "sample.txt", "text/plain", 8_000_000, cors_origin="https://example.test"
        )
    assert browser == {
        "assetId": "asset-1",
        "uploadUrl": "https://gateway.test/v1/token",
        "uploadExpiresAt": "2026-10-02T21:00:00Z",
        "uploadMode": "multipart",
        "partSize": 5 * 1024 * 1024,
        "maxConcurrency": 3,
    }


def test_multipart_resume_skips_saved_part(tmp_path):
    part_size = 5 * 1024 * 1024
    file = tmp_path / "sample.txt"
    file.write_bytes(b"a" * part_size + b"last")
    saved = []
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "PUT":
            assert request.url.path.endswith("/parts/2")
            assert request.content == b"last"
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"partNumber": 2, "etag": "etag-2"})
        if request.url.path.endswith("/complete"):
            assert json.loads(request.content)["parts"] == [
                {"partNumber": 1, "etag": "etag-1"},
                {"partNumber": 2, "etag": "etag-2"},
            ]
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": "asset-1", "status": "ready"})

    with client(handler) as sdk:
        result = sdk.files.resume(
            session("multipart", part_size + 4),
            file,
            state={
                "uploadId": "upload-1",
                "parts": [{"partNumber": 1, "etag": "etag-1"}],
            },
            on_state_change=saved.append,
        )
    assert result["status"] == "ready"
    assert saved[-1]["parts"][-1] == {"partNumber": 2, "etag": "etag-2"}
    assert not any(path.endswith("/multipart") for _, path in calls)


def test_api_error_and_asset_id_encoding():
    def handler(request):
        assert request.url.raw_path == b"/v1/assets/a%2Fb"
        return httpx.Response(
            404,
            json={"code": "not_found", "message": "Missing", "requestId": "req-123"},
        )

    with client(handler) as sdk, pytest.raises(PortabyteError) as caught:
        sdk.files.get("a/b")
    assert (caught.value.status, caught.value.code, caught.value.request_id) == (
        404,
        "not_found",
        "req-123",
    )


def test_rejects_insecure_signed_url(tmp_path):
    file = tmp_path / "sample.txt"
    file.write_bytes(b"hello")
    invalid = session()
    invalid["uploadUrl"] = "http://gateway.test/upload"
    with client(lambda request: pytest.fail("No network request expected")) as sdk:
        with pytest.raises(ValueError, match="signed URL"):
            sdk.files.resume(invalid, file)


def test_multipart_starts_and_completes(tmp_path):
    part_size = 5 * 1024 * 1024
    file = tmp_path / "sample.txt"
    file.write_bytes(b"x" * (part_size + 1))
    changes = []
    uploaded = []

    def handler(request):
        if request.url.path.endswith("/multipart"):
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"uploadId": "upload-1"})
        if "/parts/" in request.url.path:
            number = int(request.url.path.rsplit("/", 1)[1])
            uploaded.append(number)
            return httpx.Response(
                200, json={"partNumber": number, "etag": f"etag-{number}"}
            )
        if request.url.path.endswith("/complete"):
            assert [
                part["partNumber"] for part in json.loads(request.content)["parts"]
            ] == [1, 2]
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": "asset-1"})

    with client(handler) as sdk:
        sdk.files.resume(
            session("multipart", part_size + 1), file, on_state_change=changes.append
        )
    assert uploaded == [1, 2]
    assert changes[0] == {"uploadId": "upload-1", "parts": []}
    assert len(changes[-1]["parts"]) == 2


def test_cancel_aborts_transfer_and_deletes_asset():
    calls = []

    def handler(request):
        calls.append(
            (request.method, request.url.path, "authorization" in request.headers)
        )
        return httpx.Response(204)

    with client(handler) as sdk:
        sdk.files.cancel(session("multipart"), state={"uploadId": "upload-1"})
    assert calls == [
        ("DELETE", "/v1/token/multipart/upload-1", False),
        ("DELETE", "/v1/assets/asset-1", True),
    ]


def test_cancel_removes_asset_when_gateway_abort_fails():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.host == "gateway.test":
            return httpx.Response(403, json={"code": "expired", "message": "Expired"})
        return httpx.Response(204)

    with client(handler) as sdk:
        sdk.files.cancel(session("multipart"), state={"uploadId": "upload-1"})
    assert calls[-1] == "/v1/assets/asset-1"


def test_get_retries_transient_failure_only():
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(
                503,
                headers={"retry-after": "0"},
                json={"code": "unavailable", "message": "Try again"},
            )
        return httpx.Response(200, json={"id": "asset-1"})

    with Portabyte("pbt_sk_live_test", _transport=httpx.MockTransport(handler)) as sdk:
        assert sdk.files.get("asset-1")["id"] == "asset-1"
    assert calls == ["GET", "GET"]


def test_create_does_not_retry_transient_failure():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(503, json={"code": "unavailable", "message": "Try again"})

    with Portabyte("pbt_sk_live_test", _transport=httpx.MockTransport(handler)) as sdk:
        with pytest.raises(PortabyteError) as caught:
            sdk.files.create("sample.txt", "text/plain", 5)
    assert caught.value.status == 503
    assert calls == ["POST"]
