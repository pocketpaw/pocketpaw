# ee/pocketpaw_ee/sites/figma/: read a Figma design for the /sites builder agent.
#
#   urls.py          parse figma.com/(design|file)/ links; other hosts refused
#   session_keys.py  the per-conversation allowlist of file keys the owner pasted
#   client.py        the ``FigmaSource`` facade over the REST API (errors, 429,
#                    cooldown, cache keyed by the file's lastModified)
#   outline.py       depth-limited node outline under a node budget, wrapped in
#                    the ``<untrusted_design_content>`` envelope
#   assets.py        SVG sanitising and raster re-encoding
#   tokens.py        colour / radius / spacing / type roles from variables or nodes
#
# The agent tools live in ``agent/mcp_servers/figma.py``; the OAuth grant is the
# ``figma`` vault provider (``integrations/providers/figma.py``). Nothing here
# ever sees a token outside the request it signs.
