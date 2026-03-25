import React from "react";
import type { ALBData } from "../types";
import { HealthDot } from "./StatusBadge";

interface Props {
  albs: ALBData[];
}

export function ALBPillar({ albs }: Props) {
  const healthy = albs.filter((a) => a.healthy).length;

  return (
    <div style={card}>
      <div style={cardHeader}>
        <h2 style={cardTitle}>Load Balancer</h2>
        <span style={counter(healthy === albs.length)}>
          {healthy}/{albs.length} healthy
        </span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {albs.length === 0 && <p style={empty}>No ALBs configured.</p>}
        {albs.map((alb) => (
          <div key={alb.name} style={row(alb.healthy)}>
            <div style={{ display: "flex", alignItems: "center", flex: 1, minWidth: 0 }}>
              <HealthDot healthy={alb.healthy} />
              <div style={{ minWidth: 0 }}>
                <div style={nameStyle}>{alb.name}</div>
                <div style={meta}>
                  {alb.error ? alb.error : `${alb.state} · ${alb.healthy_targets}/${alb.total_targets} targets`}
                </div>
              </div>
            </div>
            {!alb.error && (
              <div style={{ textAlign: "right", flexShrink: 0 }}>
                <div style={stat}>{alb.request_count ?? "—"} <span style={statLabel}>req</span></div>
                <div style={alb.http_5xx ? errStat : meta}>
                  {alb.http_5xx ?? 0} <span style={statLabel}>5xx</span>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
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
};

const counter = (allGood: boolean): React.CSSProperties => ({
  fontSize: 12,
  fontWeight: 600,
  color: allGood ? "#22c55e" : "#f97316",
});

const row = (healthy: boolean): React.CSSProperties => ({
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  background: healthy ? "#1a2235" : "#2a1a1a",
  border: `1px solid ${healthy ? "#2d3149" : "#7f1d1d"}`,
  borderRadius: 8,
  padding: "8px 12px",
});

const nameStyle: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#e2e8f0",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const meta: React.CSSProperties = {
  fontSize: 11,
  color: "#64748b",
  marginTop: 1,
};

const stat: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 700,
  color: "#e2e8f0",
};

const statLabel: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 400,
  color: "#64748b",
};

const errStat: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: "#f87171",
  marginTop: 1,
};

const empty: React.CSSProperties = {
  fontSize: 12,
  color: "#4b5563",
  fontStyle: "italic",
  textAlign: "center",
  padding: "16px 0",
};
