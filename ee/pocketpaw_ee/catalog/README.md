<!-- ee/pocketpaw_ee/catalog/README.md — Model Catalog (MCG-1) overview +
     live-deploy captain handoff. Created 2026-06-26 (feat/mcg-1-catalog-api). -->

# Model Catalog (MCG-1)

A thin, license-gated read API over a self-hosted **LiteLLM proxy**. It exposes
the models the proxy serves, grouped by modality, to clients.

## Endpoints

- `GET /api/v1/catalog/models?modality=&provider=&q=&capability=` — filtered list
  (`{models: [...], total: N}`).
- `GET /api/v1/catalog/models/{id}` — one entry. `id` is the **URL-encoded**
  canonical key `"<provider>/<model>"` (the slash is `%2F`).

## How it works

1. `litellm_client.py` reads the proxy: `GET /v1/models` (routable ids) +
   `GET /model/info` (per-model metadata), and maps each row onto a
   `ModelCatalogEntry` (`models.py`). LiteLLM's `mode` → our `modality`;
   per-token cost → per-million-token `pricing`; `supports_*` flags →
   `capabilities`.
2. `models_dev_client.py` enriches `logo` / `description` / extra capabilities
   from `https://models.dev/api.json` — **best-effort, fail-open**. Any failure
   (or `CATALOG_MODELS_DEV_ENABLED=false`) falls back to LiteLLM-only data.
3. `service.py` assembles + **TTL-caches** the catalog in-process
   (`CATALOG_CACHE_TTL_SECONDS`, default 300s) and applies the filters.
   `service.bust_cache()` invalidates on demand.

**Catalog ⊇ routable:** the catalog is the union of `/model/info` and
`/v1/models`, so a served-but-undescribed model is still listed. v1 marks every
entry `status="available"` — `/model/info` exposes no per-model readiness signal
yet (see the MCG-1 note below).

## Configuration (env — see `.env.enterprise.example`)

| Var | Default | Meaning |
| --- | --- | --- |
| `POCKETPAW_LITELLM_API_BASE` | `http://localhost:4000` | proxy base URL |
| `POCKETPAW_LITELLM_API_KEY` | _(unset)_ | proxy admin/virtual key (Bearer) |
| `CATALOG_CACHE_TTL_SECONDS` | `300` | assembled-catalog TTL |
| `CATALOG_MODELS_DEV_ENABLED` | `true` | toggle models.dev enrichment |

## Known gaps (carried forward)

- **Non-text pricing is unmapped.** The mapper reads only per-token cost
  (`input_cost_per_token`/`output_cost_per_token`), so image / audio / video
  models report `pricing=None` even when the proxy knows a per-image or
  per-second cost. Fails soft (the field is optional). Fix belongs with the
  multi-modal execution slices (MCG-6/7), which must map the per-unit cost
  fields into a richer `Pricing` before the picker surfaces non-text prices.
- **Readiness/status.** `/model/info` exposes no per-model readiness signal, so
  v1 marks every entry `status="available"`. The `status` field + the
  catalog⊇routable union are in place for a future readiness probe to flip
  un-keyed models to `"disabled"`.
- **`streaming` capability is a heuristic** (assumed for all chat models), not
  read from `/model/info`.

## Live deploy — CAPTAIN HANDOFF

This slice ships the **app-side catalog API** and a **reference proxy config**
(`litellm.config.example.yaml`). Standing up the live LiteLLM proxy — wiring it
into the Coolify deploy, supplying real provider keys + Postgres/Redis, and
pointing `POCKETPAW_LITELLM_API_BASE` / `POCKETPAW_LITELLM_API_KEY` at it — is a **captain
handoff**; the controller has the box and the credentials. Until the live proxy
is reachable, the catalog endpoints return `502` (source of truth unreachable),
which is the intended behavior, not a bug.
