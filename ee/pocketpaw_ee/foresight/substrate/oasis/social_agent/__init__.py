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
#   - Made the ``agents_generator`` re-exports LAZY. Upstream eagerly
#     imports them here, which transitively pulls in ``pandas`` (used
#     to read OASIS's CSV/JSON profile-import format). Foresight v0.1
#     doesn't use the CSV bulk-import path — personas come from
#     ``.soul`` files via ``SoulSeededPersona.from_paw_agent``. Making
#     the generators lazy lets ``pandas`` stay an optional dep.
#   - ``SocialAgent`` and ``AgentGraph`` (the load-bearing primitives
#     for PR 3's wiring) remain eagerly imported.
#   - Documented in ``substrate/oasis/README-FORK.md``.
from .agent import SocialAgent
from .agent_graph import AgentGraph

__all__ = [
    "SocialAgent",
    "AgentGraph",
    "generate_agents_100w",
    "generate_reddit_agent_graph",
    "generate_twitter_agent_graph",
]


def __getattr__(name: str):
    """Lazy re-export so OASIS's CSV/JSON bulk-import functions still
    resolve on machines that have pandas installed, without forcing
    the pandas import on every Foresight startup.
    """
    if name in {
        "generate_agents_100w",
        "generate_reddit_agent_graph",
        "generate_twitter_agent_graph",
    }:
        from . import agents_generator  # noqa: PLC0415

        return getattr(agents_generator, name)
    raise AttributeError(f"module 'oasis.social_agent' has no attribute {name!r}")
