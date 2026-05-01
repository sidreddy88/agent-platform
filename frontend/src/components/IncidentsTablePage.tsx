import React, { useEffect, useState } from "react";
import type { AgentPR, AgentRun, Incident } from "../types";

interface Props {
  incidents: Incident[];
}

function ago(s: number) {
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function dur(ms: number | null) {
  if (ms === null) return null;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

const SEV_COLOR: Record<string, string> = {
  P0: "#ef4444", P1: "#f97316", P2: "#eab308", P3: "#6b7280",
};

const AGENT_SHORT: Record<string, string> = {
  TriageAgent: "Triage", DiagnosisAgent: "Diagnose",
  FixGenerationAgent: "Fix", FixGenAgent: "Fix",
  CodeReviewAgent: "Review", HumanApproval: "Approval",
};

function shortName(name: string) {
  return AGENT_SHORT[name] ?? name.replace("Agent", "");
}

// ── Agent run pills ──────────────────────────────────────────────────────────

function AgentRunPills({ runs, onNote }: { runs: AgentRun[]; onNote: (r: AgentRun) => void }) {
  if (runs.length === 0) return <span style={noData}>—</span>;
  return (
    <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 4 }}>
      {runs.map((r) => (
        <span
          key={r.run_id}
          title={`${r.agent_name} · ${r.status}${r.duration_ms ? ` · ${dur(r.duration_ms)}` : ""}${r.error_message ? `\n${r.error_message}` : ""}\nClick to annotate`}
          style={{ ...runPill(r.status), cursor: "pointer" }}
          onClick={() => onNote(r)}
        >
          <span style={runIcon(r.status)}>
            {r.status === "completed" ? "✓" : r.status === "failed" ? "✗" : "•"}
          </span>
          {shortName(r.agent_name)}
          {r.duration_ms !== null && (
            <span style={runDur}>{dur(r.duration_ms)}</span>
          )}
          {r.error_message && <span style={runNoteIndicator} title={r.error_message}>✎</span>}
        </span>
      ))}
    </div>
  );
}

// ── Run annotation modal ──────────────────────────────────────────────────────

function RunNoteModal({ run, onClose }: { run: AgentRun; onClose: () => void }) {
  const [note, setNote] = useState(run.error_message ?? "");
  const [markFailed, setMarkFailed] = useState(run.status === "failed");
  const [saving, setSaving] = useState(false);

  async function handleSubmit() {
    if (!note.trim()) return;
    setSaving(true);
    try {
      await fetch(`/api/agents/runs/${run.run_id}/note`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: note.trim(), mark_failed: markFailed }),
      });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={modalOverlay} onClick={onClose}>
      <div style={modalBox} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ ...modalTitle, color: "#f59e0b" }}>Annotate Agent Run</h3>
        <p style={modalSub}>
          <span style={{ color: "#e2e8f0" }}>{run.agent_name}</span>
          {" · "}{run.run_id}
          {run.duration_ms !== null && ` · ${(run.duration_ms / 1000).toFixed(1)}s`}
        </p>
        <textarea
          style={notesInput}
          placeholder="What went wrong? e.g. PR created with no code added"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={4}
          autoFocus
        />
        <label style={checkLabel}>
          <input
            type="checkbox"
            checked={markFailed}
            onChange={(e) => setMarkFailed(e.target.checked)}
            style={{ marginRight: 6 }}
          />
          Mark run as failed
        </label>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
          <button style={cancelBtn} onClick={onClose}>Cancel</button>
          <button
            style={submitBtn(saving || !note.trim())}
            onClick={handleSubmit}
            disabled={saving || !note.trim()}
          >
            {saving ? "Saving..." : "Save Note"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Wrong Fix modal ──────────────────────────────────────────────────────────

function WrongFixModal({ incident, onClose }: { incident: Incident; onClose: () => void }) {
  const [notes, setNotes] = useState(incident.wrong_fix_notes ?? "");
  const [saving, setSaving] = useState(false);

  async function handleSubmit() {
    if (!notes.trim()) return;
    setSaving(true);
    try {
      await fetch(`/incidents/${incident.id}/mark-wrong-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes: notes.trim() }),
      });
      onClose();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={modalOverlay} onClick={onClose}>
      <div style={modalBox} onClick={(e) => e.stopPropagation()}>
        <h3 style={modalTitle}>Mark as Wrong Fix</h3>
        <p style={modalSub}>Incident: <span style={{ color: "#e2e8f0" }}>{incident.error_event.title}</span></p>
        <textarea
          style={notesInput}
          placeholder="What was wrong with this fix? What should it have done instead?"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={4}
          autoFocus
        />
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
          <button style={cancelBtn} onClick={onClose}>Cancel</button>
          <button
            style={submitBtn(saving || !notes.trim())}
            onClick={handleSubmit}
            disabled={saving || !notes.trim()}
          >
            {saving ? "Saving..." : "Mark Wrong Fix"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export function IncidentsTablePage({ incidents }: Props) {
  const [filter, setFilter] = useState<"all" | "active" | "resolved">("all");
  const [agentRunMap, setAgentRunMap] = useState<Record<string, AgentRun[]>>({});
  const [archiving, setArchiving] = useState<string | null>(null);
  const [resolving, setResolving] = useState<string | null>(null);
  const [wrongFixTarget, setWrongFixTarget] = useState<Incident | null>(null);
  const [noteTarget, setNoteTarget] = useState<AgentRun | null>(null);
  // Local overlay state for archived/wrong_fix (optimistic, until WS update arrives)
  const [localArchived, setLocalArchived] = useState<Set<string>>(new Set());
  const [localWrongFix, setLocalWrongFix] = useState<Set<string>>(new Set());
  const [localResolved, setLocalResolved] = useState<Set<string>>(new Set());

  useEffect(() => {
    fetch("/api/agents/prs")
      .then((r) => r.json())
      .then((prs: AgentPR[]) => {
        const map: Record<string, AgentRun[]> = {};
        for (const pr of prs) {
          if (pr.incident_id) map[pr.incident_id] = pr.agent_runs ?? [];
        }
        setAgentRunMap(map);
      })
      .catch(() => {});
  }, [incidents]);

  async function handleArchive(id: string) {
    setArchiving(id);
    try {
      await fetch(`/incidents/${id}/archive`, { method: "POST" });
      setLocalArchived((prev) => new Set(prev).add(id));
    } finally {
      setArchiving(null);
    }
  }

  async function handleResolve(id: string) {
    setResolving(id);
    try {
      await fetch(`/incidents/${id}/resolve`, { method: "POST" });
      setLocalResolved((prev) => new Set(prev).add(id));
    } finally {
      setResolving(null);
    }
  }

  const isArchived = (inc: Incident) => inc.archived || localArchived.has(inc.id);
  const isWrongFix = (inc: Incident) => inc.wrong_fix || localWrongFix.has(inc.id);
  const isResolved = (inc: Incident) => inc.status === "resolved" || localResolved.has(inc.id);

  const TERMINAL = new Set(["resolved", "noise", "duplicate", "rejected"]);

  const visibleIncidents = incidents.filter((i) => !isArchived(i) && !isWrongFix(i));
  const wrongFixIncidents = incidents.filter((i) => isWrongFix(i));

  const filtered = visibleIncidents.filter((i) => {
    if (filter === "active") return !TERMINAL.has(i.status) && !localResolved.has(i.id);
    if (filter === "resolved") return isResolved(i);
    return true;
  });

  return (
    <div style={container}>
      {noteTarget && (
        <RunNoteModal run={noteTarget} onClose={() => setNoteTarget(null)} />
      )}
      {wrongFixTarget && (
        <WrongFixModal
          incident={wrongFixTarget}
          onClose={() => {
            setLocalWrongFix((prev) => new Set(prev).add(wrongFixTarget.id));
            setWrongFixTarget(null);
          }}
        />
      )}

      {/* Header */}
      <div style={header}>
        <h2 style={title}>Incidents Table</h2>
        <div style={filterRow}>
          {(["all", "active", "resolved"] as const).map((f) => (
            <button key={f} style={filterBtn(filter === f)} onClick={() => setFilter(f)}>
              {f.charAt(0).toUpperCase() + f.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {/* Main incidents table */}
      {filtered.length === 0 ? (
        <p style={empty}>No incidents to show.</p>
      ) : (
        <div style={tableWrapper}>
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Sev</th>
                <th style={th}>Error</th>
                <th style={th}>Service</th>
                <th style={th}>Root Cause</th>
                <th style={th}>Agent Runs</th>
                <th style={th}>PR</th>
                <th style={th}>Status</th>
                <th style={th}>Age</th>
                <th style={th}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((inc, i) => {
                const hasPR = !!inc.pr_url;
                return (
                  <tr key={inc.id} style={tRow(i % 2 === 0)}>
                    <td style={td}>
                      <span style={{ color: SEV_COLOR[inc.error_event.severity] ?? "#6b7280", fontWeight: 700, fontSize: 11 }}>
                        {inc.error_event.severity}
                      </span>
                    </td>
                    <td style={{ ...td, maxWidth: 260 }}>
                      <span style={errorTitle}>{inc.error_event.title}</span>
                      <span style={errorDesc}>{inc.error_event.description.split("\n")[0].slice(0, 110)}</span>
                    </td>
                    <td style={td}>
                      <span style={svcBadge}>{inc.error_event.service}</span>
                    </td>
                    <td style={{ ...td, maxWidth: 260 }}>
                      {inc.diagnosis ? (
                        <span style={diagText}>{inc.diagnosis.slice(0, 150)}{inc.diagnosis.length > 150 ? "…" : ""}</span>
                      ) : (
                        <span style={noData}>—</span>
                      )}
                    </td>
                    <td style={{ ...td, minWidth: 160 }}>
                      <AgentRunPills runs={agentRunMap[inc.id] ?? []} onNote={setNoteTarget} />
                    </td>
                    <td style={td}>
                      {inc.pr_url ? (
                        <a href={inc.pr_url} target="_blank" rel="noreferrer" style={prLink}>
                          #{inc.pr_number}
                        </a>
                      ) : (
                        <span style={noData}>—</span>
                      )}
                    </td>
                    <td style={td}>
                      <span style={statusPill(inc.status)}>{inc.status.replace(/_/g, " ")}</span>
                    </td>
                    <td style={{ ...td, whiteSpace: "nowrap" as const }}>
                      <span style={ageText}>{ago(inc.age_seconds)}</span>
                    </td>
                    <td style={{ ...td, whiteSpace: "nowrap" as const }}>
                      <div style={{ display: "flex", gap: 4 }}>
                        {!TERMINAL.has(inc.status) && !localResolved.has(inc.id) && (
                          <button
                            style={actionBtn("#1e3a2a", "#22c55e", resolving === inc.id)}
                            onClick={() => handleResolve(inc.id)}
                            disabled={resolving === inc.id}
                            title="Mark as resolved"
                          >
                            {resolving === inc.id ? "…" : "Resolve"}
                          </button>
                        )}
                        {isResolved(inc) && (
                          <button
                            style={actionBtn("#1e3a4a", "#60a5fa", archiving === inc.id)}
                            onClick={() => handleArchive(inc.id)}
                            disabled={archiving === inc.id}
                            title="Archive this incident"
                          >
                            {archiving === inc.id ? "…" : "Archive"}
                          </button>
                        )}
                        {hasPR && (
                          <button
                            style={actionBtn("#3a1e1e", "#f87171", false)}
                            onClick={() => setWrongFixTarget(inc)}
                            title="Mark this fix as incorrect"
                          >
                            Wrong Fix
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Wrong fixes table */}
      {wrongFixIncidents.length > 0 && (
        <>
          <div style={sectionHeader}>
            <span style={sectionTitle}>Wrong Fixes</span>
            <span style={sectionCount}>{wrongFixIncidents.length}</span>
          </div>
          <div style={tableWrapper}>
            <table style={table}>
              <thead>
                <tr>
                  <th style={th}>Sev</th>
                  <th style={th}>Error</th>
                  <th style={th}>Service</th>
                  <th style={th}>PR</th>
                  <th style={th}>Diagnosis</th>
                  <th style={th}>What Was Wrong</th>
                </tr>
              </thead>
              <tbody>
                {wrongFixIncidents.map((inc, i) => (
                  <tr key={inc.id} style={tRow(i % 2 === 0)}>
                    <td style={td}>
                      <span style={{ color: SEV_COLOR[inc.error_event.severity] ?? "#6b7280", fontWeight: 700, fontSize: 11 }}>
                        {inc.error_event.severity}
                      </span>
                    </td>
                    <td style={{ ...td, maxWidth: 220 }}>
                      <span style={errorTitle}>{inc.error_event.title}</span>
                    </td>
                    <td style={td}>
                      <span style={svcBadge}>{inc.error_event.service}</span>
                    </td>
                    <td style={td}>
                      {inc.pr_url ? (
                        <a href={inc.pr_url} target="_blank" rel="noreferrer" style={prLink}>
                          #{inc.pr_number}
                        </a>
                      ) : (
                        <span style={noData}>—</span>
                      )}
                    </td>
                    <td style={{ ...td, maxWidth: 220 }}>
                      {inc.diagnosis ? (
                        <span style={diagText}>{inc.diagnosis.slice(0, 120)}{inc.diagnosis.length > 120 ? "…" : ""}</span>
                      ) : (
                        <span style={noData}>—</span>
                      )}
                    </td>
                    <td style={{ ...td, maxWidth: 260 }}>
                      <span style={wrongNotes}>{inc.wrong_fix_notes ?? "—"}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const container: React.CSSProperties = {
  maxWidth: 1400, margin: "0 auto", padding: "24px",
  display: "flex", flexDirection: "column", gap: 16,
};

const header: React.CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "center",
};

const title: React.CSSProperties = {
  fontSize: 20, fontWeight: 700, color: "#e2e8f0", margin: 0,
};

const filterRow: React.CSSProperties = { display: "flex", gap: 4 };

const filterBtn = (active: boolean): React.CSSProperties => ({
  background: active ? "#3b4fd8" : "transparent",
  border: `1px solid ${active ? "#4f63f0" : "#2d3149"}`,
  color: active ? "#e2e8f0" : "#64748b",
  borderRadius: 6, padding: "4px 14px", fontSize: 12,
  fontWeight: active ? 600 : 400, cursor: "pointer",
});

const empty: React.CSSProperties = {
  fontSize: 14, color: "#4b5563", fontStyle: "italic",
  textAlign: "center", padding: "40px 0",
};

const tableWrapper: React.CSSProperties = {
  overflowX: "auto" as const,
  border: "1px solid #2d3149", borderRadius: 10,
};

const table: React.CSSProperties = {
  width: "100%", borderCollapse: "collapse" as const, fontSize: 12,
};

const th: React.CSSProperties = {
  padding: "10px 14px", textAlign: "left" as const,
  fontSize: 10, fontWeight: 700, color: "#475569",
  letterSpacing: "0.06em", textTransform: "uppercase" as const,
  background: "#161927", borderBottom: "1px solid #2d3149",
  whiteSpace: "nowrap" as const,
};

const tRow = (even: boolean): React.CSSProperties => ({
  background: even ? "#1e2130" : "#191e2e",
  borderBottom: "1px solid #2d3149",
});

const td: React.CSSProperties = {
  padding: "10px 14px", verticalAlign: "top" as const, color: "#94a3b8",
};

const errorTitle: React.CSSProperties = {
  display: "block", fontSize: 12, fontWeight: 600,
  color: "#e2e8f0", marginBottom: 3,
};

const errorDesc: React.CSSProperties = {
  display: "block", fontSize: 11, color: "#64748b", lineHeight: 1.4,
};

const svcBadge: React.CSSProperties = {
  fontSize: 10, background: "#161927",
  border: "1px solid #2d3149", borderRadius: 4,
  padding: "2px 6px", color: "#64748b",
};

const diagText: React.CSSProperties = {
  fontSize: 11, color: "#94a3b8", lineHeight: 1.5,
};

const noData: React.CSSProperties = { color: "#2d3149", fontSize: 12 };

const prLink: React.CSSProperties = {
  color: "#60a5fa", textDecoration: "none", fontWeight: 600,
  fontSize: 12, border: "1px solid rgba(96,165,250,0.3)",
  borderRadius: 4, padding: "1px 6px",
};

const runPill = (status: string): React.CSSProperties => ({
  display: "inline-flex", alignItems: "center", gap: 4,
  fontSize: 10, fontWeight: 600, borderRadius: 4, padding: "2px 7px",
  cursor: "default",
  color: status === "completed" ? "#22c55e" : status === "failed" ? "#f87171" : "#60a5fa",
  background: status === "completed" ? "rgba(34,197,94,0.08)"
    : status === "failed" ? "rgba(248,113,113,0.08)"
    : "rgba(96,165,250,0.08)",
  border: `1px solid ${status === "completed" ? "rgba(34,197,94,0.25)"
    : status === "failed" ? "rgba(248,113,113,0.25)"
    : "rgba(96,165,250,0.25)"}`,
});

const runIcon = (status: string): React.CSSProperties => ({
  fontSize: 9, fontWeight: 900,
  color: status === "completed" ? "#22c55e" : status === "failed" ? "#f87171" : "#60a5fa",
});

const runDur: React.CSSProperties = {
  fontSize: 9, color: "#475569", marginLeft: 2,
};

const runNoteIndicator: React.CSSProperties = {
  fontSize: 9, marginLeft: 3, opacity: 0.7,
};

const statusPill = (status: string): React.CSSProperties => {
  const colors: Record<string, [string, string]> = {
    resolved:          ["#22c55e", "rgba(34,197,94,0.1)"],
    open:              ["#3b82f6", "rgba(59,130,246,0.1)"],
    triaging:          ["#a855f7", "rgba(168,85,247,0.1)"],
    diagnosing:        ["#f97316", "rgba(249,115,22,0.1)"],
    fixing:            ["#f59e0b", "rgba(245,158,11,0.1)"],
    reviewing:         ["#06b6d4", "rgba(6,182,212,0.1)"],
    awaiting_approval: ["#8b5cf6", "rgba(139,92,246,0.1)"],
    awaiting_fix_approval: ["#f59e0b", "rgba(245,158,11,0.1)"],
    noise:             ["#6b7280", "rgba(107,114,128,0.1)"],
    duplicate:         ["#374151", "#1e2130"],
    rejected:          ["#ef4444", "rgba(239,68,68,0.1)"],
  };
  const [color, bg] = colors[status] ?? ["#6b7280", "rgba(107,114,128,0.1)"];
  return {
    fontSize: 10, fontWeight: 600, color, background: bg,
    borderRadius: 4, padding: "2px 6px", whiteSpace: "nowrap" as const,
  };
};

const ageText: React.CSSProperties = { fontSize: 11, color: "#4b5563" };

const actionBtn = (bg: string, color: string, disabled: boolean): React.CSSProperties => ({
  background: bg,
  border: `1px solid ${color}40`,
  color: disabled ? "#4b5563" : color,
  borderRadius: 4, padding: "3px 8px", fontSize: 10, fontWeight: 600,
  cursor: disabled ? "not-allowed" : "pointer", whiteSpace: "nowrap" as const,
});

const sectionHeader: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 10, marginTop: 8,
};

const sectionTitle: React.CSSProperties = {
  fontSize: 13, fontWeight: 700, color: "#f87171",
};

const sectionCount: React.CSSProperties = {
  fontSize: 11, fontWeight: 600, color: "#f87171",
  background: "rgba(248,113,113,0.1)", border: "1px solid rgba(248,113,113,0.3)",
  borderRadius: 6, padding: "1px 7px",
};

const wrongNotes: React.CSSProperties = {
  fontSize: 11, color: "#fca5a5", lineHeight: 1.5, fontStyle: "italic",
};

const modalOverlay: React.CSSProperties = {
  position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
  display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
};

const modalBox: React.CSSProperties = {
  background: "#1e2130", border: "1px solid #2d3149", borderRadius: 12,
  padding: "24px", width: 460, maxWidth: "90vw",
};

const modalTitle: React.CSSProperties = {
  fontSize: 16, fontWeight: 700, color: "#f87171", margin: "0 0 6px",
};

const modalSub: React.CSSProperties = {
  fontSize: 12, color: "#64748b", margin: "0 0 14px",
};

const notesInput: React.CSSProperties = {
  width: "100%", background: "#161927", border: "1px solid #2d3149",
  borderRadius: 6, padding: "10px 12px", color: "#e2e8f0",
  fontSize: 13, resize: "vertical" as const, boxSizing: "border-box" as const,
  fontFamily: "inherit",
};

const checkLabel: React.CSSProperties = {
  display: "flex", alignItems: "center", fontSize: 12, color: "#94a3b8",
  cursor: "pointer", marginTop: 10,
};

const cancelBtn: React.CSSProperties = {
  background: "transparent", border: "1px solid #2d3149",
  color: "#64748b", borderRadius: 6, padding: "6px 16px",
  fontSize: 12, cursor: "pointer",
};

const submitBtn = (disabled: boolean): React.CSSProperties => ({
  background: disabled ? "#3a1e1e" : "rgba(248,113,113,0.12)",
  border: "1px solid rgba(248,113,113,0.4)",
  color: disabled ? "#4b5563" : "#f87171",
  borderRadius: 6, padding: "6px 16px", fontSize: 12, fontWeight: 700,
  cursor: disabled ? "not-allowed" : "pointer",
});
