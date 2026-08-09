<!-- Verdict for SG-10 of the paw-sites greenfield proving program: preview a site's
     BUILT STATIC ARTIFACT, with no runtime. Written 2026-08-10 alongside
     ee/pocketpaw_ee/sites/artifact_preview.py. Records what the static path covers,
     what it does not, why the in-browser Node runtime is parked rather than solved,
     and the licence trap that must not be repeated.
     Revised 2026-08-10, same day, for two things that landed after the first draft:
     the captain's ruling that Daytona is a BUILD host only (so dev_server.py stays and
     the footprint question is unanswered rather than solved), and path traversal, which
     the original brief omitted and which nothing else in the program covers -- SG-11's
     checklist does not include it. -->

# Previewing a built site artifact

## What changed about the question

SG-10 was originally "preview a site by booting a Node runtime in the browser" — a
StackBlitz WebContainer session running a dev server for the site project. Two things
retired that framing before any of it was built.

**The build left the request path.** Every source-engine site now builds in an ephemeral
Daytona sandbox that deletes itself, and only the static output comes back.
`daytona_build.artifact_tar_command` packs exactly `static_output_rel(engine)` and
nothing else, so `node_modules` is excluded by construction rather than by a filter.
Measured against the live lane on 2026-08-09:

| engine | artifact | entries | wall clock | `node_modules` | `_worker.js` |
|--------|----------|---------|-----------|----------------|--------------|
| react  | 61,487 B | 4       | 8.70s     | absent         | absent       |
| svelte | 33,104 B | 24      | 14.67s    | absent         | present (4,335 B) |

WebContainer answers "run a dev server for a project with dependencies, in the browser".
Serving an already-built static tree is not that question. It needs no Node at all.

**Choosing static removes constraints instead of adding them.** The in-tab runtime needs
cross-origin isolation. COEP `require-corp` blocks presigned-S3 images, which is exactly
what the sites gallery cards render, so turning it on breaks the surface the preview
lives on. `credentialless` avoids that but is Chromium/Firefox only, so Safari lands back
on `require-corp` and the same breakage. The static path sets no COOP or COEP header, and
a test asserts neither appears on a preview response.

## What this path does

`ee/pocketpaw_ee/sites/artifact_preview.py` unpacks a build artifact and answers HTTP
requests against the unpacked tree. `local_server.py`'s handler grew a second branch:
`/<site_id>/...` is unchanged plain static serving of a local deploy, and
`/_preview/<site_id>/...` routes through `artifact_preview.resolve`.

It rides the server that already exists rather than standing up a second one. The
established in-app preview pattern here is already "a localhost HTTP server, iframed by
the builder" — `dev_server.py` does that with Vite — so the artifact preview is the same
shape with the dev server removed.

The module carries no per-engine layout knowledge, and that is not an oversight. The lane
tars with `-C <static_output_rel(engine)> .`, so react's `dist` and svelte's
`.svelte-kit/cloudflare` both arrive as a tree whose root is the deployable root. The
artifact is already engine-normalized at the point it is packed. The engine is still
consulted, through `engines.py` predicates rather than string compares, for the two things
it genuinely decides: `static_output_rel(engine) == "."` means the engine runs no build
and has no artifact to preview (so `html` is refused, mirroring the same guard in
`artifact_tar_command`), and `emits_server_worker(engine)` says whether a `_worker.js` in
the artifact is expected.

### The three edge cases, and what was decided

**`_worker.js` is skipped at unpack and refused at resolve.** svelte emits one even with
no server routes at all; `adapter-cloudflare` emits a Server shell regardless, confirmed
by deleting `src/routes/api/`, wiping `.svelte-kit` and rebuilding clean. So the preview
does two things with it. It never writes it to disk, so the file does not sit inside a
tree that gets served. And it refuses any request for it — including a directory-shaped
`_worker.js/chunks/0.js`, which is what `adapter-cloudflare` emits for larger apps.

The refusal is a 404 rather than a 403, and that is deliberate. A static preview genuinely
has no server entry to offer, and a 403 would confirm the file is there. The reason to
care is not tidiness: on the svelte track that bundle has carried a substituted per-site
`__CAPTURE_SIGNED_KEY__`, so returning its source would be a secret disclosure. Its
presence must not break anything else, and it does not — the index and the rest of the
`_app/` tree serve normally beside it.

`_routes.json` and `.assetsignore` get the same treatment for a weaker reason: they are
deploy configuration, not site content.

**`/api/submit` is refused visibly, and cannot be faked.** Lead capture is deferred, so
there is no submit endpoint to serve. A form POST that appeared to succeed would be worse
than an error, because the message would be lost silently. Any request under `api/`
returns 501 with a page that says form submissions are not handled in preview and that
publishing enables them. Any non-GET method returns 405 the same way, which covers the
form-with-no-action case that posts back to the page itself. Nothing in the module can
return 2xx for a write, and both refusals are checked before the module looks at whether
an artifact is even stored — the honest answer to a form POST does not depend on that.

**Two engine shapes, no hardcoded layout.** Covered above. The tests use tarballs shaped
like both measured artifacts (4 entries and 24 entries with a worker) rather than one,
because a preview that only ever saw react's shape would pass while being wrong about
svelte's.

### Path traversal, at both ends

A static file server over an extracted archive has two traversal surfaces, not one, and
both are guarded.

**At extract time.** A member named `../../etc/passwd`, or an absolute path, a drive
letter, a backslash, or a symlink, escapes when it is written, regardless of how careful
the request side is. `tarfile` has historically been unsafe by default here, so nothing in
this module calls `extractall`: members are written one at a time after their names are
validated, and archive permissions are discarded so nothing can arrive executable. Links
are refused rather than followed, hardlinks included — a hardlink to a member that *is* in
the archive extracts to real content, so without a member-type check it would be written
like any other file.

**At request time.** The path is percent-decoded first (`..%2f..%2f` only looks safe
before decoding), validated segment by segment, and then — the part that matters —
resolved and compared against the resolved root. A prefix check on the raw string is
defeated by encoding and, more importantly, by a symlink, which no amount of string
inspection can see. Both sides are resolved, because the root itself can sit under a link:
a temp dir on macOS does (`/tmp` → `/private/tmp`), and comparing a resolved target
against an unresolved root would refuse every legitimate request there.

Two details worth writing down.

The parser and the containment check return **different reasons** (`unsafe_path` versus
`escaped_root`) for the same 404. That is not cosmetic. The first version of the traversal
tests asserted only the status, and a mutation that disabled the parser's `..` check still
passed, because containment refused the same request afterwards. The suite would have
shipped a broken parser guard. Asserting the specific reason is what makes each layer
independently provable.

And the NUL-byte case landed somewhere unexpected. A NUL in a member name **cannot ride a
tar archive at all** — the format's name field is NUL-terminated, so writing a member
called `./safe.html\0/../../escaped.txt` and reading the archive back yields
`./safe.html`; the tail is gone before any of our code sees it. The guard is kept because
refusing a NUL is a property of a name parser rather than of tar, but it is asserted
against the parser directly. A test that packed such a tarball would have looked like
proof and been none.

### Publish does not depend on preview

Demonstrated by test rather than asserted here. A local deploy is served, the preview is
then broken three ways — an unreadable artifact, no artifact, and a resolver that raises
mid-request — and the deployed site is re-fetched after each one and still answers 200.

Two mechanisms make that true. `safe_store_artifact` swallows every failure and returns
`None`, the way `screenshot.safe_take_*` does for card images, so a publish tail can call
it without risk. And the preview branch in the handler catches everything and answers 500
for that one request, because an exception escaping there would run on the same server
thread that serves live local deploys.

### The two roots are disjoint on purpose

Deploys live under `sites_home()`, which the server hands out as plain static files.
Previews live under `previews_home()` (`~/.pocketpaw/site-previews`, or
`PAW_SITES_PREVIEW_DIR`), which the server never serves directly. If a preview tree sat
inside `sites_home()`, the static branch would serve it as plain files with none of the
refusals applied, and the `_worker.js` refusal would have a bypass. Keeping the roots
disjoint is what makes the refusals structural instead of advisory, and a test asserts the
containment both ways.

## What this path does not cover

**It is not a cloud-reachable endpoint yet.** The preview is served by the localhost
static server, which is right for the proving phase and for local development, and is the
same shape `dev_server.py` already uses. Exposing it through the authed cloud surface is a
separate decision, and there is a real finding behind that: serving customer-authored HTML
from the app's own origin means the page runs with the app's origin. The obvious mitigation
is CSP `sandbox allow-scripts`, which puts the document in an opaque origin — but a
sandboxed document's site-for-cookies is empty, so `SameSite` cookies stop riding its
subresource requests and the page's own CSS and JS fail to load. The alternative is an
unauthenticated preview URL carrying a signed, expiring token, which is a security design
of its own. Neither should be picked as a side effect of this slice.

Worth recording while adjacent: the session cookie is `HttpOnly` (`cloud/auth/core.py`
pins `cookie_httponly=True`), so customer JavaScript in a preview cannot read it via
`document.cookie`. Cookies are not port-scoped, so a localhost preview shares a cookie
domain with a locally-running app — that is pre-existing on the `dev_server.py` path and
not introduced here.

**Nothing calls the build lane yet.** `daytona_runner.run_build` returns the artifact as
in-memory `bytes` on `BuildRunResult.artifact`, and the only caller today is
`scripts/sg9_daytona_roundtrip.py`. There is no S3 upload and no persistence in the sites
module. The preview therefore takes artifact bytes as its input and does not assume where
they came from: `local_server.serve_artifact_preview(site_id, artifact, engine=...)` is the
one-call wiring point for whichever caller lands first, whether that is the publish tail
handing over `result.artifact` directly or a later read from object storage.

**No SPA fallback.** A missing asset is a 404, never a rewrite to `index.html`. Rewriting
would report a broken build as a working page, which is the one failure mode a preview
whose job is verification must not have. A client-routed project that needs deep-link
previews would have to opt into a fallback explicitly.

**No directory listings.** A directory with no `index.html` is a 404. A listing would hand
out the build tree's shape to anyone holding the URL.

## Live-edit preview is parked, not solved — and `dev_server.py` stays

The one case this does not replace is a preview that reflects edits without a rebuild:
hot module reload against a running dev server. That needs a Node runtime somewhere, and
right now the only place it can be is the box.

**`dev_server.py` stays. It is not being retired, and nothing here proposes retiring it.**
The original slice promised a verdict that would gate its retirement; that retirement is
off the table. Two independent doors closed. Daytona is a **build host only** — the captain
ruled it cannot host a dev environment, and the ruling is structurally forced rather than a
preference: a dev server is long-lived by definition, while the Daytona lane makes every
sandbox ephemeral, strict-timeout and self-deleting in a `finally`. Hosting a dev env there
means either abandoning the self-delete rule, which reinstates the `auto_stop_interval`
billing landmine that rule exists to kill, or a dev server that vanishes mid-edit. No
configuration is both. And WebContainer, the other candidate, is parked on procurement
(below). With both closed, the on-box Vite process is the only live editing path, and it is
load-bearing.

**What moved and what did not.** *Viewing* a built site moved off the box — that is what
this slice did, and it is real. *Editing* did not move at all. Keeping `dev_server.py` is
the status quo rather than a regression, and edits are already fast: P2a replaced per-edit
rebuilds with HMR, so a hot reload is milliseconds.

What stays on the box is the **footprint**. Live editing means N long-lived Vite processes
plus N `node_modules` trees on a 4-CPU / 6G api container, at 100–200 users per tenant.
Moving that to the client was WebContainer's entire value, so **that question is now
unanswered, not solved** — and it is a capacity question, not a latency one. Anyone reading
this as "preview cost left the box" has it right for viewing and wrong for the half that
actually constrains scaling.

**The WebContainer blocker is procurement, not engineering.** A commercial plan carries a
session cap. The case that matters here is sessions booted by tenants' own end visitors
rather than by our authenticated seats, and that is exactly the kind vendors price
separately. Until someone asks StackBlitz and gets an answer in writing, in-tab live-edit
preview stays parked.

**The licence trap, stated plainly so it is not repeated.** `@webcontainer/api` declares
`"license": "MIT"` in its npm manifest. That covers the client library only. It does not
cover the hosted runtime, and it is not the commercial terms. It must never be cited as
permission to ship — not in code, not in a comment, not in a doc, not in a PR body, and
not in a customer conversation. Reading the manifest as a licence for the service is the
specific mistake this paragraph exists to prevent.

**There is no server-side preview fallback, of any kind.** The earlier spec routed
"in-tab WebContainer when the project qualifies, Daytona VM otherwise". The VM half is
gone with the ruling, and nothing in this slice reintroduces it: the preview path contains
no Daytona call and no VM concept. Daytona appears in this subsystem only as the place the
artifact was *built*, before it arrived.

## Verification

Unit rules in `tests/ee/sites/test_artifact_preview.py`; HTTP behaviour and the
publish-isolation proof in `tests/ee/sites/test_artifact_preview_server.py`. Mutation plan
at `tests/mutations/sites_artifact_preview.json` — 27 mutations, all caught.

Every traversal guard has its own mutation, deliberately one per guard rather than one per
condition: a combined `if a or b or c` reads as verified when only one of the three is, so
each refusal is a separate statement that can be removed on its own and watched to break
exactly one test. That covers the `..`, absolute-path, drive-letter, backslash and NUL
checks on both the member-name and request-path sides, the link-member refusal, and the
post-join containment check. The containment check is exercised by a real symlink planted
inside a preview tree; that test skips only where the OS refuses to create symlinks, which
is worth knowing because on such a platform its mutation would escape rather than fail.
