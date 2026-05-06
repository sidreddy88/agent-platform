import React, { useEffect, useState, useCallback } from "react";
import { AgentFailure, FailureAgentName, FailureCategory } from "../types";

const API = "http://localhost:8000";

const AGENTS: FailureAgentName[] = [
  "triage", "diagnosis", "fix_generation", "code_review",
  "merge_decision", "error_clarity", "other",
];

const CATEGORIES: { value: FailureCategory; label: string }[] = [
  { value: "wrong_diagnosis",      label: "Wrong diagnosis" },
  { value: "wrong_file",           label: "Wrong file targeted" },
  { value: "wrong_fix",            label: "Wrong fix generated" },
  { value: "hallucination",        label: "Hallucination" },
  { value: "missed_root_cause",    label: "Missed root cause" },
  { value: "code_not_found",       label: "Code not found" },
  { value: "symptom_fix",          label: "Symptom fix (not root cause)" },
  { value: "wrong_agent_decision", label: "Wrong agent decision" },
  { value: "other",                label: "Other" },
];

const CATEGORY_COLORS: Record<FailureCategory, string> = {
  wrong_diagnosis:      "#ef4444",
  wrong_file:           "#f97316",
  wrong_fix:            "#eab308",
  hallucination:        "#a855f7",
  missed_root_cause:    "#ec4899",
  code_not_found:       "#64748b",
  symptom_fix:          "#06b6d4",
  wrong_agent_decision: "#8b5cf6",
  other:                "#94a3b8",
};

export function DatasetPage({ hidden = false }: { hidden?: boolean }) {
  const [failures, setFailures] = useState<AgentFailure[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterAgent, setFilterAgent] = useState<string>("");
  const [filterCategory, setFilterCategory] = useState<string>("");
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    // Stale-while-revalidate: only flash the spinner on the first load.
    // Subsequent fetches (filter change, manual refresh) keep the existing
    // rows visible until the new ones arrive.
    setFailures(prev => {
      if (prev.length === 0) setLoading(true);
      return prev;
    });
    const url = filterAgent
      ? `${API}/failures?agent=${filterAgent}`
      : `${API}/failures`;
    const res = await fetch(url);
    const data: AgentFailure[] = await res.json();
    setFailures(data);
    setLoading(false);
  }, [filterAgent]);

  useEffect(() => { load(); }, [load]);

  function exportJsonl() {
    window.open(`${API}/failures/export`, "_blank");
  }

  const filtered = filterCategory
    ? failures.filter(f => f.failure_category === filterCategory)
    : failures;

  const byCat: Record<string, number> = {};
  const byAgent: Record<string, number> = {};
  failures.forEach(f => {
    byCat[f.failure_category] = (byCat[f.failure_category] ?? 0) + 1;
    byAgent[f.agent_name] = (byAgent[f.agent_name] ?? 0) + 1;
  });

  return (
    <div style={{ padding: "24px 32px", maxWidth: 1100, margin: "0 auto",
      display: hidden ? "none" : undefined }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: "#f1f5f9" }}>
            Failure Dataset
          </h2>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "#64748b" }}>
            Human-annotated agent failures — {failures.length} example{failures.length !== 1 ? "s" : ""}
          </p>
        </div>
        <button
          onClick={exportJsonl}
          style={{ padding: "8px 16px", borderRadius: 6, border: "none", cursor: "pointer",
            background: "#1e40af", color: "#fff", fontSize: 13, fontWeight: 600 }}
        >
          Export JSONL
        </button>
      </div>

      {/* Summary chips */}
      {failures.length > 0 && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 20 }}>
          {Object.entries(byCat).sort((a,b) => b[1]-a[1]).map(([cat, count]) => (
            <span
              key={cat}
              onClick={() => setFilterCategory(filterCategory === cat ? "" : cat)}
              style={{
                padding: "4px 10px", borderRadius: 12, fontSize: 12, cursor: "pointer", fontWeight: 600,
                background: filterCategory === cat
                  ? CATEGORY_COLORS[cat as FailureCategory]
                  : "rgba(255,255,255,0.05)",
                color: filterCategory === cat ? "#fff" : CATEGORY_COLORS[cat as FailureCategory],
                border: `1px solid ${CATEGORY_COLORS[cat as FailureCategory]}`,
              }}
            >
              {cat.replace(/_/g, " ")} · {count}
            </span>
          ))}
        </div>
      )}

      {/* Filters */}
      <div style={{ display: "flex", gap: 10, marginBottom: 20 }}>
        <select
          value={filterAgent}
          onChange={e => setFilterAgent(e.target.value)}
          style={selectStyle}
        >
          <option value="">All agents</option>
          {AGENTS.map(a => (
            <option key={a} value={a}>{a} {byAgent[a] ? `(${byAgent[a]})` : ""}</option>
          ))}
        </select>
        <select
          value={filterCategory}
          onChange={e => setFilterCategory(e.target.value)}
          style={selectStyle}
        >
          <option value="">All categories</option>
          {CATEGORIES.map(c => (
            <option key={c.value} value={c.value}>{c.label}</option>
          ))}
        </select>
        {(filterAgent || filterCategory) && (
          <button
            onClick={() => { setFilterAgent(""); setFilterCategory(""); }}
            style={{ padding: "6px 12px", borderRadius: 6, border: "1px solid #334155",
              background: "transparent", color: "#94a3b8", fontSize: 12, cursor: "pointer" }}
          >
            Clear
          </button>
        )}
      </div>

      {/* List */}
      {loading ? (
        <p style={{ color: "#64748b" }}>Loading…</p>
      ) : filtered.length === 0 ? (
        <div style={{ textAlign: "center", padding: 60, color: "#475569" }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>📋</div>
          <p style={{ margin: 0, fontWeight: 600 }}>No failures annotated yet</p>
          <p style={{ margin: "6px 0 0", fontSize: 13 }}>
            Open an incident card and click "Flag Agent Failure" to add one.
          </p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {filtered.map(f => (
            <FailureCard
              key={f.id}
              failure={f}
              expanded={expanded === f.id}
              onToggle={() => setExpanded(expanded === f.id ? null : f.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function FailureCard({
  failure: f, expanded, onToggle,
}: {
  failure: AgentFailure;
  expanded: boolean;
  onToggle: () => void;
}) {
  const catColor = CATEGORY_COLORS[f.failure_category] ?? "#94a3b8";
  const catLabel = CATEGORIES.find(c => c.value === f.failure_category)?.label ?? f.failure_category;

  return (
    <div style={{ background: "#0f172a", border: "1px solid #1e293b", borderRadius: 10, overflow: "hidden" }}>
      {/* Row */}
      <div
        onClick={onToggle}
        style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 16px",
          cursor: "pointer", userSelect: "none" }}
      >
        {/* Agent badge */}
        <span style={{
          padding: "2px 8px", borderRadius: 4, fontSize: 11, fontWeight: 700,
          background: "rgba(100,116,139,0.15)", color: "#94a3b8",
          textTransform: "uppercase", whiteSpace: "nowrap",
        }}>
          {f.agent_name.replace(/_/g, " ")}
        </span>

        {/* Category badge */}
        <span style={{
          padding: "2px 8px", borderRadius: 4, fontSize: 11, fontWeight: 600,
          background: `${catColor}22`, color: catColor, whiteSpace: "nowrap",
        }}>
          {catLabel}
        </span>

        {/* Reason preview */}
        <span style={{ flex: 1, fontSize: 13, color: "#cbd5e1", overflow: "hidden",
          textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {f.failure_reason}
        </span>

        {/* Date */}
        <span style={{ fontSize: 11, color: "#475569", whiteSpace: "nowrap" }}>
          {new Date(f.created_at).toLocaleDateString()}
        </span>

        <span style={{ color: "#475569", fontSize: 12 }}>{expanded ? "▲" : "▼"}</span>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div style={{ borderTop: "1px solid #1e293b", padding: "14px 16px",
          display: "flex", flexDirection: "column", gap: 12 }}>

          {f.error_description && (
            <Field label="Error" value={f.error_description} mono />
          )}

          <Field label="What went wrong" value={f.failure_reason} />

          {f.actual_behavior && (
            <Field label="What the agent produced" value={f.actual_behavior} mono />
          )}

          {f.expected_behavior && (
            <Field label="What should have happened" value={f.expected_behavior} />
          )}

          <div style={{ marginTop: 4, fontSize: 11, color: "#475569" }}>
            <span>Incident: {f.incident_id.slice(0, 8)}… {f.run_id ? `· Run: ${f.run_id}` : ""}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div style={{ fontSize: 10, fontWeight: 700, color: "#475569",
        textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 4 }}>
        {label}
      </div>
      <div style={{
        fontSize: 12, color: "#cbd5e1", lineHeight: 1.5,
        fontFamily: mono ? "monospace" : "inherit",
        background: mono ? "rgba(255,255,255,0.03)" : "transparent",
        padding: mono ? "8px 10px" : 0, borderRadius: mono ? 6 : 0,
        whiteSpace: "pre-wrap", wordBreak: "break-word",
      }}>
        {value}
      </div>
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  padding: "6px 10px", borderRadius: 6,
  border: "1px solid #334155", background: "#0f172a",
  color: "#cbd5e1", fontSize: 12, cursor: "pointer",
};
