# PocketPaw Enterprise Extensions (ee/)
# Licensed under FSL 1.1 — see ee/LICENSE
# These features require a PocketPaw Enterprise license for production use.
# Updated: 2026-03-30 — Added api.py singleton for instinct_tools bridge.
#
# Modules:
#   api.py       — Singleton accessors (get_instinct_store)
#   fabric/      — Ontology layer (objects, links, properties)
#   instinct/    — Decision pipeline (actions, approvals, audit)
#   automations/ — Time/data triggers
#   audit/       — Enhanced compliance logging
#
# Updated: 2026-06-14 (feat/ee-cython-compile) — when ee ships Cython-compiled
# (.so, for per-tenant source protection), plain methods on Pydantic models
# become `cython_function_or_method`, which Pydantic v2 doesn't recognize as a
# method. The patch below teaches Pydantic to ignore that type. It runs on
# `import pocketpaw_ee` — before any submodule (and its models) imports — and is
# a no-op when running from readable source (dev/editable).


def _patch_pydantic_for_cython() -> None:
    try:
        import pydantic._internal._model_construction as mc
    except Exception:
        return
    import types

    def _probe() -> None:  # compiled → cython_function_or_method; source → FunctionType
        pass

    cyfunc_type = type(_probe)
    if cyfunc_type is types.FunctionType:
        return  # running from source — Pydantic already ignores normal methods
    orig = mc.default_ignored_types
    if getattr(orig, "_paw_cython_patched", False):
        return

    def _patched() -> tuple:
        return orig() + (cyfunc_type,)

    _patched._paw_cython_patched = True
    mc.default_ignored_types = _patched


_patch_pydantic_for_cython()
