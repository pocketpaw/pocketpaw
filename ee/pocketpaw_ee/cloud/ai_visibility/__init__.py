# Cloud ai_visibility entity — "do AI assistants name this business?"
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). ``service.run_check`` asks
# AI search engines (``engines.py``) local questions about a business, judges
# each answer (``judge.py``), picks one fix (``fixes.py``) and stores the check
# (``models/ai_visibility_check.py``). No HTTP routes yet: AV-4 / AV-6 add them.
