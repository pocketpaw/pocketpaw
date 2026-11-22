"""Cloud document models — re-exports for Beanie init.

Every Beanie document class is imported here and listed in
``get_all_documents()``, which ``init_beanie`` reads: a document missing from
that list has no collection binding, so its first query fails at runtime.
Registering a new doc here is load-bearing.

Docs that only one service may write (the growth docs, including the per-unit
``GrowthBlueprintUnit`` and the legacy ``GrowthBlueprint``; metering, uploads
budgets, AI visibility, Studio templates and similar) are kept out of
``__all__``; the import-linter contracts in ``ee/pyproject.toml`` name the one
module allowed to import each. ``FileUpload`` loads lazily (see
``get_all_documents``). The history of what was added when lives in git.
"""

from __future__ import annotations

from pocketpaw_ee.cloud.models.agent import Agent, AgentConfig
from pocketpaw_ee.cloud.models.agent_session_runtime import AgentSessionRuntimeDoc
from pocketpaw_ee.cloud.models.ai_visibility_check import AiVisibilityCheck
from pocketpaw_ee.cloud.models.ai_visibility_site import AiVisibilitySite
from pocketpaw_ee.cloud.models.api_key import APIKey
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.audit_webhook import AuditWebhook
from pocketpaw_ee.cloud.models.auth_session import AuthSession
from pocketpaw_ee.cloud.models.belt_run_feed import BeltRunFeed
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
from pocketpaw_ee.cloud.models.daily_usage import DailyUsage
from pocketpaw_ee.cloud.models.deep_work_log import DeepWorkLog
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing
from pocketpaw_ee.cloud.models.draft import Draft
from pocketpaw_ee.cloud.models.fabric_ingest_state import (
    FabricIngestConfig,
    FabricIngestState,
)
from pocketpaw_ee.cloud.models.file import FileObj
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
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint
from pocketpaw_ee.cloud.models.growth_blueprint_unit import GrowthBlueprintUnit
from pocketpaw_ee.cloud.models.icp import Icp
from pocketpaw_ee.cloud.models.instinct_approval import InstinctApproval
from pocketpaw_ee.cloud.models.instinct_rule import InstinctRuleDoc
from pocketpaw_ee.cloud.models.instinct_workspace_config import InstinctWorkspaceConfig
from pocketpaw_ee.cloud.models.integration import (
    IntegrationConnection,
    IntegrationDemoRecord,
    IntegrationWebhookEvent,
    NewsletterSubscriber,
    SiteIntegration,
    SiteOrder,
)
from pocketpaw_ee.cloud.models.invite import Invite, MeetingInvite
from pocketpaw_ee.cloud.models.launch_controls import LaunchControlsDoc
from pocketpaw_ee.cloud.models.lead import Lead, LeadSource
from pocketpaw_ee.cloud.models.lead_notifications import SiteNotificationSettings
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey
from pocketpaw_ee.cloud.models.meeting import (
    Meeting,
    MeetingKnock,
    MeetingProviderCredentials,
    MeetingsSettings,
    MeetingTranscript,
)
from pocketpaw_ee.cloud.models.member_ingest_state import MemberIngestState
from pocketpaw_ee.cloud.models.message import Attachment, Mention, Message, Reaction
from pocketpaw_ee.cloud.models.message_log import MessageLog
from pocketpaw_ee.cloud.models.notification import Notification, NotificationSource
from pocketpaw_ee.cloud.models.notification_delivery import NotificationDeliveryConfig
from pocketpaw_ee.cloud.models.notification_outbox import (
    NotificationOutboxItem,
    NotificationRateMarker,
)
from pocketpaw_ee.cloud.models.partner_application import PartnerApplication
from pocketpaw_ee.cloud.models.payment import Payment
from pocketpaw_ee.cloud.models.planner import PlanSession, PlanSessionAgentGap
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.platform_rollup import PlatformDailyRollup
from pocketpaw_ee.cloud.models.pocket import Pocket, Widget, WidgetPosition
from pocketpaw_ee.cloud.models.pocket_backend import PocketBackendCredential
from pocketpaw_ee.cloud.models.project import Project
from pocketpaw_ee.cloud.models.prospect import Prospect
from pocketpaw_ee.cloud.models.push_subscription import PushSubscription
from pocketpaw_ee.cloud.models.read_state import ReadState
from pocketpaw_ee.cloud.models.released_slug import ReleasedSlug
from pocketpaw_ee.cloud.models.reply_draft import ReplyDraft
from pocketpaw_ee.cloud.models.repo_metric import RepoMetric
from pocketpaw_ee.cloud.models.request_log import RequestLog
from pocketpaw_ee.cloud.models.sense_preference import WorkspaceSensePreference
from pocketpaw_ee.cloud.models.session import Session
from pocketpaw_ee.cloud.models.session_transcript import SessionTranscriptDoc
from pocketpaw_ee.cloud.models.ship import ShipApp, ShipBox, ShipDeploy
from pocketpaw_ee.cloud.models.site import Site, SiteDomain
from pocketpaw_ee.cloud.models.site_ai import (
    SiteAiBudgetDoc,
    SiteAiCallDoc,
    SiteAiCounterDoc,
    SiteAiPresetDoc,
    SiteAiSettingsDoc,
    SiteAiTranscriptDoc,
)
from pocketpaw_ee.cloud.models.site_dashboard import (
    SiteDashboard,
    SiteDashboardJob,
    SiteDashboardRevision,
)
from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief
from pocketpaw_ee.cloud.models.site_do_teardown import SiteDoTeardown
from pocketpaw_ee.cloud.models.site_draft_worker import SiteDraftWorker
from pocketpaw_ee.cloud.models.site_export import SiteExport
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.cloud.models.site_rate_counter import SiteRateCounter
from pocketpaw_ee.cloud.models.site_redirect import SiteRedirect
from pocketpaw_ee.cloud.models.site_secret import SiteSecret
from pocketpaw_ee.cloud.models.site_template import SiteTemplate
from pocketpaw_ee.cloud.models.social_experiment import SocialExperiment
from pocketpaw_ee.cloud.models.social_idea import SocialIdea
from pocketpaw_ee.cloud.models.social_learning import SocialLearning
from pocketpaw_ee.cloud.models.social_link import SocialLink
from pocketpaw_ee.cloud.models.social_post_metric import SocialPostMetric
from pocketpaw_ee.cloud.models.social_postiz_org import SocialPostizOrg
from pocketpaw_ee.cloud.models.social_profile import SocialProfile
from pocketpaw_ee.cloud.models.social_pulse import SocialPulse
from pocketpaw_ee.cloud.models.social_task import SocialTask
from pocketpaw_ee.cloud.models.spend_reconciliation import SpendReconciliation
from pocketpaw_ee.cloud.models.studio_generation import StudioGeneration
from pocketpaw_ee.cloud.models.studio_template import StudioTemplate
from pocketpaw_ee.cloud.models.subscription import Subscription
from pocketpaw_ee.cloud.models.task import Task, TaskAssignee, TaskSource
from pocketpaw_ee.cloud.models.task_attachment import TaskAttachment
from pocketpaw_ee.cloud.models.task_event import TaskEvent
from pocketpaw_ee.cloud.models.temporal_sweep_state import TemporalSweepStateDoc
from pocketpaw_ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership
from pocketpaw_ee.cloud.models.vapid_keypair import VapidKeypair
from pocketpaw_ee.cloud.models.waitlist import WaitlistEntry
from pocketpaw_ee.cloud.models.web_sandbox import WebSandbox
from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings
from pocketpaw_ee.cloud.models.workspace_automation_config import WorkspaceAutomationConfig
from pocketpaw_ee.cloud.models.workspace_job import WorkspaceJobDoc
from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm

# Lazy import to avoid circular imports
FileUpload: type = None  # type: ignore[assignment]
FileFolder: type = None  # type: ignore[assignment]
# Multipart upload sessions. Lazy-loaded with FileUpload (same package) so
# init_beanie registers it without ee.cloud.models hard-importing uploads.
MultipartUpload: type = None  # type: ignore[assignment]
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
    global FileUpload, FileFolder, ShareLink, MultipartUpload
    if FileUpload is None:
        from pocketpaw_ee.cloud.uploads.models import FileFolder as _FileFolder
        from pocketpaw_ee.cloud.uploads.models import FileUpload as _FileUpload
        from pocketpaw_ee.cloud.uploads.multipart_models import (
            MultipartUpload as _MultipartUpload,
        )
        from pocketpaw_ee.cloud.uploads.share_models import ShareLink as _ShareLink

        FileUpload = _FileUpload
        FileFolder = _FileFolder
        ShareLink = _ShareLink
        MultipartUpload = _MultipartUpload
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
    "PlatformAuditEvent",
    "AuditWebhook",
    "AuthSession",
    "BeltRunFeed",
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
    "MultipartUpload",
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
    "LiteLLMTenantKey",
    "ShipApp",
    "ShipBox",
    "ShipDeploy",
    "Meeting",
    "MeetingKnock",
    "MeetingProviderCredentials",
    "MeetingsSettings",
    "MeetingTranscript",
    "MemberIngestState",
    "Mention",
    "Message",
    "Notification",
    "NotificationDeliveryConfig",
    "NotificationOutboxItem",
    "NotificationRateMarker",
    "SiteNotificationSettings",
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
    "SiteDesignBrief",
    "SiteDoTeardown",
    "SiteDraftWorker",
    "SiteExport",
    "SiteDomain",
    "SiteOriginClaim",
    "SiteAiBudgetDoc",
    "SiteAiCallDoc",
    "SiteAiCounterDoc",
    "SiteAiPresetDoc",
    "SiteAiSettingsDoc",
    "SiteAiTranscriptDoc",
    "SiteRateCounter",
    "SiteSecret",
    "IntegrationConnection",
    "IntegrationDemoRecord",
    "IntegrationWebhookEvent",
    "NewsletterSubscriber",
    "SiteIntegration",
    "SiteOrder",
    "SiteTemplate",
    "StudioTemplate",
    "DiscoverListing",
    "SpendReconciliation",
    "StudioGeneration",
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
        # External-delivery outbox (email / signed webhook / Slack). Only
        # ``ee.cloud.notifications.outbox`` reads or writes it.
        NotificationOutboxItem,
        NotificationRateMarker,
        # Per-site owner-notification settings. Only
        # ``ee.cloud.leads.notification_settings`` writes it.
        SiteNotificationSettings,
        FileObj,
        FileUpload,
        FileFolder,
        # Public file share links (FL-12b). Only ``uploads.share_store``
        # writes these; the public GET /share/{token} route reads by token.
        ShareLink,
        # In-flight multipart upload sessions. Only
        # ``ee.cloud.uploads.multipart_store`` reads/writes these. Separate
        # collection from FileUpload on purpose — a session is not a file and
        # must never appear in a library listing; see the model docstring.
        MultipartUpload,
        # Every daily cap's counter: one row per (subject, meter, UTC day).
        # Only ``ee.cloud.metering.service`` reads/writes this. Meters stay
        # separate rows, so a bulk photo import cannot exhaust the ceiling
        # that exists to stop a podcast library.
        DailyUsage,
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
        StudioGeneration,
        Task,
        TaskAttachment,
        TemporalSweepStateDoc,
        Cycle,
        Project,
        PlanSession,
        Meeting,
        MeetingKnock,
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
        # User-saved site templates. Only ``ee.cloud.site_templates.service``
        # writes it.
        SiteTemplate,
        # Discover index (DS-1): one public card per source item. Only
        # ``ee.cloud.discover.service`` / ``service_admin`` write it.
        DiscoverListing,
        # Public partner applications, reviewed by operators. Only
        # ``ee.cloud.partners.service_admin`` reads or writes it.
        PartnerApplication,
        # Studio generations published as templates. Only
        # ``ee.cloud.studio_templates.service`` / ``service_admin`` write it.
        StudioTemplate,
        # AI visibility checks and per-site check state. Only
        # ``ee.cloud.ai_visibility.service`` / ``service_admin`` write them.
        AiVisibilityCheck,
        AiVisibilitySite,
        SiteDesignBrief,
        SiteExport,
        # SF-8 — the proof that a workspace controls an origin. The later
        # concierge crawl reads it before fetching anyone's pages.
        SiteOriginClaim,
        SiteRateCounter,
        # Site AI apps (settings, presets, budget, per-call reservations, spend
        # counters). Only ``pocketpaw_ee.site_ai`` touches them.
        SiteAiSettingsDoc,
        SiteAiPresetDoc,
        SiteAiBudgetDoc,
        SiteAiCallDoc,
        SiteAiCounterDoc,
        SiteAiTranscriptDoc,
        # Per-site secrets (encrypted at rest). Only ``sites.site_secrets`` touches it.
        SiteSecret,
        # Sites integrations (vault, bindings, orders, webhook dedupe, newsletter).
        # Only ``pocketpaw_ee.integrations`` touches them.
        IntegrationConnection,
        SiteIntegration,
        SiteOrder,
        IntegrationWebhookEvent,
        NewsletterSubscriber,
        IntegrationDemoRecord,
        # Draft Workers of site pockets. Only ``sites.draft_worker`` touches it.
        SiteDraftWorker,
        # Durable Object teardowns a site delete could not finish, retried by
        # ``sites.do_metering.sweep_do_teardowns``. Only that module touches it.
        SiteDoTeardown,
        # VS-4 — an address a renamed site gave up, held 30 days for its old
        # workspace. Only ``pocketpaw_ee.sites.service`` reads/writes it.
        ReleasedSlug,
        # A renamed site's old address, 301ing to its current one until the hold
        # ends. Only ``pocketpaw_ee.sites.slug_redirect`` reads/writes it.
        SiteRedirect,
        # A site's AI dashboards, their revisions and generation jobs. Only
        # ``pocketpaw_ee.sites.dashboard_service`` touches them.
        SiteDashboard,
        SiteDashboardRevision,
        SiteDashboardJob,
        # Early-access launch switches (singleton) and the paused-signup waitlist.
        # Only ``ee.cloud.launch_controls`` and ``ee.cloud.early_access`` touch them.
        LaunchControlsDoc,
        WaitlistEntry,
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
        # Growth › Social — the per-workspace company profile and its post
        # ideas. Only ``ee.cloud.growth.social.service`` imports these.
        SocialProfile,
        SocialIdea,
        SocialPulse,
        SocialPostizOrg,
        # Growth › Social daily repo stars and traffic; same import boundary.
        RepoMetric,
        SocialPostMetric,
        # Tracked short links behind /api/v1/go/{code}; counts only, nothing
        # about the requester. Same import boundary.
        SocialLink,
        # The learning loop's experiments and founder-approved learnings; same
        # import boundary.
        SocialExperiment,
        SocialLearning,
        # Each brand's recurring "By hand" tasks; same import boundary.
        SocialTask,
        # Each brand profile's versioned Brand Blueprint. Only
        # ``ee.cloud.growth.social.blueprint`` imports it.
        GrowthBlueprint,
        # Per-unit Blueprint version chains (brand, strategy, plan, play:<id>);
        # same import boundary.
        GrowthBlueprintUnit,
        ReplyDraft,
        PushSubscription,
        VapidKeypair,
        WorkspaceSensePreference,
        AuditEvent,
        PlatformAuditEvent,
        # Platform-wide per-tenant daily spend rollup (chunk 8, Paw Admin
        # PRD). Read by ee.cloud.platform.stats; nothing writes it in this
        # PR (the nightly backfill job is a follow-up — see stats.py).
        PlatformDailyRollup,
        AuditWebhook,
        AuthSession,
        APIKey,
        BeltWorkspaceConfig,
        # A Belt run stage's step feed. Only ``ee.cloud.belt.service`` writes it.
        BeltRunFeed,
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
