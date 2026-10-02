"""Public response and resume-state shapes.

These are type hints for the API's JSON objects. Runtime responses stay plain
``dict`` objects so callers can also access fields added by the service later.
"""

from typing import List, TypedDict


class _AssetOptional(TypedDict, total=False):
    path: str
    etag: str
    publicUrl: str


class Asset(_AssetOptional):
    id: str
    projectId: str
    name: str
    contentType: str
    sizeBytes: int
    visibility: str
    createdAt: str


class _SessionOptional(TypedDict, total=False):
    partSize: int
    maxConcurrency: int


class CreateSession(Asset, _SessionOptional):
    uploadUrl: str
    uploadExpiresAt: str
    uploadMode: str


class BrowserUploadSession(_SessionOptional):
    assetId: str
    uploadUrl: str
    uploadExpiresAt: str
    uploadMode: str


class MultipartPart(TypedDict):
    partNumber: int
    etag: str


class MultipartUploadState(TypedDict):
    uploadId: str
    parts: List[MultipartPart]


class _DeliveryOptional(TypedDict, total=False):
    expiresAt: str


class AssetDeliveryURL(_DeliveryOptional):
    url: str
    public: bool


class _ListOptional(TypedDict, total=False):
    cursor: str


class ListAssetsResult(_ListOptional):
    records: List[Asset]
