import React from "react";
import type { GitHubData } from "../types";

interface Props {
  data: GitHubData;
}

function conclusionColor(conclusion: string): string {
  switch (conclusion) {
    case "SUCCESS": return "#22c55e";
    case "FAILURE": return "#f87171";
    case "CANCELLED": return "#94a3b8";
    case "IN PROGRESS": return "#60a5fa";
    case "QUEUED": return "#f59e0b";
    default: return "#64748b";
  }
}

function conclusionBg(conclusion: string): string {
  switch (conclusion) {
    case "SUCCESS": return "#14532d";
    case "FAILURE": return "#450a0a";
    case "CANCELLED": return "#1e293b";
    case "IN PROGRESS": return "#1e3a5f";
    case "QUEUED": return "#451a03";
    default: return "#1e293b";
  }
}

function formatTs(iso: string): string {
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export function GitHubPillar({ data }: Props) {
  const latestOk = data.latest_healthy;
  const statusColor = data.error ? "#f87171" : latestOk === null ? "#64748b" : latestOk ? "#22c55e" : "#f87171";
  const statusText = data.error ? "ERROR" : latestOk === null ? "NO RUNS" : latestOk ? "PASSING" : "FAILING";

  return (
    <div style={card}>
      <div style={cardHeader}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <h2 style={cardTitle}>GitHub Actions</h2>
          {data.repo && (
            <span style={repoTag}>{data.repo} · {data.branch}</span>
          )}
        </div>
        <span style={{ fontSize: 11, color: statusColor, fontWeight: 600 }}>{statusText}</span>
      </div>

      {data.error && <p style={errText}>{data.error}</p>}

      {!data.error && data.runs.length === 0 && (
        <p style={hint}>No workflow runs found.</p>
      )}

      {data.runs.map((run) => (
        <div key={run.id} style={runRow}>
          <div style={runLeft}>
            <span style={badge(run.display_conclusion)}>{run.display_conclusion}</span>
            <div style={runInfo}>
              <span style={workflowName}>{run.workflow_name} <span style={runNum}>#{run.run_number}</span></span>
              <span style={commitMsg}>{run.commit_message}</span>
            </div>
          </div>
          <div style={runRight}>
            <span style={sha}>{run.commit_sha}</span>
            <span style={ts}>{formatTs(run.created_at)}</span>
            <a href={run.url} target="_blank" rel="noreferrer" style={link}>↗</a>
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

const repoTag: React.CSSProperties = {
  fontSize: 11,
  color: "#475569",
  fontFamily: "monospace",
};

const runRow: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  borderTop: "1px solid #2d3149",
  paddingTop: 10,
  marginTop: 10,
  gap: 12,
};

const runLeft: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  flex: 1,
  minWidth: 0,
};

const runInfo: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
  minWidth: 0,
};

const workflowName: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#e2e8f0",
  whiteSpace: "nowrap",
  overflow: "hidden",
  textOverflow: "ellipsis",
};

const runNum: React.CSSProperties = {
  fontSize: 12,
  color: "#475569",
  fontWeight: 400,
};

const commitMsg: React.CSSProperties = {
  fontSize: 11,
  color: "#64748b",
  whiteSpace: "nowrap",
  overflow: "hidden",
  textOverflow: "ellipsis",
};

const runRight: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  flexShrink: 0,
};

const badge = (conclusion: string): React.CSSProperties => ({
  fontSize: 10,
  fontWeight: 700,
  letterSpacing: "0.05em",
  padding: "3px 8px",
  borderRadius: 4,
  background: conclusionBg(conclusion),
  color: conclusionColor(conclusion),
  whiteSpace: "nowrap",
  flexShrink: 0,
});

const sha: React.CSSProperties = {
  fontSize: 11,
  color: "#475569",
  fontFamily: "monospace",
};

const ts: React.CSSProperties = {
  fontSize: 11,
  color: "#475569",
};

const link: React.CSSProperties = {
  fontSize: 13,
  color: "#60a5fa",
  textDecoration: "none",
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
