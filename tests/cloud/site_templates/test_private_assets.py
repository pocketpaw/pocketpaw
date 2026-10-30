# tests/cloud/site_templates/test_private_assets.py — the publish-time asset detector.
#
# ``site_templates.assets.find_private_asset_refs`` must find every URL shape the
# platform mints for a workspace's own files, wherever it sits in a snapshot
# (rippleSpec JSON, any source file), and must leave external images alone. One
# test per shape, so deleting a shape's row from the detector fails exactly one
# test here (tests/mutations/site_templates.json).
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.site_templates.assets import find_private_asset_refs


def _in_source(text: str) -> dict:
    return {"source": {"src/routes/+page.svelte": text}, "rippleSpec": None}


def test_uploads_api() -> None:
    assert find_private_asset_refs(_in_source('<img src="/api/v1/uploads/abc123">')) == [
        "/api/v1/uploads/abc123"
    ]


def test_uploads_api_signed_grant_on_any_host() -> None:
    url = "https://app.example.com/api/v1/uploads/abc123?t=tok"
    assert find_private_asset_refs(_in_source(url)) == [url]


def test_files_api() -> None:
    refs = find_private_asset_refs(
        _in_source("fetch('/api/v1/files/content?path=a.png'); x('/api/v1/files?pocket_id=1')")
    )
    assert refs == ["/api/v1/files/content?path=a.png", "/api/v1/files?pocket_id=1"]


def test_media_api_from_studio_save_generated() -> None:
    ref = "/api/v1/media/1759300000000-abcdef012345.png"
    assert find_private_asset_refs(_in_source(f"url({ref})")) == [ref]


def test_avatar_api() -> None:
    assert find_private_asset_refs(_in_source("/api/v1/auth/avatar/u1.png")) == [
        "/api/v1/auth/avatar/u1.png"
    ]


def test_uploads_mount_relative() -> None:
    assert find_private_asset_refs(_in_source('src="/uploads/avatars/u1.png"')) == [
        "/uploads/avatars/u1.png"
    ]


def test_uploads_mount_on_the_deployments_own_host(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_PUBLIC_BASE_URL", "https://app.example.com/")
    url = "https://app.example.com/uploads/avatars/u1.png"
    assert find_private_asset_refs(_in_source(f'src="{url}"')) == [url]


def test_uploads_storage_key() -> None:
    key = "chat/202609/0123456789abcdef0123456789abcdef.png"
    assert find_private_asset_refs(_in_source(f'"{key}"')) == [key]


def test_media_storage_key() -> None:
    key = "generated/1759300000000-abcdef012345.png"
    assert find_private_asset_refs(_in_source(f'"{key}"')) == [key]


def test_presigned_s3() -> None:
    url = "https://b.s3.amazonaws.com/k.png?X-Amz-Algorithm=AWS4&X-Amz-Signature=abc"
    assert find_private_asset_refs(_in_source(f'<img src="{url}">')) == [url]


def test_presigned_r2_lowercase() -> None:
    url = "https://acct.r2.cloudflarestorage.com/b/k?x-amz-signature=1"
    assert find_private_asset_refs(_in_source(url)) == [url]


def test_found_anywhere_in_the_ripple_spec() -> None:
    spec = {"ui": {"children": [{"props": {"items": [{"img": "/api/v1/uploads/z9"}]}}]}}
    assert find_private_asset_refs({"rippleSpec": spec, "source": None}) == ["/api/v1/uploads/z9"]


def test_repeats_count_once() -> None:
    snap = {"source": {"a": "/api/v1/uploads/a1 /api/v1/uploads/a1", "b": "/api/v1/uploads/a1"}}
    assert find_private_asset_refs(snap) == ["/api/v1/uploads/a1"]


@pytest.mark.parametrize(
    "text",
    [
        "https://images.unsplash.com/photo-1500000000000-abc?w=800&q=80",
        "https://blog.example.com/wp-content/uploads/2024/01/x.jpg",
        "https://pub.r2.dev/sites-assets/w1/p1/abcd-hero.png",
        "GET /api/v1/uploads/{id}",
        "/api/v1/pockets/abc",
    ],
)
def test_external_and_public_urls_are_allowed(text: str) -> None:
    assert find_private_asset_refs(_in_source(text)) == []
