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
       }
   }
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
  unpacked drafts (128 MB) and its token lookups (hits for 5 minutes, unknown
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
