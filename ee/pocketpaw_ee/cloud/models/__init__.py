"""Cloud document models — re-exports for Beanie init.

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
"""

from __future__ import annotations

from pocketpaw_ee.cloud.models.agent import Agent, AgentConfig
from pocketpaw_ee.cloud.models.api_key import APIKey
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.audit_webhook import AuditWebhook
from pocketpaw_ee.cloud.models.auth_session import AuthSession
from pocketpaw_ee.cloud.models.belt_workspace_config import BeltWorkspaceConfig
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
from pocketpaw_ee.cloud.models.comment import Comment, CommentAuthor, CommentTarget
from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector
from pocketpaw_ee.cloud.models.cycle import Cycle, CycleDailyPoint
from pocketpaw_ee.cloud.models.fabric_ingest_state import (
    FabricIngestConfig,
    FabricIngestState,
)
from pocketpaw_ee.cloud.models.file import FileObj
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
from pocketpaw_ee.cloud.models.instinct_approval import InstinctApproval
from pocketpaw_ee.cloud.models.invite import Invite
from pocketpaw_ee.cloud.models.lead import Lead, LeadSource
from pocketpaw_ee.cloud.models.meeting import (
    Meeting,
    MeetingProviderCredentials,
    MeetingsSettings,
    MeetingTranscript,
)
from pocketpaw_ee.cloud.models.member_ingest_state import MemberIngestState
from pocketpaw_ee.cloud.models.message import Attachment, Mention, Message, Reaction
from pocketpaw_ee.cloud.models.notification import Notification, NotificationSource
from pocketpaw_ee.cloud.models.planner import PlanSession, PlanSessionAgentGap
from pocketpaw_ee.cloud.models.pocket import Pocket, Widget, WidgetPosition
from pocketpaw_ee.cloud.models.pocket_backend import PocketBackendCredential
from pocketpaw_ee.cloud.models.project import Project
from pocketpaw_ee.cloud.models.read_state import ReadState
from pocketpaw_ee.cloud.models.deep_work_log import DeepWorkLog
from pocketpaw_ee.cloud.models.request_log import RequestLog
from pocketpaw_ee.cloud.models.sense_preference import WorkspaceSensePreference
from pocketpaw_ee.cloud.models.session import Session
from pocketpaw_ee.cloud.models.site import Site, SiteDomain
from pocketpaw_ee.cloud.models.site_rate_counter import SiteRateCounter
from pocketpaw_ee.cloud.models.task import Task, TaskAssignee, TaskSource
from pocketpaw_ee.cloud.models.task_attachment import TaskAttachment
from pocketpaw_ee.cloud.models.task_event import TaskEvent
from pocketpaw_ee.cloud.models.temporal_sweep_state import TemporalSweepStateDoc
from pocketpaw_ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import Workspace, WorkspaceSettings
from pocketpaw_ee.cloud.models.workspace_job import WorkspaceJobDoc

# Lazy import to avoid circular imports
FileUpload: type = None  # type: ignore[assignment]
FileFolder: type = None  # type: ignore[assignment]
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
    global FileUpload, FileFolder
    if FileUpload is None:
        from pocketpaw_ee.cloud.uploads.models import FileFolder as _FileFolder
        from pocketpaw_ee.cloud.uploads.models import FileUpload as _FileUpload

        FileUpload = _FileUpload
        FileFolder = _FileFolder
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
    "ChatRunDoc",
    "Comment",
    "CommentAuthor",
    "CommentTarget",
    "ComposioConnection",
    "Cycle",
    "CycleDailyPoint",
    "FabricIngestConfig",
    "FabricIngestState",
    "FileFolder",
    "FileObj",
    "FileUpload",
    "ForesightBacktest",
    "ForesightPredictionRecord",
    "ForesightProjectedDecision",
    "ForesightRun",
    "ForesightWorkspaceConfig",
    "ForesightWorkspaceScenario",
    "Group",
    "GroupAgent",
    "InstinctApproval",
    "Invite",
    "Lead",
    "LeadSource",
    "Meeting",
    "MeetingProviderCredentials",
    "MeetingsSettings",
    "MeetingTranscript",
    "MemberIngestState",
    "Mention",
    "Message",
    "Notification",
    "NotificationSource",
    "OAuthAccount",
    "PlanSession",
    "PlanSessionAgentGap",
    "Pocket",
    "Project",
    "Reaction",
    "ReadState",
    "DeepWorkLog",
    "RequestLog",
    "Session",
    "Site",
    "SiteDomain",
    "SiteRateCounter",
    "WorkspaceSensePreference",
    "Task",
    "TaskAssignee",
    "TaskAttachment",
    "TaskSource",
    "TaskEvent",
    "TemporalSweepStateDoc",
    "User",
    "Widget",
    "WidgetPosition",
    "Workspace",
    "WorkspaceConnector",
    "WorkspaceJobDoc",
    "WorkspaceMembership",
    "WorkspaceSettings",
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
        Comment,
        Notification,
        FileObj,
        FileUpload,
        FileFolder,
        Workspace,
        WorkspaceConnector,
        ComposioConnection,
        Invite,
        Group,
        InstinctApproval,
        Message,
        ReadState,
        RequestLog,
        DeepWorkLog,
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
