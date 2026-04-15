import React, { useEffect, useState } from "react";
import type { AgentPR } from "../types";
import { StatusBadge } from "./StatusBadge";

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function mttr(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export function PRsPage() {
  const [prs, setPrs] = useState<AgentPR[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/agents/prs")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        setPrs(data.prs);
        setLoading(false);
      })
      .catch((e) => {
        setError(e.message);
        setLoading(false);
      });
  }, []);

  return (
    <div style={page}>
      <div style={header}>
        <div>
          <h2 style={title}>Agent PRs</h2>
          <p style={subtitle}>Pull requests opened by the fix-generation agent</p>
        </div>
        <div style={stats}>
          <StatChip label="Total" value={prs.length} />
          <StatChip label="Approved" value={prs.filter((p) => p.human_decision === "approved").length} color="#22c55e" />
          <StatChip label="Pending" value={prs.filter((p) => p.status === "awaiting_approval").length} color="#a855f7" />
          <StatChip label="Rejected" value={prs.filter((p) => p.human_decision === "rejected").length} color="#ef4444" />
        </div>
      </div>

      {loading && <p style={msg}>Loading...</p>}
      {error && <p style={{ ...msg, color: "#ef4444" }}>Failed to load: {error}</p>}
      {!loading && !error && prs.length === 0 && (
        <p style={msg}>No agent PRs yet. Run the demo from the Agents tab to generate one.</p>
      )}

      {prs.map((pr) => (
        <PRCard key={pr.incident_id} pr={pr} />
      ))}
    </div>
  );
}

function PRCard({ pr }: { pr: AgentPR }) {
  return (
    <div style={card}>
      {/* Top row */}
      <div style={cardTop}>
        <div style={cardLeft}>
          <div style={cardTitleRow}>
            {pr.pr_number && (
              <span style={prNumber}>#{pr.pr_number}</span>
            )}
            <span style={cardTitle}>{pr.title}</span>
          </div>
          <div style={cardMeta}>
            <span style={metaChip}>{pr.service}</span>
            {pr.severity && (
              <StatusBadge type="severity" value={pr.severity as any} />
            )}
            <StatusBadge type="status" value={pr.status} />
            {pr.review_posted && (
              <span style={reviewedBadge}>reviewed</span>
            )}
          </div>
        </div>
        <a href={pr.pr_url} target="_blank" rel="noopener noreferrer" style={prLink}>
          View PR →
        </a>
      </div>

      {/* Diagnosis */}
      {pr.diagnosis && (
        <p style={diagnosisText}>
          <span style={diagnosisLabel}>Root cause: </span>
          {pr.diagnosis}
        </p>
      )}

      {/* Bottom row */}
      <div style={cardBottom}>
        <div style={cardBottomLeft}>
          {pr.confidence !== null && (
            <ConfidenceBar confidence={pr.confidence} />
          )}
          {pr.human_decision && (
            <span style={humanDecision(pr.human_decision)}>
              {pr.human_decision === "approved" ? "Approved by human" : "Rejected by human"}
            </span>
          )}
        </div>
        <div style={cardMeta}>
          <span style={metaText}>Created {timeAgo(pr.pr_created_at)}</span>
          {pr.mttr_seconds !== null && (
            <span style={metaText}>MTTR {mttr(pr.mttr_seconds)}</span>
          )}
        </div>
      </div>
    </div>
  );
}

function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100);
  const color = pct >= 80 ? "#22c55e" : pct >= 60 ? "#f59e0b" : "#ef4444";
  return (
    <div style={confRow}>
      <span style={confLabel}>Confidence</span>
      <div style={confTrack}>
        <div style={{ ...confFill, width: `${pct}%`, background: color }} />
      </div>
      <span style={{ ...confLabel, color }}>{pct}%</span>
    </div>
  );
}

function StatChip({ label, value, color = "#94a3b8" }: { label: string; value: number; color?: string }) {
  return (
    <div style={statChip}>
      <span style={{ fontSize: 18, fontWeight: 700, color }}>{value}</span>
      <span style={{ fontSize: 11, color: "#4b5563" }}>{label}</span>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const page: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 12,
};

const header: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  background: "#1e2130",
  border: "1px solid #2d3149",
  borderRadius: 12,
  padding: "16px 20px",
  flexWrap: "wrap",
  gap: 12,
};

const title: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 700,
  color: "#94a3b8",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  margin: 0,
};

const subtitle: React.CSSProperties = {
  fontSize: 12,
  color: "#4b5563",
  marginTop: 4,
  marginBottom: 0,
};

const stats: React.CSSProperties = {
  display: "flex",
  gap: 20,
};

const statChip: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  gap: 2,
};

const card: React.CSSProperties = {
  background: "#1e2130",
  border: "1px solid #2d3149",
  borderRadius: 10,
  padding: "14px 18px",
  display: "flex",
  flexDirection: "column",
  gap: 10,
};

const cardTop: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  gap: 12,
};

const cardLeft: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 6,
  minWidth: 0,
};

const cardTitleRow: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexWrap: "wrap",
};

const prNumber: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 700,
  color: "#60a5fa",
  fontFamily: "monospace",
  flexShrink: 0,
};

const cardTitle: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 600,
  color: "#e2e8f0",
  lineHeight: 1.4,
};

const cardMeta: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  flexWrap: "wrap",
};

const metaChip: React.CSSProperties = {
  fontSize: 11,
  color: "#94a3b8",
  background: "#161927",
  border: "1px solid #2d3149",
  borderRadius: 4,
  padding: "1px 7px",
};

const reviewedBadge: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: "#06b6d4",
  background: "rgba(6,182,212,0.1)",
  border: "1px solid rgba(6,182,212,0.2)",
  borderRadius: 4,
  padding: "1px 6px",
};

const prLink: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#60a5fa",
  textDecoration: "none",
  background: "rgba(96,165,250,0.08)",
  border: "1px solid rgba(96,165,250,0.2)",
  borderRadius: 6,
  padding: "5px 12px",
  whiteSpace: "nowrap",
  flexShrink: 0,
};

const diagnosisText: React.CSSProperties = {
  fontSize: 12,
  color: "#94a3b8",
  margin: 0,
  lineHeight: 1.5,
  borderLeft: "2px solid #2d3149",
  paddingLeft: 10,
};

const diagnosisLabel: React.CSSProperties = {
  color: "#64748b",
  fontWeight: 600,
};

const cardBottom: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  flexWrap: "wrap",
  gap: 8,
};

const cardBottomLeft: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 16,
  flexWrap: "wrap",
};

const metaText: React.CSSProperties = {
  fontSize: 11,
  color: "#4b5563",
};

const humanDecision = (decision: string): React.CSSProperties => ({
  fontSize: 11,
  fontWeight: 600,
  color: decision === "approved" ? "#22c55e" : "#ef4444",
  background: decision === "approved" ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)",
  border: `1px solid ${decision === "approved" ? "rgba(34,197,94,0.2)" : "rgba(239,68,68,0.2)"}`,
  borderRadius: 4,
  padding: "2px 8px",
});

const confRow: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
};

const confLabel: React.CSSProperties = {
  fontSize: 11,
  color: "#4b5563",
  whiteSpace: "nowrap",
};

const confTrack: React.CSSProperties = {
  width: 80,
  height: 4,
  background: "#2d3149",
  borderRadius: 2,
  overflow: "hidden",
};

const confFill: React.CSSProperties = {
  height: "100%",
  borderRadius: 2,
  transition: "width 0.3s ease",
};

const msg: React.CSSProperties = {
  fontSize: 13,
  color: "#4b5563",
  padding: "40px 0",
  textAlign: "center",
};
