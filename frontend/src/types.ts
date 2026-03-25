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

export interface IncidentMetrics {
  total: number;
  active: number;
  resolved: number;
  noise: number;
  duplicate: number;
  avg_mttr_seconds: number | null;
  false_positive_rate: number;
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

export interface DashboardData {
  ecs: ECSService[];
  ecs_task_clusters: ECSTaskCluster[];
  ec2: EC2Data;
  digital_ocean: DOData;
  cloudflare: CFData;
  queue: QueueStats;
  incidents: IncidentMetrics;
}

export type Severity = "P0" | "P1" | "P2" | "P3";
export type IncidentStatus =
  | "open"
  | "triaging"
  | "diagnosing"
  | "fixing"
  | "reviewing"
  | "awaiting_approval"
  | "resolved"
  | "noise"
  | "duplicate";

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
  human_decision: string | null;
  outcome: string | null;
  detected_at: string;
  resolved_at: string | null;
  mttr_seconds: number | null;
  age_seconds: number;
}

export type WSMessage =
  | { type: "init"; incidents: Incident[]; metrics: IncidentMetrics; queue: QueueStats; timestamp: string }
  | { type: "event"; event: ErrorEvent }
  | { type: "incident_update"; incident: Incident }
  | { type: "pong" };
