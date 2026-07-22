# ee/pocketpaw_ee/cloud/ship/__init__.py — the /ship cloud entity package.
#
# SHIP-2 lands the provisioning half: ``store`` (the ShipBox persistence seam,
# the only module that touches the ShipBox Beanie doc) and ``provisioning`` (the
# pure box-lifecycle orchestrator the arq job drives). SHIP-3 adds the
# domain/dto/service/router HTTP surface alongside these.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new package.
