import type { Severity, IncidentStatus } from "../types";

const SEVERITY_STYLES: Record<Severity, string> = {
  P0: "background:#ef4444;color:#fff;",
  P1: "background:#f97316;color:#fff;",
  P2: "background:#eab308;color:#000;",
  P3: "background:#6b7280;color:#fff;",
};

const STATUS_STYLES: Record<IncidentStatus, string> = {
  open:              "background:#3b82f6;color:#fff;",
  triaging:          "background:#a855f7;color:#fff;",
  diagnosing:        "background:#f97316;color:#fff;",
  fixing:            "background:#f59e0b;color:#000;",
  reviewing:         "background:#06b6d4;color:#000;",
  awaiting_approval: "background:#8b5cf6;color:#fff;",
  resolved:          "background:#22c55e;color:#000;",
  noise:             "background:#6b7280;color:#fff;",
  duplicate:         "background:#374151;color:#9ca3af;",
};

interface Props {
  type: "severity" | "status";
  value: Severity | IncidentStatus;
}

export function StatusBadge({ type, value }: Props) {
  const style = type === "severity"
    ? SEVERITY_STYLES[value as Severity]
    : STATUS_STYLES[value as IncidentStatus];

  return (
    <span style={{
      ...Object.fromEntries(style.split(";").filter(Boolean).map(s => s.split(":") as [string, string])),
      padding: "2px 8px",
      borderRadius: 4,
      fontSize: 11,
      fontWeight: 700,
      letterSpacing: "0.05em",
      textTransform: "uppercase",
    }}>
      {value}
    </span>
  );
}

export function HealthDot({ healthy }: { healthy: boolean }) {
  return (
    <span style={{
      display: "inline-block",
      width: 8,
      height: 8,
      borderRadius: "50%",
      background: healthy ? "#22c55e" : "#ef4444",
      marginRight: 6,
      flexShrink: 0,
    }} />
  );
}
