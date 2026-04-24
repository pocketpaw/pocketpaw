---
{
  "title": "A2ADelegateTool: Secure Cross-Agent Task Delegation via the A2A Protocol",
  "summary": "`A2ADelegateTool` allows PocketPaw's agent to delegate tasks to any external agent that speaks the Agent-to-Agent (A2A) protocol, with built-in SSRF protection that blocks requests to private IP ranges. It bridges PocketPaw's internal tool system to the open A2A agent network.",
  "concepts": [
    "A2A protocol",
    "A2ADelegateTool",
    "SSRF protection",
    "inter-agent communication",
    "trust level",
    "elevated trust",
    "ipaddress",
    "timeout",
    "TaskSendParams",
    "A2AClient",
    "multi-agent"
  ],
  "categories": [
    "tool-system",
    "security",
    "multi-agent"
  ],
  "source_docs": [
    "f66849a577879429"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Multi-agent architectures require a mechanism for one agent to hand off work to another. `A2ADelegateTool` is PocketPaw's implementation of this for the A2A (Agent-to-Agent) protocol — a JSON-based protocol for inter-agent communication. The tool takes an agent URL and a task description, sends a structured `TaskSendParams` message to the remote agent, waits for completion, and returns the result.

## SSRF Protection

The most security-critical piece of this tool is its SSRF (Server-Side Request Forgery) guard. Without it, an adversarial prompt could instruct the agent to make HTTP requests to internal services (AWS metadata endpoint, internal admin APIs, private network services) by disguising them as "A2A agent URLs."

The guard resolves the hostname to an IP address using `socket.getaddrinfo` and then checks that IP against a list of private/loopback/link-local ranges using Python's `ipaddress` module:

```python
parsed = urlparse(agent_url)
hostname = parsed.hostname
addrs = socket.getaddrinfo(hostname, None)
for addr in addrs:
    ip = ipaddress.ip_address(addr[4][0])
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        if not settings.a2a_allow_local:
            raise ValueError(f"SSRF: {agent_url} resolves to private IP")
```

The `a2a_allow_local` setting acts as an escape hatch for local development — developers running agents on `localhost` can opt in explicitly. In production this setting should remain `False`.

## Trust Level: Elevated

The tool declares `trust_level = "elevated"` because it makes outbound HTTP requests to external URLs. Even with the SSRF guard, elevated trust ensures this tool is only available in sessions that have been explicitly configured to allow network-calling tools. A minimal or restricted agent profile will not receive this tool.

## Blocking Execution with Timeout

The tool documentation explicitly states it "blocks while waiting for the remote agent to complete the task (up to a 120-second timeout)." This is an important signal to the LLM: it should not use this tool for tasks that might take longer than two minutes, and it should not call it concurrently with other tools expecting rapid results.

The 120-second timeout is enforced via `asyncio.wait_for` around the `A2AClient.send_task()` call, returning an error string on timeout rather than raising.

## Message Construction

The tool constructs an `A2AMessage` with a `TextPart` containing the task description, wrapped in `TaskSendParams`. This follows the A2A protocol's message format exactly, ensuring interoperability with any compliant external agent.

## Known Gaps

- **DNS rebinding** — the SSRF check resolves the hostname once at call time. A DNS rebinding attack (changing DNS response between resolution and connection) could bypass this check. A more robust approach would re-resolve inside the HTTP client or use the resolved IP directly for the connection.
- **No authentication** — the tool sends tasks to any A2A-compliant agent without verifying the remote agent's identity. There is no mutual TLS or token-based auth, so the tool relies entirely on the SSRF guard to prevent accidental or malicious misdirection.