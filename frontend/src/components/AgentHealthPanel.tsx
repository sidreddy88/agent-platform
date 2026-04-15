import React from "react";
import type { AgentRun, AgentSnapshot, AgentStats } from "../types";

interface Props {
  snapshot: AgentSnapshot | null;
  connected?: boolean;
}

function elapsed(startedAt: string): string {
  const ms = Date.now() - new Date(startedAt).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function duration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function tsAgo(iso: string | null): string {
  if (!iso) return "—";
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function ActiveRunPill({ run }: { run: AgentRun }) {
  return (
    <div style={pill}>
      <span style={pulseDot} />
      <div>
        <div style={pillName}>{run.agent_name}</div>
        <div style={pillMeta}>
          {elapsed(run.started_at)} · {run.tool_calls} tool calls
          {run.incident_id && <span> · #{run.incident_id.slice(0, 6)}</span>}
        </div>
      </div>
    </div>
  );
}

function StatsRow({ s }: { s: AgentStats }) {
  const errorPct = (s.error_rate * 100).toFixed(0);
  const hasErrors = s.errors_today > 0;
  return (
    <tr style={tableRow}>
      <td style={td}>
        <span style={agentName}>{s.agent_name}</span>
        {s.currently_active > 0 && (
          <span style={activeBadge}>{s.currently_active} active</span>
        )}
      </td>
      <td style={{ ...td, textAlign: "right" }}>{s.runs_today}</td>
      <td style={{ ...td, textAlign: "right", color: hasErrors ? "#ef4444" : "#64748b" }}>
        {s.errors_today}
      </td>
      <td style={{ ...td, textAlign: "right", color: hasErrors ? "#ef4444" : "#64748b" }}>
        {errorPct}%
      </td>
      <td style={{ ...td, textAlign: "right", color: "#94a3b8" }}>
        {duration(s.avg_duration_ms)}
      </td>
    </tr>
  );
}

function ErrorRow({ run }: { run: AgentRun }) {
  return (
    <div style={errorItem}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span style={errorAgent}>{run.agent_name}</span>
        <span style={errorTs}>{tsAgo(run.completed_at)}</span>
      </div>
      <p style={errorMsg}>{run.error_message ?? "Unknown error"}</p>
      {run.incident_id && (
        <span style={errorMeta}>incident #{run.incident_id.slice(0, 8)}</span>
      )}
    </div>
  );
}

export function AgentHealthPanel({ snapshot, connected }: Props) {
  const activeRuns = snapshot?.active_runs ?? [];
  const recentErrors = snapshot?.recent_errors ?? [];
  const stats = snapshot?.stats ?? [];

  const hasActivity = activeRuns.length > 0 || recentErrors.length > 0 || stats.length > 0;

  return (
    <div style={panel}>
      <div style={header}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <h2 style={title}>Agent Health</h2>
          {connected !== undefined && (
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                background: connected ? "#22c55e" : "#ef4444",
                display: "inline-block",
                flexShrink: 0,
              }}
              title={connected ? "Live" : "Reconnecting..."}
            />
          )}
        </div>
        <div style={headerRight}>
          {activeRuns.length > 0 && (
            <span style={runningBadge}>{activeRuns.length} running</span>
          )}
          {recentErrors.length > 0 && (
            <span style={errorBadge}>{recentErrors.length} recent errors</span>
          )}
        </div>
      </div>

      {!hasActivity && (
        <p style={empty}>No agent activity yet. Agents appear here when they run.</p>
      )}

      {/* Active runs */}
      {activeRuns.length > 0 && (
        <div style={section}>
          <div style={sectionLabel}>Active</div>
          <div style={pillGrid}>
            {activeRuns.map((r) => (
              <ActiveRunPill key={r.run_id} run={r} />
            ))}
          </div>
        </div>
      )}

      {/* Stats table */}
      {stats.length > 0 && (
        <div style={section}>
          <div style={sectionLabel}>Today</div>
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Agent</th>
                <th style={{ ...th, textAlign: "right" }}>Runs</th>
                <th style={{ ...th, textAlign: "right" }}>Errors</th>
                <th style={{ ...th, textAlign: "right" }}>Err%</th>
                <th style={{ ...th, textAlign: "right" }}>Avg Duration</th>
              </tr>
            </thead>
            <tbody>
              {stats.map((s) => (
                <StatsRow key={s.agent_name} s={s} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Recent errors */}
      {recentErrors.length > 0 && (
        <div style={section}>
          <div style={sectionLabel}>Recent Errors</div>
          <div style={errorList}>
            {recentErrors.slice(0, 5).map((r) => (
              <ErrorRow key={r.run_id} run={r} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const panel: React.CSSProperties = {
  background: "#1e2130",
  border: "1px solid #2d3149",
  borderRadius: 12,
  padding: "16px 20px",
};

const header: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  marginBottom: 16,
};

const headerRight: React.CSSProperties = {
  display: "flex",
  gap: 8,
  alignItems: "center",
};

const title: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 700,
  color: "#94a3b8",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  margin: 0,
};

const runningBadge: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: "#22c55e",
  background: "rgba(34,197,94,0.1)",
  border: "1px solid rgba(34,197,94,0.25)",
  borderRadius: 6,
  padding: "2px 8px",
};

const errorBadge: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: "#ef4444",
  background: "rgba(239,68,68,0.1)",
  border: "1px solid rgba(239,68,68,0.25)",
  borderRadius: 6,
  padding: "2px 8px",
};

const section: React.CSSProperties = {
  marginBottom: 16,
};

const sectionLabel: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: "#4b5563",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  marginBottom: 8,
};

const pillGrid: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
};

const pill: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  background: "#161927",
  border: "1px solid #2d3149",
  borderRadius: 8,
  padding: "8px 12px",
  minWidth: 180,
};

const pulseDot: React.CSSProperties = {
  width: 8,
  height: 8,
  borderRadius: "50%",
  background: "#22c55e",
  flexShrink: 0,
  boxShadow: "0 0 0 2px rgba(34,197,94,0.3)",
  animation: "pulse 2s infinite",
};

const pillName: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#e2e8f0",
};

const pillMeta: React.CSSProperties = {
  fontSize: 11,
  color: "#64748b",
  marginTop: 2,
};

const table: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: 13,
};

const th: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: "#4b5563",
  letterSpacing: "0.05em",
  textTransform: "uppercase",
  padding: "4px 8px",
  borderBottom: "1px solid #2d3149",
  textAlign: "left",
};

const tableRow: React.CSSProperties = {
  borderBottom: "1px solid #1a1f30",
};

const td: React.CSSProperties = {
  padding: "7px 8px",
  color: "#94a3b8",
  verticalAlign: "middle",
};

const agentName: React.CSSProperties = {
  color: "#cbd5e1",
  fontWeight: 500,
};

const activeBadge: React.CSSProperties = {
  marginLeft: 8,
  fontSize: 10,
  fontWeight: 600,
  color: "#22c55e",
  background: "rgba(34,197,94,0.1)",
  borderRadius: 4,
  padding: "1px 5px",
};

const errorList: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 8,
};

const errorItem: React.CSSProperties = {
  background: "rgba(239,68,68,0.05)",
  border: "1px solid rgba(239,68,68,0.15)",
  borderRadius: 8,
  padding: "8px 12px",
};

const errorAgent: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: "#fca5a5",
};

const errorTs: React.CSSProperties = {
  fontSize: 11,
  color: "#64748b",
};

const errorMsg: React.CSSProperties = {
  fontSize: 12,
  color: "#94a3b8",
  margin: "4px 0",
  fontFamily: "monospace",
  wordBreak: "break-all",
};

const errorMeta: React.CSSProperties = {
  fontSize: 11,
  color: "#4b5563",
};

const empty: React.CSSProperties = {
  fontSize: 13,
  color: "#4b5563",
  margin: 0,
};
