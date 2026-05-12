# pocketpaw-scanner

Native Rust extension for PocketPaw security scanning, built with PyO3 + maturin.

## Install

Install this package into the same virtual environment as PocketPaw so the runtime can import
`pocketpaw_scanner` instead of falling back to the Python-only path.

### Prerequisites

- Python 3.11+
- `uv`
- Rust toolchain (`rustup`, `cargo`)

### From a fresh clone

From the repository root:

```bash
uv pip install -e ./pocketpaw-scanner
```

That builds the native extension in-place and makes it available to the active environment.

### One-off check without installing

```bash
uv run --with ./pocketpaw-scanner python -c "import pocketpaw_scanner; print('native scanner ok')"
```

If the package is not installed in the interpreter you are running, PocketPaw will log that the
native scanner is unavailable and continue with the Python fallback.

## Modules

- `injection_detect`: prompt/command/SQL-style injection pattern detection.
- `pii_detect`: regex-based PII scanning with match ranges.
- `content_classify`: lightweight rule-based `safe` / `spam` / `toxic` classification.

## Build

```bash
uvx maturin develop --manifest-path pocketpaw-scanner/Cargo.toml
```

## Run parity tests

```bash
uv run pytest tests/test_native_scanner_parity.py -q
```

## Run benchmark suite

```bash
uv run python pocketpaw-scanner/benchmarks/benchmark_suite.py --iterations 20000 --require-speedup 5
```
