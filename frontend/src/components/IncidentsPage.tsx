import React, { useEffect, useRef, useState } from "react";
import type { Incident, IncidentMetrics, ScanLogEntry, FailureAgentName, FailureCategory, AgentRun } from "../types";
import { StatusBadge } from "./StatusBadge";

// ── Pipeline definition ───────────────────────────────────────────────────────

const STEPS = [
  { label: "Triage",    agent: "TriageAgent"       },
  { label: "Diagnose",  agent: "DiagnosisAgent"    },
  { label: "Fix",       agent: "FixGenAgent"       },
  { label: "Review",    agent: "CodeReviewAgent"   },
  { label: "Approval",  agent: "Human"             },
];

const STATUS_STEP: Record<string, number> = {
  open:                    -1,
  triaging:                 0,
  diagnosing:               1,
  fixing:                   2,
  awaiting_fix_approval:    2,
  fix_failed:               2,
  reviewing:                3,
  awaiting_approval:        4,
  resolved:                 5,
  rejected:                 5,
  noise:                    1,
  duplicate:                1,
};

type StepState = "done" | "active" | "pending" | "skipped" | "failed";

function getStepState(inc: Incident, i: number): StepState {
  const { status } = inc;
  if (status === "noise" || status === "duplicate") return i === 0 ? "done" : "skipped";
  if (status === "resolved" || status === "rejected") return "done";
  if (status === "fix_failed") {
    if (i < 2) return "done";
    if (i === 2) return "failed";
    return "skipped";
  }
  const cur = STATUS_STEP[status] ?? -1;
  if (i < cur) return "done";
  if (i === cur) return "active";
  return "pending";
}

function getStepDetail(inc: Incident, i: number): string | null {
  const s = getStepState(inc, i);
  if (s === "skipped") return "—";
  if (s === "pending") return null;
  if (s === "failed") return "Failed";
  switch (i) {
    case 0: return inc.triage_decision ?? null;
    case 1: return inc.confidence !== null ? `${Math.round(inc.confidence * 100)}%` : null;
    case 2: return inc.pr_number ? `PR #${inc.pr_number}` : null;
    case 4: return inc.human_decision ?? null;
    default: return null;
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function ago(s: number) {
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function mttr(s: number) {
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

const SOURCE_ICON: Record<string, string> = {
  cloudwatch: "☁", digital_ocean: "🌊", cloudflare: "🔶", application: "⚙",
};

// ── Page ──────────────────────────────────────────────────────────────────────

interface Props {
  incidents: Incident[];
  metrics: IncidentMetrics | null;
  connected: boolean;
  scanLog: ScanLogEntry[];
  onPendingEventsChanged: () => Promise<void>;
}

export function IncidentsPage({
  incidents,
  metrics,
  connected,
  scanLog,
  onPendingEventsChanged,
}: Props) {
  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState<{ events_found: number } | null>(null);
  const [clearing, setClearing] = useState(false);
  const [clearingEvents, setClearingEvents] = useState(false);
  const logEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [scanLog]);

  async function handleClear() {
    if (!window.confirm("Clear all active incidents? Resolved incidents will be preserved.")) return;
    setClearing(true);
    try {
      await fetch("/incidents", { method: "DELETE" });
    } finally {
      setClearing(false);
    }
  }

  async function handleClearEvents() {
    if (!window.confirm("Clear all pending events?")) return;
    setClearingEvents(true);
    try {
      await fetch("/incidents/events", { method: "DELETE" });
      await onPendingEventsChanged();
    } finally {
      setClearingEvents(false);
    }
  }

  async function handleScan() {
    setScanning(true);
    setScanResult(null);
    try {
      const res = await fetch("/incidents/scan", { method: "POST" });
      setScanResult(await res.json());
      await onPendingEventsChanged();
    } catch {
      setScanResult({ events_found: 0 });
    } finally {
      setScanning(false);
    }
  }

  return (
    <div style={container}>
      {/* Header */}
      <div style={pageHeader}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <h2 style={pageTitle}>Incidents</h2>
          <span style={liveDot(connected)} title={connected ? "Live" : "Reconnecting..."} />
          {metrics && <span style={countBadge}>{metrics.active} active</span>}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {scanResult !== null && (
            <span style={scanResultBadge(scanResult.events_found > 0)}>
              {scanResult.events_found > 0
                ? `${scanResult.events_found} error${scanResult.events_found === 1 ? "" : "s"} found — pipeline started`
                : "No errors detected"}
            </span>
          )}
          <button style={scanBtn(scanning)} onClick={handleScan} disabled={scanning}>
            {scanning ? "Scanning..." : "Scan Last 24 Hours"}
          </button>
          <button style={clearBtn(clearing)} onClick={handleClear} disabled={clearing}>
            {clearing ? "Clearing..." : "Clear Non-Resolved"}
          </button>
          <button style={clearBtn(clearingEvents)} onClick={handleClearEvents} disabled={clearingEvents}>
            {clearingEvents ? "Clearing..." : "Clear Events"}
          </button>
        </div>
      </div>

      {/* Metrics bar */}
      {metrics && (
        <div style={metricsBar}>
          <Chip label="Total"    value={metrics.total}                                          color="#94a3b8" />
          <Chip label="Active"   value={metrics.active}                                         color="#3b82f6" />
          <Chip label="Resolved" value={metrics.resolved}                                       color="#22c55e" />
          <Chip label="Noise"    value={metrics.noise}                                          color="#6b7280" />
          <Chip label="Dup"      value={metrics.duplicate}                                      color="#6b7280" />
          <Chip label="False +ve" value={`${(metrics.false_positive_rate * 100).toFixed(0)}%`} color="#94a3b8" />
          {metrics.avg_mttr_seconds !== null && (
            <Chip label="Avg MTTR" value={mttr(metrics.avg_mttr_seconds)} color="#a855f7" />
          )}
        </div>
      )}

      {/* Pipeline dedup stats */}
      {metrics?.pipeline_stats && (() => {
        const s = metrics.pipeline_stats!;
        const total = s.sql_dedup + s.regression + s.rag_hit + s.cold_start;
        if (total === 0) return null;
        return (
          <div style={pipelineBar}>
            <span style={pipelineLabel}>Pipeline</span>
            <PipelineSegment pct={s.sql_dedup_pct}  count={s.sql_dedup}  label="SQL dedup"   color="#3b82f6" />
            <PipelineSegment pct={s.regression_pct} count={s.regression} label="Regression"  color="#f59e0b" />
            <PipelineSegment pct={s.rag_hit_pct}    count={s.rag_hit}    label="RAG hit"     color="#a855f7" />
            <PipelineSegment pct={s.cold_start_pct} count={s.cold_start} label="Cold start"  color="#22c55e" />
            <span style={pipelineTotal}>{total} events</span>
          </div>
        );
      })()}

      {/* Scan log */}
      {scanLog.length > 0 && (
        <div style={scanLogBox}>
          <div style={scanLogHeader}>
            <span style={scanLogTitle}>Scan Log</span>
            {scanning && <span style={scanningPulse}>scanning…</span>}
          </div>
          <div style={scanLogBody}>
            {scanLog.map((entry, i) => (
              <div key={i} style={scanLogLine}>
                <span style={scanLogTs}>{entry.ts}</span>
                <span style={scanLogMsg(entry.level)}>{entry.message}</span>
              </div>
            ))}
            <div ref={logEndRef} />
          </div>
        </div>
      )}

      {/* List */}
      {incidents.length === 0 ? (
        <p style={empty}>No incidents. All systems healthy.</p>
      ) : (
        incidents.map((inc) => <IncidentCard key={inc.id} inc={inc} />)
      )}
    </div>
  );
}

// ── Incident card ─────────────────────────────────────────────────────────────

function IncidentCard({ inc }: { inc: Incident }) {
  const [restarting, setRestarting] = useState(false);
  const [showNotes, setShowNotes] = useState(false);
  const [notes, setNotes] = useState("");
  const [refixing, setRefixing] = useState(false);
  const [showRefixNotes, setShowRefixNotes] = useState(false);
  const [refixNotes, setRefixNotes] = useState("");
  const [approving, setApproving] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [showFlagModal, setShowFlagModal] = useState(false);
  const [flagRuns, setFlagRuns] = useState<AgentRun[]>([]);
  const [flagRunId, setFlagRunId] = useState<string | null>(null);
  const [flagAgent, setFlagAgent] = useState<FailureAgentName>("diagnosis");
  const [flagCategory, setFlagCategory] = useState<FailureCategory>("wrong_diagnosis");
  const [flagReason, setFlagReason] = useState("");
  const [flagExpected, setFlagExpected] = useState("");
  const [flagging, setFlagging] = useState(false);
  const [flagDone, setFlagDone] = useState(false);
  const [flagRunsLoading, setFlagRunsLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function handleDelete() {
    if (!window.confirm("Delete this incident? Flagged failures in the dataset are kept.")) return;
    setDeleting(true);
    try {
      await fetch(`/incidents/${inc.id}`, { method: "DELETE" });
    } finally {
      setDeleting(false);
    }
  }

  async function openFlagModal() {
    setShowFlagModal(true);
    setFlagRuns([]);
    setFlagRunId(null);
    setFlagRunsLoading(true);
    try {
      const res = await fetch(`/agents/runs/incident/${inc.id}`);
      const runs: AgentRun[] = await res.json();
      setFlagRuns(runs);
    } catch { /* show modal anyway, user can still fill manually */ }
    finally { setFlagRunsLoading(false); }
  }

  function selectRun(run: AgentRun) {
    setFlagRunId(run.run_id);
    const nameMap: Record<string, FailureAgentName> = {
      triage: "triage", diagnosis: "diagnosis",
      fix_generation: "fix_generation", code_review: "code_review",
      merge_decision: "merge_decision", error_clarity: "error_clarity",
    };
    const agent = Object.keys(nameMap).find(k => run.agent_name.toLowerCase().includes(k));
    if (agent) setFlagAgent(nameMap[agent]);
    if (run.status === "failed") setFlagCategory("wrong_diagnosis");
  }

  async function handleFlag() {
    if (!flagReason.trim()) return;
    setFlagging(true);
    try {
      await fetch("http://localhost:8000/failures", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          incident_id: inc.id,
          run_id: flagRunId,
          agent_name: flagAgent,
          failure_category: flagCategory,
          failure_reason: flagReason.trim(),
          expected_behavior: flagExpected.trim() || null,
        }),
      });
      setFlagDone(true);
      setTimeout(() => { setShowFlagModal(false); setFlagDone(false); setFlagReason(""); setFlagExpected(""); setFlagRunId(null); }, 1200);
    } finally {
      setFlagging(false);
    }
  }
  async function handleRestart() {
    setRestarting(true);
    setShowNotes(false);
    try {
      await fetch(`/incidents/${inc.id}/restart`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes: notes.trim() || null }),
      });
      setNotes("");
    } finally {
      setRestarting(false);
    }
  }
  async function handleApproveRefix() {
    setRefixing(true);
    try {
      await fetch(`/incidents/${inc.id}/refix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes: refixNotes.trim() || null }),
      });
      setRefixNotes("");
      setShowRefixNotes(false);
    } finally {
      setRefixing(false);
    }
  }
  async function handleRejectRefix() {
    await fetch(`/incidents/${inc.id}/reject-refix`, { method: "POST" });
  }
  async function handleApproveEscalation() {
    if (!inc.approval_id) return;
    setApproving(true);
    try {
      await fetch(`/approvals/${inc.approval_id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approver: "dashboard" }),
      });
    } finally {
      setApproving(false);
    }
  }
  async function handleRejectEscalation() {
    if (!inc.approval_id) return;
    setRejecting(true);
    try {
      await fetch(`/approvals/${inc.approval_id}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approver: "dashboard", reason: "" }),
      });
    } finally {
      setRejecting(false);
    }
  }

  return (
    <div style={card}>
      {/* Top row */}
      <div style={cardTop}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" as const }}>
          <span style={{ fontSize: 14 }}>{SOURCE_ICON[inc.error_event.source] ?? "·"}</span>
          <StatusBadge type="severity" value={inc.error_event.severity} />
          <StatusBadge type="status"   value={inc.status} />
          <span style={incTitle}>{inc.error_event.title}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
          <span style={svcBadge}>{inc.error_event.service}</span>
          <span style={ts}>{ago(inc.age_seconds)}</span>
        </div>
      </div>

      {/* Description */}
      <p style={desc}>{inc.error_event.description}</p>

      {/* Pipeline stepper */}
      <Stepper inc={inc} />

      {/* Diagnosis */}
      {inc.diagnosis && (
        <div style={diagBox}>
          <span style={diagLabel}>Root cause</span>
          <span style={diagText}>{inc.diagnosis}</span>
        </div>
      )}

      {/* Human notes (existing) */}
      {inc.human_notes && (
        <div style={{ ...diagBox, borderColor: "#d97706", background: "rgba(217,119,6,0.06)" }}>
          <span style={{ ...diagLabel, color: "#d97706" }}>Human feedback</span>
          <span style={diagText}>{inc.human_notes}</span>
        </div>
      )}

      {/* Error clarity */}
      {inc.clarity_summary && (
        <div style={{ ...diagBox, borderColor: "#818cf8", background: "rgba(129,140,248,0.06)" }}>
          <span style={{ ...diagLabel, color: "#818cf8" }}>Error Clarity</span>
          <span style={diagText}>{inc.clarity_summary}</span>
          {inc.clarity_pr_url && (
            <a href={inc.clarity_pr_url} target="_blank" rel="noreferrer"
               style={{ fontSize: 11, color: "#818cf8", marginTop: 4, display: "block" }}>
              Observability PR #{inc.clarity_pr_number} ↗
            </a>
          )}
        </div>
      )}

      {/* Fix generation failed — escalated to human */}
      {inc.status === "fix_failed" && (
        <div style={{
          ...diagBox, borderColor: "#ef4444", background: "rgba(239,68,68,0.08)",
          flexDirection: "column" as const, gap: 6, alignItems: "stretch" as const,
        }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span style={{ ...diagLabel, color: "#ef4444" }}>Fix Generation Failed</span>
            <span style={{ fontSize: 10, color: "#fca5a5", fontWeight: 600 }}>
              · assigned to human
            </span>
          </div>
          <span style={{ ...diagText, color: "#fecaca" }}>
            {inc.fix_description || inc.fix_attempted || "FixGenerationAgent could not produce a PR."}
          </span>
        </div>
      )}

      {/* Merge decision */}
      {inc.merge_decision && (
        <div style={{
          ...diagBox,
          borderColor: inc.merge_decision === "merge_now" ? "#22c55e" : "#f97316",
          background: inc.merge_decision === "merge_now" ? "rgba(34,197,94,0.06)" : "rgba(249,115,22,0.06)",
        }}>
          <span style={{ ...diagLabel, color: inc.merge_decision === "merge_now" ? "#22c55e" : "#f97316" }}>
            {inc.merge_decision === "merge_now" ? "Merge Decision: Ship Now" : "Merge Decision: Refix First"}
          </span>
          <span style={diagText}>{inc.merge_decision_reasoning}</span>
        </div>
      )}

      {/* Restart notes input */}
      {showNotes && (
        <div style={{ marginTop: 8 }}>
          <textarea
            value={notes}
            onChange={e => setNotes(e.target.value)}
            placeholder="Explain what was wrong with the previous fix and how it should be corrected..."
            rows={3}
            style={{
              width: "100%", boxSizing: "border-box" as const, padding: "6px 8px",
              fontSize: 12, borderRadius: 6, border: "1px solid #d1d5db",
              resize: "vertical" as const, fontFamily: "inherit",
            }}
          />
        </div>
      )}

      {/* Bottom meta */}
      <div style={bottomRow}>
        {inc.pr_url && (
          <a href={inc.pr_url} target="_blank" rel="noreferrer" style={prLink}>
            PR #{inc.pr_number}
          </a>
        )}
        {inc.human_decision && (
          <span style={decisionBadge(inc.human_decision)}>
            {inc.human_decision === "approved" ? "Approved" : "Rejected"}
          </span>
        )}
        {inc.mttr_seconds !== null && (
          <span style={metaPill}>MTTR {mttr(inc.mttr_seconds)}</span>
        )}
        {inc.status === "awaiting_approval" && inc.approval_id && !inc.human_decision ? (
          <>
            <button style={refixApproveBtn(approving)} onClick={handleApproveEscalation} disabled={approving || rejecting}>
              {approving ? "Approving..." : "✓ Approve — proceed to fix"}
            </button>
            <button style={refixRejectBtn} onClick={handleRejectEscalation} disabled={approving || rejecting}>
              {rejecting ? "Rejecting..." : "✕ Reject"}
            </button>
          </>
        ) : inc.status === "awaiting_refix_approval" ? (
          <>
            {showRefixNotes && (
              <textarea
                value={refixNotes}
                onChange={e => setRefixNotes(e.target.value)}
                placeholder="Optional instruction for the fix agent (e.g. 'make sure to add response_format: json_object')..."
                rows={3}
                style={{
                  width: "100%", boxSizing: "border-box" as const, padding: "6px 8px",
                  fontSize: 12, borderRadius: 6, border: "1px solid #d97706",
                  resize: "vertical" as const, fontFamily: "inherit", marginBottom: 6,
                }}
              />
            )}
            <button style={refixApproveBtn(refixing)} onClick={handleApproveRefix} disabled={refixing}>
              {refixing ? "Re-fixing..." : "✓ Approve Re-fix"}
            </button>
            <button
              style={{ ...refixApproveBtn(false), background: "#d97706" }}
              onClick={() => setShowRefixNotes(v => !v)}
            >
              {showRefixNotes ? "Hide note" : "+ Add note"}
            </button>
            <button style={refixRejectBtn} onClick={handleRejectRefix}>
              ✕ Reject Re-fix
            </button>
          </>
        ) : showNotes ? (
          <>
            <button style={restartBtn(restarting)} onClick={handleRestart} disabled={restarting}>
              {restarting ? "↺ Restarting..." : "↺ Confirm Restart"}
            </button>
            <button style={{ ...restartBtn(false), background: "#6b7280" }} onClick={() => setShowNotes(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button style={restartBtn(restarting)} onClick={() => setShowNotes(true)} disabled={restarting} title="Restart pipeline with optional feedback">
            ↺ Restart
          </button>
        )}
        <button
          onClick={() => showFlagModal ? setShowFlagModal(false) : openFlagModal()}
          style={{ padding: "4px 10px", borderRadius: 6, border: "1px solid #ef444455",
            background: showFlagModal ? "#ef44441a" : "transparent",
            color: "#ef4444", fontSize: 11, cursor: "pointer", fontWeight: 600 }}
          title="Flag a failure for the training dataset"
        >
          Flag
        </button>
        <button
          onClick={handleDelete}
          disabled={deleting}
          style={{ padding: "4px 10px", borderRadius: 6,
            border: "1px solid #475569",
            background: "transparent",
            color: deleting ? "#374151" : "#94a3b8",
            fontSize: 11, fontWeight: 600,
            cursor: deleting ? "not-allowed" : "pointer" }}
          title="Delete incident (flagged failures in the dataset are preserved)"
        >
          {deleting ? "Deleting…" : "Delete"}
        </button>
      </div>

      {/* Flag failure modal */}
      {showFlagModal && (
        <div style={{ margin: "8px 0 0", padding: 14, borderRadius: 8,
          background: "rgba(239,68,68,0.05)", border: "1px solid #ef444433" }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#ef4444", marginBottom: 10 }}>
            Flag agent failure
          </div>

          {/* Runs list */}
          {flagRunsLoading && (
            <div style={{ fontSize: 11, color: "#475569", marginBottom: 10 }}>Loading runs…</div>
          )}
          {!flagRunsLoading && flagRuns.length > 0 && (
            <div style={{ marginBottom: 10, display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={{ fontSize: 10, color: "#475569", fontWeight: 700, letterSpacing: "0.06em",
                textTransform: "uppercase", marginBottom: 4 }}>Select the failing run</div>
              {flagRuns.map(run => {
                const selected = flagRunId === run.run_id;
                const durationSec = run.duration_ms != null ? (run.duration_ms / 1000).toFixed(0) : null;
                const findings = run.output_summary ?? [];
                return (
                  <div
                    key={run.run_id}
                    onClick={() => selectRun(run)}
                    style={{
                      display: "flex", flexDirection: "column", gap: 6,
                      padding: "6px 10px", borderRadius: 6, cursor: "pointer",
                      background: selected ? "rgba(239,68,68,0.12)" : "rgba(15,23,42,0.6)",
                      border: `1px solid ${selected ? "#ef4444" : "#1e293b"}`,
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span style={{
                        fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 4,
                        background: run.status === "failed" ? "rgba(239,68,68,0.2)"
                          : run.status === "completed" ? "rgba(34,197,94,0.15)"
                          : "rgba(59,130,246,0.15)",
                        color: run.status === "failed" ? "#f87171"
                          : run.status === "completed" ? "#4ade80"
                          : "#60a5fa",
                      }}>{run.status}</span>
                      <span style={{ fontSize: 12, color: "#cbd5e1", fontWeight: 500, flex: 1 }}>
                        {run.agent_name}
                      </span>
                      {findings.length > 0 && (
                        <span style={{ fontSize: 10, color: "#64748b" }}>
                          {selected ? "▾" : "▸"} {findings.length} finding{findings.length === 1 ? "" : "s"}
                        </span>
                      )}
                      {durationSec && (
                        <span style={{ fontSize: 10, color: "#475569" }}>{durationSec}s</span>
                      )}
                      {run.error_message && (
                        <span style={{ fontSize: 10, color: "#f87171", maxWidth: 200,
                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                          title={run.error_message}>
                          {run.error_message}
                        </span>
                      )}
                    </div>
                    {selected && findings.length > 0 && (
                      <div style={{ display: "flex", flexDirection: "column", gap: 4,
                        marginTop: 2, paddingTop: 6, borderTop: "1px solid rgba(239,68,68,0.25)" }}>
                        {findings.map((f, idx) => (
                          <div key={idx} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                            <span style={{ fontSize: 10, fontWeight: 700, color: "#94a3b8",
                              letterSpacing: "0.04em", textTransform: "uppercase",
                              minWidth: 88, flexShrink: 0, paddingTop: 1 }}>
                              {f.label}
                            </span>
                            <span style={{ fontSize: 11, color: "#e2e8f0", lineHeight: 1.45,
                              whiteSpace: "pre-wrap", wordBreak: "break-word", flex: 1 }}>
                              {f.value}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                    {selected && findings.length === 0 && !run.error_message && (
                      <div style={{ fontSize: 10, color: "#64748b", fontStyle: "italic",
                        marginTop: 2, paddingTop: 6, borderTop: "1px solid rgba(239,68,68,0.25)" }}>
                        No output captured for this agent on this incident.
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
            <select value={flagAgent} onChange={e => setFlagAgent(e.target.value as FailureAgentName)}
              style={flagSelect}>
              {(["triage","diagnosis","fix_generation","code_review","merge_decision","error_clarity","other"] as FailureAgentName[]).map(a => (
                <option key={a} value={a}>{a.replace(/_/g," ")}</option>
              ))}
            </select>
            <select value={flagCategory} onChange={e => setFlagCategory(e.target.value as FailureCategory)}
              style={flagSelect}>
              {([
                ["wrong_diagnosis","Wrong diagnosis"],
                ["wrong_file","Wrong file targeted"],
                ["wrong_fix","Wrong fix generated"],
                ["hallucination","Hallucination"],
                ["missed_root_cause","Missed root cause"],
                ["code_not_found","Code not found"],
                ["symptom_fix","Symptom fix (not root cause)"],
                ["wrong_agent_decision","Wrong agent decision"],
                ["other","Other"],
              ] as [FailureCategory, string][]).map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          </div>
          <textarea
            value={flagReason}
            onChange={e => setFlagReason(e.target.value)}
            placeholder="What went wrong? (required)"
            rows={2}
            style={{ ...flagTextarea, marginBottom: 6 }}
          />
          <textarea
            value={flagExpected}
            onChange={e => setFlagExpected(e.target.value)}
            placeholder="What should have happened? (optional)"
            rows={2}
            style={{ ...flagTextarea, marginBottom: 8 }}
          />
          <div style={{ display: "flex", gap: 8 }}>
            <button
              onClick={handleFlag}
              disabled={flagging || !flagReason.trim()}
              style={{ padding: "5px 14px", borderRadius: 6, border: "none", cursor: "pointer",
                background: flagDone ? "#16a34a" : "#ef4444", color: "#fff",
                fontSize: 12, fontWeight: 600, opacity: flagging || !flagReason.trim() ? 0.5 : 1 }}
            >
              {flagDone ? "Saved!" : flagging ? "Saving…" : "Save to dataset"}
            </button>
            <button
              onClick={() => setShowFlagModal(false)}
              style={{ padding: "5px 12px", borderRadius: 6, border: "1px solid #334155",
                background: "transparent", color: "#94a3b8", fontSize: 12, cursor: "pointer" }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Stepper ───────────────────────────────────────────────────────────────────

function Stepper({ inc }: { inc: Incident }) {
  return (
    <div style={stepperRow}>
      {STEPS.map((step, i) => {
        const state = getStepState(inc, i);
        const detail = getStepDetail(inc, i);
        return (
          <React.Fragment key={step.label}>
            <div style={stepCol}>
              <Circle state={state} />
              <span style={stepLabelStyle(state)}>{step.label}</span>
              {detail && <span style={stepDetailStyle}>{detail}</span>}
            </div>
            {i < STEPS.length - 1 && (
              <div style={connectorLine(state === "done")} />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

function Circle({ state }: { state: StepState }) {
  const color = state === "done" ? "#22c55e"
    : state === "active" ? "#3b82f6"
    : state === "failed" ? "#ef4444"
    : state === "skipped" ? "#1e293b"
    : "#2d3149";

  return (
    <div style={{
      width: 28, height: 28, borderRadius: "50%",
      background: state === "done" ? "rgba(34,197,94,0.12)"
        : state === "active" ? "rgba(59,130,246,0.12)"
        : state === "failed" ? "rgba(239,68,68,0.15)"
        : "#161927",
      border: `2px solid ${color}`,
      display: "flex", alignItems: "center", justifyContent: "center",
    }}>
      {state === "done"   && <span style={{ fontSize: 11, color: "#22c55e", fontWeight: 700 }}>✓</span>}
      {state === "active" && <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#3b82f6", boxShadow: "0 0 0 3px rgba(59,130,246,0.25)", display: "block" }} />}
      {state === "failed"  && <span style={{ fontSize: 13, color: "#ef4444", fontWeight: 700, lineHeight: 1 }}>✕</span>}
      {state === "skipped" && <span style={{ fontSize: 9, color: "#334155" }}>—</span>}
    </div>
  );
}

// ── Shared sub-components ────────────────────────────────────────────────────

function Chip({ label, value, color }: { label: string; value: string | number; color: string }) {
  return (
    <div style={{ textAlign: "center" as const }}>
      <div style={{ fontSize: 17, fontWeight: 700, color }}>{value}</div>
      <div style={{ fontSize: 10, color: "#64748b" }}>{label}</div>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const container: React.CSSProperties = {
  maxWidth: 1000, margin: "0 auto", padding: "24px",
  display: "flex", flexDirection: "column", gap: 16,
};

const pageHeader: React.CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "center",
};

const pageTitle: React.CSSProperties = {
  fontSize: 20, fontWeight: 700, color: "#e2e8f0", margin: 0,
};

const liveDot = (ok: boolean): React.CSSProperties => ({
  width: 8, height: 8, borderRadius: "50%",
  background: ok ? "#22c55e" : "#f97316", flexShrink: 0,
});

const countBadge: React.CSSProperties = {
  fontSize: 11, fontWeight: 600, color: "#3b82f6",
  background: "rgba(59,130,246,0.1)", border: "1px solid rgba(59,130,246,0.3)",
  borderRadius: 6, padding: "2px 8px",
};

const scanBtn = (loading: boolean): React.CSSProperties => ({
  background: loading ? "#1e2d40" : "#1e3a5f",
  border: `1px solid ${loading ? "#2d4a6a" : "#3b82f6"}`,
  color: loading ? "#4b5563" : "#60a5fa",
  borderRadius: 6, padding: "6px 16px", fontSize: 13, fontWeight: 600,
  cursor: loading ? "not-allowed" : "pointer",
});

const clearBtn = (loading: boolean): React.CSSProperties => ({
  background: loading ? "#1e2130" : "rgba(239,68,68,0.08)",
  border: `1px solid ${loading ? "#2d3149" : "rgba(239,68,68,0.35)"}`,
  color: loading ? "#4b5563" : "#f87171",
  borderRadius: 6, padding: "6px 16px", fontSize: 13, fontWeight: 600,
  cursor: loading ? "not-allowed" : "pointer",
});

const scanResultBadge = (found: boolean): React.CSSProperties => ({
  fontSize: 12, fontWeight: 600,
  color: found ? "#22c55e" : "#64748b",
  background: found ? "rgba(34,197,94,0.1)" : "#161927",
  border: `1px solid ${found ? "rgba(34,197,94,0.3)" : "#2d3149"}`,
  borderRadius: 6, padding: "5px 12px",
});

const metricsBar: React.CSSProperties = {
  display: "flex", gap: 24, flexWrap: "wrap",
  background: "#1e2130", border: "1px solid #2d3149",
  borderRadius: 10, padding: "12px 20px",
};

const empty: React.CSSProperties = {
  fontSize: 14, color: "#4b5563", fontStyle: "italic",
  textAlign: "center", padding: "40px 0",
};

const card: React.CSSProperties = {
  background: "#1e2130", border: "1px solid #2d3149",
  borderRadius: 12, padding: "16px 20px",
  display: "flex", flexDirection: "column", gap: 12,
};

const cardTop: React.CSSProperties = {
  display: "flex", justifyContent: "space-between",
  alignItems: "flex-start", gap: 8,
};

const incTitle: React.CSSProperties = {
  fontSize: 14, fontWeight: 600, color: "#e2e8f0",
};

const svcBadge: React.CSSProperties = {
  fontSize: 11, color: "#4b5563",
  background: "#161927", border: "1px solid #2d3149",
  borderRadius: 4, padding: "2px 8px",
};

const ts: React.CSSProperties = {
  fontSize: 11, color: "#4b5563", whiteSpace: "nowrap" as const,
};

const desc: React.CSSProperties = {
  fontSize: 12, color: "#64748b", lineHeight: 1.5, margin: 0,
};

const stepperRow: React.CSSProperties = {
  display: "flex", alignItems: "flex-start",
  padding: "4px 0 0",
};

const stepCol: React.CSSProperties = {
  display: "flex", flexDirection: "column",
  alignItems: "center", gap: 4, minWidth: 70,
};

const connectorLine = (filled: boolean): React.CSSProperties => ({
  flex: 1, height: 2, marginTop: 13, minWidth: 8,
  background: filled ? "#22c55e" : "#2d3149",
});

const stepLabelStyle = (state: StepState): React.CSSProperties => ({
  fontSize: 10, fontWeight: 600, letterSpacing: "0.04em",
  textTransform: "uppercase" as const, textAlign: "center" as const,
  color: state === "done" ? "#22c55e"
    : state === "active" ? "#60a5fa"
    : state === "failed" ? "#f87171"
    : state === "skipped" ? "#1e293b"
    : "#475569",
});

const stepDetailStyle: React.CSSProperties = {
  fontSize: 10, color: "#64748b",
  textAlign: "center" as const, maxWidth: 68,
  wordBreak: "break-word" as const,
};

const diagBox: React.CSSProperties = {
  background: "#161927", border: "1px solid #2d3149",
  borderRadius: 6, padding: "8px 12px",
  display: "flex", gap: 10, alignItems: "flex-start",
};

const diagLabel: React.CSSProperties = {
  fontSize: 10, fontWeight: 700, color: "#475569",
  letterSpacing: "0.06em", textTransform: "uppercase" as const,
  whiteSpace: "nowrap" as const, paddingTop: 2,
};

const diagText: React.CSSProperties = {
  fontSize: 12, color: "#94a3b8", lineHeight: 1.5,
};

const bottomRow: React.CSSProperties = {
  display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" as const,
};

const prLink: React.CSSProperties = {
  color: "#60a5fa", textDecoration: "none", fontSize: 12, fontWeight: 600,
  border: "1px solid rgba(96,165,250,0.3)", borderRadius: 4, padding: "2px 8px",
};

const decisionBadge = (d: string): React.CSSProperties => ({
  fontSize: 11, fontWeight: 600,
  color: d === "approved" ? "#22c55e" : "#f87171",
  background: d === "approved" ? "rgba(34,197,94,0.1)" : "rgba(248,113,113,0.1)",
  border: `1px solid ${d === "approved" ? "rgba(34,197,94,0.3)" : "rgba(248,113,113,0.3)"}`,
  borderRadius: 4, padding: "2px 8px",
});

const restartBtn = (loading: boolean): React.CSSProperties => ({
  marginLeft: "auto",
  background: "transparent",
  border: `1px solid ${loading ? "#2d3149" : "#334155"}`,
  color: loading ? "#374151" : "#64748b",
  borderRadius: 4, padding: "2px 10px", fontSize: 11, fontWeight: 600,
  cursor: loading ? "not-allowed" : "pointer",
});

const metaPill: React.CSSProperties = {
  fontSize: 11, color: "#64748b",
  background: "#161927", border: "1px solid #2d3149",
  borderRadius: 4, padding: "2px 8px",
};

const refixApproveBtn = (loading: boolean): React.CSSProperties => ({
  marginLeft: "auto",
  background: loading ? "rgba(34,197,94,0.05)" : "rgba(34,197,94,0.1)",
  border: "1px solid rgba(34,197,94,0.4)",
  color: loading ? "#4b5563" : "#22c55e",
  borderRadius: 4, padding: "2px 10px", fontSize: 11, fontWeight: 700,
  cursor: loading ? "not-allowed" : "pointer",
});

const refixRejectBtn: React.CSSProperties = {
  background: "rgba(239,68,68,0.08)",
  border: "1px solid rgba(239,68,68,0.35)",
  color: "#f87171",
  borderRadius: 4, padding: "2px 10px", fontSize: 11, fontWeight: 700,
  cursor: "pointer",
};

const flagSelect: React.CSSProperties = {
  padding: "4px 8px", borderRadius: 6, border: "1px solid #334155",
  background: "#0f172a", color: "#cbd5e1", fontSize: 12, cursor: "pointer", flex: 1,
};

const flagTextarea: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", padding: "6px 8px",
  fontSize: 12, borderRadius: 6, border: "1px solid #334155",
  background: "#0f172a", color: "#cbd5e1",
  resize: "vertical", fontFamily: "inherit",
};

const scanLogBox: React.CSSProperties = {
  background: "#0d1117", border: "1px solid #2d3149",
  borderRadius: 10, overflow: "hidden",
};

const scanLogHeader: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 10,
  padding: "8px 14px", borderBottom: "1px solid #2d3149",
  background: "#161927",
};

const scanLogTitle: React.CSSProperties = {
  fontSize: 11, fontWeight: 700, color: "#475569",
  letterSpacing: "0.08em", textTransform: "uppercase" as const,
};

const scanningPulse: React.CSSProperties = {
  fontSize: 11, color: "#3b82f6", fontStyle: "italic",
};

const scanLogBody: React.CSSProperties = {
  padding: "10px 14px", maxHeight: 220, overflowY: "auto" as const,
  display: "flex", flexDirection: "column" as const, gap: 3,
  fontFamily: "monospace",
};

const scanLogLine: React.CSSProperties = {
  display: "flex", gap: 10, alignItems: "baseline",
};

const scanLogTs: React.CSSProperties = {
  fontSize: 10, color: "#374151", flexShrink: 0,
};

const scanLogMsg = (level: ScanLogEntry["level"]): React.CSSProperties => ({
  fontSize: 12,
  color: level === "event" ? "#22c55e"
    : level === "error" ? "#f87171"
    : level === "done" ? "#60a5fa"
    : "#64748b",
});

// ── Pipeline dedup stats bar ──────────────────────────────────────────────────

function PipelineSegment({ pct, count, label, color }: { pct: number; count: number; label: string; color: string }) {
  if (count === 0) return null;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ width: 8, height: 8, borderRadius: "50%", background: color, flexShrink: 0 }} />
      <span style={{ fontSize: 11, color, fontWeight: 600 }}>{pct.toFixed(0)}%</span>
      <span style={{ fontSize: 10, color: "#475569" }}>{label}</span>
      <span style={{ fontSize: 10, color: "#2d3149" }}>({count})</span>
    </div>
  );
}

const pipelineBar: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 20, flexWrap: "wrap" as const,
  background: "#161927", border: "1px solid #2d3149",
  borderRadius: 8, padding: "8px 16px",
};

const pipelineLabel: React.CSSProperties = {
  fontSize: 10, fontWeight: 700, color: "#334155",
  letterSpacing: "0.08em", textTransform: "uppercase" as const,
  marginRight: 4,
};

const pipelineTotal: React.CSSProperties = {
  fontSize: 10, color: "#2d3149", marginLeft: "auto",
};
