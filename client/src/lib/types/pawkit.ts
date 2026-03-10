// ---------------------------------------------------------------------------
// PawKit Types — configurable command center dashboards
// ---------------------------------------------------------------------------

export interface PawKitMeta {
  name: string;
  author: string;
  version: string;
  description: string;
  category: string;
  tags: string[];
  icon: string;
  built_in?: boolean;
}

export interface MetricItem {
  label: string;
  source: string; // "workflow:<id>" or "api:<endpoint>"
  field: string;
  format: "number" | "currency" | "percent" | "text";
  trend?: boolean;
}

export interface PanelConfig {
  id: string;
  type: "metrics-row" | "table" | "kanban" | "chart" | "feed" | "markdown" | "agent-roster" | "standup";
  [key: string]: unknown; // type-specific config
}

export interface SectionConfig {
  title: string;
  span: "full" | "left" | "right";
  panels: PanelConfig[];
}

export interface LayoutConfig {
  columns: number;
  sections: SectionConfig[];
}

export interface WorkflowConfig {
  schedule?: string;
  trigger?: { type: string; source: string; condition: string };
  instruction: string;
  output_type: "structured" | "feed" | "task_list" | "document";
  retry?: number;
}

export interface UserConfigField {
  key: string;
  label: string;
  type: "text" | "secret" | "select" | "number";
  placeholder?: string;
  options?: string[];
  help_url?: string;
}

export interface PawKitConfig {
  meta: PawKitMeta;
  layout: LayoutConfig;
  workflows: Record<string, WorkflowConfig>;
  user_config?: UserConfigField[];
  skills?: string[];
  integrations?: {
    required?: string[];
    optional?: string[];
  };
}

export interface InstalledKit {
  id: string;
  config: PawKitConfig;
  user_values: Record<string, string>;
  installed_at: string;
  active: boolean;
}

// ---------------------------------------------------------------------------
// Mission Control types
// ---------------------------------------------------------------------------

export type TaskStatus =
  | "inbox"
  | "assigned"
  | "in_progress"
  | "review"
  | "done"
  | "blocked"
  | "skipped";

export type TaskPriority = "low" | "medium" | "high" | "urgent";

export type DocumentType = "deliverable" | "research" | "protocol" | "template" | "draft";

export type AgentStatus = "idle" | "active" | "blocked" | "offline";
export type AgentLevel = "intern" | "specialist" | "lead";

export type ProjectStatus =
  | "draft"
  | "planning"
  | "awaiting_approval"
  | "approved"
  | "executing"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface AgentProfile {
  id: string;
  name: string;
  role: string;
  description: string;
  session_key: string;
  backend: string;
  status: AgentStatus;
  level: AgentLevel;
  current_task_id: string | null;
  specialties: string[];
  last_heartbeat: string | null;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface MCTask {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  priority: TaskPriority;
  assignee_ids: string[];
  creator_id: string | null;
  parent_task_id: string | null;
  blocked_by: string[];
  tags: string[];
  due_date: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  project_id: string | null;
  task_type: "agent" | "human" | "review";
  blocks: string[];
  active_description: string;
  estimated_minutes: number | null;
  output: string | null;
  retry_count: number;
  max_retries: number;
  timeout_minutes: number | null;
  error_message: string | null;
  metadata: Record<string, unknown>;
}

export interface MCMessage {
  id: string;
  task_id: string;
  from_agent_id: string;
  content: string;
  attachment_ids: string[];
  mentions: string[];
  created_at: string;
}

export interface MCDocument {
  id: string;
  title: string;
  content: string;
  type: DocumentType;
  task_id: string | null;
  author_id: string | null;
  tags: string[];
  version: number;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface MCNotification {
  id: string;
  agent_id: string;
  type: string;
  content: string;
  source_message_id: string | null;
  source_task_id: string | null;
  delivered: boolean;
  read: boolean;
  created_at: string;
}

export interface MCProject {
  id: string;
  title: string;
  description: string;
  status: ProjectStatus;
  planner_agent_id: string | null;
  team_agent_ids: string[];
  task_ids: string[];
  prd_document_id: string | null;
  creator_id: string;
  tags: string[];
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  folder_path?: string;
  file_count?: number;
  metadata: Record<string, unknown>;
}

export interface MCProjectProgress {
  total: number;
  completed: number;
  skipped: number;
  in_progress: number;
  blocked: number;
  human_pending: number;
  percent: number;
}

// ---------------------------------------------------------------------------
// Kit Catalog (Store)
// ---------------------------------------------------------------------------

export interface KitCatalogEntry {
  id: string;
  name: string;
  description: string;
  icon: string;
  category: string;
  author: string;
  tags: string[];
  preview: string;
  installed: boolean;
}
