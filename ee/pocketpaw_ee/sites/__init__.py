# Sites control plane (RFC 12) — the cloud-side glue that publishes a
# generated Paw Site and wires its custom domain. This package is a
# sibling of `cloud/` (not nested under it) and is intentionally thin:
# Cloudflare API access (cloudflare_client.py) plus the frozen value
# objects the publish/domain service passes around (domain.py).
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.2).
