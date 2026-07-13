# PocketPaw Enterprise (`pocketpaw-ee`)

The enterprise layer for [PocketPaw](https://github.com/pocketpaw/pocketpaw) —
multi-tenant cloud, authentication, rooms, messaging, billing, knowledge base,
file storage, fleet, instinct, and the pocket specialist.

`pocketpaw-ee` is a thin package: it contains only `pocketpaw_ee/` and depends
on the OSS core package [`pocketpaw`](https://pypi.org/project/pocketpaw/). When
installed alongside the core, it activates via `pocketpaw`'s entry-point
extension registry — the core discovers it automatically, no wiring required.

## Install

```bash
# Core only (MIT, no enterprise code on disk)
pip install pocketpaw

# Core + enterprise
pip install pocketpaw pocketpaw-ee
```

`pocketpaw-ee` pins the exact `pocketpaw` version it ships with — the two are
released lockstep.

## Compiled distribution (source protection)

The **enterprise Docker image ships `pocketpaw_ee` compiled**: every module is a
native `.so` built via Cython (hatch-cython) with the `.py`/`.c` source stripped,
so a per-tenant deployment carries no readable enterprise source. The MIT core
stays readable.

The compile is **gated OFF by default** so it does not fire on every ee install:

- **Dev / CI / editable installs stay readable and fast.** `uv sync --group ee`,
  `pip install ./ee`, and a plain `uv build` all skip the compile (~15s) and use
  the readable `.py` source. The ~25-min compile does **not** run here.
- **The source-free compiled wheel is a deliberate release build**, produced by
  `ee/scripts/build_compiled_wheel.sh`: it compiles with the hook on
  (`HATCH_BUILD_HOOK_ENABLE_CYTHON=1 uv build --wheel`), then strips the source in
  a post-build step, regenerating `RECORD` with the `wheel` tool. The result
  contains only `.so` plus data files — no `.py`/`.c`.
- **The enterprise Docker image runs that script** and installs the stripped
  wheel, so the shipped image carries no readable enterprise source.
- Source removal is **not** a static hatch `exclude` — that would empty the
  default readable wheel, and neither hatch-cython nor a build hook can drop
  source conditionally.
- The release build needs a C toolchain (`gcc`) and the `wheel` tool; the
  enterprise Docker stage provides both (`gcc` from the builder base; `uv` +
  `wheel` added for the script).

See `[tool.hatch.build.targets.wheel.hooks.cython]` in `pyproject.toml` and
`ee/scripts/build_compiled_wheel.sh`.

## License

`pocketpaw-ee` is licensed under the **Functional Source License, Version 1.1
(Apache-2.0 Future License)** — see [`LICENSE`](./LICENSE). This differs from the
MIT-licensed OSS core. The FSL grants full source access and permits use,
modification, and redistribution for any purpose that is not a competing
product; each release converts to Apache-2.0 two years after publication.

## Development

`pocketpaw-ee` lives in the `ee/` subdirectory of the PocketPaw backend
monorepo. For a full development environment (core + enterprise, editable):

```bash
cd backend
uv sync --dev               # installs the OSS core + dev tooling
uv pip install -e ./ee      # adds the enterprise layer (editable)
```

See `backend/CLAUDE.md` for the complete contributor workflow.
