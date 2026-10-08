<!-- Operator setup for the Paw Sites draft preview origin: the wildcard host that
     serves unpublished site drafts to the builder. -->
# Sites: the draft preview origin

The site builder previews a draft from a real web origin, not from an iframe
`srcdoc`. Every draft gets its own host:

```
https://<token>.<preview host>/index.html
```

`<token>` is 32 lowercase hex characters, random, minted per (pocket, content
hash). An edit produces a new content hash and so a new URL; the old URL keeps
serving its own build until the artifact store evicts it, then it 404s. The token
sits in the subdomain rather than the path because built sites reference their
assets root-absolutely (`/assets/index-9f8e.js`, `/_app/immutable/...`), and those
only resolve correctly when the draft owns the whole origin.

`GET /api/v1/sites/by-pocket/{id}/native-artifact` returns the URL as
`preview_url` (see `docs/api-reference.md`). The builder appends `?paw_edit=1` to
arm the edit bridge.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PAW_SITES_PREVIEW_BASE_URL` | `http://preview.localhost:<web port>` | Scheme, host and optional port of the preview origin. Drafts are served from subdomains of this host. |

The base is checked at startup. It is refused, and draft previews are turned off
with an `ERROR` log line ("draft previews are DISABLED"), when it has no `http://`
or `https://` scheme, has no dotted host, carries a path, or when its host equals
or is a parent of the app's own host (`POCKETPAW_PUBLIC_BASE_URL`,
`POCKETPAW_FRONTEND_BASE_URL`, `PAW_SITES_BUILDER_ORIGIN`). That last case matters:
with a base of `example.com` and an API on `api.example.com`, every API request
would be routed to the preview app. A refused base never takes the API down; the
`native-artifact` response just carries `preview_url: null`.

The local default needs no setup in Chromium-based browsers, which resolve
`*.localhost` to loopback, and the API process serves preview hosts itself (see
below). Browsers that don't resolve `*.localhost` need a hosts entry per draft or a
real base URL.

## How requests reach it

The API process routes by `Host`. `PreviewHostDispatch` sends any request whose
`Host` is `<label>.<preview host>` to `pocketpaw_ee.sites.preview_origin.preview_app`
and everything else to the API as usual. It is the outermost middleware in both app
factories (`mount_cloud()` lists it in `app.state.outermost_middleware` and
`install_cors()` adds it after CORS), so a preview request never meets CORS, the
body-size limit, auth, rate limits, CSRF or request logging, and never touches a
session or a cookie. A self-hosted install needs one process, no extra service.

The files come from the native-artifact store (`PAW_SITES_ARTIFACT_STORE`), so on a
multi-replica deploy set `PAW_SITES_ARTIFACT_STORE=s3` and any replica can serve any
draft.

## Production setup

1. **Pick a separate registrable domain.** Use something like
   `paw-preview.example` rather than `preview.app.example.com`. Draft pages run the
   author's code (and the agent's), and a cookie your app sets with
   `Domain=.example.com` would be sent to every draft under that domain. A separate
   registrable domain means no app cookie can ever reach a preview host, and no
   draft can set a cookie the app will read.
2. **Wildcard DNS.** Point `*.paw-preview.example` (an `A`/`AAAA` or `CNAME`
   record) at the same load balancer or proxy that serves the API.
3. **Wildcard TLS.** Issue a certificate for `*.paw-preview.example`. With Let's
   Encrypt that means a DNS-01 challenge; HTTP-01 cannot issue wildcards.
4. **Proxy the wildcard to the API upstream and keep the Host header.** nginx:

   ```nginx
   server {
       listen 443 ssl;
       server_name *.paw-preview.example;
       ssl_certificate     /etc/ssl/paw-preview/fullchain.pem;
       ssl_certificate_key /etc/ssl/paw-preview/privkey.pem;

       location / {
           proxy_pass http://pocketpaw_api;   # same upstream as the API host
           proxy_set_header Host $host;       # the token lives in the Host
           proxy_set_header X-Forwarded-Proto $scheme;
           # WebSockets to project drafts (see "Draft Workers" below)
           proxy_http_version 1.1;
           proxy_set_header Upgrade $http_upgrade;
           proxy_set_header Connection $connection_upgrade;
           proxy_read_timeout 3600s;
       }
   }
   # in the http block:
   map $http_upgrade $connection_upgrade { default upgrade; '' close; }
   ```

   Caddy (forwards the original Host by default):

   ```caddy
   *.paw-preview.example {
       tls {
           dns <provider>
       }
       reverse_proxy pocketpaw:8888
   }
   ```

   If the proxy caches responses, put `$host` in the cache key
   (`proxy_cache_key "$scheme$host$request_uri";`). Every draft serves the same
   paths (`/index.html`, `/assets/...`), so a key without the host would hand one
   draft's files to another.

   The paw-enterprise container's `deploy/nginx.conf` only serves the SPA; it does
   not proxy the API, so it needs no change. If you front the dashboard with
   `PAW_COI` (cross-origin isolation), the preview responses already carry
   `Cross-Origin-Resource-Policy: cross-origin`, so the builder can still frame them.
5. **Set the variable** on the API service:
   `PAW_SITES_PREVIEW_BASE_URL=https://paw-preview.example`.
6. **Keep `PAW_SITES_BUILDER_ORIGIN` set** to the dashboard origin. The edit bridge
   inside a draft posts only to that origin.
7. **On S3, add a bucket lifecycle rule.** The filesystem store keeps the newest
   two content hashes per pocket and deletes the rest; the S3 store never deletes.
   Expire old drafts with a rule on the artifact prefix, for example: expire
   objects under `site-artifacts/` 30 days after creation. That removes the files
   (`<pocket>/<hash>.dist.tgz`), the forward token key (`<pocket>/<hash>.token`)
   and the reverse pointers (`_preview_tokens/<token>.json`) alike. A token
   resolves only while its forward key still names it, so an expired draft is a
   404 even if a pointer outlives it. An expired draft that is viewed again in the
   builder is rebuilt and gets a new URL.

## What the preview origin does and does not do

- Serves `GET`/`HEAD` only. `OPTIONS` answers `204`; anything else is `405`.
  (A draft proxied to its draft Worker is the exception; see below.)
- Never reads cookies or `Authorization`, never sets a cookie. The token is the
  only credential, so treat a preview URL like a share link. There is no
  revocation: unsharing or deleting the pocket does not invalidate a URL that was
  already handed out. It keeps serving that draft until the store evicts it (the
  filesystem store's per-pocket retention, or the S3 lifecycle rule above). If a
  link leaks, the remedy is to delete the draft's objects from the store.
- Every response carries `Access-Control-Allow-Origin: *`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (the token
  stays out of `Referer` headers sent to CDNs the draft loads from) and
  `X-Robots-Tag: noindex, nofollow`. There is no `X-Frame-Options` or
  `frame-ancestors`, because the builder frames it.
- Content types come from an explicit table (`.js`/`.mjs` are
  `application/javascript`). Content-addressed files (`_app/immutable/`, or a
  filename carrying a bundler hash such as `index-BvK3x9_a.js`) are
  `Cache-Control: public, max-age=31536000, immutable`; everything else, including
  un-hashed files under `assets/`, is `private, no-cache`; errors are `no-store`.
- One draft unpacks to at most 64 MB and 20,000 files. Each process keeps up to 8
  unpacked drafts (128 MB) and its token lookups (hits for 60 seconds, unknown
  tokens for 30 seconds) in memory, and concurrent requests for one draft share a
  single store read.
- Path resolution matches the published site: `path`, then `path/index.html`, then
  `path.html`, then `404.html` (status 404). With no `404.html`, an extensionless
  path falls back to `index.html` for client-routed apps. An unknown token is a
  plain 404.
- html drafts are not built. Their source files are served as they are, with the
  declared packages' import map injected into each page. The `?paw_edit=1` variant
  carries the edit bridge and, when the paw-sites toolchain is available on the API
  host, the same `data-uid` stamping `/html-armed-source` uses.

## Draft Workers (server code in drafts)

Off by default. With `PAW_SITES_DRAFT_WORKERS=1` and the account project deploy
target (`PAW_SITES_PROJECT_DEPLOY_TARGET=account`, or `PAW_CF_DEPLOY_MODE=workers`),
a `project` build with server code also deploys as an account-level **draft
Worker**, `paw-draft-[<env tag>-]<pocket id>-<random>`, and its preview URL is
reverse-proxied to it. API routes, SSR pages and auth then run in the draft, and
next / sveltekit drafts get a preview at all. The workers.dev address is never
handed out. Needs `CLOUD_ENCRYPTION_KEY` (the per-draft keys are stored encrypted);
without it draft Workers stay off.

- **Data.** The draft binds its own D1 (`paw-draft-<pocket id>`), KV and R2,
  never the published site's. Migrations apply with destructive changes allowed; a
  changed applied migration recreates the draft database. Top-level `seed/*.sql`
  runs once on a fresh draft database.
- **Secrets.** Production secret values are never bound to a draft. A draft gets
  only the owner's `NAME__DRAFT` secrets (bound as `NAME`), its own
  `BETTER_AUTH_SECRET` / `AUTH_SECRET` / `SESSION_SECRET`, and `BETTER_AUTH_URL` /
  `PAW_SITE_URL` set to its preview URL. A build that requires a secret with no
  `NAME__DRAFT` value falls back (`draft_worker:secrets_missing`), and the agent
  is told which `NAME__DRAFT` names to ask the owner for.
- **Guard.** The draft's entry module is a small wrapper that answers 404 unless
  the request carries `X-Paw-Draft-Key` equal to the draft's own random key, which
  only the preview proxy sends, so the workers.dev address is useless on its own.
  Static assets the platform serves before the Worker runs are not covered by it.
- **Proxy.** Every method is forwarded, the request body up to
  `PAW_SITES_DRAFT_MAX_BODY` (10 MiB). A request target that does not start with
  `/` is a 400. HTML gets the runtime-error reporter, and the edit bridge under
  `?paw_edit=1`. A URL of an older build of a proxied draft is a 404.
- **WebSockets.** Proxied for a draft with a live draft Worker, under the same
  target, header, draft-key and cookie rules (the upstream's handshake
  `Set-Cookie` is mapped like any other). Every other preview host refuses them
  before accept: an unknown, static or superseded token closes with 4404 (an HTTP
  404 when the server supports `websocket.http.response`), a target without a
  leading `/` with 1008. The browser `Origin` must be the draft's own preview
  origin or the builder origin (`PAW_SITES_BUILDER_ORIGIN`, or the origin the
  editor was last opened from); anything else closes with 4403 before the draft
  is dialed. Nothing is accepted until the draft Worker accepts, and its
  subprotocol is mirrored. Close codes and reasons pass both ways. Caps:
  - `PAW_SITES_DRAFT_WS_MAX_MSG`: largest message either way, default 64 KiB
    (close 1009);
  - `PAW_SITES_DRAFT_WS_PER_TOKEN`: open connections per preview token, default
    60 (refused with 1013). Counted per API process, so with N replicas a token
    can hold up to N times this;
  - `PAW_SITES_DRAFT_WS_RATE`: browser messages per second per connection,
    default 50 (close 1008);
  - `PAW_SITES_DRAFT_WS_IDLE_SECONDS` (default 300) and
    `PAW_SITES_DRAFT_WS_LIFETIME_SECONDS` (default 3600) close with 1001.

  The ingress in front of the preview host must pass `Upgrade` (the nginx
  example above does) and keep idle connections open at least as long as the
  idle cap.
- **Cookies.** Every cookie a draft sets reaches the browser as a host-only
  `__Host-` cookie (`Path=/; Secure; SameSite=None; Partitioned`, so sign-in works
  inside the builder iframe): an app's own `__Host-` names stay, any other name `n`
  becomes `__Host-paw~n`. Only `__Host-` cookies are sent back to the draft
  (`__Host-paw~n` as `n`). The preview base host is not on the Public Suffix List,
  so any draft can set `Domain=<preview host>` cookies on every other draft; those
  can never carry the `__Host-` prefix, so they are never forwarded. Recommended
  later: serve previews from their own registrable domain (on the PSL, or one domain
  per draft) so the browser isolates drafts itself.
- **Limits.** The site's plan caps (`PAW_SITES_DRAFT_CPU_MS`,
  `PAW_SITES_DRAFT_SUBREQUESTS` override them), Workers Logs at
  `PAW_SITES_DRAFT_OBSERVABILITY_SAMPLE` (default 1.0), no Smart Placement.
- **Script cap.** A new draft reserves a slot under `PAW_SITES_DRAFT_SCRIPT_CAP`
  (default 450) account Worker scripts and gives it back if its deploy fails; it is
  refused when the cap is reached or the count cannot be read. Cloudflare allows
  500 per Paid account. Each API process caches the count for a minute, so several
  replicas can still overshoot by what they create inside that window.
- **Shared accounts.** Set `PAW_SITES_DRAFT_ENV_TAG` (up to 8 of `a-z0-9`) when
  more than one deployment (staging, production) uses the same Cloudflare account.
  It goes into every draft script and database name, and the orphan sweep only
  deletes `paw-draft-*` scripts carrying this deployment's tag (no tag: only
  untagged ones).
- **Races.** A publish or delete that lands while a draft is deploying wins: the
  deploy writes its registry row only by compare-and-set, re-checks that the
  pocket and site still exist before uploading, and deletes what it uploaded if
  it lost.
- **Fallback.** A draft Worker failure never fails the build: the preview falls
  back to `static` (or `server_only`) and the build record's `draft_worker_reason`
  says why (`draft_worker:cap`, `secrets_missing`, `deploy_failed`, ...).
- **Cleanup.** A successful publish deletes every draft of the pocket: the draft
  Worker, its D1 / KV / R2, and every preview token and draft file set, so old
  preview URLs stop serving within a minute. Site delete and pocket delete do the
  same. Cloudflare 404s count as done; anything that fails is retried by the
  `sweep_draft_workers` sweep, which also removes drafts idle for
  `PAW_SITES_DRAFT_TTL_DAYS` (default 7) and unregistered `paw-draft-*` scripts.
