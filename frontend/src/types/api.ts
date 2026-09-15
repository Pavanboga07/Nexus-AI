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

export type Memory = {
  id: string;
  owner_id: string;
  content: string;
  category: string;
  confidence: number;
  source_type: string;
  source_message_id: string | null;
  created_at: string;
  updated_at: string;
  similarity?: number;
};

export type MemoryCreateRequest = {
  content: string;
  category?: string;
  confidence?: number;
  source_type?: string;
};

export type MemorySearchRequest = {
  query: string;
  limit?: number;
  min_similarity?: number;
  category?: string;
};

export type MemorySearchResult = {
  memory_id: string;
  content: string;
  category: string;
  confidence: number;
  similarity: number;
  created_at: string;
};

export type PublicIdentity = {
  agent_id: string;
  public_key_b64: string;
  fingerprint: string;
};

export type AgentCard = {
  agent_id: string;
  name: string;
  description: string;
  version: string;
  endpoints: {
    a2a: string;
    discovery?: string;
  };
  capabilities: string[];
  public_key: string;
  signature: string;
  created_at: string;
  expires_at: string;
};

export type TrustedAgent = {
  agent_id: string;
  display_name: string;
  public_key_b64: string;
  endpoint_url: string;
  capabilities: string[];
  status: "active" | "revoked";
  created_at: string;
  updated_at: string;
};

export type DiscoveredAgent = {
  card: AgentCard;
  verified: boolean;
  endpoint_url: string;
  is_trusted: boolean;
};

export type PolicyDecision = "ALLOW" | "ASK" | "DENY";

export type PolicyRule = {
  id: string;
  rule_type: string;
  action: string;
  resource: string;
  decision: PolicyDecision;
  description?: string;
  priority?: number;
};

export type Consent = {
  consent_id: string;
  owner_id: string;
  action: string;
  resource: string;
  single_use: boolean;
  used: boolean;
  expires_at: string;
  created_at: string;
};

export type PolicyEvaluateRequest = {
  action: string;
  resource: string;
  peer_agent_id?: string;
  context?: Record<string, unknown>;
};

export type PolicyEvaluateResponse = {
  decision: PolicyDecision;
  reason: string;
  rule_id?: string;
};

export type ToolInfo = {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  requires_policy: boolean;
};

export type ToolExecuteRequest = {
  tool_name: string;
  parameters: Record<string, unknown>;
};

export type ToolExecuteResponse = {
  tool_name: string;
  result: unknown;
  duration_ms: number;
  success: boolean;
  error?: string;
};

export type A2ATaskStatus =
  | "pending"
  | "running"
  | "negotiating"
  | "completed"
  | "failed"
  | "cancelled";

export type A2ATask = {
  task_id: string;
  sender_id: string;
  recipient_id: string;
  capability: string;
  payload: Record<string, unknown>;
  status: A2ATaskStatus;
  round: number;
  max_rounds: number;
  history: Array<{
    round: number;
    proposed_by: string;
    terms: Record<string, unknown>;
    timestamp: string;
  }>;
  result?: unknown;
  error?: string;
  created_at: string;
  expires_at: string;
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
  step_number: number;
  step_type: string;
  status: StepStatus;
  input_payload?: Record<string, unknown>;
  output_payload?: Record<string, unknown>;
  task_id?: string;
  attempt_count: number;
  max_attempts: number;
  failure_reason?: string;
  started_at?: string;
  completed_at?: string;
};

export type WorkflowOut = {
  workflow_id: string;
  owner_id: string;
  workflow_type: string;
  purpose: string;
  status: WorkflowStatus;
  current_step_number: number;
  context_data: Record<string, unknown>;
  expires_at: string;
  completed_at?: string;
  created_at?: string;
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

export type AutonomyConfigOut = {
  id: string;
  owner_id: string;
  mode: AutonomyMode;
  enabled: boolean;
  max_steps_per_run: number;
  max_runtime_seconds: number;
  max_tool_calls_per_run: number;
  max_remote_tasks_per_run: number;
  require_approval_for_external_communication: boolean;
  require_approval_for_sensitive_data: boolean;
  auto_stop_on_anomaly: boolean;
  created_at: string;
  updated_at: string;
};

export type AutonomyDecisionOut = {
  decision_id: string;
  run_id?: string;
  trigger: string;
  goal: string;
  proposed_action: string;
  action_type: string;
  purpose: string;
  risk_level: string;
  decision: "allow" | "ask" | "deny" | "stop";
  reason: string;
  policy_decision?: string;
  created_at: string;
};

export type AutonomyApprovalOut = {
  approval_id: string;
  run_id: string;
  status: "pending" | "approved" | "rejected";
  requested_action: string;
  purpose: string;
  risk_level: string;
  required_data?: Record<string, unknown>;
  recipient?: string;
  notes?: string;
  created_at: string;
  resolved_at?: string;
};

export type AutonomyRunOut = {
  id: string;
  owner_id: string;
  goal: string;
  status: string;
  current_step: number;
  steps_executed: number;
  tool_calls: number;
  remote_tasks: number;
  plan?: Array<Record<string, unknown>>;
  context_data?: Record<string, unknown>;
  failure_reason?: string;
  stop_reason?: string;
  started_at?: string;
  completed_at?: string;
  created_at: string;
  decisions?: AutonomyDecisionOut[];
  approvals?: AutonomyApprovalOut[];
};

