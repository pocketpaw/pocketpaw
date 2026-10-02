# Cloud studio_templates entity — Studio generations published as templates.
#
# Created 2026-10-02 (feat/studio-templates). A template is a frozen snapshot of
# one generation (cover asset + recipe). Publish / list / patch / delete live in
# ``service.py``; ``router.py`` is the thin HTTP surface under /studio-templates;
# ``service_admin.py`` holds the cross-tenant reads Discover uses.
