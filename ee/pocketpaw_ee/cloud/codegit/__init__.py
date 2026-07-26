# codegit — Code Mode git smart-HTTP proxy (CM-3d).
# Created 2026-07-16 (feat/code-mode): lets the sandbox VM ``git push``/``fetch``
# its repo WITHOUT the GitHub token ever entering the VM. The VM's ``origin``
# points at this broker (carrying a sandbox+repo-scoped ticket, NOT the GitHub
# token); the broker verifies the ticket, mints a fresh repo-scoped GitHub token
# server-side, and proxies the git smart-HTTP request upstream to github.com with
# that token injected. The token lives only in the backend.
