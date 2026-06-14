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

The `pocketpaw-ee` **wheel ships compiled**: every module is built to a native
`.so` via Cython (hatch-cython) and the `.py`/`.c` source is excluded. An
installed enterprise package — including per-tenant deployments — therefore
carries no readable enterprise source. The MIT core stays readable source.

- Building the wheel (`uv build`, `pip install ./ee`, or the enterprise Docker
  stage) compiles automatically — no extra steps.
- **Development is unaffected:** an editable install (`uv sync --group ee`) uses
  the readable `.py` source; only the built wheel compiles.
- Requires a C toolchain at build time (the enterprise Docker image already
  ships `gcc`; CI runners include it).

See `[tool.hatch.build.targets.wheel.hooks.cython]` in `pyproject.toml`.

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
