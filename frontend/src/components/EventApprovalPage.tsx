import React, { useState, useCallback } from "react";
import type { PendingEvent } from "../types";

interface Props {
  events: PendingEvent[];
}

export function EventApprovalPage({ events }: Props) {
  const [approving, setApproving] = useState<string | null>(null);
  const [approvingAll, setApprovingAll] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [dismissing, setDismissing] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState<"crashes" | "errors" | "non_errors">("crashes");

  const toggleExpand = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }, []);

  async function handleApprove(id: string) {
    setApproving(id);
    try {
      await fetch(`/events/${id}/approve`, { method: "POST" });
      setDismissed((prev) => new Set([...prev, id]));
    } finally {
      setApproving(null);
    }
  }

  async function handleDismiss(id: string) {
    setDismissing((prev) => new Set([...prev, id]));
    setDismissed((prev) => new Set([...prev, id]));
    try {
      await fetch(`/events/${id}/dismiss`, { method: "POST" });
    } catch {
      // revert on error
      setDismissed((prev) => { const n = new Set(prev); n.delete(id); return n; });
    } finally {
      setDismissing((prev) => { const n = new Set(prev); n.delete(id); return n; });
    }
  }

  async function handleApproveAll() {
    setApprovingAll(true);
    try {
      await fetch("/events/approve-all", { method: "POST" });
      setDismissed(new Set(events.map((e) => e.id)));
    } finally {
      setApprovingAll(false);
    }
  }

  const undismissed = events.filter((e) => !dismissed.has(e.id));
  const crashCount    = undismissed.filter((e) => e.category === "crash").length;
  const errorCount    = undismissed.filter((e) => (e.category ?? "error") === "error").length;
  const nonErrorCount = undismissed.filter((e) => (e.category ?? "error") === "non_error").length;
  const categoryForTab = tab === "crashes" ? "crash" : tab === "errors" ? "error" : "non_error";
  const visible = undismissed.filter((e) => (e.category ?? "error") === categoryForTab);

  return (
    <div style={container}>
      {/* Header */}
      <div style={header}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <h2 style={title}>Event Approval</h2>
          {visible.length > 0 && (
            <span style={countBadge}>{visible.length} pending</span>
          )}
        </div>
        {visible.length > 0 && (
          <button style={approveAllBtn(approvingAll)} onClick={handleApproveAll} disabled={approvingAll}>
            {approvingAll ? "Queuing all..." : `Approve All (${visible.length})`}
          </button>
        )}
      </div>

      {/* Sub-tabs */}
      <div style={tabRow}>
        <button style={tabBtn(tab === "crashes")} onClick={() => setTab("crashes")}>
          Crashes
          <span style={tabCountBadge(tab === "crashes")}>{crashCount}</span>
        </button>
        <button style={tabBtn(tab === "errors")} onClick={() => setTab("errors")}>
          Errors
          <span style={tabCountBadge(tab === "errors")}>{errorCount}</span>
        </button>
        <button style={tabBtn(tab === "non_errors")} onClick={() => setTab("non_errors")}>
          Non-errors
          <span style={tabCountBadge(tab === "non_errors")}>{nonErrorCount}</span>
        </button>
      </div>

      {visible.length === 0 ? (
        <div style={empty}>
          <p style={emptyText}>
            {tab === "crashes"
              ? "No crashes detected. Process exits and nodemon restarts will appear here."
              : tab === "errors"
              ? "No pending errors. Run a scan to detect new issues."
              : "No non-error signals. Deprecation warnings and connection timeouts will appear here."}
          </p>
        </div>
      ) : (
        <div style={list}>
          {visible.map((ev) => {
            const hasMore = ev.full_description && ev.full_description.trim() !== ev.first_line.trim();
            const isDismissing = dismissing.has(ev.id);
            return (
              <div key={ev.id} style={row}>
                <div style={rowLeft}>
                  <div style={rowTop}>
                    <span style={svcBadge}>{ev.service}</span>
                    <span style={errorTypeBadge}>{ev.error_type}</span>
                    {ev.handling && ev.handling !== "unknown" && (
                      <span
                        style={handlingBadge(ev.handling)}
                        title={ev.handling === "uncaught"
                          ? `Uncaught — no error handler caught this${ev.handling_evidence ? ` (${ev.handling_evidence})` : ""}`
                          : `Caught — code logged this via an error handler${ev.handling_evidence ? ` (${ev.handling_evidence})` : ""}`}
                      >
                        {ev.handling === "uncaught" ? "⚠ uncaught" : "✓ caught"}
                      </span>
                    )}
                    {ev.handling === "unknown" && (
                      <span style={handlingBadge("unknown")}
                        title="Could not determine if the error was caught by a handler">
                        ? handling
                      </span>
                    )}
                    {(ev.occurrences ?? 1) > 1 && (
                      <span style={occurrenceBadge} title="Identical errors collapsed into this entry">
                        ×{ev.occurrences}
                      </span>
                    )}
                    {ev.log_group && <span style={logGroupText}>{ev.log_group}</span>}
                  </div>
                  <p style={firstLine}>{ev.first_line}</p>
                  {hasMore && (
                    <div>
                      <button
                        style={expandBtn}
                        onClick={() => toggleExpand(ev.id)}
                      >
                        <span style={expandChevron(expanded.has(ev.id))}>▸</span>
                        {expanded.has(ev.id) ? "Hide details" : "Show details"}
                      </button>
                      {expanded.has(ev.id) && (
                        <pre style={detailPre}>{ev.full_description}</pre>
                      )}
                    </div>
                  )}
                  <span style={detectedAt}>{new Date(ev.detected_at).toLocaleTimeString()}</span>
                </div>
                <div style={rowActions}>
                  <button
                    style={approveBtn(approving === ev.id)}
                    onClick={() => handleApprove(ev.id)}
                    disabled={approving === ev.id}
                  >
                    {approving === ev.id ? "Queuing..." : "Approve"}
                  </button>
                  <button
                    style={dismissBtnStyle(isDismissing)}
                    onClick={() => handleDismiss(ev.id)}
                    disabled={isDismissing}
                  >
                    {isDismissing ? "Dismissing..." : "Dismiss"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const container: React.CSSProperties = {
  maxWidth: 900, margin: "0 auto", padding: "24px",
  display: "flex", flexDirection: "column", gap: 16,
};

const header: React.CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "center",
};

const title: React.CSSProperties = {
  fontSize: 20, fontWeight: 700, color: "#e2e8f0", margin: 0,
};

const countBadge: React.CSSProperties = {
  fontSize: 11, fontWeight: 600, color: "#f59e0b",
  background: "rgba(245,158,11,0.1)", border: "1px solid rgba(245,158,11,0.3)",
  borderRadius: 6, padding: "2px 8px",
};

const approveAllBtn = (loading: boolean): React.CSSProperties => ({
  background: loading ? "#14532d" : "rgba(34,197,94,0.1)",
  border: `1px solid ${loading ? "#166534" : "rgba(34,197,94,0.4)"}`,
  color: loading ? "#4b5563" : "#22c55e",
  borderRadius: 6, padding: "6px 16px", fontSize: 13, fontWeight: 700,
  cursor: loading ? "not-allowed" : "pointer",
});

const tabRow: React.CSSProperties = {
  display: "flex", gap: 4, borderBottom: "1px solid #2d3149",
};

const tabBtn = (active: boolean): React.CSSProperties => ({
  background: "transparent",
  border: "none",
  borderBottom: `2px solid ${active ? "#22c55e" : "transparent"}`,
  color: active ? "#e2e8f0" : "#64748b",
  padding: "8px 16px",
  fontSize: 13,
  fontWeight: active ? 700 : 500,
  cursor: "pointer",
  display: "flex", alignItems: "center", gap: 8,
  marginBottom: -1,
});

const tabCountBadge = (active: boolean): React.CSSProperties => ({
  fontSize: 11, fontWeight: 600,
  color: active ? "#22c55e" : "#64748b",
  background: active ? "rgba(34,197,94,0.1)" : "rgba(100,116,139,0.1)",
  border: `1px solid ${active ? "rgba(34,197,94,0.3)" : "#2d3149"}`,
  borderRadius: 4, padding: "1px 6px",
});

const empty: React.CSSProperties = {
  textAlign: "center", padding: "60px 0",
};

const emptyText: React.CSSProperties = {
  fontSize: 14, color: "#4b5563", fontStyle: "italic",
};

const list: React.CSSProperties = {
  display: "flex", flexDirection: "column", gap: 10,
};

const row: React.CSSProperties = {
  background: "#1e2130", border: "1px solid #2d3149",
  borderRadius: 10, padding: "14px 18px",
  display: "flex", alignItems: "flex-start",
  justifyContent: "space-between", gap: 16,
};

const rowLeft: React.CSSProperties = {
  display: "flex", flexDirection: "column", gap: 6, flex: 1, minWidth: 0,
};

const rowTop: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" as const,
};

const svcBadge: React.CSSProperties = {
  fontSize: 11, color: "#94a3b8",
  background: "#161927", border: "1px solid #2d3149",
  borderRadius: 4, padding: "2px 8px",
};

const errorTypeBadge: React.CSSProperties = {
  fontSize: 11, color: "#f87171",
  background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.25)",
  borderRadius: 4, padding: "2px 8px", fontWeight: 600,
};

const occurrenceBadge: React.CSSProperties = {
  fontSize: 11, color: "#fbbf24", fontWeight: 700,
  background: "rgba(251,191,36,0.1)", border: "1px solid rgba(251,191,36,0.35)",
  borderRadius: 4, padding: "2px 8px",
};

const handlingBadge = (kind: "caught" | "uncaught" | "unknown"): React.CSSProperties => {
  if (kind === "uncaught") return {
    fontSize: 11, color: "#ef4444", fontWeight: 700,
    background: "rgba(239,68,68,0.12)", border: "1px solid rgba(239,68,68,0.4)",
    borderRadius: 4, padding: "2px 8px",
  };
  if (kind === "caught") return {
    fontSize: 11, color: "#22c55e", fontWeight: 700,
    background: "rgba(34,197,94,0.1)", border: "1px solid rgba(34,197,94,0.35)",
    borderRadius: 4, padding: "2px 8px",
  };
  return {
    fontSize: 11, color: "#64748b", fontWeight: 600,
    background: "transparent", border: "1px solid #334155",
    borderRadius: 4, padding: "2px 8px",
  };
};

const logGroupText: React.CSSProperties = {
  fontSize: 10, color: "#374151", fontFamily: "monospace",
};

const firstLine: React.CSSProperties = {
  fontSize: 13, color: "#94a3b8", margin: 0,
  lineHeight: 1.5, wordBreak: "break-word" as const,
};

const detectedAt: React.CSSProperties = {
  fontSize: 10, color: "#374151",
};

const rowActions: React.CSSProperties = {
  display: "flex", flexDirection: "column", gap: 6, flexShrink: 0,
};

const approveBtn = (loading: boolean): React.CSSProperties => ({
  background: loading ? "#14532d" : "rgba(34,197,94,0.1)",
  border: `1px solid ${loading ? "#166534" : "rgba(34,197,94,0.35)"}`,
  color: loading ? "#4b5563" : "#22c55e",
  borderRadius: 5, padding: "5px 16px", fontSize: 12, fontWeight: 700,
  cursor: loading ? "not-allowed" : "pointer", whiteSpace: "nowrap" as const,
});

const dismissBtnStyle = (loading: boolean): React.CSSProperties => ({
  background: "transparent",
  border: "1px solid #2d3149",
  color: loading ? "#374151" : "#4b5563",
  borderRadius: 5, padding: "5px 16px", fontSize: 12, fontWeight: 600,
  cursor: loading ? "not-allowed" : "pointer", whiteSpace: "nowrap" as const,
  opacity: loading ? 0.5 : 1,
});

const expandBtn: React.CSSProperties = {
  background: "none", border: "none", padding: 0,
  color: "#475569", fontSize: 11, cursor: "pointer",
  display: "flex", alignItems: "center", gap: 4,
};

const expandChevron = (open: boolean): React.CSSProperties => ({
  display: "inline-block",
  transition: "transform 0.15s",
  transform: open ? "rotate(90deg)" : "rotate(0deg)",
  fontSize: 10,
});

const detailPre: React.CSSProperties = {
  margin: "6px 0 0",
  padding: "10px 12px",
  background: "#161927",
  border: "1px solid #2d3149",
  borderRadius: 6,
  fontSize: 11,
  color: "#94a3b8",
  lineHeight: 1.5,
  whiteSpace: "pre-wrap" as const,
  wordBreak: "break-word" as const,
  maxHeight: 220,
  overflowY: "auto" as const,
};
