export type HealthResponse = {
  status: string;
  version: string;
  environment: string;
  llm_provider: string;
  llm_configured: boolean;
  database: boolean;
  memory: boolean;
  identity: boolean;
  tools: boolean;
  a2a: boolean;
  workflows?: boolean;
};

export type Message = {
  role: "system" | "user" | "assistant";
  content: string;
};

export type Session = {
  session_id: string;
  messages: Message[];
  created_at: string;
  updated_at: string;
};

export type ChatRequest = {
  session_id: string;
  message: string;
};

export type ChatResponse = {
  session_id: string;
  response: string;
};

/**
 * A stored memory, matching `MemoryOut` (`app/schemas/memory.py`).
 *
 * The previous shape (owner_id / category / source_type / source_message_id)
 * matched nothing: `Memory.to_dict()` emits `memory_type` / `importance` /
 * `last_accessed_at` / `metadata` and none of those four. The page read
 * `category` for grouping and got `undefined` for every row.
 */
export type Memory = {
  id: string;
  memory_type: "semantic" | "episodic" | "relationship";
  content: string;
  importance: number;
  confidence: number;
  created_at?: string | null;
  last_accessed_at?: string | null;
  metadata: Record<string, unknown>;
};

export type MemoryCreateRequest = {
  content: string;
  memory_type?: Memory["memory_type"];
  importance?: number;
  confidence?: number;
};

export type MemorySearchRequest = {
  query: string;
  limit?: number;
  memory_types?: Memory["memory_type"][];
};

/**
 * A search hit.
 *
 * The backend nests the memory (`MemorySearchResultOut` = `{memory, similarity}`)
 * rather than flattening it. The old flat type declared `memory_id`, `category`,
 * `content` and `confidence` at the top level, so `item.similarity` was the only
 * field that worked and `item.memory_id` (used for delete) was `undefined`.
 */
export type MemorySearchResult = {
  memory: Memory;
  similarity: number;
};

export type PublicIdentity = {
  agent_id: string;
  public_key: string;
  key_algorithm?: string;
  fingerprint: string;
};

/**
 * A capability advertised in an agent card (`AgentCapabilityOut`).
 *
 * The old type declared `capabilities: string[]`; the card carries objects, so
 * anything rendering a capability name printed "[object Object]".
 */
export type AgentCapability = {
  name: string;
  description: string;
  data_category: string;
};

/**
 * The signed agent card (`AgentCardResponse`, `app/schemas/discovery.py`).
 *
 * Field names are the wire names: `display_name` and a single `endpoint`, not
 * `name` / `endpoints.a2a` / `description`. `description` is not in the card at
 * all - the codebase had invented it, along with a nested `endpoints` object.
 */
export type AgentCard = {
  type: string;
  protocol: string;
  version: string;
  agent_id: string;
  display_name: string;
  public_key: string;
  endpoint: string;
  capabilities: AgentCapability[];
  supported_purposes: string[];
  issued_at: string;
  expires_at: string;
  signature: string;
};

/**
 * Trusted (remote) agents.
 *
 * Re-exported from the API layer rather than redeclared. The previous
 * declaration here listed `public_key_b64`, `endpoint_url` and `capabilities`,
 * none of which the backend returns (`TrustedAgentOut` has `endpoint` and no
 * key or capability list), so every page reading them silently got
 * `undefined`. One definition, taken from the code that talks to the API.
 */
export type { TrustedAgent } from "@/lib/api/a2a";

export type DiscoveredAgent = {
  card: AgentCard;
  verified: boolean;
  endpoint_url: string;
  is_trusted: boolean;
};

export type PolicyDecision = "ALLOW" | "ASK" | "DENY";

export type ConsentDecision = "ALLOW" | "DENY";

/**
 * A policy rule or recorded consent, matching `PolicyOut` / `ConsentOut`.
 *
 * The policy dimensions a person actually reasons about - who is asking
 * (`requester_agent_id`), what data (`data_category`), why (`purpose`), and how
 * much is disclosed (`disclosure_scope`) - were absent from the previous
 * declarations, which instead carried `resource` and `rule_type`. Nothing
 * renamed them; they were invented, so the page could not show the peer or the
 * purpose of a rule, and printed the purpose under the label "on".
 */
export type PolicyRule = {
  id: string;
  requester_agent_id: string;
  data_category: string;
  action: string;
  purpose: string;
  decision: PolicyDecision;
  disclosure_scope: "category" | "value" | "summary" | "full";
  priority: number;
  starts_at?: string | null;
  expires_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type Consent = {
  id: string;
  requester_agent_id: string;
  data_category: string;
  action: string;
  purpose: string;
  decision: ConsentDecision;
  disclosure_scope: PolicyRule["disclosure_scope"];
  expires_at?: string | null;
  single_use: boolean;
  used_at?: string | null;
  created_at?: string | null;
};

export type PolicyEvaluateRequest = {
  action: string;
  resource: string;
  peer_agent_id?: string;
  /** The declared purpose. The policy engine matches on it, so it is required
   *  by the API schema - a default of "user-evaluation" was being invented. */
  purpose?: string;
  context?: Record<string, unknown>;
};

export type PolicyEvaluateResponse = {
  decision: PolicyDecision;
  reason: string;
  rule_id?: string;
  /** True when the decision is ASK and a human must answer. */
  requires_user_approval: boolean;
  disclosure_scope?: PolicyRule["disclosure_scope"];
};

/** JSON Schema for a tool's arguments, as the MCP tool definition declares it. */
export type ToolInputSchema = {
  type?: string;
  properties?: Record<string, unknown>;
  required?: string[];
};

/**
 * Tool metadata (`ToolMetadataOut`).
 *
 * The schema returns exactly `name`, `description` and `inputSchema` - the
 * JSON-Schema field is camelCase, and there is no `parameters` and no
 * `requires_policy`. The runner modal prefilled its parameter form from
 * `tool.parameters.properties`, which was therefore always empty: every tool
 * opened with a blank `{}` and no hint of what it accepts.
 */
export type ToolInfo = {
  name: string;
  description: string;
  inputSchema: ToolInputSchema;
};

export type ToolExecuteRequest = {
  tool_name: string;
  arguments: Record<string, unknown>;
  purpose: string;
  request_id?: string;
};

/**
 * Tool execution result (`ToolExecuteResponse`).
 *
 * `data` and `error` are the real field names; the previous `result` and
 * `duration_ms`/`success`-only shape meant a failed execution's reason was
 * dropped on the floor (`error` is an object here, not a string).
 */
export type ToolExecuteResponse = {
  success: boolean;
  status: string;
  tool_name: string;
  request_id: string;
  data?: Record<string, unknown> | null;
  error?: Record<string, string> | null;
};

/**
 * A2A task statuses.
 *
 * Taken from `app.a2a.models.TaskStatus` - the enum that produces the value.
 * The previous list invented `running` and `negotiating` and omitted
 * `pending_approval`, which is the ONE status that means "waiting for you":
 * the approve/reject buttons were gated on `pending`, so the actions the Tasks
 * page exists for were unreachable.
 */
export type A2ATaskStatus =
  | "pending"
  | "pending_approval"
  | "accepted"
  | "waiting_remote"
  | "rejected"
  | "completed"
  | "failed"
  | "expired"
  | "cancelled";

/**
 * A2A task, matching `TaskOut` (`app/schemas/tasks.py`) field for field.
 *
 * The previous version declared `sender_id` / `recipient_id` / `payload` /
 * `round` / `max_rounds` / `history` / `result`, none of which the API returns.
 * Every one of those reads produced `undefined`, so the page rendered a bare
 * status badge, "Round undefined", and two empty JSON blocks - with nothing to
 * indicate that anything had failed. The backend names are used verbatim so a
 * rename shows up as a TypeScript error rather than a blank panel.
 */
export type A2ATask = {
  task_id: string;
  sender_agent_id: string;
  recipient_agent_id: string;
  status: A2ATaskStatus;
  task_type?: string | null;
  purpose?: string | null;
  request_payload?: Record<string, unknown> | null;
  response_payload?: Record<string, unknown> | null;
  negotiation_round: number;
  failure_reason?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  expires_at?: string | null;
  completed_at?: string | null;
};

export type WorkflowStatus =
  | "pending"
  | "running"
  | "waiting_approval"
  | "waiting_remote"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired";

export type StepStatus =
  | "pending"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "skipped";

export type WorkflowStepOut = {
  step_id: string;
  workflow_id: string;
  step_number: number;
  step_type: string;
  status: StepStatus;
  input_payload: Record<string, unknown>;
  output_payload?: Record<string, unknown> | null;
  task_id?: string | null;
  attempt_count: number;
  max_attempts: number;
  failure_reason?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type WorkflowOut = {
  workflow_id: string;
  owner_id: string;
  workflow_type: string;
  purpose: string;
  status: WorkflowStatus;
  current_step_number: number;
  context_data: Record<string, unknown>;
  workflow_metadata: Record<string, unknown>;
  failure_reason?: string | null;
  expires_at?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  steps: WorkflowStepOut[];
};

export type AuditEntry = {
  id: string;
  category: "policy" | "tool" | "a2a" | "task" | "workflow" | "autonomy";
  timestamp: string;
  action: string;
  actor: string;
  status: string;
  details: Record<string, unknown>;
};

export type AutonomyMode = "off" | "assisted" | "bounded" | "fully_delegated";

/**
 * Autonomy configuration (`AutonomyConfigOut`).
 *
 * The ceilings are `max_remote_tasks` and `max_tool_calls` - the previous names
 * carried a `_per_run` suffix the backend does not use, so both read
 * `undefined`. `require_approval_for_unknown_actions` was missing, which is the
 * setting that governs the fail-closed default.
 */
export type AutonomyConfigOut = {
  id: string;
  owner_id: string;
  mode: AutonomyMode;
  enabled: boolean;
  max_steps_per_run: number;
  max_runtime_seconds: number;
  max_remote_tasks: number;
  max_tool_calls: number;
  require_approval_for_unknown_actions: boolean;
  require_approval_for_external_communication: boolean;
  require_approval_for_sensitive_data: boolean;
  created_at?: string | null;
  updated_at?: string | null;
};

export type AutonomyDecisionOut = {
  decision_id: string;
  owner_id: string;
  run_id?: string | null;
  trigger: string;
  goal: string;
  proposed_action: string;
  action_type: string;
  purpose: string;
  risk_level: string;
  required_capability?: string | null;
  required_data_categories: string[];
  decision: "allow" | "ask" | "deny" | "stop";
  reason: string;
  policy_decision?: string | null;
  consent_decision?: string | null;
  created_at?: string | null;
};

export type AutonomyApprovalOut = {
  approval_id: string;
  run_id: string;
  decision_id?: string | null;
  owner_id: string;
  status: "pending" | "approved" | "rejected";
  requested_action: string;
  purpose: string;
  risk_level: string;
  required_data: Record<string, unknown>;
  recipient?: string | null;
  notes?: string | null;
  created_at?: string | null;
  resolved_at?: string | null;
};

export type AutonomyRunOut = {
  id: string;
  owner_id: string;
  workflow_id?: string | null;
  goal: string;
  status: string;
  current_step: number;
  steps_executed: number;
  tool_calls: number;
  remote_tasks: number;
  plan: Array<Record<string, unknown>>;
  context_data: Record<string, unknown>;
  failure_reason?: string | null;
  stop_reason?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  decisions: AutonomyDecisionOut[];
  approvals: AutonomyApprovalOut[];
};

