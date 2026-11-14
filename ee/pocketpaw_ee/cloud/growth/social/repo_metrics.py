# ee/pocketpaw_ee/cloud/growth/social/repo_metrics.py — one product repo's GitHub
# numbers for one UTC day, the input to the daily ``RepoMetric`` rows.
#
# ``github_repo_metrics`` is the production ``RepoMetricsFn``. It reads
# ``GET /repos/{owner}/{name}`` for stars and forks (a failure there raises, so no
# row with fake zeros is ever written) and, only when ``POCKETPAW_GROWTH_GITHUB_TOKEN``
# is set, the three traffic endpoints. Traffic needs push access: any traffic
# failure (403 / 404 included) or a missing token gives ``traffic_available=False``
# and zero traffic, while stars and forks are still returned.
#
# Day rule: the caller passes ``day`` (``YYYY-MM-DD``, normally yesterday UTC, the
# last complete day; today's entry is partial). The views and clones counts are the
# entry whose timestamp falls on that date, 0 when GitHub lists none (it omits empty
# days). Referrers are GitHub's rolling 14-day top list, stored as returned.
#
# Slugs are checked against ``changes``' ``owner/name`` pattern before any URL is
# built. The host is fixed (``changes.GITHUB_API``), never user-supplied, so
# ``safe_fetch`` is not needed. The token is never logged.
#
# The ``set_production_repo_metrics_fn`` / ``resolve_repo_metrics_fn`` seam mirrors
# ``changes``: nothing is installed by default; the growth worker installs the
# production fn and tests install a fake.

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from typing import Any, Protocol

import httpx

from pocketpaw_ee.cloud.growth.social.changes import _SLUG_RE, GITHUB_API, TOKEN_ENV

logger = logging.getLogger(__name__)

_PER_DAY = {"per": "day"}
_TRAFFIC = (("views", _PER_DAY), ("clones", _PER_DAY), ("popular/referrers", None))


@dataclass(frozen=True)
class RepoSnapshot:
    repo: str
    day: str
    stars: int = 0
    forks: int = 0
    views: int = 0
    uniques: int = 0
    clones: int = 0
    clone_uniques: int = 0
    referrers: tuple[dict[str, Any], ...] = ()
    traffic_available: bool = False


class RepoMetricsFn(Protocol):
    async def __call__(self, repo: str, *, day: str) -> RepoSnapshot: ...


def is_repo_slug(repo: Any) -> bool:
    return isinstance(repo, str) and bool(_SLUG_RE.match(repo))


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _day_counts(payload: Any, key: str, day: str) -> tuple[int, int]:
    for row in payload.get(key) or []:
        if isinstance(row, dict) and str(row.get("timestamp") or "")[:10] == day:
            return _int(row.get("count")), _int(row.get("uniques"))
    return 0, 0


def _referrers(payload: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "referrer": str(r.get("referrer") or ""),
            "count": _int(r.get("count")),
            "uniques": _int(r.get("uniques")),
        }
        for r in payload or []
        if isinstance(r, dict)
    )


async def _traffic(
    client: httpx.AsyncClient, base: str, headers: dict[str, str], snap: RepoSnapshot
) -> RepoSnapshot:
    try:
        got = []
        for path, params in _TRAFFIC:
            resp = await client.get(f"{base}/traffic/{path}", params=params, headers=headers)
            resp.raise_for_status()
            got.append(resp.json())
        views, clones, refs = got
        v, vu = _day_counts(views, "views", snap.day)
        c, cu = _day_counts(clones, "clones", snap.day)
        return replace(
            snap,
            views=v,
            uniques=vu,
            clones=c,
            clone_uniques=cu,
            referrers=_referrers(refs),
            traffic_available=True,
        )
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", type(exc).__name__)
        logger.info("growth repo metrics: %s traffic unavailable (%s)", snap.repo, status)
        return snap


async def github_repo_metrics(
    repo: str, *, day: str, client: httpx.AsyncClient | None = None
) -> RepoSnapshot:
    """The production ``RepoMetricsFn``. Raises when the repo itself cannot be read."""
    if not is_repo_slug(repo):
        raise ValueError("invalid repo slug")
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = f"{GITHUB_API}/repos/{repo}"
    own = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.get(base, headers=headers)
        resp.raise_for_status()
        info = resp.json()
        snap = RepoSnapshot(
            repo, day, stars=_int(info.get("stargazers_count")), forks=_int(info.get("forks_count"))
        )
        return await _traffic(client, base, headers, snap) if token else snap
    finally:
        if own:
            await client.aclose()


_PRODUCTION_REPO_METRICS_FN: RepoMetricsFn | None = None


def set_production_repo_metrics_fn(fn: RepoMetricsFn | None) -> None:
    global _PRODUCTION_REPO_METRICS_FN
    _PRODUCTION_REPO_METRICS_FN = fn


def resolve_repo_metrics_fn() -> RepoMetricsFn | None:
    return _PRODUCTION_REPO_METRICS_FN


__all__ = [
    "RepoMetricsFn",
    "RepoSnapshot",
    "github_repo_metrics",
    "is_repo_slug",
    "resolve_repo_metrics_fn",
    "set_production_repo_metrics_fn",
]
