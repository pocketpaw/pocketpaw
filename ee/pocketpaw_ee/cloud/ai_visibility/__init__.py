# Cloud ai_visibility entity — "do AI assistants name this business?"
#
# ``service.run_check`` asks AI search engines (``engines.py``) local questions
# about a business, judges each answer (``judge.py``), picks one fix
# (``fixes.py``) and stores the check (``models/ai_visibility_check.py``).
# ``router.py`` serves the free public check (``public_check.py``).
