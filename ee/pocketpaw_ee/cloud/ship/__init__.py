# ee/pocketpaw_ee/cloud/ship/__init__.py — the /ship cloud entity package.
#
# SHIP-2 landed the provisioning half: ``store`` (the persistence seam, the only
# module that touches the Beanie docs) and ``provisioning`` + ``job`` (the
# box-lifecycle orchestrator and its arq entry point).
#
# SHIP-3 adds the HTTP surface on top of it:
#
#   domain/dto/service/router  the 4-file entity shape behind /api/v1/ship
#   engine                     box -> live DokkuDriver session (SSH key handling)
#   deploy_job                 the arq deploy pipeline + its orchestrator
#   enqueue                    the web-side dispatch for both jobs
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new package.
# Changed 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): the entity + HTTP
# surface landed alongside the provisioning half.
