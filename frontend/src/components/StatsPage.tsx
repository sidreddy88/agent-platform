import React, { useEffect, useState } from "react";

interface CICheck {
  name: string;
  status: string;
  conclusion: string | null;
  url: string | null;
}

interface PRStat {
  incident_id: string;
  pr_url: string | null;
  pr_number: number | null;
  service: string;
  severity: string | null;
  error_title: string;
  error_description: string;
  log_source: string | null;
  files_changed: string[];
  pr_description: string | null;
  diagnosis: string | null;
  confidence: number | null;
  detected_at: string | null;
  pr_created_at: string | null;
  resolved_at: string | null;
  mttd_seconds: number | null;
  mttr_seconds: number | null;
  ci_conclusion: "success" | "failure" | "pending" | null;
  ci_checks: CICheck[];
}

interface Summary {
  total: number;
  avg_mttr_seconds: number | null;
  avg_confidence: number | null;
  ci_pass_rate: number | null;
}

function dur(s: number | null): string {
  if (s === null) return "—";
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

const SEV_COLOR: Record<string, string> = {
  P0: "#ef4444", P1: "#f97316", P2: "#eab308", P3: "#6b7280",
};

export function StatsPage() {
  const [prs, setPrs] = useState<PRStat[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/agents/pr-stats")
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((data) => { setPrs(data.prs); setSummary(data.summary); setLoading(false); })
      .catch((e) => { setError(e.message); setLoading(false); });
  }, []);

  if (loading) return <div style={loadingStyle}>Loading stats…</div>;
  if (error) return <div style={errorStyle}>Error: {error}</div>;

  return (
    <div style={page}>
      <div style={pageHeader}>
        <div>
          <h2 style={titleStyle}>Agent PR Stats</h2>
          <p style={subtitleStyle}>Per-PR breakdown — building baseline across resolved incidents</p>
        </div>
        {summary && (
          <div style={summaryRow}>
            <SummaryChip label="PRs Analyzed" value={String(summary.total)} />
            <SummaryChip label="Avg MTTR" value={dur(summary.avg_mttr_seconds)} color="#22c55e" />
            <SummaryChip
              label="CI Pass Rate"
              value={summary.ci_pass_rate !== null ? `${Math.round(summary.ci_pass_rate * 100)}%` : "—"}
              color={summary.ci_pass_rate !== null && summary.ci_pass_rate >= 0.9 ? "#22c55e" : "#f97316"}
            />
            <SummaryChip
              label="Avg Confidence"
              value={summary.avg_confidence !== null ? `${Math.round(summary.avg_confidence * 100)}%` : "—"}
              color="#60a5fa"
            />
          </div>
        )}
      </div>

      {prs.length === 0 ? (
        <p style={emptyStyle}>No agent PRs yet.</p>
      ) : (
        <div style={cards}>
          {prs.map((pr) => (
            <PRCard
              key={pr.incident_id}
              pr={pr}
              onUnresolve={(id) => setPrs((prev) => prev.filter((p) => p.incident_id !== id))}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function PRCard({ pr, onUnresolve }: { pr: PRStat; onUnresolve: (id: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [unresolving, setUnresolving] = useState(false);

  async function handleUnresolve() {
    setUnresolving(true);
    try {
      await fetch(`/incidents/${pr.incident_id}/unresolve`, { method: "POST" });
      onUnresolve(pr.incident_id);
    } finally {
      setUnresolving(false);
    }
  }

  return (
    <div style={card}>
      {/* Card header */}
      <div style={cardHeader}>
        <div style={cardHeaderLeft}>
          {pr.severity && (
            <span style={{ color: SEV_COLOR[pr.severity] ?? "#6b7280", fontWeight: 700, fontSize: 11, marginRight: 8 }}>
              {pr.severity}
            </span>
          )}
          <span style={svcBadge}>{pr.service}</span>
          <span style={errorTitleStyle}>{pr.error_title}</span>
        </div>
        <div style={cardHeaderRight}>
          {pr.pr_url && (
            <a href={pr.pr_url} target="_blank" rel="noreferrer" style={prLink}>
              PR #{pr.pr_number} ↗
            </a>
          )}
          <CIBadge conclusion={pr.ci_conclusion} />
          <button style={actionBtn("#1c2a3a", "#60a5fa", unresolving)} onClick={handleUnresolve} disabled={unresolving}>
            {unresolving ? "…" : "Unresolve"}
          </button>
          <DeleteButton incidentId={pr.incident_id} onDelete={onUnresolve} />
        </div>
      </div>

      {/* 4-column data grid */}
      <div style={dataGrid}>
        <DataCell label="Bug" value={pr.error_description.split("\n")[0].slice(0, 120)} mono />
        <DataCell label="Log Source" value={pr.log_source ?? "—"} mono />
        <DataCell
          label={`Files Changed (${pr.files_changed.length})`}
          value={pr.files_changed.length > 0 ? pr.files_changed.join("\n") : "—"}
          mono
        />
        <DataCell
          label="CI Checks"
          value={
            pr.ci_checks.length === 0
              ? "—"
              : pr.ci_checks.map((c) => `${ciIcon(c.conclusion)} ${c.name}`).join("\n")
          }
          mono
        />
      </div>

      {/* Timeline */}
      <div style={timeline}>
        <TimelineStep label="Detected" sub={pr.detected_at ? shortTs(pr.detected_at) : "—"} />
        <TimelineArrow label={dur(pr.mttd_seconds)} sublabel="MTTD" />
        <TimelineStep label="PR Opened" sub={pr.pr_created_at ? shortTs(pr.pr_created_at) : "—"} />
        <TimelineArrow label={pr.mttr_seconds !== null && pr.mttd_seconds !== null ? dur(pr.mttr_seconds - pr.mttd_seconds) : "—"} sublabel="review→merge" />
        <TimelineStep label="Resolved" sub={pr.resolved_at ? shortTs(pr.resolved_at) : "—"} highlight />
        <div style={mttrTotal}>Total MTTR: {dur(pr.mttr_seconds)}</div>
      </div>

      {/* Diagnosis + PR description (expandable) */}
      {(pr.diagnosis || pr.pr_description) && (
        <div style={expandSection}>
          <button style={expandBtn} onClick={() => setExpanded((v) => !v)}>
            {expanded ? "▲ Hide details" : "▼ Show diagnosis & PR description"}
          </button>
          {expanded && (
            <div style={expandContent}>
              {pr.diagnosis && (
                <div style={detailBlock}>
                  <div style={detailLabel}>Diagnosis</div>
                  <div style={detailText}>{pr.diagnosis}</div>
                </div>
              )}
              {pr.pr_description && (
                <div style={detailBlock}>
                  <div style={detailLabel}>PR Description</div>
                  <div style={detailText}>{pr.pr_description}</div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Confidence footer */}
      {pr.confidence !== null && (
        <div style={confidenceBar}>
          <span style={confidenceLabel}>Agent confidence</span>
          <div style={confTrack}>
            <div style={confFill(pr.confidence)} />
          </div>
          <span style={confidenceValue}>{Math.round(pr.confidence * 100)}%</span>
        </div>
      )}
    </div>
  );
}

function ciIcon(conclusion: string | null): string {
  if (conclusion === "success") return "✓";
  if (conclusion === "failure" || conclusion === "timed_out") return "✗";
  if (conclusion === "skipped") return "–";
  return "·";
}

function shortTs(iso: string): string {
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function DeleteButton({ incidentId, onDelete }: { incidentId: string; onDelete: (id: string) => void }) {
  const [deleting, setDeleting] = useState(false);
  async function handleDelete() {
    if (!window.confirm("Permanently delete this incident?")) return;
    setDeleting(true);
    try {
      await fetch(`/incidents/${incidentId}`, { method: "DELETE" });
      onDelete(incidentId);
    } finally {
      setDeleting(false);
    }
  }
  return (
    <button style={actionBtn("#3a1e1e", "#f87171", deleting)} onClick={handleDelete} disabled={deleting}>
      {deleting ? "…" : "Delete"}
    </button>
  );
}

function CIBadge({ conclusion }: { conclusion: "success" | "failure" | "pending" | null }) {
  if (!conclusion) return <span style={ciBadge("#374151", "#6b7280")}>CI —</span>;
  if (conclusion === "success") return <span style={ciBadge("rgba(34,197,94,0.12)", "#22c55e")}>CI Pass</span>;
  if (conclusion === "failure") return <span style={ciBadge("rgba(239,68,68,0.12)", "#ef4444")}>CI Fail</span>;
  return <span style={ciBadge("rgba(245,158,11,0.12)", "#f59e0b")}>CI Pending</span>;
}

function SummaryChip({ label, value, color = "#94a3b8" }: { label: string; value: string; color?: string }) {
  return (
    <div style={chip}>
      <div style={chipLabel}>{label}</div>
      <div style={{ ...chipValue, color }}>{value}</div>
    </div>
  );
}

function DataCell({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={dataCell}>
      <div style={dataCellLabel}>{label}</div>
      <div style={mono ? dataCellMono : dataCellText}>{value}</div>
    </div>
  );
}

function TimelineStep({ label, sub, highlight }: { label: string; sub: string; highlight?: boolean }) {
  return (
    <div style={tlStep}>
      <div style={{ ...tlDot, background: highlight ? "#22c55e" : "#3b4fd8" }} />
      <div style={tlLabel(highlight)}>{label}</div>
      <div style={tlSub}>{sub}</div>
    </div>
  );
}

function TimelineArrow({ label, sublabel }: { label: string; sublabel: string }) {
  return (
    <div style={tlArrow}>
      <div style={tlArrowLine} />
      <div style={tlArrowLabel}>{label}</div>
      <div style={tlArrowSub}>{sublabel}</div>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const page: React.CSSProperties = {
  maxWidth: 1100, margin: "0 auto", padding: "24px",
  display: "flex", flexDirection: "column", gap: 20,
};

const pageHeader: React.CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap" as const, gap: 16,
};

const titleStyle: React.CSSProperties = { fontSize: 20, fontWeight: 700, color: "#e2e8f0", margin: 0 };
const subtitleStyle: React.CSSProperties = { fontSize: 12, color: "#475569", margin: "4px 0 0" };

const summaryRow: React.CSSProperties = {
  display: "flex", gap: 10, flexWrap: "wrap" as const,
};

const chip: React.CSSProperties = {
  background: "#161927", border: "1px solid #2d3149",
  borderRadius: 8, padding: "10px 16px", minWidth: 100,
};
const chipLabel: React.CSSProperties = { fontSize: 9, fontWeight: 700, color: "#475569", letterSpacing: "0.08em", textTransform: "uppercase" as const };
const chipValue: React.CSSProperties = { fontSize: 20, fontWeight: 700, marginTop: 2 };

const emptyStyle: React.CSSProperties = { color: "#4b5563", fontStyle: "italic", fontSize: 13, textAlign: "center", padding: "40px 0" };
const loadingStyle: React.CSSProperties = { color: "#475569", padding: 40, textAlign: "center" };
const errorStyle: React.CSSProperties = { color: "#f87171", padding: 40 };

const cards: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 16 };

const card: React.CSSProperties = {
  background: "#1e2130", border: "1px solid #2d3149", borderRadius: 12,
  overflow: "hidden",
};

const cardHeader: React.CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "center",
  padding: "14px 18px", borderBottom: "1px solid #2d3149",
  background: "#161927", gap: 12, flexWrap: "wrap" as const,
};
const cardHeaderLeft: React.CSSProperties = { display: "flex", alignItems: "center", gap: 8, flex: 1, minWidth: 0 };
const cardHeaderRight: React.CSSProperties = { display: "flex", alignItems: "center", gap: 8, flexShrink: 0 };

const errorTitleStyle: React.CSSProperties = {
  fontSize: 13, fontWeight: 600, color: "#e2e8f0",
  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" as const,
};

const svcBadge: React.CSSProperties = {
  fontSize: 10, background: "#0f1320", border: "1px solid #2d3149",
  borderRadius: 4, padding: "2px 7px", color: "#64748b", flexShrink: 0,
};

const prLink: React.CSSProperties = {
  color: "#60a5fa", textDecoration: "none", fontWeight: 700, fontSize: 11,
  border: "1px solid rgba(96,165,250,0.3)", borderRadius: 4, padding: "3px 8px",
};

const actionBtn = (bg: string, color: string, disabled: boolean): React.CSSProperties => ({
  background: bg,
  border: `1px solid ${color}40`,
  color: disabled ? "#4b5563" : color,
  borderRadius: 4, padding: "3px 9px", fontSize: 10, fontWeight: 600,
  cursor: disabled ? "not-allowed" : "pointer", whiteSpace: "nowrap" as const,
});

const ciBadge = (bg: string, color: string): React.CSSProperties => ({
  fontSize: 10, fontWeight: 700, background: bg, color, borderRadius: 4, padding: "3px 8px",
});

const dataGrid: React.CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(4, 1fr)",
  gap: 0, borderBottom: "1px solid #2d3149",
};

const dataCell: React.CSSProperties = {
  padding: "12px 16px", borderRight: "1px solid #2d3149",
};

const dataCellLabel: React.CSSProperties = {
  fontSize: 9, fontWeight: 700, color: "#475569",
  letterSpacing: "0.07em", textTransform: "uppercase" as const, marginBottom: 6,
};

const dataCellText: React.CSSProperties = { fontSize: 11, color: "#94a3b8", lineHeight: 1.5, whiteSpace: "pre-wrap" as const };
const dataCellMono: React.CSSProperties = { fontSize: 10, color: "#94a3b8", fontFamily: "monospace", lineHeight: 1.6, whiteSpace: "pre-wrap" as const, wordBreak: "break-all" as const };

const timeline: React.CSSProperties = {
  display: "flex", alignItems: "center", padding: "14px 18px",
  gap: 0, borderBottom: "1px solid #2d3149", flexWrap: "wrap" as const,
};

const tlStep: React.CSSProperties = { display: "flex", flexDirection: "column", alignItems: "center", gap: 3, minWidth: 80 };
const tlDot: React.CSSProperties = { width: 8, height: 8, borderRadius: "50%", marginBottom: 2 };
const tlLabel = (highlight?: boolean): React.CSSProperties => ({
  fontSize: 10, fontWeight: 700, color: highlight ? "#22c55e" : "#94a3b8",
});
const tlSub: React.CSSProperties = { fontSize: 9, color: "#475569" };

const tlArrow: React.CSSProperties = {
  flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 2, minWidth: 60,
};
const tlArrowLine: React.CSSProperties = { width: "80%", height: 1, background: "#2d3149" };
const tlArrowLabel: React.CSSProperties = { fontSize: 11, fontWeight: 700, color: "#60a5fa" };
const tlArrowSub: React.CSSProperties = { fontSize: 9, color: "#475569" };

const mttrTotal: React.CSSProperties = {
  marginLeft: "auto", fontSize: 11, fontWeight: 700,
  color: "#22c55e", background: "rgba(34,197,94,0.08)",
  border: "1px solid rgba(34,197,94,0.2)", borderRadius: 6, padding: "3px 10px",
};

const expandSection: React.CSSProperties = { padding: "10px 16px", borderBottom: "1px solid #2d3149" };
const expandBtn: React.CSSProperties = {
  background: "transparent", border: "none", color: "#475569",
  fontSize: 11, cursor: "pointer", padding: 0,
};
const expandContent: React.CSSProperties = { marginTop: 12, display: "flex", flexDirection: "column", gap: 12 };
const detailBlock: React.CSSProperties = {};
const detailLabel: React.CSSProperties = { fontSize: 9, fontWeight: 700, color: "#475569", letterSpacing: "0.07em", textTransform: "uppercase" as const, marginBottom: 4 };
const detailText: React.CSSProperties = { fontSize: 11, color: "#94a3b8", lineHeight: 1.6, whiteSpace: "pre-wrap" as const };

const confidenceBar: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 10, padding: "10px 16px",
};
const confidenceLabel: React.CSSProperties = { fontSize: 10, color: "#475569", width: 110, flexShrink: 0 };
const confTrack: React.CSSProperties = { flex: 1, height: 4, background: "#2d3149", borderRadius: 2 };
const confFill = (v: number): React.CSSProperties => ({
  height: "100%", width: `${Math.round(v * 100)}%`, borderRadius: 2,
  background: v >= 0.8 ? "#22c55e" : v >= 0.6 ? "#f59e0b" : "#ef4444",
});
const confidenceValue: React.CSSProperties = { fontSize: 11, fontWeight: 700, color: "#94a3b8", width: 36, textAlign: "right" as const };
