# =========== Copyright 2023 @ CAMEL-AI.org. All Rights Reserved. ===========
# Licensed under the Apache License, Version 2.0 (the “License”);
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an “AS IS” BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========== Copyright 2023 @ CAMEL-AI.org. All Rights Reserved. ===========
#
# Modified by PocketPaw, 2026-05-25 — RFC 08 PR 3:
#   - Made the ``Platform`` re-export LAZY. Upstream eagerly imports
#     ``.platform`` here, which transitively pulls
#     ``social_platform.recsys`` → ``import torch``. Foresight per
#     RFC 08 §6.2 explicitly DROPS the Platform (replaced with the
#     Fabric-backed ``ForesightWorld``), so dragging torch into every
#     `import oasis.social_platform.channel` path is a regression.
#   - ``Channel`` (the lightweight async-queue primitive ``SocialAgent``
#     needs) remains eagerly imported because it has no heavy
#     transitive deps.
#   - ``Platform`` is still reachable via attribute access
#     (``oasis.social_platform.Platform``) — the ``__getattr__`` shim
#     below imports it on first access and raises a helpful
#     ImportError if torch isn't installed.
#   - This change is documented in
#     ``ee/pocketpaw_ee/foresight/substrate/oasis/README-FORK.md`` under
#     "What we modified".
from .channel import Channel

__all__ = [
    "Channel",
    "Platform",
]


def __getattr__(name: str):
    """Lazy re-export so ``oasis.social_platform.Platform`` still
    resolves on machines that have torch installed, without forcing
    the import path on every consumer of ``Channel`` / ``SocialAgent``.
    """
    if name == "Platform":
        from .platform import Platform  # noqa: PLC0415

        return Platform
    raise AttributeError(f"module 'oasis.social_platform' has no attribute {name!r}")
