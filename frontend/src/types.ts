export interface ECSService {
  service: string;
  cluster: string;
  running: number;
  desired: number;
  pending?: number;
  status: string;
  healthy: boolean;
  recent_events?: string[];
  error?: string;
}

export interface Droplet {
  id: number;
  name: string;
  status: string;
  healthy: boolean;
  region: string;
  ip: string | null;
  size: string;
  load_1: number | null;
  memory_percent: number | null;
}

export interface DOData {
  droplets: Droplet[];
  total: number;
  healthy: number;
  error?: string;
}

export interface CFZone {
  zone_name: string;
  total_requests: number;
  error_rate: number;
  cache_hit_rate: number;
  http_5xx: number;
  http_4xx: number;
  threats_blocked: number;
  healthy: boolean;
}

export interface CFData {
  zones: CFZone[];
  total_requests: number;
  overall_error_rate: number;
  overall_cache_hit_rate: number;
  healthy: boolean;
  error?: string;
}

export interface QueueStats {
  queue_size: number;
  total_enqueued: number;
  total_dequeued: number;
}

export interface PipelineStats {
  sql_dedup: number;
  regression: number;
  rag_hit: number;
  cold_start: number;
  sql_dedup_pct: number;
  regression_pct: number;
  rag_hit_pct: number;
  cold_start_pct: number;
}

export interface IncidentMetrics {
  total: number;
  active: number;
  resolved: number;
  noise: number;
  duplicate: number;
  avg_mttr_seconds: number | null;
  false_positive_rate: number;
  pipeline_stats?: PipelineStats;
}

export interface EC2Instance {
  instance_id: string;
  state: string;
  instance_type: string;
  public_ip: string | null;
  private_ip: string | null;
  cpu_utilization: number | null;
  status_checks: string;
  healthy: boolean;
  error?: string;
}

export interface EC2Data {
  instances: EC2Instance[];
  total: number;
  healthy: number;
}

export interface ECSTaskCluster {
  cluster: string;
  running_tasks: number;
  recent_failures: number;
  healthy: boolean;
  error?: string;
}

export interface ALBData {
  name: string;
  dns_name: string;
  state: string;
  healthy_targets: number;
  unhealthy_targets: number;
  total_targets: number;
  request_count: number | null;
  http_5xx: number | null;
  healthy: boolean;
  error?: string;
}

export interface WorkflowRun {
  id: number;
  workflow_name: string;
  run_number: number;
  status: string;
  conclusion: string | null;
  display_conclusion: string;
  commit_sha: string;
  commit_message: string;
  actor: string;
  url: string;
  created_at: string;
  healthy: boolean;
}

export interface GitHubData {
  repo: string;
  branch: string;
  runs: WorkflowRun[];
  latest_healthy: boolean | null;
  error?: string;
}

export interface MongoDBCluster {
  name: string;
  state: string;
  mongo_version: string;
  connections: number | null;
  disk_used_pct: number | null;
  ops_per_sec: number | null;
  replication_lag_sec: number | null;
  healthy: boolean;
  error?: string;
}

export interface MongoDBData {
  clusters: MongoDBCluster[];
  total: number;
  healthy: number;
  error?: string;
}

export interface DashboardData {
  ecs: ECSService[];
  ecs_task_clusters: ECSTaskCluster[];
  ec2: EC2Data;
  digital_ocean: DOData;
  cloudflare: CFData;
  alb: ALBData[];
  mongodb: MongoDBData;
  github: GitHubData;
  queue: QueueStats;
  incidents: IncidentMetrics;
}

export type Severity = "P0" | "P1" | "P2" | "P3";
export type IncidentStatus =
  | "open"
  | "triaging"
  | "diagnosing"
  | "fixing"
  | "awaiting_fix_approval"
  | "reviewing"
  | "awaiting_approval"
  | "resolved"
  | "rejected"
  | "noise"
  | "duplicate"
  | "verification_failed"
  | "awaiting_refix_approval";

export interface ErrorEvent {
  id: string;
  source: string;
  severity: Severity;
  title: string;
  description: string;
  service: string;
  resource_id: string | null;
  detected_at: string;
}

export interface Incident {
  id: string;
  error_event: ErrorEvent;
  status: IncidentStatus;
  triage_decision: string | null;
  diagnosis: string | null;
  confidence: number | null;
  pr_url: string | null;
  pr_number: number | null;
  human_decision: string | null;
  human_notes: string | null;
  pending_fix_file: string | null;
  pending_fix_old: string | null;
  pending_fix_new: string | null;
  pending_fix_critique: string | null;
  outcome: string | null;
  archived: boolean;
  wrong_fix: boolean;
  wrong_fix_notes: string | null;
  merge_decision: "merge_now" | "refix_first" | null;
  merge_decision_reasoning: string | null;
  clarity_summary: string | null;
  clarity_pr_url: string | null;
  clarity_pr_number: number | null;
  detected_at: string;
  resolved_at: string | null;
  mttr_seconds: number | null;
  age_seconds: number;
}

export interface AgentRun {
  run_id: string;
  agent_name: string;
  incident_id: string | null;
  status: "running" | "completed" | "failed";
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  error_message: string | null;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

export interface AgentStats {
  agent_name: string;
  runs_today: number;
  errors_today: number;
  error_rate: number;
  avg_duration_ms: number | null;
  currently_active: number;
}

export interface AgentPR {
  incident_id: string;
  pr_url: string;
  pr_number: number | null;
  title: string;
  service: string;
  severity: string | null;
  status: IncidentStatus;
  confidence: number | null;
  diagnosis: string | null;
  human_decision: string | null;
  review_posted: boolean;
  pr_branch: string | null;
  pr_files_changed: string[];
  pr_test_added: boolean;
  occurrences_24h: number | null;
  pr_created_at: string | null;
  resolved_at: string | null;
  mttr_seconds: number | null;
  agent_runs: AgentRun[];
}

export interface AgentSnapshot {
  active_runs: AgentRun[];
  pipeline_activity: AgentRun[];  // active + completed in last 30s
  recent_errors: AgentRun[];
  stats: AgentStats[];
}

export interface ScanLogEntry {
  ts: string;
  level: "info" | "event" | "error" | "done";
  message: string;
}

export interface PendingEvent {
  id: string;
  first_line: string;
  full_description: string;
  service: string;
  error_type: string;
  log_group: string;
  detected_at: string;
}

export type WSMessage =
  | { type: "init"; incidents: Incident[]; metrics: IncidentMetrics; queue: QueueStats; agents?: AgentSnapshot; pending_events?: PendingEvent[]; timestamp: string }
  | { type: "event"; event: ErrorEvent }
  | { type: "incident_update"; incident: Incident }
  | { type: "agent_status"; agents: AgentSnapshot }
  | { type: "scan_progress"; ts: string; level: ScanLogEntry["level"]; message: string }
  | { type: "pending_event_added"; event: PendingEvent }
  | { type: "pending_event_removed"; id: string }
  | { type: "pending_events_cleared" }
  | { type: "incidents_cleared"; incidents: Incident[]; deleted: number }
  | { type: "pong" };
