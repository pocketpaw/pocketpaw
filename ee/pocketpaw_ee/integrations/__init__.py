# ee/pocketpaw_ee/integrations: one-click integrations for Paw Sites.
#
# The pieces, each its own module:
#   providers/  the provider protocol, the registry and the wave-0 providers
#   vault.py    encrypted workspace connections and the one token-refresh path
#   bindings.py site <-> provider links, authz, the per-site gateway token
#   oauth.py    hosted connect + callback (single-use server-side state)
#   gateway.py  POST /sites/{site_id}/x/{provider}/{action}, the gate chain
#   webhooks.py provider webhook ingress, dedupe and order persistence
#   router.py   the HTTP surface (owner routes + public routes)
# Credentials never reach a browser, the agent, or a site's source.
