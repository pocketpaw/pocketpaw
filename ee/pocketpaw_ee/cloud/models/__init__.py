"""Cloud document models — re-exports for Beanie init.

Updated: 2026-08-29 (T2 "Audio/video transcription at ingest") — added
``FileTranscriptionUsage`` (one row per workspace per UTC day, the atomic
counter behind the media-transcription daily cap) to the imports and
``get_all_documents()`` so the ``file_transcription_usage`` collection is wired
into ``init_beanie``. Kept out of ``__all__``: only
``ee.cloud.uploads.transcription_budget`` imports the doc class directly.
Registering it here is load-bearing — an unregistered document makes
``get_pymongo_collection()`` raise inside that budget's fail-CLOSED except, so
every transcription is refused and the feature reads as switched off.

Updated: 2026-08-28 (FC-3 "File comprehension") — added
``FileComprehensionUsage`` (one row per workspace per UTC day, the atomic
counter behind the comprehension daily cap) to the imports and
``get_all_documents()`` so the ``file_comprehension_usage`` collection is
wired into ``init_beanie``. Kept out of ``__all__``: only
``ee.cloud.uploads.comprehension_budget`` imports the doc class directly.

Updated: 2026-07-27 (integration/growth-v1) — G-6's ``WhatsAppSendLog`` is
gone: it and G-5's ``MessageLog`` were the same send record built in parallel
under two names, unified onto ``MessageLog`` (which gained the ``sending`` /
``blocked`` outcomes, ``blocked_reason``, ``error_code`` and
``opted_in_at_attempt``). The stale G-6 note below is kept for history.

Superseded: 2026-07-27 (feat/growth-g6) — added ``WhatsAppSendLog`` (the /growth
per-attempt outbound WhatsApp compliance record: one row per attempt, including
the attempts the opt-in guard REFUSED) to the imports and
``get_all_documents()`` so the ``growth_whatsapp_send_logs`` collection is wired
into ``init_beanie``. Kept out of ``__all__`` like ``Prospect`` / ``Draft`` —
only ``ee.cloud.growth.service`` imports the doc class directly (import-linter
"Growth" contract).
Updated: 2026-07-29 (feat/growth-discovery) — added ``Icp`` (the standing
description of who a workspace wants, plus the discovery cadence) to the
imports and ``get_all_documents()`` so the ``growth_icps`` collection is wired
into ``init_beanie``. Kept out of ``__all__`` like the other growth docs.
Updated: 2026-07-27 (feat/growth-g4 merge) — merged integration/ship-v1 into the
growth stack so the ``_growth_send`` gate slice can wire the sixth gated kind on
top of ship's instinct-router changes; both changelog blocks below retained.
Updated: 2026-07-27 (feat/growth-g5) — added ``MessageLog`` (the /growth
outbound audit row: one record per delivery ATTEMPT, ``sent`` | ``failed``) to
the imports and ``get_all_documents()`` so the ``growth_message_logs``
collection is wired into ``init_beanie``. Kept out of ``__all__`` like
``Prospect`` / ``Draft`` — only ``ee.cloud.growth.service`` imports the doc
class directly (import-linter "Growth" contract).
Updated: 2026-07-27 (feat/growth-g3) — added ``Draft`` (the /growth per-channel
outreach draft: workspace-scoped, attached to a prospect, status lifecycle
enforced in the service) to the imports and ``get_all_documents()`` so the
``growth_drafts`` collection is wired into ``init_beanie``. Kept out of
``__all__`` like ``Prospect`` — only ``ee.cloud.growth.service`` imports the
doc class directly (import-linter "Growth" contract).
Updated: 2026-07-27 (feat/growth-g1) — added ``Prospect`` (the /growth
outbound-engine prospect store: workspace-scoped, unique (workspace, domain)
dedupe key) to the imports and ``get_all_documents()`` so the
``growth_prospects`` collection is wired into ``init_beanie``. Kept out of
``__all__`` so it can't be star-imported into routers/DTOs/domains — only
``ee.cloud.growth.service`` imports the doc class directly (import-linter
"Growth" contract).
Updated: 2026-07-22 (SHIP-3, feat/ship-3-cloud-entity) — added ``ShipApp`` and
``ShipDeploy`` (the /ship app + deploy-attempt docs) to the imports, ``__all__``
and ``get_all_documents()`` so the ``ship_apps`` / ``ship_deploys`` collections
are wired into ``init_beanie``. Only ``ee.cloud.ship.store`` imports the doc
classes directly (import-linter "Ship" contract).

Updated: 2026-07-15 (fix/workspace-vm-map-to-db) — added ``WorkspaceVm`` (the
workspace→Daytona-VM mapping, moved out of the local
``~/.pocketpaw/daytona_workspace_vm_map.json`` file into the ``workspace_vms``
collection) to the imports, ``__all__``, and ``get_all_documents()`` so the
collection is wired into ``init_beanie``. Only ``ee.cloud.daytona.store``
imports the doc class directly.
Updated: 2026-07-15 (WC-1, feat/websandbox-registry) — added ``WebSandbox``
(the Web Cursor sandbox registry: the (workspace_id, user_id, repo) -> sandbox
tenancy/auth oracle) to the imports, ``__all__``, and ``get_all_documents()`` so
the ``web_sandboxes`` collection is wired into ``init_beanie``. Only
``ee.cloud.websandbox.service`` imports the doc class directly (import-linter
"WebSandbox" contract).
Updated: 2026-07-08 (feat/external-alerting-delivery) — added
``NotificationDeliveryConfig`` (the per-workspace external-delivery config: Slack
incoming-webhook + generic HTTPS webhook + enabled switch + per-kind routing) to
the imports, ``__all__``, and ``get_all_documents()`` so the
``notification_delivery_configs`` collection is wired into ``init_beanie``. Only
``ee.cloud.notifications`` (service writes, delivery reads) imports the doc class
directly (import-linter "Notifications" contract).
Updated: 2026-07-03 (feat/files-share-links FL-12b) — added ``ShareLink`` (the
public, token-gated file share-link doc) to the lazy uploads loader,
``get_all_documents()`` and ``__all__`` so the ``file_share_links`` collection
is wired into ``init_beanie``. Only ``ee.cloud.uploads.share_store`` writes it.
Updated: 2026-06-30 (feat/session-supervisor SS-3) — added
``AgentSessionRuntimeDoc`` (the durable, tenant-scoped
``(workspace, session_id, agent_id) -> cli_session_id`` mapping) to the
imports, ``__all__``, and ``get_all_documents()`` so the
``agent_session_runtimes`` collection is wired into ``init_beanie``. Only
``ee.cloud.agent_sessions.runtime_service`` imports the doc class directly.
Updated: 2026-06-30 (feat/session-supervisor SS-2) — added ``SessionTranscriptDoc``
(the durable, tenant-scoped transcript rows backing the Mongo ``SessionStore``)
to the imports, ``__all__``, and ``get_all_documents()`` so the
``session_transcripts`` collection is wired into ``init_beanie``. Only
``ee.cloud.agent_sessions.store`` imports the doc class directly.
Updated: 2026-06-09 (feat/push-subscription-store, pocketpaw#1391) — added the
``PushSubscription`` (with its ``PushKeys`` subdoc) and ``VapidKeypair``
tenant-scoped Web Push documents to the imports and ``get_all_documents()``
registry so the push-subscription store + per-workspace VAPID keypair are
wired into ``init_beanie``. Only ``ee.cloud.push.service`` imports those doc
classes directly (import-linter contract); they're kept out of ``__all__`` so
they can't be star-imported into routers/DTOs/domains.
Updated: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.2) — added the
``Lead`` and ``Site`` tenant-scoped Paw Sites documents (plus their
``LeadSource`` / ``SiteDomain`` subdocs) to the imports, ``__all__``, and the
``get_all_documents()`` registry so the cloud capture sink is wired into
``init_beanie``.
Updated: 2026-05-26 (feat/foresight-v10-scenario-editor-backend) — added
``ForesightWorkspaceScenario`` (RFC 08 v1.0 wave 3) to the registered
docs + ``__all__`` so workspace-scoped custom scenarios are wired into
``init_beanie``. Only ``ee.cloud.foresight.scenarios`` imports the doc
class directly (import-linter contract).
Updated: 2026-05-26 (feat/foresight-v10-threshold-override-cloud) — added
``ForesightWorkspaceConfig`` to the registered docs + ``__all__`` so RFC 08
v1.0's per-workspace onboarding threshold override is wired into
``init_beanie``. Only ``ee.cloud.foresight.service`` imports the doc class
directly (import-linter contract).
Updated: 2026-05-26 (feat/foresight-v10-prediction-record-persist) — added
``ForesightPredictionRecord`` to the registered docs + ``__all__`` so the
RFC 08 §9 calibration buffer's Mongo persistence is wired into ``init_beanie``.
Updated: 2026-05-21 (PR #1177 security pass) — dropped PocketBackendCredential
from ``__all__`` so it cannot be star-imported into routers/DTOs/domains; it
remains registered in ``get_all_documents()`` for Beanie init.
Updated: 2026-05-30 (feat/paw-sites-backend, RFC 12 follow-up item 3) — added
``SiteRateCounter`` (the atomic per-minute capture rate-limit counter) to the
imports, ``__all__``, and ``get_all_documents()`` so the counter collection is
wired into ``init_beanie``.
Updated: 2026-06-10 (feat/belt-console-backend, SC-1) — added
``BeltWorkspaceConfig`` (the per-workspace Belt console allowlist-extension doc)
to the imports, ``__all__``, and ``get_all_documents()`` so the console's
add-repo route persistence is wired into ``init_beanie``. Only
``ee.cloud.belt.service`` imports the doc class directly.
Updated: 2026-06-11 (feat/firestore-fabric-ingest) — added
``FabricIngestConfig`` (the per-workspace Firestore→Fabric mapping) and
``FabricIngestState`` (the per-(workspace, collection) sync bookkeeping) to the
imports, ``__all__``, and ``get_all_documents()`` so the ingestion worker's
collections are wired into ``init_beanie``. Only
``ee.cloud.fabric_ingest.service`` imports the doc classes directly.
Updated: 2026-06-20 (feat/workspace-jobs, pp#1459) — added ``WorkspaceJobDoc``
(the durable status record for ARQ-backed pocket jobs) to the imports,
``__all__``, and ``get_all_documents()`` so the ``workspace_jobs`` collection is
wired into ``init_beanie``. Only ``ee.cloud.jobs.service`` writes the doc
directly (import-linter "Jobs" contract).
Updated: 2026-06-18 (feat/branch-primitive-versions, BP-1) — registered the
``ArtifactVersion`` doc (the universal Branch-primitive version log) in
``get_all_documents()`` via the lazy ``_ensure_version_docs()`` helper. The doc
lives in the ``pocketpaw_ee.versions`` package (its own entity), so it is
imported lazily here — the same out-of-models discipline the belt/mandates docs
use — to keep ``cloud.models`` from hard-importing the versions package. Only
``pocketpaw_ee.versions.service`` imports the doc class directly.
Updated: 2026-06-24 (integration/billing-credits, BC-2) — added ``Payment``
(the top-up payment record captured via a verified Dodo webhook) to the imports,
``__all__``, and ``get_all_documents()`` so the ``billing_payments`` collection
is wired into ``init_beanie``. Only ``ee.cloud.billing.service`` writes the doc.
Updated: 2026-06-24 (integration/billing-credits, BC-7) — added ``Subscription``
(the recurring plan subscription record captured via a verified Dodo
``subscription.active`` webhook) to the imports, ``__all__``, and
``get_all_documents()`` so the ``billing_subscriptions`` collection is wired into
``init_beanie``. Only ``ee.cloud.billing.service`` writes the doc.
Updated: 2026-06-20 (feat/szd-slice2-discovery, S2-R1) — added ``InstinctRuleDoc``
(the persisted, approved, workspace-scoped, owned rule discovered from exhaust) to
the imports, ``__all__``, and ``get_all_documents()`` so the ``instinct_rules``
collection is wired into ``init_beanie`` and the ``beanie_test_db`` fixture. Only
``ee.cloud.rules.service`` imports the doc class directly (import-linter "Rules").
Updated: 2026-07-09 (feat/instinct-guardrail-rules) — added
``InstinctWorkspaceConfig`` (the per-workspace tri-state override on the global
``instinct_enforce_discovered_rules`` flag) to the imports, ``__all__``, and
``get_all_documents()`` so the ``instinct_workspace_configs`` collection is wired
into ``init_beanie``. Only ``ee.cloud.rules.service`` writes it (import-linter
"Rules").
Updated: 2026-07-11 (feat/external-alerting-c2c3) — added
``WorkspaceAutomationConfig`` (the per-workspace opt-out for the always-on
background sweeps) to the imports, ``__all__``, and ``get_all_documents()`` so the
``workspace_automation_configs`` collection is wired into ``init_beanie``. Only
``ee.cloud.automations_status.service`` writes it (import-linter "AutomationsStatus").
"""

from __future__ import annotations

from pocketpaw_ee.cloud.models.agent import Agent, AgentConfig
from pocketpaw_ee.cloud.models.agent_session_runtime import AgentSessionRuntimeDoc
from pocketpaw_ee.cloud.models.api_key import APIKey
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.audit_webhook import AuditWebhook
from pocketpaw_ee.cloud.models.auth_session import AuthSession
from pocketpaw_ee.cloud.models.belt_workspace_config import BeltWorkspaceConfig
from pocketpaw_ee.cloud.models.builtin_widget import BuiltInWidget, BuiltInWidgetPosition
from pocketpaw_ee.cloud.models.byok_key import ByokProviderKey
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
from pocketpaw_ee.cloud.models.code_connection import CodeConnection
from pocketpaw_ee.cloud.models.code_project import CodeProject
from pocketpaw_ee.cloud.models.comment import Comment, CommentAuthor, CommentTarget
from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector
from pocketpaw_ee.cloud.models.credit import CreditBalance, CreditLedgerEntry
from pocketpaw_ee.cloud.models.cycle import Cycle, CycleDailyPoint
from pocketpaw_ee.cloud.models.deep_work_log import DeepWorkLog
from pocketpaw_ee.cloud.models.draft import Draft
from pocketpaw_ee.cloud.models.fabric_ingest_state import (
    FabricIngestConfig,
    FabricIngestState,
)
from pocketpaw_ee.cloud.models.file import FileObj
from pocketpaw_ee.cloud.models.file_comprehension_usage import FileComprehensionUsage
from pocketpaw_ee.cloud.models.file_transcription_usage import FileTranscriptionUsage
from pocketpaw_ee.cloud.models.file_version import FileVersionDoc
from pocketpaw_ee.cloud.models.foresight_backtest import ForesightBacktest
from pocketpaw_ee.cloud.models.foresight_prediction_record import (
    ForesightPredictionRecord,
)
from pocketpaw_ee.cloud.models.foresight_projected_decision import (
    ForesightProjectedDecision,
)
from pocketpaw_ee.cloud.models.foresight_run import ForesightRun
from pocketpaw_ee.cloud.models.foresight_workspace_config import (
    ForesightWorkspaceConfig,
)
from pocketpaw_ee.cloud.models.foresight_workspace_scenario import (
    ForesightWorkspaceScenario,
)
from pocketpaw_ee.cloud.models.group import Group, GroupAgent
from pocketpaw_ee.cloud.models.guest_turn_usage import GuestTurnUsage
from pocketpaw_ee.cloud.models.icp import Icp
from pocketpaw_ee.cloud.models.instinct_approval import InstinctApproval
from pocketpaw_ee.cloud.models.instinct_rule import InstinctRuleDoc
from pocketpaw_ee.cloud.models.instinct_workspace_config import InstinctWorkspaceConfig
from pocketpaw_ee.cloud.models.invite import Invite, MeetingInvite
from pocketpaw_ee.cloud.models.lead import Lead, LeadSource
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey
from pocketpaw_ee.cloud.models.meeting import (
    Meeting,
    MeetingProviderCredentials,
    MeetingsSettings,
    MeetingTranscript,
)
from pocketpaw_ee.cloud.models.member_ingest_state import MemberIngestState
from pocketpaw_ee.cloud.models.message import Attachment, Mention, Message, Reaction
from pocketpaw_ee.cloud.models.message_log import MessageLog
from pocketpaw_ee.cloud.models.notification import Notification, NotificationSource
from pocketpaw_ee.cloud.models.notification_delivery import NotificationDeliveryConfig
from pocketpaw_ee.cloud.models.other_hand_usage import IllustrationUsage
from pocketpaw_ee.cloud.models.payment import Payment
from pocketpaw_ee.cloud.models.planner import PlanSession, PlanSessionAgentGap
from pocketpaw_ee.cloud.models.pocket import Pocket, Widget, WidgetPosition
from pocketpaw_ee.cloud.models.pocket_backend import PocketBackendCredential
from pocketpaw_ee.cloud.models.project import Project
from pocketpaw_ee.cloud.models.prospect import Prospect
from pocketpaw_ee.cloud.models.push_subscription import PushSubscription
from pocketpaw_ee.cloud.models.read_state import ReadState
from pocketpaw_ee.cloud.models.request_log import RequestLog
from pocketpaw_ee.cloud.models.sense_preference import WorkspaceSensePreference
from pocketpaw_ee.cloud.models.session import Session
from pocketpaw_ee.cloud.models.session_transcript import SessionTranscriptDoc
from pocketpaw_ee.cloud.models.ship import ShipApp, ShipBox, ShipDeploy
from pocketpaw_ee.cloud.models.site import Site, SiteDomain
from pocketpaw_ee.cloud.models.site_rate_counter import SiteRateCounter
from pocketpaw_ee.cloud.models.spend_reconciliation import SpendReconciliation
from pocketpaw_ee.cloud.models.subscription import Subscription
from pocketpaw_ee.cloud.models.task import Task, TaskAssignee, TaskSource
from pocketpaw_ee.cloud.models.task_attachment import TaskAttachment
from pocketpaw_ee.cloud.models.task_event import TaskEvent
from pocketpaw_ee.cloud.models.temporal_sweep_state import TemporalSweepStateDoc
from pocketpaw_ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership
from pocketpaw_ee.cloud.models.vapid_keypair import VapidKeypair
from pocketpaw_ee.cloud.models.web_sandbox import WebSandbox
from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings
from pocketpaw_ee.cloud.models.workspace_automation_config import WorkspaceAutomationConfig
from pocketpaw_ee.cloud.models.workspace_job import WorkspaceJobDoc
from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm

# Lazy import to avoid circular imports
FileUpload: type = None  # type: ignore[assignment]
FileFolder: type = None  # type: ignore[assignment]
# FL-12b public share links. Lazy-loaded with FileUpload (same package) so
# init_beanie registers it without ee.cloud.models hard-importing uploads.
ShareLink: type = None  # type: ignore[assignment]
_CalendarDoc: type = None  # type: ignore[assignment]
_EventDoc: type = None  # type: ignore[assignment]
# The Belt MANDATE docs live in ee.cloud.mandates.domain (4-file entity, sole
# importer = its own service). Lazy-loaded here so init_beanie registers them
# without ee.cloud.models taking a hard import on the mandates package (same
# out-of-models discipline the calendar docs use).
_MandateDoc: type = None  # type: ignore[assignment]
_ShiftDoc: type = None  # type: ignore[assignment]
_SightingDoc: type = None  # type: ignore[assignment]
# The ArtifactVersion doc lives in pocketpaw_ee.versions (its own Branch-
# primitive entity, sole importer = its own service). Lazy-loaded here so
# init_beanie registers it without ee.cloud.models taking a hard import on the
# versions package (same out-of-models discipline as belt/mandates).
_ArtifactVersionDoc: type = None  # type: ignore[assignment]


def _ensure_file_upload():
    global FileUpload, FileFolder, ShareLink
    if FileUpload is None:
        from pocketpaw_ee.cloud.uploads.models import FileFolder as _FileFolder
        from pocketpaw_ee.cloud.uploads.models import FileUpload as _FileUpload
        from pocketpaw_ee.cloud.uploads.share_models import ShareLink as _ShareLink

        FileUpload = _FileUpload
        FileFolder = _FileFolder
        ShareLink = _ShareLink
    return FileUpload


def _ensure_calendar_docs():
    # Why: calendar.__init__ eagerly imports the router which transitively
    # imports cloud.auth.current_active_user. Deferred to break the cycle
    # when cloud.models is loaded during cloud.auth's own init.
    global _CalendarDoc, _EventDoc
    if _CalendarDoc is None:
        from pocketpaw_ee.calendar.models import _CalendarDoc as _CD
        from pocketpaw_ee.calendar.models import _EventDoc as _ED

        _CalendarDoc = _CD
        _EventDoc = _ED
    return _CalendarDoc, _EventDoc


def _ensure_mandate_docs():
    # Why: the mandates package's __init__ imports its router (→ deps → auth),
    # so registering the docs via the package would pull the auth chain in
    # during cloud.models init. Import the doc module directly + deferred.
    global _MandateDoc, _ShiftDoc, _SightingDoc
    if _MandateDoc is None:
        from pocketpaw_ee.cloud.mandates.domain import MandateDoc as _MD
        from pocketpaw_ee.cloud.mandates.domain import ShiftDoc as _SD
        from pocketpaw_ee.cloud.mandates.domain import SightingDoc as _SG

        _MandateDoc = _MD
        _ShiftDoc = _SD
        _SightingDoc = _SG
    return _MandateDoc, _ShiftDoc, _SightingDoc


def _ensure_version_docs():
    # Why: the versions package is its own Branch-primitive entity whose sole
    # importer is its own service. Import the doc class directly + deferred so
    # cloud.models doesn't take a hard import on pocketpaw_ee.versions.
    global _ArtifactVersionDoc
    if _ArtifactVersionDoc is None:
        from pocketpaw_ee.versions.models import ArtifactVersion as _AV

        _ArtifactVersionDoc = _AV
    return _ArtifactVersionDoc


__all__ = [
    "APIKey",
    "Agent",
    "AgentConfig",
    "Attachment",
    "AuditEvent",
    "AuditWebhook",
    "AuthSession",
    "BeltWorkspaceConfig",
    "BuiltInWidget",
    "BuiltInWidgetPosition",
    "ChatRunDoc",
    "CodeConnection",
    "CodeProject",
    "Comment",
    "CommentAuthor",
    "CommentTarget",
    "ComposioConnection",
    "CreditBalance",
    "CreditLedgerEntry",
    "Cycle",
    "CycleDailyPoint",
    "FabricIngestConfig",
    "FabricIngestState",
    "FileFolder",
    "FileObj",
    "FileUpload",
    "FileVersionDoc",
    "ShareLink",
    "ForesightBacktest",
    "ForesightPredictionRecord",
    "ForesightProjectedDecision",
    "ForesightRun",
    "ForesightWorkspaceConfig",
    "ForesightWorkspaceScenario",
    "Group",
    "GroupAgent",
    "InstinctApproval",
    "InstinctRuleDoc",
    "InstinctWorkspaceConfig",
    "WorkspaceAutomationConfig",
    "Invite",
    "MeetingInvite",
    "Lead",
    "LeadSource",
    "ByokProviderKey",
    "IllustrationUsage",
    "LiteLLMTenantKey",
    "ShipApp",
    "ShipBox",
    "ShipDeploy",
    "Meeting",
    "MeetingProviderCredentials",
    "MeetingsSettings",
    "MeetingTranscript",
    "MemberIngestState",
    "Mention",
    "Message",
    "Notification",
    "NotificationDeliveryConfig",
    "NotificationSource",
    "OAuthAccount",
    "Payment",
    "PlanSession",
    "PlanSessionAgentGap",
    "Pocket",
    "Project",
    "Reaction",
    "ReadState",
    "DeepWorkLog",
    "RequestLog",
    "Session",
    "SessionTranscriptDoc",
    "AgentSessionRuntimeDoc",
    "Site",
    "SiteDomain",
    "SiteRateCounter",
    "SpendReconciliation",
    "Subscription",
    "WorkspaceSensePreference",
    "Task",
    "TaskAssignee",
    "TaskAttachment",
    "TaskSource",
    "TaskEvent",
    "TemporalSweepStateDoc",
    "User",
    "WebSandbox",
    "Widget",
    "WidgetPosition",
    "Workspace",
    "WorkspaceConnector",
    "WorkspaceJobDoc",
    "WorkspaceMembership",
    "WorkspaceSettings",
    "WorkspaceVm",
]


def get_all_documents():
    """Get all Beanie documents, with lazy FileUpload loading."""
    _ensure_file_upload()
    cal_doc, evt_doc = _ensure_calendar_docs()
    mandate_doc, shift_doc, sighting_doc = _ensure_mandate_docs()
    artifact_version_doc = _ensure_version_docs()
    return [
        User,
        Agent,
        Pocket,
        PocketBackendCredential,
        Session,
        # Built-in widget definitions — system-level rows every new home pocket
        # seeds from. Read by GET /pockets/builtin-widgets + ensure_home_pocket.
        BuiltInWidget,
        # Agent-session transcript rows backing the Mongo SessionStore (SS-2).
        # Only ``ee.cloud.agent_sessions.store`` imports this doc directly.
        SessionTranscriptDoc,
        # Durable (workspace, session_id, agent_id) -> cli_session_id mapping
        # so any turn can resume the native session (SS-3). Only
        # ``ee.cloud.agent_sessions.runtime_service`` imports this doc directly.
        AgentSessionRuntimeDoc,
        Comment,
        Notification,
        # Per-workspace external-delivery config (Slack + generic webhook).
        # Only ``ee.cloud.notifications`` service/delivery import it.
        NotificationDeliveryConfig,
        FileObj,
        FileUpload,
        FileFolder,
        # Public file share links (FL-12b). Only ``uploads.share_store``
        # writes these; the public GET /share/{token} route reads by token.
        ShareLink,
        # Per-workspace/day file-comprehension spend counter (FC-3). Only
        # ``ee.cloud.uploads.comprehension_budget`` reads/writes this.
        FileComprehensionUsage,
        # Per-guest-user/day turn counter (BYOK-first onboarding). Only
        # ``ee.cloud.auth.guest_budget`` reads/writes this.
        GuestTurnUsage,
        # Per-workspace/day media-transcription spend counter (T2). Only
        # ``ee.cloud.uploads.transcription_budget`` reads/writes this. Separate
        # from the comprehension counter on purpose: the two meter different
        # bills, and one shared row would let a bulk photo import exhaust the
        # ceiling that exists to stop a podcast library.
        FileTranscriptionUsage,
        # file_versions edit history (ART-1). Only ``file_versions.service``
        # imports this class (import-linter "FileVersions" contract).
        FileVersionDoc,
        Workspace,
        WorkspaceConnector,
        ComposioConnection,
        # Credit ledger (BC-1) — workspace-scoped wallet + append-only audit.
        # Only ``ee.cloud.credits.service`` writes these.
        CreditBalance,
        CreditLedgerEntry,
        # Billing payments (BC-2) — top-up payment records captured via a
        # gateway webhook. Only ``ee.cloud.billing.service`` writes this.
        Payment,
        # Billing subscriptions (BC-7) — recurring plan subscription records
        # captured via verified ``subscription.*`` webhooks. Only
        # ``ee.cloud.billing.service`` writes this.
        Subscription,
        # LiteLLM per-tenant virtual-key mapping (MCG-8). Only
        # ``ee.cloud.llm_provisioning.service`` writes this.
        ByokProviderKey,
        IllustrationUsage,
        LiteLLMTenantKey,
        # Managed-deploy boxes + their apps and deploy attempts (SHIP-2/SHIP-3).
        # Only ``ee.cloud.ship.store`` reads/writes these.
        ShipBox,
        ShipApp,
        ShipDeploy,
        Invite,
        MeetingInvite,
        Group,
        InstinctApproval,
        # Discovered governed rules (SZD slice-2). Only ``ee.cloud.rules.service``
        # writes it.
        InstinctRuleDoc,
        # Per-workspace Instinct enforcement override (feat/instinct-guardrail-rules).
        # Only ``ee.cloud.rules.service`` writes it.
        InstinctWorkspaceConfig,
        # Per-workspace automation opt-out (feat/external-alerting-c2c3). Only
        # ``ee.cloud.automations_status.service`` writes it.
        WorkspaceAutomationConfig,
        Message,
        ReadState,
        RequestLog,
        DeepWorkLog,
        # Shadow-compare reconciliation rows (WU-F). One per tenant per window
        # during shadow mode. Only ``ee.cloud.llm_provisioning.service`` writes it.
        SpendReconciliation,
        Task,
        TaskAttachment,
        TemporalSweepStateDoc,
        Cycle,
        Project,
        PlanSession,
        Meeting,
        MeetingTranscript,
        MeetingProviderCredentials,
        MeetingsSettings,
        MemberIngestState,
        FabricIngestConfig,
        FabricIngestState,
        # Calendar — sibling enterprise package.
        _CalendarDoc,
        _EventDoc,
        ForesightRun,
        ForesightBacktest,
        ForesightProjectedDecision,
        ForesightPredictionRecord,
        ForesightWorkspaceConfig,
        ForesightWorkspaceScenario,
        ChatRunDoc,
        Lead,
        Site,
        SiteRateCounter,
        # Growth prospect store (G-1) — the /growth outbound engine's
        # workspace-scoped, domain-deduped prospect record. Only
        # ``ee.cloud.growth.service`` imports this doc directly (import-linter
        # "Growth" contract).
        Prospect,
        # Growth drafts (G-3) — per-channel outreach copy attached to a
        # prospect, status lifecycle enforced in the service. Same import
        # boundary as Prospect.
        Draft,
        # Growth message log (G-5) — one audit row per outbound delivery
        # ATTEMPT (sent | failed) written by the dispatch worker through
        # ``growth.service.record_message_log``. Same import boundary.
        MessageLog,
        # Growth ICP (feat/growth-discovery) — the standing description of who
        # a workspace wants, and the cadence the discovery cron runs it on.
        # Same import boundary as Prospect / Draft / MessageLog.
        Icp,
        PushSubscription,
        VapidKeypair,
        WorkspaceSensePreference,
        AuditEvent,
        AuditWebhook,
        AuthSession,
        APIKey,
        BeltWorkspaceConfig,
        TaskEvent,
        # Workspace jobs — durable status record for ARQ-backed pocket jobs
        # (pp#1459). Only ``ee.cloud.jobs.service`` writes it.
        WorkspaceJobDoc,
        cal_doc,
        evt_doc,
        mandate_doc,
        shift_doc,
        sighting_doc,
        # Branch primitive — universal artifact version log (BP-1).
        artifact_version_doc,
        # Web Cursor sandbox registry (WC-1) — the (workspace, user, repo) ->
        # sandbox tenancy/auth oracle. Only ``ee.cloud.websandbox.service``
        # imports this doc directly (import-linter "WebSandbox" contract).
        WebSandbox,
        # Workspace→Daytona-VM mapping (fix/workspace-vm-map-to-db) — moved out
        # of the local daytona_workspace_vm_map.json file. One VM per workspace.
        # Only ``ee.cloud.daytona.store`` imports this doc directly.
        WorkspaceVm,
        # Code Mode durable-project registry (CM-2a) — the (workspace, user,
        # provider, repo) -> durable project that outlives any single ephemeral
        # sandbox. Only ``ee.cloud.codeproject.service`` imports this doc directly
        # (import-linter "CodeProject" contract).
        CodeProject,
        # Code Mode GitHub connection (CM-3) — the (workspace, user, provider,
        # installation_id) binding that lets a user list + open private repos.
        # Only ``ee.cloud.codeconnect.service`` imports this doc directly
        # (import-linter "CodeConnection" contract).
        CodeConnection,
    ]


# For backward compat, expose as lazy-loading list
class _LazyAllDocuments(list):
    """Lazy-loads ALL_DOCUMENTS on first access."""

    def __init__(self):
        super().__init__()
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            docs = get_all_documents()
            self.clear()
            self.extend(docs)
            self._loaded = True

    def __getitem__(self, index):
        self._ensure_loaded()
        return super().__getitem__(index)

    def __iter__(self):
        self._ensure_loaded()
        return super().__iter__()

    def __len__(self):
        self._ensure_loaded()
        return super().__len__()

    def __contains__(self, item):
        self._ensure_loaded()
        return super().__contains__(item)


ALL_DOCUMENTS = _LazyAllDocuments()
