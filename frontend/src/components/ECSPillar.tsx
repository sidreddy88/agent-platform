import React from "react";
import type { ECSService } from "../types";
import { HealthDot } from "./StatusBadge";

interface Props {
  services: ECSService[];
}

export function ECSPillar({ services }: Props) {
  const healthy = services.filter((s) => s.healthy).length;

  return (
    <div style={card}>
      <div style={cardHeader}>
        <h2 style={cardTitle}>ECS</h2>
        <span style={counter(healthy === services.length)}>
          {healthy}/{services.length} healthy
        </span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {services.length === 0 && <p style={empty}>No ECS services configured.</p>}
        {services.map((svc) => (
          <div key={svc.service} style={row(svc.healthy)}>
            <div style={{ display: "flex", alignItems: "center", flex: 1, minWidth: 0 }}>
              <HealthDot healthy={svc.healthy} />
              <div style={{ minWidth: 0 }}>
                <div style={svcName}>{svc.service}</div>
                <div style={meta}>{svc.cluster}</div>
              </div>
            </div>
            <div style={{ textAlign: "right", flexShrink: 0 }}>
              {svc.error ? (
                <span style={errText}>{svc.error}</span>
              ) : (
                <>
                  <div style={taskCount}>{svc.running}/{svc.desired}</div>
                  <div style={meta}>tasks</div>
                </>
              )}
            </div>
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

const svcName: React.CSSProperties = {
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

const taskCount: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 700,
  color: "#e2e8f0",
};

const errText: React.CSSProperties = {
  fontSize: 11,
  color: "#f87171",
};

const empty: React.CSSProperties = {
  fontSize: 12,
  color: "#4b5563",
  fontStyle: "italic",
  textAlign: "center",
  padding: "16px 0",
};
