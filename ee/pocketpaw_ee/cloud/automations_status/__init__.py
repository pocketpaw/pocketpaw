# automations_status — the workspace-scoped aggregate status entity for the
# always-on automation surface (feat/external-alerting-c2c3, C3).
#
# Answers one question the merged-screen UI asks: "what automation is running for
# this workspace, and is it on?" It aggregates four things behind a single read:
#   1. OSS automation rules (threshold / data-change / schedule) from the OSS
#      ``pocketpaw.automations.store`` — the box-local rule set.
#   2. The OSS AutomationEvaluator status (running + interval).
#   3. The cloud sweep REGISTRY — a constructed enumeration of every background
#      sweep (cycles, decisions, member_ingest, fabric_ingest, temporal, refresh),
#      the env flag that gates each, and whether that gate is currently on in this
#      process. There is no queryable registry in the fleet today; this module
#      CONSTRUCTS it from a static descriptor table kept next to the sweeps.
#   4. The per-workspace enable state (``WorkspaceAutomationConfig``) — the
#      opt-out the sweeps consult at their per-workspace fan-out.
#
# 4-file cloud entity: domain (frozen value objects) / dto (wire schemas) /
# service (the sole Beanie writer + the sweep gate the schedulers import) /
# router (thin HTTP surface, CloudError never HTTPException).
