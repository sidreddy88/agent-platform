import React from "react";
import type { CFData } from "../types";
import { HealthDot } from "./StatusBadge";

interface Props {
  data: CFData;
}

function pct(n: number) {
  return (n * 100).toFixed(1) + "%";
}

function fmt(n: number) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(0) + "k";
  return String(n);
}

export function CloudfarePillar({ data }: Props) {
  if (data.error) {
    return (
      <div style={card}>
        <div style={cardHeader}>
          <h2 style={cardTitle}>Cloudflare</h2>
        </div>
        <p style={{ color: "#f87171", fontSize: 13 }}>{data.error}</p>
      </div>
    );
  }

  return (
    <div style={card}>
      <div style={cardHeader}>
        <h2 style={cardTitle}>Cloudflare</h2>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <HealthDot healthy={data.healthy} />
          <span style={{ fontSize: 12, color: "#94a3b8" }}>
            {fmt(data.total_requests)} req · {pct(data.overall_error_rate)} errors
          </span>
        </span>
      </div>

      {/* Summary row */}
      <div style={summaryRow}>
        <Stat label="Total Requests" value={fmt(data.total_requests)} />
        <Stat label="Error Rate" value={pct(data.overall_error_rate)} highlight={data.overall_error_rate > 0.05} />
        <Stat label="Cache Hit" value={pct(data.overall_cache_hit_rate)} />
      </div>

      {/* Per-zone rows */}
      <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 12 }}>
        {data.zones.length === 0 && (
          <p style={{ fontSize: 12, color: "#4b5563", fontStyle: "italic", textAlign: "center", padding: "12px 0" }}>
            No Cloudflare zones configured.
          </p>
        )}
        {data.zones.map((z) => (
          <div key={z.zone_name} style={zoneRow(z.healthy)}>
            <div style={{ display: "flex", alignItems: "center", flex: 1, minWidth: 0 }}>
              <HealthDot healthy={z.healthy} />
              <span style={zoneName}>{z.zone_name}</span>
            </div>
            <div style={zoneStats}>
              <span style={{ color: z.error_rate > 0.05 ? "#f87171" : "#64748b" }}>
                {pct(z.error_rate)} err
              </span>
              <span style={{ color: "#64748b" }}>{pct(z.cache_hit_rate)} cached</span>
              <span style={{ color: "#64748b" }}>{fmt(z.total_requests)} req</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Stat({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div style={{ textAlign: "center" }}>
      <div style={{ fontSize: 18, fontWeight: 700, color: highlight ? "#f87171" : "#e2e8f0" }}>
        {value}
      </div>
      <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>{label}</div>
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

const summaryRow: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-around",
  background: "#161927",
  borderRadius: 8,
  padding: "10px 0",
};

const zoneRow = (healthy: boolean): React.CSSProperties => ({
  display: "flex",
  alignItems: "center",
  background: healthy ? "#1a2235" : "#2a1a1a",
  border: `1px solid ${healthy ? "#2d3149" : "#7f1d1d"}`,
  borderRadius: 8,
  padding: "7px 12px",
  gap: 8,
});

const zoneName: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: "#e2e8f0",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const zoneStats: React.CSSProperties = {
  display: "flex",
  gap: 12,
  fontSize: 11,
  flexShrink: 0,
};
