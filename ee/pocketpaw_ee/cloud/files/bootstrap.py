"""Compose registry + providers + rules from config into a ready-to-mount router."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Request

from pocketpaw_ee.cloud.files.abac_config import load_rules
from pocketpaw_ee.cloud.files.dto import RequestContext
from pocketpaw_ee.cloud.files.mounts_config import load_mounts
from pocketpaw_ee.cloud.files.providers.kb import KbProvider
from pocketpaw_ee.cloud.files.providers.uploads import UploadsProvider
from pocketpaw_ee.cloud.files.registry import ProviderRegistry
from pocketpaw_ee.cloud.files.router import build_router


def build_files_router(
    *,
    uploads_store,
    kb_service,
    ctx_factory: Callable[[Request], RequestContext],
) -> APIRouter:
    registry = ProviderRegistry(configs=load_mounts())
    registry.register(UploadsProvider(store=uploads_store))
    registry.register(KbProvider(service=kb_service))
    rules = load_rules()
    return build_router(registry=registry, rules=rules, ctx_factory=ctx_factory)
