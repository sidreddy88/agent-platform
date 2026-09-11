import React, { useEffect, useState } from "react";

/**
 * DedupHealthPage — health monitoring for the 4-layer incident dedup gate.
 *
 * Backed by /metrics/dedup (app/services/dedup_metrics.py). Three things this
 * page answers that a cumulative lifetime counter can't:
 *   1. Is the gate's block rate trending normally, or did it silently drop?
 *   2. Is the RAG call path itself healthy (it used to fail at debug-log
 *      level with zero visibility)?
 *   3. Ground truth: did a duplicate actually leak through anyway? (Two open
 *      incidents, same error_type+service, close together in time — the one
 *      signal that's independent of the gate's own internal bookkeeping.)
 */

// ---------------------------------------------------------------------------
// Types (mirrors app/services/dedup_metrics.py + /metrics/dedup response)
// ---------------------------------------------------------------------------

interface DuplicateLeak {
  error_type: string;
  service: string;
  incident_a: string;
  incident_b: string;
  minutes_apart: number;
}

interface DedupSummary {
  window_hours: number;
  outcomes: Record<string, number>;
  total_events: number;
  block_rate: number | null;
  rag_error_count: number;
  rag_error_rate: number | null;
  duplicate_leak_count: number;
  duplicate_leaks: DuplicateLeak[];
  healthy: boolean;
}

interface TimeseriesBucket {
  hour: string;
  sql_dedup: number;
  regression: number;
  rag_hard_block: number;
  rag_hit: number;
  cold_start: number;
  rag_error: number;
}

interface LatencyStat {
  stage: string;
  count: number;
  p25: number | null;
  p50: number | null;
  p75: number | null;
  p95: number | null;
  p99: number | null;
  min: number | null;
  max: number | null;
  mean: number | null;
}

interface DedupHealth {
  summary: DedupSummary;
  timeseries: TimeseriesBucket[];
  latency: LatencyStat[];
}

type OutcomeKey = "sql_dedup" | "regression" | "rag_hard_block" | "rag_hit" | "cold_start";

// ---------------------------------------------------------------------------
// Validated categorical palette — fixed order, dark-mode steps
// (dataviz skill reference palette; `node scripts/validate_palette.js` passed
// adjacent-pair CVD + normal-vision gates for this 5-slot ordering)
// ---------------------------------------------------------------------------

const OUTCOME_ORDER: OutcomeKey[] = ["sql_dedup", "regression", "rag_hard_block", "rag_hit", "cold_start"];

const OUTCOME_LABEL: Record<OutcomeKey, string> = {
  sql_dedup: "Layer 1 · SQL match",
  regression: "Layer 2 · regression",
  rag_hard_block: "Layer 3 · RAG hard block",
  rag_hit: "Layer 3b · RAG soft hint",
  cold_start: "Cold start (no match)",
};

const OUTCOME_COLOR: Record<OutcomeKey, string> = {
  sql_dedup: "#3987e5",
  regression: "#d95926",
  rag_hard_block: "#199e70",
  rag_hit: "#c98500",
  cold_start: "#d55181",
};

// Status palette — reserved, never reused for a series
const STATUS = {
  good: "#0ca30c",
  warning: "#fab219",
  critical: "#d03b3b",
};

const STAGE_LABEL: Record<string, string> = {
  layer1_sql: "Layer 1 — Postgres match",
  layer2_sql: "Layer 2 — Postgres regression lookup",
  layer3_rag_search: "Layer 3 — RAG semantic search",
  layer3_live_lookup: "Layer 3 — live store confirm",
};

const GATE_STEPS: { key: OutcomeKey; label: string; sub: string; kind: "hard" | "soft" }[] = [
  { key: "sql_dedup", label: "Layer 1", sub: "Postgres match", kind: "hard" },
  { key: "regression", label: "Layer 2", sub: "Resolved lookup", kind: "soft" },
  { key: "rag_hard_block", label: "Layer 3", sub: "RAG + live lookup", kind: "hard" },
  { key: "rag_hit", label: "Layer 3b", sub: "RAG soft hint", kind: "soft" },
];

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function fmtMs(v: number | null): string {
  if (v === null) return "—";
  if (v < 1) return "<1ms";
  if (v < 1000) return `${v.toFixed(1)}ms`;
  return `${(v / 1000).toFixed(2)}s`;
}

function fmtPct(v: number | null): string {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function fmtHour(h: string): string {
  const d = new Date(h.replace(":00", ":00:00") + "Z");
  return d.toLocaleTimeString([], { hour: "numeric" });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface HoverInfo { x: number; y: number; label: string; value: string }

export function DedupHealthPage() {
  const [data, setData] = useState<DedupHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<HoverInfo | null>(null);

  useEffect(() => {
    let cancelled = false;
    function load() {
      fetch("/metrics/dedup")
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then((d) => { if (!cancelled) { setData(d); setError(null); } })
        .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Failed"); });
    }
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error && !data) return <div style={errorStyle}>Cannot reach API: {error}</div>;
  if (!data) return <div style={loadingStyle}>Loading dedup gate health…</div>;

  const { summary, timeseries, latency } = data;
  const maxBar = Math.max(
    1,
    ...timeseries.map((b) => OUTCOME_ORDER.reduce((sum, k) => sum + b[k], 0))
  );
  const maxLatencyScale = Math.max(1, ...latency.map((l) => l.p95 ?? 0));

  return (
    <div style={page}>
      {/* Header */}
      <div style={pageHeader}>
        <div>
          <h2 style={titleStyle}>Dedup Gate Health</h2>
          <p style={subtitleStyle}>
            4-layer incident deduplication — windowed gate health, not a lifetime cumulative counter
          </p>
        </div>
        <HealthBadge healthy={summary.healthy} leaks={summary.duplicate_leak_count} errors={summary.rag_error_count} />
      </div>

      {/* Headline stats */}
      <div style={summaryRow}>
        <StatChip
          label="24h Block Rate"
          value={fmtPct(summary.block_rate)}
          color={OUTCOME_COLOR.sql_dedup}
          title="Share of incoming events dropped by Layer 1 (SQL match) or Layer 3 (RAG hard block) — the only two deterministic gates"
        />
        <StatChip label="Events (24h)" value={String(summary.total_events)} color="#94a3b8" />
        <StatChip
          label="RAG Error Rate"
          value={fmtPct(summary.rag_error_rate)}
          color={summary.rag_error_count > 0 ? STATUS.critical : STATUS.good}
          title="Layer 3's RAG call throwing. Used to be swallowed at debug level with zero visibility — now counted and surfaced here."
        />
        <StatChip
          label="Duplicate Leaks"
          value={String(summary.duplicate_leak_count)}
          color={summary.duplicate_leak_count > 0 ? STATUS.critical : STATUS.good}
          title="Ground truth check: two open incidents, same error_type + service, within 60 minutes of each other. Should always be zero."
        />
      </div>

      {/* Gate flow */}
      <GateFlow outcomes={summary.outcomes} total={summary.total_events} />

      {/* Outcome timeseries */}
      <div style={panel}>
        <div style={panelHeader}>
          <span style={panelTitle}>Gate Outcomes — last {timeseries.length}h</span>
          <Legend />
        </div>
        {timeseries.length === 0 ? (
          <p style={emptyStyle}>No dedup events recorded yet this window.</p>
        ) : (
          <div style={chartArea}>
            {timeseries.map((b) => (
              <StackedBar key={b.hour} bucket={b} maxValue={maxBar} onHover={setHover} />
            ))}
          </div>
        )}
      </div>

      {/* Latency percentiles */}
      <div style={panel}>
        <div style={panelHeader}>
          <span style={panelTitle}>Per-Layer Latency — p25 / p50 / p75 / p95</span>
        </div>
        <div style={latencyList}>
          {latency.length === 0 && <p style={emptyStyle}>No latency samples recorded yet.</p>}
          {latency.map((l) => (
            <LatencyRow key={l.stage} stat={l} maxScale={maxLatencyScale} />
          ))}
        </div>
      </div>

      {/* Duplicate leak table — ground truth, table view always available */}
      {summary.duplicate_leaks.length > 0 && (
        <div style={{ ...panel, border: `1px solid ${STATUS.critical}66` }}>
          <div style={panelHeader}>
            <span style={{ ...panelTitle, color: STATUS.critical }}>⚠ Duplicate Leaks Detected</span>
          </div>
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Error Type</th>
                <th style={th}>Service</th>
                <th style={th}>Incident A</th>
                <th style={th}>Incident B</th>
                <th style={{ ...th, textAlign: "right" }}>Minutes Apart</th>
              </tr>
            </thead>
            <tbody>
              {summary.duplicate_leaks.map((l, i) => (
                <tr key={i} style={tableRow}>
                  <td style={td}>{l.error_type}</td>
                  <td style={td}>{l.service}</td>
                  <td style={{ ...td, fontFamily: "monospace" }}>{l.incident_a.slice(0, 8)}</td>
                  <td style={{ ...td, fontFamily: "monospace" }}>{l.incident_b.slice(0, 8)}</td>
                  <td style={{ ...td, textAlign: "right" }}>{l.minutes_apart}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

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

function HealthBadge({ healthy, leaks, errors }: { healthy: boolean; leaks: number; errors: number }) {
  if (healthy) {
    return (
      <span style={{ ...badge, color: STATUS.good, borderColor: `${STATUS.good}55`, background: `${STATUS.good}18` }}>
        ✓ Gate healthy — 0 leaks, 0 RAG errors
      </span>
    );
  }
  const parts: string[] = [];
  if (leaks > 0) parts.push(`${leaks} duplicate leak${leaks > 1 ? "s" : ""}`);
  if (errors > 0) parts.push(`${errors} RAG error${errors > 1 ? "s" : ""}`);
  return (
    <span style={{ ...badge, color: STATUS.critical, borderColor: `${STATUS.critical}55`, background: `${STATUS.critical}18` }}>
      ⚠ {parts.join(" · ")}
    </span>
  );
}

function StatChip({ label, value, color, title }: { label: string; value: string; color: string; title?: string }) {
  return (
    <div style={chip} title={title}>
      <div style={chipLabel}>{label}</div>
      <div style={{ ...chipValue, color }}>{value}</div>
    </div>
  );
}

function GateFlow({ outcomes, total }: { outcomes: Record<string, number>; total: number }) {
  const pct = (n: number) => (total > 0 ? `${((n / total) * 100).toFixed(1)}%` : "—");
  return (
    <div style={flowPanel}>
      {GATE_STEPS.map((s, i) => (
        <React.Fragment key={s.key}>
          <div style={flowStep}>
            <div style={{ ...flowKindTag, ...(s.kind === "hard" ? flowHard : flowSoft) }}>
              {s.kind === "hard" ? "HARD BLOCK" : "SOFT HINT"}
            </div>
            <div style={flowLabel}>{s.label}</div>
            <div style={flowSub}>{s.sub}</div>
            <div style={{ ...flowCount, color: OUTCOME_COLOR[s.key] }}>{outcomes[s.key] ?? 0}</div>
            <div style={flowPct}>{pct(outcomes[s.key] ?? 0)} of events</div>
          </div>
          {i < GATE_STEPS.length - 1 && <div style={flowArrow}>→</div>}
        </React.Fragment>
      ))}
    </div>
  );
}

function Legend() {
  return (
    <div style={legendRow}>
      {OUTCOME_ORDER.map((k) => (
        <div key={k} style={legendItem}>
          <span style={{ ...legendSwatch, background: OUTCOME_COLOR[k] }} />
          <span style={legendText}>{OUTCOME_LABEL[k]}</span>
        </div>
      ))}
      <div style={legendItem}>
        <span style={{ ...legendSwatch, background: STATUS.critical, borderRadius: "50%" }} />
        <span style={legendText}>RAG error</span>
      </div>
    </div>
  );
}

const BAR_HEIGHT_PX = 140;

function StackedBar({
  bucket, maxValue, onHover,
}: { bucket: TimeseriesBucket; maxValue: number; onHover: (h: HoverInfo | null) => void }) {
  return (
    <div style={barCol} onMouseLeave={() => onHover(null)}>
      <div style={barStack}>
        {bucket.rag_error > 0 && (
          <span
            style={errorTick}
            onMouseEnter={(e) => onHover({
              x: e.clientX, y: e.clientY,
              label: `${bucket.hour} · RAG error`, value: `${bucket.rag_error} failed call(s)`,
            })}
          />
        )}
        {[...OUTCOME_ORDER].reverse().map((key) => {
          const value = bucket[key];
          if (value <= 0) return null;
          return (
            <div
              key={key}
              style={{
                height: `${(value / maxValue) * BAR_HEIGHT_PX}px`,
                background: OUTCOME_COLOR[key],
                marginTop: 2,
                borderRadius: 2,
              }}
              onMouseEnter={(e) => onHover({
                x: e.clientX, y: e.clientY,
                label: OUTCOME_LABEL[key], value: `${value} · ${bucket.hour}`,
              })}
            />
          );
        })}
      </div>
      <div style={barHourLabel}>{fmtHour(bucket.hour)}</div>
    </div>
  );
}

function LatencyRow({ stat, maxScale }: { stat: LatencyStat; maxScale: number }) {
  const label = STAGE_LABEL[stat.stage] ?? stat.stage;
  if (stat.count === 0 || stat.p25 === null || stat.p50 === null || stat.p75 === null || stat.p95 === null) {
    return (
      <div style={latencyRow}>
        <div style={latencyLabel}>{label}</div>
        <div style={latencyEmpty}>no samples yet</div>
      </div>
    );
  }
  const pct = (v: number) => `${Math.min(100, (v / maxScale) * 100)}%`;
  return (
    <div style={latencyRow}>
      <div style={latencyLabel}>{label}</div>
      <div style={latencyTrack}>
        <div style={{ ...latencyBand, left: pct(stat.p25), width: `calc(${pct(stat.p75)} - ${pct(stat.p25)})` }} />
        <div style={{ ...latencyP50Dot, left: pct(stat.p50) }} title={`p50: ${fmtMs(stat.p50)}`} />
        <div style={{ ...latencyP95Tick, left: pct(stat.p95) }} title={`p95: ${fmtMs(stat.p95)}`} />
      </div>
      <div style={latencyValues}>
        <span style={lv}>p25 {fmtMs(stat.p25)}</span>
        <span style={{ ...lv, color: "#e2e8f0", fontWeight: 700 }}>p50 {fmtMs(stat.p50)}</span>
        <span style={lv}>p75 {fmtMs(stat.p75)}</span>
        <span style={{ ...lv, color: "#4b5563" }}>p95 {fmtMs(stat.p95)}</span>
      </div>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const page: React.CSSProperties = {
  maxWidth: 1200, margin: "0 auto", padding: "24px",
  display: "flex", flexDirection: "column", gap: 20,
};

const pageHeader: React.CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 16,
};

const titleStyle: React.CSSProperties = { fontSize: 20, fontWeight: 700, color: "#e2e8f0", margin: 0 };
const subtitleStyle: React.CSSProperties = { fontSize: 12, color: "#475569", margin: "4px 0 0" };

const badge: React.CSSProperties = {
  fontSize: 12, fontWeight: 700, borderRadius: 8, border: "1px solid",
  padding: "6px 14px", whiteSpace: "nowrap",
};

const summaryRow: React.CSSProperties = { display: "flex", gap: 10, flexWrap: "wrap" };

const chip: React.CSSProperties = {
  background: "#161927", border: "1px solid #2d3149", borderRadius: 8, padding: "10px 16px", minWidth: 130,
};
const chipLabel: React.CSSProperties = {
  fontSize: 9, fontWeight: 700, color: "#475569", letterSpacing: "0.08em", textTransform: "uppercase",
};
const chipValue: React.CSSProperties = { fontSize: 20, fontWeight: 700, marginTop: 2 };

const flowPanel: React.CSSProperties = {
  display: "flex", alignItems: "center", background: "#1e2130", border: "1px solid #2d3149",
  borderRadius: 12, padding: "18px 20px", overflowX: "auto", gap: 4,
};
const flowStep: React.CSSProperties = {
  display: "flex", flexDirection: "column", alignItems: "center", gap: 4, minWidth: 140, padding: "0 10px",
};
const flowKindTag: React.CSSProperties = {
  fontSize: 9, fontWeight: 700, letterSpacing: "0.06em", borderRadius: 4, padding: "2px 6px", marginBottom: 4,
};
const flowHard: React.CSSProperties = { color: "#e2e8f0", background: "#3b4fd8" };
const flowSoft: React.CSSProperties = { color: "#94a3b8", background: "#2d3149" };
const flowLabel: React.CSSProperties = { fontSize: 14, fontWeight: 700, color: "#e2e8f0" };
const flowSub: React.CSSProperties = { fontSize: 11, color: "#64748b" };
const flowCount: React.CSSProperties = { fontSize: 22, fontWeight: 700, marginTop: 4 };
const flowPct: React.CSSProperties = { fontSize: 10, color: "#475569" };
const flowArrow: React.CSSProperties = { fontSize: 18, color: "#374151", flexShrink: 0 };

const panel: React.CSSProperties = { background: "#1e2130", border: "1px solid #2d3149", borderRadius: 12, padding: "18px 20px" };
const panelHeader: React.CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10, marginBottom: 16,
};
const panelTitle: React.CSSProperties = {
  fontSize: 12, fontWeight: 700, color: "#94a3b8", letterSpacing: "0.06em", textTransform: "uppercase",
};

const legendRow: React.CSSProperties = { display: "flex", flexWrap: "wrap", gap: 12 };
const legendItem: React.CSSProperties = { display: "flex", alignItems: "center", gap: 5 };
const legendSwatch: React.CSSProperties = { width: 9, height: 9, borderRadius: 2, flexShrink: 0 };
const legendText: React.CSSProperties = { fontSize: 11, color: "#64748b" };

const chartArea: React.CSSProperties = {
  display: "flex", alignItems: "flex-end", gap: 3, height: BAR_HEIGHT_PX + 24, overflowX: "auto", padding: "0 2px",
};
const barCol: React.CSSProperties = {
  display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "flex-end",
  minWidth: 10, height: "100%", position: "relative",
};
const barStack: React.CSSProperties = {
  display: "flex", flexDirection: "column-reverse", alignItems: "stretch", width: 10, position: "relative",
};
const errorTick: React.CSSProperties = {
  position: "absolute", top: -8, left: 1, width: 8, height: 5, borderRadius: 1, background: STATUS.critical, cursor: "pointer",
};
const barHourLabel: React.CSSProperties = { fontSize: 8, color: "#374151", marginTop: 4, whiteSpace: "nowrap" };

const latencyList: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 14 };
const latencyRow: React.CSSProperties = { display: "grid", gridTemplateColumns: "220px 1fr 260px", gap: 14, alignItems: "center" };
const latencyLabel: React.CSSProperties = { fontSize: 12, color: "#94a3b8" };
const latencyEmpty: React.CSSProperties = { fontSize: 11, color: "#374151", fontStyle: "italic" };
const latencyTrack: React.CSSProperties = { position: "relative", height: 6, background: "#161927", borderRadius: 3 };
const latencyBand: React.CSSProperties = { position: "absolute", top: 0, height: "100%", background: "#3987e544", borderRadius: 3 };
const latencyP50Dot: React.CSSProperties = {
  position: "absolute", top: -3, width: 12, height: 12, borderRadius: "50%", background: "#3987e5",
  border: "2px solid #1e2130", transform: "translateX(-50%)", cursor: "pointer",
};
const latencyP95Tick: React.CSSProperties = {
  position: "absolute", top: -2, width: 2, height: 10, background: "#4b5563", transform: "translateX(-50%)", cursor: "pointer",
};
const latencyValues: React.CSSProperties = { display: "flex", gap: 10, justifyContent: "flex-end", flexWrap: "wrap" };
const lv: React.CSSProperties = { fontSize: 10, color: "#64748b", fontFamily: "monospace" };

const table: React.CSSProperties = { width: "100%", borderCollapse: "collapse", fontSize: 12 };
const th: React.CSSProperties = {
  fontSize: 10, fontWeight: 600, color: "#4b5563", letterSpacing: "0.05em", textTransform: "uppercase",
  padding: "6px 8px", borderBottom: "1px solid #2d3149", textAlign: "left",
};
const tableRow: React.CSSProperties = { borderBottom: "1px solid #1a1f30" };
const td: React.CSSProperties = { padding: "8px", color: "#94a3b8" };

const emptyStyle: React.CSSProperties = { color: "#4b5563", fontStyle: "italic", fontSize: 13, textAlign: "center", padding: "20px 0" };
const loadingStyle: React.CSSProperties = { color: "#475569", padding: 40, textAlign: "center" };
const errorStyle: React.CSSProperties = { color: "#f87171", padding: 40 };

const tooltip: React.CSSProperties = {
  position: "fixed", zIndex: 50, background: "#0b0d14", border: "1px solid #2d3149", borderRadius: 6,
  padding: "6px 10px", pointerEvents: "none", boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
};
const tooltipLabel: React.CSSProperties = { fontSize: 10, color: "#64748b" };
const tooltipValue: React.CSSProperties = { fontSize: 12, color: "#e2e8f0", fontWeight: 700 };
