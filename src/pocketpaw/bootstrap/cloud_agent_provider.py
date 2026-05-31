"""Bootstrap provider for cloud-defined agents.

The default provider assembles identity/style/instructions from the OSS
config (IDENTITY.md, USER.md, etc.). Cloud agents live in MongoDB with
their own ``soul_persona`` + ``system_prompt`` + archetype/values, and
should present that as the identity block for per-agent AgentLoops.
"""

from __future__ import annotations

from pocketpaw.bootstrap.protocol import BootstrapContext, BootstrapProviderProtocol


class CloudAgentBootstrapProvider(BootstrapProviderProtocol):
    """Build a BootstrapContext from a cloud Agent.config dict."""

    def __init__(self, agent_name: str, agent_config: dict) -> None:
        self.agent_name = agent_name
        self.agent_config = agent_config or {}

    async def get_context(self) -> BootstrapContext:
        cfg = self.agent_config
        persona = (cfg.get("soul_persona") or "").strip()
        system_prompt = (cfg.get("system_prompt") or "").strip()
        archetype = (cfg.get("soul_archetype") or "").strip()
        values = cfg.get("soul_values") or []

        # Identity = the "who are you" block. Prefer an explicit system_prompt
        # if set, otherwise fall back to the soul persona. Both together when
        # the user configured both (system_prompt as extra directive).
        identity_parts: list[str] = []
        if persona:
            identity_parts.append(persona)
        if system_prompt and system_prompt != persona:
            identity_parts.append(system_prompt)
        identity = "\n\n".join(identity_parts) or f"You are {self.agent_name}."

        soul_parts: list[str] = []
        if archetype:
            soul_parts.append(f"Archetype: {archetype}")
        if values and isinstance(values, list):
            soul_parts.append("Core values: " + ", ".join(str(v) for v in values))
        soul = "\n".join(soul_parts)

        return BootstrapContext(
            name=self.agent_name,
            identity=identity,
            soul=soul,
            style="",
            instructions="",
        )
