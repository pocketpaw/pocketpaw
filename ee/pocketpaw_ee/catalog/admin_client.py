# ee/pocketpaw_ee/catalog/admin_client.py — async ADMIN client over the LiteLLM
# proxy's management API (MCG-8). The read-only catalog client lives in
# ``litellm_client.py``; this is its MUTATING sibling — it mints per-tenant
# virtual keys and reads per-key spend, authorized by the proxy MASTER key.
#
# Reuses the catalog's proxy base/key resolution (``catalog.config``) so the whole
# deployment has ONE proxy config path — the admin key is the same
# ``POCKETPAW_LITELLM_API_KEY`` (the master key) the read client already uses; it
# is the only key authorized for ``/key/*`` and ``/spend/*``.
#
# Endpoints wrapped:
#   * POST /key/generate     — mint a virtual key with max_budget / budget_duration
#                              / rpm_limit / tpm_limit / models / metadata. Returns
#                              the new ``key`` string (+ echoed fields).
#   * GET  /key/info?key=    — read back a key's live config + spend.
#   * GET  /spend/logs?api_key=<key> — the per-key spend rows (cost + token counts,
#                              incl. cached tokens). MUST pass ``api_key`` — an
#                              unfiltered /spend/logs scans the whole proxy and
#                              times out (noted in the MCG-8 task brief).
#   * GET  /user/daily/activity?start_date=&end_date=&api_key=<key> — DAILY usage
#                              for one virtual key, broken down BY MODEL (spend +
#                              token counts + request counts). Paginated; this
#                              client walks every page (follows ``metadata.has_more``)
#                              and returns the merged ``results`` list. Scoped by the
#                              ``api_key`` filter, called with the master key (which
#                              the proxy treats as admin view), so it returns exactly
#                              that tenant's daily activity. Powers the billing usage
#                              graph (the WorkspaceUsage transform in cloud.billing.usage).
#   * GET  /spend/logs/v2?end_user=&start_date=&end_date= — the spend rows for one
#                              CUSTOMER (LiteLLM's word for the ``user`` field on a
#                              request) rather than one virtual key. Date-bounded and
#                              paginated; this client walks every page. This is the
#                              read that makes chat billable: a chat request carries
#                              the deployment key, so the per-key read above cannot
#                              see it, and only the workspace id the request carries
#                              in its ``user`` field can.
#   * POST /key/delete       — revoke keys (used by the live-check teardown so a
#                              throwaway probe key never lingers on the proxy).
#
# httpx-based with an injectable ``_transport`` so tests stand in an
# httpx.MockTransport (no live proxy) — the same seam ``LiteLLMClient`` exposes.
# Fail-LOUD: a non-2xx raises ``LiteLLMAdminError`` so a broken provision surfaces
# rather than silently minting nothing / billing nothing.
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-8): the proxy admin client.
# Updated 2026-06-29 (feat/billing-usage-endpoint): added ``user_daily_activity`` —
#   the per-key DAILY usage read (GET /user/daily/activity) that backs the billing
#   usage graph. Walks pagination internally and returns the merged daily records.
# Updated 2026-09-02 (feat/proxy-spend-ingest-by-customer): added
#   ``spend_logs_by_end_user`` and ``spend_log_count`` over /spend/logs/v2. The
#   per-KEY read was the only spend read this client had, and chat never sends a
#   tenant key — it sends the deployment master key — so in ``live`` mode chat
#   billed zero for everyone while the proxy's own log showed real dollars. These
#   two read by the customer id the request carries instead, which is the only
#   attribution a chat row has. ``spend_log_count`` exists for the sweep's coverage
#   check: it asks for one row and reads the ``total``, which is how unattributed
#   spend becomes visible rather than merely uncharged.
# Updated 2026-09-02 (fix/bill-workspaces-the-sweep-cannot-see): added
#   ``list_customers`` (GET /customer/list). The spend reads above can only be
#   aimed at a customer id somebody already knows; every caller got that list from
#   our OWN provisioning table, so a workspace that spends without a provisioned
#   key was unreachable by any of them. This asks the PROXY who spent instead,
#   which is the only source that includes those workspaces.

# Updated 2026-09-04 (fix/litellm-spend-leaks): added ``spend_logs_window`` — the
# unfiltered, row-returning sibling of ``spend_logs_by_end_user``. The coverage
# check's counts can say how many rows no tenant claims but not WHAT they are, and
# the proxy's own dashboard / health-check traffic needs the opposite response from
# a caller that forgot to name a workspace. Hard-capped at ``max_rows``: a
# diagnostic must not be able to cost more than the billing it observes.

from __future__ import annotations

import logging
from typing import Any

import httpx

from pocketpaw_ee.catalog import config

logger = logging.getLogger(__name__)


class LiteLLMAdminError(Exception):
    """A LiteLLM proxy management call failed (non-2xx or malformed body). Raised
    so provisioning / spend ingestion fail loud rather than silently no-op."""


class LiteLLMAdminClient:
    """Thin async client over the LiteLLM proxy MANAGEMENT API (key + spend).

    Authorized by the proxy master key (``catalog.config.litellm_proxy_api_key``).
    Mutating sibling of the read-only ``LiteLLMClient`` — same base-URL + header +
    transport shape, different (privileged) endpoints.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        _transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = (base_url or config.litellm_proxy_url()).rstrip("/")
        # api_key explicitly passed wins; otherwise resolve the master key from env.
        self._api_key = api_key if api_key is not None else config.litellm_proxy_api_key()
        self._transport = _transport  # tests inject httpx.MockTransport
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._headers(),
            transport=self._transport,
            timeout=self._timeout,
        )

    @staticmethod
    def _json_or_raise(resp: httpx.Response, what: str) -> dict[str, Any]:
        """Return the JSON body of a 2xx response, or raise ``LiteLLMAdminError``.
        A non-2xx surfaces the status + (when present) the proxy error message so a
        bad-budget / bad-key / no-permission response is legible."""
        if resp.status_code // 100 != 2:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    err = body.get("error")
                    if isinstance(err, dict):
                        detail = str(err.get("message") or "")
                    elif isinstance(err, str):
                        detail = err
                    detail = detail or str(body.get("detail") or body.get("message") or "")
            except Exception:  # noqa: BLE001
                detail = ""
            raise LiteLLMAdminError(
                f"LiteLLM admin {what} returned {resp.status_code}"
                + (f" — {detail}" if detail else "")
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise LiteLLMAdminError(f"LiteLLM admin {what} returned non-JSON") from exc
        return body if isinstance(body, dict) else {"data": body}

    async def generate_key(
        self,
        *,
        key_alias: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        models: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /key/generate — mint a virtual key. Returns the proxy's JSON body
        (carries the new ``key`` string and echoes the budget / limit fields).

        Only non-None fields are sent so the proxy applies its own defaults for
        anything we leave unset. ``models=[]`` is sent as an explicit empty
        allowlist (all models) only when ``models`` is the empty list, not None.
        """
        payload: dict[str, Any] = {}
        if key_alias is not None:
            payload["key_alias"] = key_alias
        if max_budget is not None:
            payload["max_budget"] = max_budget
        if budget_duration is not None:
            payload["budget_duration"] = budget_duration
        if rpm_limit is not None:
            payload["rpm_limit"] = rpm_limit
        if tpm_limit is not None:
            payload["tpm_limit"] = tpm_limit
        if models is not None:
            payload["models"] = models
        if metadata is not None:
            payload["metadata"] = metadata

        async with self._client() as client:
            resp = await client.post(f"{self._base_url}/key/generate", json=payload)
        return self._json_or_raise(resp, "/key/generate")

    async def key_info(self, key: str) -> dict[str, Any]:
        """GET /key/info?key=<key> — read back a key's live config + spend."""
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/key/info", params={"key": key})
        return self._json_or_raise(resp, "/key/info")

    async def spend_logs(self, *, api_key: str) -> list[dict[str, Any]]:
        """GET /spend/logs?api_key=<key> — the spend rows for ONE virtual key.

        ``api_key`` is REQUIRED: an unfiltered /spend/logs scans the proxy's whole
        spend table and times out (per the MCG-8 brief). Returns the list of rows
        (each carries ``spend``, ``startTime``, token counts incl. cached, and a
        ``request_id``). A proxy that returns a bare list yields it directly; a
        dict-wrapped ``{"data": [...]}`` is unwrapped.
        """
        if not api_key:
            raise LiteLLMAdminError("spend_logs requires api_key (unfiltered scan times out)")
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/spend/logs", params={"api_key": api_key})
        body = self._json_or_raise(resp, "/spend/logs")
        # /spend/logs returns a top-level JSON array; _json_or_raise wraps a bare
        # list as {"data": [...]}. A dict response with a "data" list is handled
        # the same way. Keep only dict rows.
        data = body.get("data")
        rows = data if isinstance(data, list) else []
        return [r for r in rows if isinstance(r, dict)]

    async def user_daily_activity(
        self,
        *,
        start_date: str,
        end_date: str,
        api_key: str,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """GET /user/daily/activity — DAILY usage for ONE virtual key, by model.

        Returns the merged list of daily-activity records (LiteLLM ``DailySpendData``
        shape: each has a ``date``, a day-level ``metrics`` block, and a
        ``breakdown.models`` map keyed by model name, each value carrying
        ``metrics`` with ``spend`` (USD) + token + request counts). The caller
        (cloud.billing.usage) folds the per-model breakdown into the usage graph
        and converts spend USD -> credits.

        ``start_date`` / ``end_date`` are ``YYYY-MM-DD`` (the proxy 400s if either is
        missing). ``api_key`` REQUIRED — it filters the analytics to a single
        tenant's virtual key (the proxy supports a key filter on this route); calling
        with the master key gives the admin view so the filter is honoured. An empty
        ``api_key`` raises rather than returning every tenant's usage.

        PAGINATION: the route is paginated (default page_size 50, max 1000). This
        walks every page — following the response ``metadata.has_more`` flag,
        advancing ``page`` — and merges ``results`` so the caller gets the whole
        window. A defensive page ceiling stops a malformed proxy that never clears
        ``has_more`` from looping forever.
        """
        if not api_key:
            raise LiteLLMAdminError(
                "user_daily_activity requires api_key (an unscoped read returns every "
                "tenant's usage)"
            )

        results: list[dict[str, Any]] = []
        page = 1
        # Defensive ceiling: 1000 rows/page over a year is ~ a handful of pages; 200
        # pages (up to 200k daily rows) is far beyond any real window and bounds a
        # proxy bug that never clears has_more.
        max_pages = 200
        while page <= max_pages:
            params: dict[str, Any] = {
                "start_date": start_date,
                "end_date": end_date,
                "api_key": api_key,
                "page": page,
                "page_size": page_size,
            }
            async with self._client() as client:
                resp = await client.get(f"{self._base_url}/user/daily/activity", params=params)
            body = self._json_or_raise(resp, "/user/daily/activity")
            rows = body.get("results")
            if isinstance(rows, list):
                results.extend(r for r in rows if isinstance(r, dict))
            metadata = body.get("metadata")
            has_more = bool(metadata.get("has_more")) if isinstance(metadata, dict) else False
            if not has_more:
                break
            page += 1
        else:
            logger.warning(
                "LiteLLM /user/daily/activity never cleared has_more after %d pages — "
                "stopping (usage may be truncated)",
                max_pages,
            )
        return results

    async def _spend_logs_v2(
        self,
        *,
        start_date: str,
        end_date: str,
        end_user: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of GET /spend/logs/v2. Returns ``(rows, total_matching)``.

        ``/spend/logs/v2`` is the paginated, date-bounded successor to
        ``/spend/logs`` (which the proxy's own docstring now marks deprecated for
        exactly the reason our per-key read warns about — it is unpaginated and
        scans). It is also the only spend route that filters on ``end_user``.

        Dates are ``YYYY-MM-DD HH:MM:SS`` or ``YYYY-MM-DD``; BOTH are required —
        the proxy 400s without them, so there is no unbounded form of this call to
        fall into by accident.
        """
        params: dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
            "page": page,
            "page_size": page_size,
            # Oldest first, so a caller that stops early keeps a contiguous
            # prefix rather than a hole in the middle of its window.
            "sort_by": "startTime",
            "sort_order": "asc",
        }
        if end_user is not None:
            params["end_user"] = end_user

        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/spend/logs/v2", params=params)
        body = self._json_or_raise(resp, "/spend/logs/v2")

        data = body.get("data")
        rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        try:
            total = int(body["total"])
        except (KeyError, TypeError, ValueError):
            # A proxy build that omits or mangles ``total``. Fall back to what this
            # page actually returned rather than to zero: zero reads as an empty
            # window, which would make the coverage check report perfect attribution
            # over data it never saw — the exact shape of the bug it exists to catch.
            total = len(rows)
        return rows, total

    async def spend_logs_by_end_user(
        self,
        *,
        end_user: str,
        start_date: str,
        end_date: str,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """The spend rows one CUSTOMER accrued over a window, across every key.

        ``end_user`` is the value the request carried in its ``user`` field — for
        us, the workspace id. This is the read that makes a chat run billable:
        chat authenticates with the deployment's master key, so ``spend_logs``
        (filtered by a tenant's virtual key) returns nothing for it no matter how
        much it cost.

        Rows carry the same fields the per-key read returns and the ingest already
        parses — ``request_id``, ``spend``, ``startTime``, ``model``, token counts.
        They do NOT carry the nested ``prompt_tokens_details``, so the cached-token
        figure degrades to zero on this path; that number is reporting, not money,
        and the charge comes from ``spend``.

        Walks every page. ``page_size`` is capped at 100 by the proxy.
        """
        if not end_user:
            raise LiteLLMAdminError(
                "spend_logs_by_end_user requires end_user (an unscoped read returns "
                "every tenant's spend)"
            )

        rows: list[dict[str, Any]] = []
        page = 1
        # Defensive ceiling. 100 rows/page x 200 pages is 20k spend rows for ONE
        # workspace in ONE window — far past any real sweep, and it bounds a proxy
        # that misreports ``total``.
        max_pages = 200
        while page <= max_pages:
            batch, total = await self._spend_logs_v2(
                start_date=start_date,
                end_date=end_date,
                end_user=end_user,
                page=page,
                page_size=page_size,
            )
            rows.extend(batch)
            if not batch or len(rows) >= total:
                break
            page += 1
        else:
            logger.warning(
                "LiteLLM /spend/logs/v2 still had pages for end_user=%s after %d — "
                "stopping (spend may be truncated and will be re-read next sweep)",
                end_user,
                max_pages,
            )
        return rows

    async def spend_logs_window(
        self,
        *,
        start_date: str,
        end_date: str,
        page_size: int = 100,
        max_rows: int = 1000,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Every spend row in a window, whoever it belongs to. Returns ``(rows, complete)``.

        The unfiltered sibling of ``spend_logs_by_end_user``, and the only read that
        can answer WHAT an unattributed row was. The counts the coverage check runs
        every tick say how many rows no tenant claims; they cannot say whether those
        are a caller that forgot to name a workspace or the proxy's own dashboard
        poking at a model, and those need opposite responses.

        ``max_rows`` is a hard ceiling, and ``complete`` is False when it was hit.
        This is deliberately NOT the paging-to-exhaustion loop the per-customer read
        uses: this one is unfiltered, so on a busy proxy the window is unbounded, and
        a diagnostic must never be able to cost more than the billing it observes. A
        truncated read still classifies a representative prefix (rows come back
        oldest-first) and the caller reports it as partial rather than as a finding.
        """
        rows: list[dict[str, Any]] = []
        page = 1
        total = 0
        while len(rows) < max_rows:
            batch, total = await self._spend_logs_v2(
                start_date=start_date,
                end_date=end_date,
                end_user=None,
                page=page,
                page_size=min(page_size, max_rows - len(rows)),
            )
            rows.extend(batch)
            if not batch or len(rows) >= total:
                break
            page += 1
        return rows[:max_rows], len(rows) >= total

    async def spend_log_count(
        self,
        *,
        start_date: str,
        end_date: str,
        end_user: str | None = None,
    ) -> int:
        """How many spend rows a window holds, optionally for one customer.

        Asks for a single row and reads the ``total`` the proxy reports, so the
        cost is one small request whatever the window holds. The sweep's coverage
        check subtracts the per-customer counts from the unfiltered one; what is
        left is spend no workspace claimed, which is the failure mode that
        otherwise presents as silence.
        """
        _rows, total = await self._spend_logs_v2(
            start_date=start_date,
            end_date=end_date,
            end_user=end_user,
            page=1,
            page_size=1,
        )
        return total

    async def list_customers(self) -> list[str]:
        """Every customer id the proxy has recorded spend against.

        ``GET /customer/list``. A customer is what LiteLLM calls the ``user`` field
        on a request body, which for us is the workspace that should pay — so this
        is the proxy's own answer to "who has been spending", independent of
        anything we provisioned.

        That independence is the point. Every other spend read here takes a
        customer id or a virtual key from the caller, and the only list of those we
        kept was our provisioning table. A workspace that never minted a key is
        absent from that table, so its spend was unreadable and therefore unbilled,
        while the coverage check reported it as untagged. Both symptoms came from
        asking ourselves who the tenants were instead of asking the proxy.

        Ids only. The endpoint also returns a lifetime ``spend`` per customer, but
        that is a running total the proxy never resets, so billing off it would
        re-charge history on every sweep; the per-window row reads stay the source
        of truth for money.
        """
        async with self._client() as client:
            resp = await client.get(f"{self._base_url}/customer/list")

        # This route answers with a bare JSON list, not the ``{"data": [...]}``
        # envelope the /spend routes use.
        if resp.status_code // 100 != 2:
            raise LiteLLMAdminError(f"LiteLLM admin /customer/list returned {resp.status_code}")
        try:
            body = resp.json()
        except Exception as exc:
            raise LiteLLMAdminError(
                "LiteLLM admin /customer/list returned a malformed body"
            ) from exc

        if isinstance(body, list):
            rows: list[Any] = body
        elif isinstance(body, dict) and isinstance(body.get("data"), list):
            rows = body["data"]
        else:
            return []

        customers: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # ``user_id`` is LiteLLM's column name for the customer id. Blank ids
            # are dropped rather than passed on: an empty string is a legal Mongo
            # query that matches no workspace, so it would read as a tenant with
            # no spend forever instead of as the malformed row it is.
            cid = str(row.get("user_id") or "").strip()
            if cid:
                customers.append(cid)
        return customers

    async def delete_keys(self, keys: list[str]) -> dict[str, Any]:
        """POST /key/delete — revoke the given virtual keys. Used by the live-check
        teardown so a throwaway probe key is never left on the proxy."""
        async with self._client() as client:
            resp = await client.post(f"{self._base_url}/key/delete", json={"keys": keys})
        return self._json_or_raise(resp, "/key/delete")


__all__ = [
    "LiteLLMAdminClient",
    "LiteLLMAdminError",
]
