import React, { useEffect, useState } from "react";

/**
 * TriageHealthPage — health monitoring for TriageAgent.
 *
 * Backed by /metrics/triage (app/services/triage_metrics.py). TriageAgent's
 * latency and cost are already tracked automatically elsewhere (latency_tracker
 * via @trace_agent, llm_gateway.costs_today()) — this page adds the two things
 * that had zero visibility before:
 *   1. Silent-fallback rate — incident_loop._run_triage() defaults to
 *      real/P2 on any schema-validation or provider failure, previously only
 *      a per-incident log line, never counted in aggregate.
 *   2. Decision (real/noise/duplicate) and severity (P0-P3) distribution
 *      drift — a regression here doesn't crash, it silently reshapes the mix.
 *
 * The CI regression gate (triage-regression.yml) validates against a frozen
 * 80-case dataset on every PR — it answers "did this PR regress?", not
 * "is triage behaving normally on live traffic right now?". This page is
 * for the second question.
 */

// ---------------------------------------------------------------------------
// Types (mirrors app/services/triage_metrics.py + /metrics/triage response)
// ---------------------------------------------------------------------------

interface FallbackSummary {
  window_hours: number;
  total_triages: number;
  fallback_counts: Record<string, number>;
  total_fallbacks: number;
  fallback_rate: number | null;
}

interface TriageSummary {
  window_hours: number;
  fallback: FallbackSummary;
  decision_totals: Record<string, number>;
  severity_totals: Record<string, number>;
}

interface DecisionBucket { hour: string; real: number; noise: number; duplicate: number }
interface SeverityBucket { hour: string; P0: number; P1: number; P2: number; P3: number }

interface LatencyStat {
  agent: string;
  count: number;
  p25: number | null; p50: number | null; p75: number | null; p95: number | null; p99: number | null;
  min: number | null; max: number | null; mean: number | null;
}

interface TriageHealth {
  summary: TriageSummary;
  decision_timeseries: DecisionBucket[];
  severity_timeseries: SeverityBucket[];
  latency: LatencyStat;
  cost_today_usd: number;
}

type DecisionKey = "real" | "noise" | "duplicate";
type SeverityKey = "P0" | "P1" | "P2" | "P3";

// ---------------------------------------------------------------------------
// Palette — decision type is genuinely categorical (distinct classes), so it
// gets the validated categorical order; severity is a state gradient, so it
// maps onto the reserved status palette (critical -> good) instead.
// ---------------------------------------------------------------------------

const DECISION_ORDER: DecisionKey[] = ["real", "noise", "duplicate"];
const DECISION_LABEL: Record<DecisionKey, string> = { real: "Real", noise: "Noise", duplicate: "Duplicate" };
const DECISION_COLOR: Record<DecisionKey, string> = {
  real: "#3987e5",
  noise: "#d95926",
  duplicate: "#199e70",
};

const SEVERITY_ORDER: SeverityKey[] = ["P0", "P1", "P2", "P3"];
const SEVERITY_COLOR: Record<SeverityKey, string> = {
  P0: "#d03b3b", // critical
  P1: "#ec835a", // serious
  P2: "#fab219", // warning
  P3: "#0ca30c", // good — least severe
};

const STATUS = { good: "#0ca30c", critical: "#d03b3b" };

function fmtMs(v: number | null): string {
  if (v === null) return "—";
  if (v < 1000) return `${v.toFixed(0)}ms`;
  return `${(v / 1000).toFixed(2)}s`;
}
function fmtPct(v: number | null): string { return v === null ? "—" : `${(v * 100).toFixed(1)}%`; }
function fmtHour(h: string): string {
  const d = new Date(h.replace(":00", ":00:00") + "Z");
  return d.toLocaleTimeString([], { hour: "numeric" });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface HoverInfo { x: number; y: number; label: string; value: string }

export function TriageHealthPage() {
  const [data, setData] = useState<TriageHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);

  useEffect(() => {
    let cancelled = false;
    function load() {
      fetch("/metrics/triage")
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then((d) => { if (!cancelled) { setData(d); setError(null); } })
        .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Failed"); });
    }
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error && !data) return <div style={errorStyle}>Cannot reach API: {error}</div>;
  if (!data) return <div style={loadingStyle}>Loading triage health…</div>;

  const { summary, decision_timeseries, severity_timeseries, latency, cost_today_usd } = data;
  const maxDecisionBar = Math.max(1, ...decision_timeseries.map((b) => DECISION_ORDER.reduce((s, k) => s + b[k], 0)));
  const maxSeverityBar = Math.max(1, ...severity_timeseries.map((b) => SEVERITY_ORDER.reduce((s, k) => s + b[k], 0)));

  const fallbackHealthy = summary.fallback.total_fallbacks === 0;
  const decisionTotal = DECISION_ORDER.reduce((s, k) => s + (summary.decision_totals[k] ?? 0), 0);

  return (
    <div style={page}>
      {/* Header */}
      <div style={pageHeader}>
        <div>
          <h2 style={titleStyle}>Triage Health</h2>
          <p style={subtitleStyle}>
            Haiku ReAct-loop TriageAgent — silent-fallback rate + decision/severity drift on live traffic
          </p>
        </div>
        <span style={{
          ...badge,
          color: fallbackHealthy ? STATUS.good : STATUS.critical,
          borderColor: `${fallbackHealthy ? STATUS.good : STATUS.critical}55`,
          background: `${fallbackHealthy ? STATUS.good : STATUS.critical}18`,
        }}>
          {fallbackHealthy ? "✓ No silent fallbacks (24h)" : `⚠ ${summary.fallback.total_fallbacks} silent fallback(s) (24h)`}
        </span>
      </div>

      {/* Headline stats */}
      <div style={summaryRow}>
        <StatChip
          label="Fallback Rate (24h)"
          value={fmtPct(summary.fallback.fallback_rate)}
          color={summary.fallback.total_fallbacks > 0 ? STATUS.critical : STATUS.good}
          title="Share of triage attempts that hit a schema-validation or provider failure and silently defaulted to real/P2. Used to be log-only — this is the first aggregate view of it."
        />
        <StatChip label="Triages (24h)" value={String(summary.fallback.total_triages)} color="#94a3b8" />
        <StatChip
          label="Latency p50"
          value={fmtMs(latency.p50)}
          color="#3987e5"
          title={latency.count > 0 ? `p25 ${fmtMs(latency.p25)} · p75 ${fmtMs(latency.p75)} · p95 ${fmtMs(latency.p95)} (n=${latency.count})` : "No samples yet"}
        />
        <StatChip label="Cost Today" value={`$${cost_today_usd.toFixed(4)}`} color="#a78bfa" title="Today's Haiku spend for task:triage, from llm_gateway.costs_today()" />
      </div>

      {/* Decision distribution */}
      <div style={panel}>
        <div style={panelHeader}>
          <span style={panelTitle}>Decision Mix — last {decision_timeseries.length}h</span>
          <div style={legendRow}>
            {DECISION_ORDER.map((k) => (
              <div key={k} style={legendItem}>
                <span style={{ ...legendSwatch, background: DECISION_COLOR[k] }} />
                <span style={legendText}>
                  {DECISION_LABEL[k]} {decisionTotal > 0 ? `· ${fmtPct((summary.decision_totals[k] ?? 0) / decisionTotal)}` : ""}
                </span>
              </div>
            ))}
          </div>
        </div>
        {decision_timeseries.length === 0 ? (
          <p style={emptyStyle}>No triage decisions recorded yet this window.</p>
        ) : (
          <div style={chartArea}>
            {decision_timeseries.map((b) => (
              <StackedBar key={b.hour} hour={b.hour} values={DECISION_ORDER.map((k) => ({ key: k, value: b[k], color: DECISION_COLOR[k], label: DECISION_LABEL[k] }))} maxValue={maxDecisionBar} onHover={setHover} />
            ))}
          </div>
        )}
      </div>

      {/* Severity distribution */}
      <div style={panel}>
        <div style={panelHeader}>
          <span style={panelTitle}>Severity Mix (real incidents only) — last {severity_timeseries.length}h</span>
          <div style={legendRow}>
            {SEVERITY_ORDER.map((k) => (
              <div key={k} style={legendItem}>
                <span style={{ ...legendSwatch, background: SEVERITY_COLOR[k] }} />
                <span style={legendText}>{k} · {summary.severity_totals[k] ?? 0}</span>
              </div>
            ))}
          </div>
        </div>
        {severity_timeseries.length === 0 ? (
          <p style={emptyStyle}>No severity data recorded yet this window.</p>
        ) : (
          <div style={chartArea}>
            {severity_timeseries.map((b) => (
              <StackedBar key={b.hour} hour={b.hour} values={SEVERITY_ORDER.map((k) => ({ key: k, value: b[k], color: SEVERITY_COLOR[k], label: k }))} maxValue={maxSeverityBar} onHover={setHover} />
            ))}
          </div>
        )}
      </div>

      {hover && (
        <div style={{ ...tooltip, left: hover.x + 12, top: hover.y - 12 }}>
          <div style={tooltipLabel}>{hover.label}</div>
          <div style={tooltipValue}>{hover.value}</div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function StatChip({ label, value, color, title }: { label: string; value: string; color: string; title?: string }) {
  return (
    <div style={chip} title={title}>
      <div style={chipLabel}>{label}</div>
      <div style={{ ...chipValue, color }}>{value}</div>
    </div>
  );
}

const BAR_HEIGHT_PX = 120;

function StackedBar({
  hour, values, maxValue, onHover,
}: { hour: string; values: { key: string; value: number; color: string; label: string }[]; maxValue: number; onHover: (h: HoverInfo | null) => void }) {
  return (
    <div style={barCol} onMouseLeave={() => onHover(null)}>
      <div style={barStack}>
        {[...values].reverse().map(({ key, value, color, label }) => {
          if (value <= 0) return null;
          return (
            <div
              key={key}
              style={{ height: `${(value / maxValue) * BAR_HEIGHT_PX}px`, background: color, marginTop: 2, borderRadius: 2 }}
              onMouseEnter={(e) => onHover({ x: e.clientX, y: e.clientY, label, value: `${value} · ${hour}` })}
            />
          );
        })}
      </div>
      <div style={barHourLabel}>{fmtHour(hour)}</div>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const page: React.CSSProperties = { maxWidth: 1200, margin: "0 auto", padding: "24px", display: "flex", flexDirection: "column", gap: 20 };
const pageHeader: React.CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 16 };
const titleStyle: React.CSSProperties = { fontSize: 20, fontWeight: 700, color: "#e2e8f0", margin: 0 };
const subtitleStyle: React.CSSProperties = { fontSize: 12, color: "#475569", margin: "4px 0 0" };

const badge: React.CSSProperties = { fontSize: 12, fontWeight: 700, borderRadius: 8, border: "1px solid", padding: "6px 14px", whiteSpace: "nowrap" };

const summaryRow: React.CSSProperties = { display: "flex", gap: 10, flexWrap: "wrap" };
const chip: React.CSSProperties = { background: "#161927", border: "1px solid #2d3149", borderRadius: 8, padding: "10px 16px", minWidth: 130 };
const chipLabel: React.CSSProperties = { fontSize: 9, fontWeight: 700, color: "#475569", letterSpacing: "0.08em", textTransform: "uppercase" };
const chipValue: React.CSSProperties = { fontSize: 20, fontWeight: 700, marginTop: 2 };

const panel: React.CSSProperties = { background: "#1e2130", border: "1px solid #2d3149", borderRadius: 12, padding: "18px 20px" };
const panelHeader: React.CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10, marginBottom: 16 };
const panelTitle: React.CSSProperties = { fontSize: 12, fontWeight: 700, color: "#94a3b8", letterSpacing: "0.06em", textTransform: "uppercase" };

const legendRow: React.CSSProperties = { display: "flex", flexWrap: "wrap", gap: 12 };
const legendItem: React.CSSProperties = { display: "flex", alignItems: "center", gap: 5 };
const legendSwatch: React.CSSProperties = { width: 9, height: 9, borderRadius: 2, flexShrink: 0 };
const legendText: React.CSSProperties = { fontSize: 11, color: "#64748b" };

const chartArea: React.CSSProperties = { display: "flex", alignItems: "flex-end", gap: 3, height: BAR_HEIGHT_PX + 24, overflowX: "auto", padding: "0 2px" };
const barCol: React.CSSProperties = { display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "flex-end", minWidth: 10, height: "100%", position: "relative" };
const barStack: React.CSSProperties = { display: "flex", flexDirection: "column-reverse", alignItems: "stretch", width: 10 };
const barHourLabel: React.CSSProperties = { fontSize: 8, color: "#374151", marginTop: 4, whiteSpace: "nowrap" };

const emptyStyle: React.CSSProperties = { color: "#4b5563", fontStyle: "italic", fontSize: 13, textAlign: "center", padding: "20px 0" };
const loadingStyle: React.CSSProperties = { color: "#475569", padding: 40, textAlign: "center" };
const errorStyle: React.CSSProperties = { color: "#f87171", padding: 40 };

const tooltip: React.CSSProperties = {
  position: "fixed", zIndex: 50, background: "#0b0d14", border: "1px solid #2d3149", borderRadius: 6,
  padding: "6px 10px", pointerEvents: "none", boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
};
const tooltipLabel: React.CSSProperties = { fontSize: 10, color: "#64748b" };
const tooltipValue: React.CSSProperties = { fontSize: 12, color: "#e2e8f0", fontWeight: 700 };
