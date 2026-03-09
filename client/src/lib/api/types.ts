// ---------------------------------------------------------------------------
// PocketPaw API Types
// Matches the Python backend's event model and REST response shapes exactly.
// ---------------------------------------------------------------------------

// -- Media ------------------------------------------------------------------

export interface MediaAttachment {
  type: "image" | "file" | "audio";
  url?: string;
  data?: string;
  filename?: string;
  mime_type?: string;
}

// -- File Context -----------------------------------------------------------

export interface FileContext {
  current_dir?: string;
  open_file?: string;
  open_file_name?: string;
  open_file_extension?: string;
  open_file_size?: number;
  selected_files?: string[];
  source?: string;
}

// -- Chat Messages ----------------------------------------------------------

export interface ChatMessage {
  id?: string;
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
  metadata?: Record<string, unknown>;
  media?: MediaAttachment[];
}

// -- Sessions ---------------------------------------------------------------

export interface Session {
  id: string;
  title: string;
  channel: string;
  last_activity: string;
  message_count: number;
}

export interface SessionListResponse {
  sessions: Session[];
  total: number;
}

// -- Settings ---------------------------------------------------------------

export interface Settings {
  agent_backend: string;

  // Per-backend provider/model/max_turns
  claude_sdk_provider?: string;
  claude_sdk_model?: string;
  claude_sdk_max_turns?: number;
  openai_agents_provider?: string;
  openai_agents_model?: string;
  openai_agents_max_turns?: number;
  google_adk_model?: string;
  google_adk_max_turns?: number;
  codex_cli_model?: string;
  codex_cli_max_turns?: number;
  copilot_sdk_provider?: string;
  copilot_sdk_model?: string;
  copilot_sdk_max_turns?: number;
  opencode_base_url?: string;
  opencode_model?: string;
  opencode_max_turns?: number;

  // Global LLM provider settings
  llm_provider?: string;
  ollama_host?: string;
  ollama_model?: string;
  anthropic_model?: string;
  openai_model?: string;
  openai_compatible_base_url?: string;
  openai_compatible_model?: string;
  openai_compatible_max_tokens?: number;
  gemini_model?: string;

  // Memory
  memory_backend?: string;
  mem0_auto_learn?: boolean;
  mem0_llm_provider?: string;
  mem0_llm_model?: string;
  mem0_embedder_provider?: string;
  mem0_embedder_model?: string;
  mem0_vector_store?: string;
  mem0_ollama_base_url?: string;

  // Security/features
  tool_profile?: string;
  plan_mode?: boolean;
  plan_mode_tools?: string;
  smart_routing_enabled?: boolean;
  bypass_permissions?: boolean;
  injection_scan_enabled?: boolean;
  injection_scan_llm?: boolean;
  self_audit_enabled?: boolean;
  self_audit_schedule?: string;

  // Audio
  tts_provider?: string;
  tts_voice?: string;
  stt_provider?: string;
  stt_model?: string;
  ocr_provider?: string;

  [key: string]: unknown;
}

// -- Skills -----------------------------------------------------------------

export interface Skill {
  name: string;
  description: string;
  argument_hint: string;
}

// -- Channels ---------------------------------------------------------------

export interface ChannelInfo {
  configured: boolean;
  running: boolean;
  autostart: boolean;
  mode?: string;
  error?: string;
}

export type ChannelStatusMap = Record<string, ChannelInfo>;

export interface ChannelConfig {
  channel: string;
  values: Record<string, string>;
  has_secret: Record<string, boolean>;
}

export interface ChannelTestResult {
  ok: boolean;
  error?: string;
}

// -- Backends ---------------------------------------------------------------

export interface BackendInstallHint {
  verify_import?: string;
  verify_attr?: string;
  pip_spec?: string;
  pip_package?: string;
  external_cmd?: string;
  docs_url?: string;
}

export interface BackendInfo {
  name: string;
  displayName: string;
  available: boolean;
  capabilities: string[];
  builtinTools: string[];
  requiredKeys: string[];
  supportedProviders: string[];
  installHint: BackendInstallHint;
  beta: boolean;
}

// -- Memory -----------------------------------------------------------------

export interface MemoryEntry {
  id: string;
  content: string;
  timestamp: string;
  tags: string[];
}

export interface MemorySettings {
  memory_backend: string;
  memory_use_inference: boolean;
  mem0_llm_provider: string;
  mem0_llm_model: string;
  mem0_embedder_provider: string;
  mem0_embedder_model: string;
  mem0_vector_store: string;
  mem0_ollama_base_url: string;
  mem0_auto_learn: boolean;
}

export interface MemoryStats {
  backend: string;
  total_memories: number | string;
  memories_by_type?: {
    long_term?: number;
    daily?: number;
    session?: number;
  };
}

// -- Identity ---------------------------------------------------------------

export interface IdentityFiles {
  identity_file: string;
  soul_file: string;
  style_file: string;
  instructions_file: string;
  user_file: string;
}

export interface IdentitySaveResponse {
  ok: boolean;
  updated: string[];
}

// -- Health -----------------------------------------------------------------

export interface HealthIssue {
  check_id: string;
  name: string;
  category: "config" | "connectivity" | "storage" | "updates";
  status: "ok" | "warning" | "critical";
  message: string;
  fix_hint: string;
  timestamp: string;
  details?: string[] | null;
}

export interface HealthSummary {
  status: "unknown" | "healthy" | "degraded" | "unhealthy";
  check_count: number;
  issues: HealthIssue[];
  error?: string;
  last_check?: string;
}

export interface HealthErrorEntry {
  id: string;
  timestamp: string;
  source: string;
  severity: "error" | "warning" | "critical";
  message: string;
  traceback?: string;
  context?: Record<string, unknown>;
}

export interface SecurityCheckResult {
  check: string;
  passed: boolean;
  message: string;
  fixable: boolean;
}

export interface SecurityAuditResponse {
  total: number;
  passed: number;
  issues: number;
  results: SecurityCheckResult[];
}

// -- MCP Servers ------------------------------------------------------------

export interface MCPServerInfo {
  connected: boolean;
  connecting?: boolean;
  tool_count: number;
  error: string;
  transport: string;
  enabled: boolean;
}

export type MCPStatusMap = Record<string, MCPServerInfo>;

export interface MCPPresetEnvKey {
  key: string;
  label: string;
  required: boolean;
  placeholder: string;
  secret: boolean;
}

export interface MCPPreset {
  id: string;
  name: string;
  description: string;
  icon: string;
  category: string;
  package: string;
  transport: string;
  url?: string;
  docs_url: string;
  needs_args: boolean;
  oauth: boolean;
  installed: boolean;
  env_keys: MCPPresetEnvKey[];
}

export interface MCPTestResponse {
  connected: boolean;
  error?: string;
  tools: { name: string; description: string }[];
}

// -- Version ----------------------------------------------------------------

export interface VersionInfo {
  version: string;
  python: string;
  agent_backend: string;
}

// -- Reminders --------------------------------------------------------------

export interface Reminder {
  id: string;
  text: string;
  trigger_at: string;
  created_at: string;
  time_remaining: string;
}

export interface RemindersResponse {
  reminders: Reminder[];
}

// -- Files ------------------------------------------------------------------

export interface FileEntry {
  name: string;
  isDir: boolean;
  size?: string;
}

// -- Token Usage ------------------------------------------------------------

export interface TokenUsage {
  input_tokens?: number;
  output_tokens?: number;
  [key: string]: unknown;
}

// -- WebSocket Actions (client → server) ------------------------------------
// WS is now push-only. Only authenticate and ping are sent from the client.

export type WSAction =
  | { action: "authenticate"; token: string }
  | { action: "ping" };

// -- WebSocket Events (server → client) -------------------------------------
// WS now carries only push events — notifications, health, reminders, skills.

export interface WSNotification {
  type: "notification";
  content: string;
}

export interface WSError {
  type: "error";
  content: string;
}

export interface WSHealthUpdate {
  type: "health_update";
  data: HealthSummary;
}

export interface WSReminders {
  type: "reminders";
  reminders: Reminder[];
}

export interface WSReminderAdded {
  type: "reminder_added";
  reminder: Reminder;
}

export interface WSReminderDeleted {
  type: "reminder_deleted";
  id: string;
}

export interface WSSkills {
  type: "skills";
  skills: Skill[];
}

export interface WSKitDataUpdate {
  type: "kit_data_update";
  kit_id: string;
  source: string;
  data: unknown;
}

export interface WSMCTaskStarted {
  type: "mc_task_started";
  task_id: string;
  agent_id: string;
  agent_name: string;
  task_title: string;
  timestamp: string;
}

export interface WSMCTaskOutput {
  type: "mc_task_output";
  task_id: string;
  content: string;
  output_type: "message" | "tool_use" | "tool_result";
  timestamp: string;
}

export interface WSMCTaskCompleted {
  type: "mc_task_completed";
  task_id: string;
  agent_id: string;
  status: "completed" | "error" | "stopped" | "timeout";
  error?: string;
  retry?: boolean;
  retry_count?: number;
  max_retries?: number;
  timestamp: string;
}

export interface WSMCTaskRetry {
  type: "mc_task_retry";
  task_id: string;
  agent_id: string;
  retry_count: number;
  max_retries: number;
  error: string;
  timestamp: string;
}

export interface WSMCActivityCreated {
  type: "mc_activity_created";
  activity: Record<string, unknown>;
}

export interface WSDWPlanningPhase {
  type: "dw_planning_phase";
  project_id: string;
  phase: "goal_analysis" | "research" | "prd" | "tasks" | "team";
  message: string;
}

export interface WSDWPlanningComplete {
  type: "dw_planning_complete";
  project_id: string;
  status: string;
  title: string;
  error?: string;
}

export interface WSDWProjectCancelled {
  type: "dw_project_cancelled";
  project_id: string;
  title: string;
}

export interface WSOpenPath {
  type: "open_path";
  path: string;
  action: "navigate" | "view";
}

export type WSEvent =
  | WSNotification
  | WSError
  | WSHealthUpdate
  | WSReminders
  | WSReminderAdded
  | WSReminderDeleted
  | WSSkills
  | WSKitDataUpdate
  | WSMCTaskStarted
  | WSMCTaskOutput
  | WSMCTaskCompleted
  | WSMCTaskRetry
  | WSMCActivityCreated
  | WSDWPlanningPhase
  | WSDWPlanningComplete
  | WSDWProjectCancelled
  | WSOpenPath;

// -- SSE Events (from POST /chat/stream) ------------------------------------

export interface SSEChunk {
  content: string;
  type: string;
}

export interface SSEToolStart {
  tool: string;
  input: Record<string, unknown>;
}

export interface SSEToolResult {
  tool: string;
  output: string;
}

export interface SSEThinking {
  content: string;
}

export interface SSEStreamEnd {
  session_id: string;
  usage?: TokenUsage;
}

export interface SSEAskUser {
  question: string;
  options: Array<string | { label?: string; text?: string; description?: string }>;
}

export interface SSEError {
  detail: string;
}

// -- API Error --------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
