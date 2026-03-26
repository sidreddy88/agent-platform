import React from "react";
import type { MongoDBData } from "../types";

interface Props {
  data: MongoDBData;
}

function HealthDot({ ok }: { ok: boolean }) {
  return (
    <span style={{
      display: "inline-block",
      width: 8, height: 8,
      borderRadius: "50%",
      background: ok ? "#22c55e" : "#f87171",
      flexShrink: 0,
      marginTop: 2,
    }} />
  );
}

function fmt(v: number | null | undefined, decimals = 1, unit = ""): string {
  if (v == null) return "—";
  return `${v.toFixed(decimals)}${unit}`;
}

export function MongoDBPillar({ data }: Props) {
  const allHealthy = data.healthy === data.total && data.total > 0;
  const statusColor = data.error ? "#f87171" : allHealthy ? "#22c55e" : "#f59e0b";
  const statusText = data.error ? "ERROR" : allHealthy ? "HEALTHY" : `${data.healthy}/${data.total}`;

  return (
    <div style={card}>
      <div style={cardHeader}>
        <h2 style={cardTitle}>MongoDB Atlas</h2>
        <span style={{ fontSize: 11, color: statusColor, fontWeight: 600 }}>{statusText}</span>
      </div>

      {data.error && <p style={errText}>{data.error}</p>}

      {!data.error && data.clusters.length === 0 && (
        <p style={hint}>No clusters configured.</p>
      )}

      {data.clusters.map((c) => (
        <div key={c.name} style={clusterRow}>
          <div style={clusterTop}>
            <HealthDot ok={c.healthy} />
            <span style={clusterName}>{c.name}</span>
            <span style={stateTag(c.state)}>{c.state}</span>
            {c.mongo_version && (
              <span style={version}>v{c.mongo_version}</span>
            )}
          </div>

          <div style={metricsGrid}>
            <div style={metric}>
              <span style={metricLabel}>CONNECTIONS</span>
              <span style={metricValue}>{c.connections ?? "—"}</span>
            </div>
            <div style={metric}>
              <span style={metricLabel}>DISK</span>
              <span style={{
                ...metricValue,
                color: c.disk_used_pct != null && c.disk_used_pct > 80 ? "#f87171" : "#e2e8f0",
              }}>
                {fmt(c.disk_used_pct, 1, "%")}
              </span>
            </div>
            <div style={metric}>
              <span style={metricLabel}>OPS/SEC</span>
              <span style={metricValue}>{fmt(c.ops_per_sec, 1)}</span>
            </div>
            <div style={metric}>
              <span style={metricLabel}>REPL LAG</span>
              <span style={{
                ...metricValue,
                color: c.replication_lag_sec != null && c.replication_lag_sec > 10 ? "#f59e0b" : "#e2e8f0",
              }}>
                {c.replication_lag_sec != null ? `${c.replication_lag_sec.toFixed(1)}s` : "—"}
              </span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

const card: React.CSSProperties = {
  background: "#1e2130",
  border: "1px solid #2d3149",
  borderRadius: 12,
  padding: "16px 20px",
};

const cardHeader: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  marginBottom: 14,
};

const cardTitle: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 700,
  color: "#94a3b8",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  margin: 0,
};

const clusterRow: React.CSSProperties = {
  borderTop: "1px solid #2d3149",
  paddingTop: 10,
  marginTop: 10,
};

const clusterTop: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  marginBottom: 8,
};

const clusterName: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#e2e8f0",
  flex: 1,
};

const stateTag = (state: string): React.CSSProperties => ({
  fontSize: 10,
  fontWeight: 700,
  letterSpacing: "0.06em",
  padding: "2px 7px",
  borderRadius: 4,
  background: state === "IDLE" ? "#14532d" : "#451a03",
  color: state === "IDLE" ? "#22c55e" : "#f59e0b",
});

const version: React.CSSProperties = {
  fontSize: 11,
  color: "#475569",
};

const metricsGrid: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(4, 1fr)",
  gap: 8,
};

const metric: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
};

const metricLabel: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  letterSpacing: "0.06em",
  color: "#475569",
  textTransform: "uppercase",
};

const metricValue: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#e2e8f0",
  fontFamily: "monospace",
};

const errText: React.CSSProperties = {
  fontSize: 12,
  color: "#f87171",
  margin: 0,
};

const hint: React.CSSProperties = {
  fontSize: 13,
  color: "#4b5563",
  fontStyle: "italic",
  margin: 0,
};
