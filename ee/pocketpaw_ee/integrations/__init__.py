# ee/pocketpaw_ee/integrations: one-click integrations for Paw Sites.
#
# The pieces, each its own module:
#   providers/  the provider protocol + optional hooks, the registry, the providers
#   vault.py    encrypted workspace connections and the one token-refresh path
#   bindings.py site <-> provider links, status, site_setup runs, authz, gateway token
#   settings.py per-site settings validated against a provider's settings_fields
#   oauth.py    hosted connect, resume + callback (single-use server-side state)
#   gateway.py  POST /sites/{site_id}/x/{provider}/{action}, the gate chain
#   webhooks.py provider webhook ingress, dedupe and order persistence
#   router.py   the HTTP surface (owner routes + public routes)
# Credentials never reach a browser, the agent, or a site's source.
