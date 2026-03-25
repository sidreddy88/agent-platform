import React from "react";
import type { DOData } from "../types";
import { HealthDot } from "./StatusBadge";

interface Props {
  data: DOData;
}

export function DOPillar({ data }: Props) {
  if (data.error) {
    return (
      <div style={card}>
        <div style={cardHeader}>
          <h2 style={cardTitle}>Digital Ocean</h2>
        </div>
        <p style={{ color: "#f87171", fontSize: 13 }}>{data.error}</p>
      </div>
    );
  }

  return (
    <div style={card}>
      <div style={cardHeader}>
        <h2 style={cardTitle}>Digital Ocean</h2>
        <span style={counter(data.healthy === data.total)}>
          {data.healthy}/{data.total} droplets
        </span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {data.droplets.length === 0 && <p style={empty}>No DO token configured.</p>}
        {data.droplets.map((d) => (
          <div key={d.id} style={row(d.healthy)}>
            <div style={{ display: "flex", alignItems: "center", flex: 1, minWidth: 0 }}>
              <HealthDot healthy={d.healthy} />
              <div style={{ minWidth: 0 }}>
                <div style={name}>{d.name}</div>
                <div style={meta}>{d.region} · {d.size}</div>
              </div>
            </div>
            <div style={{ display: "flex", gap: 12, flexShrink: 0 }}>
              <div style={{ textAlign: "center" }}>
                <div style={metricVal}>{d.load_1 !== null ? d.load_1 : "—"}</div>
                <div style={metricLabel}>LOAD</div>
              </div>
              <div style={{ textAlign: "center" }}>
                <div style={metricVal}>{d.memory_percent !== null ? `${d.memory_percent}%` : "—"}</div>
                <div style={metricLabel}>MEM</div>
              </div>
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

const name: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: "#e2e8f0",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const meta: React.CSSProperties = {
  fontSize: 10,
  color: "#64748b",
  marginTop: 1,
};

const metricVal: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 700,
  color: "#e2e8f0",
};

const metricLabel: React.CSSProperties = {
  fontSize: 10,
  color: "#64748b",
  marginTop: 1,
};

const empty: React.CSSProperties = {
  fontSize: 12,
  color: "#4b5563",
  fontStyle: "italic",
  textAlign: "center",
  padding: "16px 0",
};
