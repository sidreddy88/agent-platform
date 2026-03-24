import React from "react";
import type { Incident, IncidentMetrics } from "../types";
import { StatusBadge } from "./StatusBadge";

interface Props {
  incidents: Incident[];
  metrics: IncidentMetrics | null;
  connected: boolean;
}

function ago(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function mttr(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

const SOURCE_ICON: Record<string, string> = {
  cloudwatch: "☁",
  digital_ocean: "🌊",
  cloudflare: "🔶",
  application: "⚙",
};

export function IncidentFeed({ incidents, metrics, connected }: Props) {
  return (
    <div style={panel}>
      <div style={header}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <h2 style={title}>Incident Feed</h2>
          <span style={dot(connected)} title={connected ? "Live" : "Reconnecting..."} />
        </div>
        {metrics && (
          <div style={metricRow}>
            <MetricChip label="Active" value={metrics.active} color="#3b82f6" />
            <MetricChip label="Resolved" value={metrics.resolved} color="#22c55e" />
            <MetricChip label="Noise" value={metrics.noise} color="#6b7280" />
            <MetricChip
              label="False +ve"
              value={`${(metrics.false_positive_rate * 100).toFixed(0)}%`}
              color="#94a3b8"
            />
            {metrics.avg_mttr_seconds !== null && (
              <MetricChip
                label="Avg MTTR"
                value={mttr(metrics.avg_mttr_seconds)}
                color="#a855f7"
              />
            )}
          </div>
        )}
      </div>

      <div style={list}>
        {incidents.length === 0 && (
          <p style={empty}>No active incidents. All systems healthy.</p>
        )}
        {incidents.map((inc) => (
          <div key={inc.id} style={incidentCard}>
            <div style={incidentTop}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <span style={sourceIcon}>{SOURCE_ICON[inc.error_event.source] ?? "·"}</span>
                <StatusBadge type="severity" value={inc.error_event.severity} />
                <StatusBadge type="status" value={inc.status} />
                <span style={incidentTitle}>{inc.error_event.title}</span>
              </div>
              <span style={timestamp}>{ago(inc.age_seconds)}</span>
            </div>
            <p style={description}>{inc.error_event.description}</p>
            <div style={incidentMeta}>
              <span style={metaItem}>
                <span style={metaLabel}>Service</span> {inc.error_event.service}
              </span>
              {inc.pr_url && (
                <span style={metaItem}>
                  <a href={inc.pr_url} target="_blank" rel="noreferrer" style={prLink}>PR</a>
                </span>
              )}
              {inc.confidence !== null && (
                <span style={metaItem}>
                  <span style={metaLabel}>Confidence</span> {Math.round(inc.confidence * 100)}%
                </span>
              )}
              {inc.mttr_seconds !== null && (
                <span style={metaItem}>
                  <span style={metaLabel}>MTTR</span> {mttr(inc.mttr_seconds)}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function MetricChip({ label, value, color }: { label: string; value: string | number; color: string }) {
  return (
    <div style={{ textAlign: "center" }}>
      <div style={{ fontSize: 16, fontWeight: 700, color }}>{value}</div>
      <div style={{ fontSize: 10, color: "#64748b" }}>{label}</div>
    </div>
  );
}

const panel: React.CSSProperties = {
  background: "#1e2130",
  border: "1px solid #2d3149",
  borderRadius: 12,
  padding: "16px 20px",
};

const header: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  marginBottom: 16,
  flexWrap: "wrap",
  gap: 12,
};

const title: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 700,
  color: "#94a3b8",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
};

const dot = (connected: boolean): React.CSSProperties => ({
  width: 8,
  height: 8,
  borderRadius: "50%",
  background: connected ? "#22c55e" : "#f97316",
  flexShrink: 0,
});

const metricRow: React.CSSProperties = {
  display: "flex",
  gap: 20,
  background: "#161927",
  borderRadius: 8,
  padding: "8px 16px",
};

const list: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 8,
  maxHeight: 460,
  overflowY: "auto",
};

const empty: React.CSSProperties = {
  fontSize: 13,
  color: "#4b5563",
  fontStyle: "italic",
  textAlign: "center",
  padding: "24px 0",
};

const incidentCard: React.CSSProperties = {
  background: "#161927",
  border: "1px solid #2d3149",
  borderRadius: 8,
  padding: "10px 14px",
};

const incidentTop: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  gap: 8,
  marginBottom: 6,
};

const sourceIcon: React.CSSProperties = {
  fontSize: 14,
};

const incidentTitle: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#e2e8f0",
};

const timestamp: React.CSSProperties = {
  fontSize: 11,
  color: "#64748b",
  flexShrink: 0,
};

const description: React.CSSProperties = {
  fontSize: 12,
  color: "#94a3b8",
  marginBottom: 8,
  lineHeight: 1.4,
};

const incidentMeta: React.CSSProperties = {
  display: "flex",
  gap: 16,
  flexWrap: "wrap",
};

const metaItem: React.CSSProperties = {
  fontSize: 11,
  color: "#94a3b8",
};

const metaLabel: React.CSSProperties = {
  color: "#4b5563",
  marginRight: 3,
};

const prLink: React.CSSProperties = {
  color: "#60a5fa",
  textDecoration: "none",
  fontWeight: 600,
};
