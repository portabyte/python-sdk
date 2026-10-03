<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-horizontal-white.svg">
  <img src="assets/logo-horizontal-color.svg" alt="Portabyte" width="260">
</picture>

# Portabyte Python SDK

Upload, deliver, and manage files from a Python server with `portabyte`.

[![PyPI version](https://img.shields.io/pypi/v/portabyte)](https://pypi.org/project/portabyte/)
[![CI](https://github.com/portabyte/python-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/portabyte/python-sdk/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)

## Requirements

- Python 3.9 or later
- A Portabyte project API key (`pbt_sk_live_...`)

Keep the key on your server. This package is not for browser code.

## Install

```sh
pip install portabyte
```

## Quick start

Create a [project API key](https://portabyte.dev/docs/getting-started/api-keys), then place a PDF named `summary.pdf` beside your script:

```sh
export PORTABYTE_API_KEY="pbt_sk_live_your_key_here"
```

Save this as `quickstart.py`:

```python
import os
from portabyte import Portabyte

with Portabyte(os.environ["PORTABYTE_API_KEY"]) as portabyte:
    asset = portabyte.files.upload("summary.pdf", visibility="public")
    print(asset["id"], asset["publicUrl"])
```

Run `python quickstart.py`. The SDK creates an upload session, sends the file to its signed upload URL, and confirms it. The API endpoint is built in; you only provide the project key.

## Async applications

Use `AsyncPortabyte` in an asyncio-based server. It offers the same `files` operations and closes its connection pool when the `async with` block ends:

```python
import os
from portabyte import AsyncPortabyte


async def upload_summary():
    async with AsyncPortabyte(os.environ["PORTABYTE_API_KEY"]) as portabyte:
        return await portabyte.files.upload("summary.pdf", visibility="public")
```

For an interrupted multipart upload, call `await portabyte.files.resume(session, path, state=saved_state, on_state_change=save_state)`. The callback may be a regular function or an async function; the SDK waits for it before uploading the next part.

## Common tasks

```python
with Portabyte(os.environ["PORTABYTE_API_KEY"]) as portabyte:
    asset = portabyte.files.get(asset_id)
    delivery = portabyte.files.url(asset_id)
    page = portabyte.files.list(limit=20)
    next_page = (
        portabyte.files.list(cursor=page["cursor"], limit=20)
        if page.get("cursor")
        else None
    )
    portabyte.files.remove(asset_id)
```

Public files have stable delivery URLs. Private files receive short-lived signed URLs; request a fresh one when needed. Set `path` during upload to replace the current file at an application-owned path.

Large files use multipart upload automatically. To recover from an interrupted upload, persist the session returned by `files.create()` and the state passed to the callback:

```python
from pathlib import Path

file = Path("video.mp4")
with Portabyte(os.environ["PORTABYTE_API_KEY"]) as portabyte:
    session = portabyte.files.create(file.name, "video/mp4", file.stat().st_size)
    # Save session somewhere durable before starting the transfer.
    asset = portabyte.files.resume(
        session,
        file,
        state=saved_state,
        on_state_change=lambda state: save_upload_state(state),
    )
```

If you need a direct browser upload, call `files.prepare_browser_upload(...)` on your server, send that result to the browser, then call `files.confirm(asset_id)` from your server after the upload succeeds. Never send the API key to the browser. See [Browser uploads](https://portabyte.dev/docs/upload-delivery/browser-uploads).

To abandon a multipart upload, call `files.cancel(session, state=saved_state)`. The SDK attempts to abort the transfer and removes the pending asset even if its signed URL has expired.

Response shapes such as `Asset`, `CreateSession`, and `ListAssetsResult` are exported as type hints. Runtime responses remain plain dictionaries so newly added API fields remain accessible.

## Errors

Failed requests raise `PortabyteError`:

```python
from portabyte import PortabyteError

try:
    asset = portabyte.files.get(asset_id)
except PortabyteError as error:
    print(error.status, error.code, error.request_id)
    raise
```

The error exposes `status`, `code`, and `request_id` when the API supplies them.

## Client options

| Option | Purpose | Default |
| --- | --- | --- |
| `api_key` | Server API key scoped to a project | Required |
| `max_retries` | Retries for reads and byte transfers | `2` |
| `timeout` | Request timeout in seconds | `300` |

Both `Portabyte` and `AsyncPortabyte` accept these options. The SDK retries reads and byte transfers on network failures, `429`, and `5xx`. It does not retry state-changing API requests.

## When to use this SDK

Use it on a trusted server to upload and manage files, prepare direct browser uploads, and request delivery URLs. A browser can send file bytes to a signed upload URL prepared by your server; it must never receive your project API key.

## Documentation

- [REST API reference](https://portabyte.dev/docs/api-reference)
- [Public and private files](https://portabyte.dev/docs/upload-delivery/public-and-private-files)
- [Changelog](./CHANGELOG.md)

## Development

```sh
uv run --with pytest pytest
uv run --with mypy mypy src/portabyte --ignore-missing-imports
uv run --with ruff==0.16.10 ruff format src tests README.md
uv run --with ruff==0.16.10 ruff format --check src tests README.md
uv build
```

Tests use a mock HTTP transport and do not require an API key.

Report SDK bugs in [GitHub issues](https://github.com/portabyte/python-sdk/issues).

## License

[MIT](./LICENSE)
