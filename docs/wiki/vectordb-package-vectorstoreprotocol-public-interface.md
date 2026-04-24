---
{
  "title": "vectordb Package: VectorStoreProtocol Public Interface",
  "summary": "The `vectordb` package's `__init__.py` re-exports `VectorStoreProtocol` as the single public symbol, giving callers a stable import path that is decoupled from the internal module layout. This thin re-export layer prevents callers from depending on the internal `protocol` module path directly and preserves refactoring freedom.",
  "concepts": [
    "VectorStoreProtocol",
    "__all__",
    "re-export",
    "package encapsulation",
    "public API surface",
    "vectordb",
    "dependency boundary",
    "module layout",
    "import path stability",
    "plugin authors"
  ],
  "categories": [
    "vectordb",
    "package structure"
  ],
  "source_docs": [
    "581232ee6afa0f86"
  ],
  "backlinks": null,
  "word_count": 402,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `vectordb` package provides PocketPaw's pluggable vector database abstraction. Its `__init__.py` has a single responsibility: re-export `VectorStoreProtocol` from the internal `protocol` submodule, so that consumers of the package import from a stable, versioned public surface rather than an internal path.

## Why a Re-Export Layer?

Without this re-export, callers would write:

```python
from pocketpaw.vectordb.protocol import VectorStoreProtocol
```

That import path exposes the internal module layout. If the protocol is ever moved (e.g., merged into a `base.py` or renamed), every caller breaks. With the `__init__.py` re-export, the public contract becomes:

```python
from pocketpaw.vectordb import VectorStoreProtocol
```

Internal restructuring is then an invisible refactoring detail. This pattern is especially important for libraries consumed by plugin authors or third-party channel adapters who cannot be expected to track internal reorganisations.

## `__all__` Declaration

The module declares `__all__ = ["VectorStoreProtocol"]`, which controls what `from pocketpaw.vectordb import *` exposes. More importantly, it serves as explicit documentation: readers immediately know which names are intentionally public versus merely importable. Linters and type-checkers use `__all__` to surface unintentional public API leaks and to validate that re-exported names are actually defined.

## Relationship to the Package

The package currently contains three files:
- `__init__.py` — public API surface (this file)
- `protocol.py` — defines `VectorStoreProtocol`
- `chroma_adapter.py` — provides the Chroma-backed implementation

Keeping the protocol separate from the adapter means that code depending only on the interface—the agent loop, tools that use semantic search, test mocks—does not transitively import Chroma, which carries heavy ML dependencies like SentenceTransformers. This is a deliberate dependency boundary enforced by the package structure.

## What Belongs Here vs. in `protocol.py`

The `__init__.py` does not define anything; it only re-exports. All type definitions belong in `protocol.py`. This discipline means there is no risk of circular imports: `protocol.py` imports nothing from the package root, and `chroma_adapter.py` imports from `protocol.py` directly during type-checking only (`TYPE_CHECKING` guard).

## Future Extension Points

If a second adapter is added (e.g., Pinecone, Qdrant), a factory or registry in `__init__.py` would be the natural place to expose it. For example:

```python
from .protocol import VectorStoreProtocol
from .factory import get_vector_store

__all__ = ["VectorStoreProtocol", "get_vector_store"]
```

Currently `ChromaAdapter` must be imported from `pocketpaw.vectordb.chroma_adapter` directly.

## Known Gaps

- Only `VectorStoreProtocol` is re-exported. Adapter classes remain on internal paths, which means third-party code constructing a `ChromaAdapter` is coupled to the internal layout. A factory function or registry would resolve this without exposing the adapter class directly.
