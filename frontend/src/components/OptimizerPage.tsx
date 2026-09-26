import React, { useEffect, useState } from "react";

/**
 * OptimizerPage — status of the harness optimizer (app/harness_optimizer/).
 *
 * Backed by /optimizer/runs (app/api/routes/optimizer.py), which reads the
 * run directories the optimizer writes under runs/harness/ — read-only. The
 * runs live on the machine that ran them, so the deployed dashboard shows none.
 *
 * What it answers: is the run alive (heartbeat), where is it (round, phase,
 * cases done), what has it spent, has a tripwire fired, what did each round
 * propose and why was it accepted or rejected, and — once written — the
 * held-out report against the matched-budget rerun baseline.
 */

// ---------------------------------------------------------------------------
// Types (mirror app/api/routes/optimizer.py)
// ---------------------------------------------------------------------------

interface RunBrief {
  run_id: string;
  round: number;
  phase: string;
  spent_usd: number;
  budget_usd: number;
  S_star: number | null;
  accepted: string[];
  stop_reason: string | null;
  running: boolean;
  updated_at: number;
}

interface EvalSummary {
  cases: number;
  trials: number;
  S: number | null;
  C: number | null;
  escalation_rate: number | null;
  cost_usd: number;
}

interface InFlight {
  label: string;
  trials: number;
  total: number;
  done: number;
  so_far: EvalSummary;
  incumbent_same_cases?: EvalSummary;
}

interface Session {
  start: string;
  end?: string;
  last_seen?: string;
  seconds?: number;
  replays?: number;
  ended_because?: string;
}

interface HealthCheck { time: string; round: number; phase: string; cases: number; problems: string[] }

interface HistoryEntry {
  round: number;
  candidate_id: string;
  component: string;
  hypothesis: string;
  diff: string;
  outcome: string;
  reason: string;
  delta_S: number | null;
  delta_C: number | null;
  improved: string[];
  regressed: string[];
  cost_usd: number;
}

interface Arm { [key: string]: number }

interface Report {
  verdict: string;
  original: Arm;
  evolved: Arm | null;
  comparisons?: Record<string, { diff: number; ci95: [number, number]; direction: string }>;
  paired?: { improved: string[]; regressed: string[]; unchanged: number };
}

interface RunDetail extends RunBrief {
  heartbeat: { time: string; seconds_since_progress: number } | null;
  heartbeat_age_s: number | null;
  delta: number | null;
  delta_esc: number | null;
  rounds_stop_reason: string | null;
  config: Record<string, number>;
  timing: { started_at?: string; finished_at?: string; sessions: Session[]; active_hours: number;
            phases: { phase: string; start: string; seconds: number }[] };
  health: { checks?: number; last_check?: HealthCheck; trips?: HealthCheck[];
            smoke?: { verdicts: string[]; cost_usd: number; problems: string[] } };
  candidate: { id: string; component?: string; hypothesis?: string; repairs?: number } | null;
  in_flight: InFlight | null;
  incumbent_tiers: ({ tier: string } & EvalSummary)[] | null;
  history: HistoryEntry[];
  report: Report | null;
  log_tail: string[];
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

const STATUS = { good: "#0ca30c", warning: "#fab219", critical: "#d03b3b", info: "#3987e5", muted: "#64748b" };

function fmt(v: number | null | undefined, digits = 3): string {
  return v === null || v === undefined ? "—" : v.toFixed(digits);
}
function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;
}
function usd(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `$${v.toFixed(2)}`;
}
function signed(v: number | null | undefined, asPct = false): string {
  if (v === null || v === undefined) return "—";
  const s = asPct ? `${(v * 100).toFixed(1)}%` : v.toFixed(3);
  return v > 0 ? `+${s}` : s;
}
function hours(seconds: number | undefined): string {
  if (!seconds) return "0m";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function time(iso: string | undefined): string {
  return iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
}

function runStatus(r: RunBrief): { label: string; color: string } {
  if (r.running) return { label: "RUNNING", color: STATUS.good };
  if (r.phase === "done") return { label: "DONE", color: STATUS.info };
  if (r.stop_reason) return { label: "PAUSED", color: STATUS.warning };
  return { label: "NOT RUNNING", color: STATUS.critical };
}

const OUTCOME_COLOR: Record<string, string> = {
  accepted: STATUS.good,
  rejected: STATUS.muted,
  critic_rejected: STATUS.warning,
  invalid: STATUS.critical,
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function OptimizerPage() {
  const [runs, setRuns] = useState<RunBrief[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    function load() {
      fetch("/api/optimizer/runs")
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then((d: RunBrief[]) => {
          if (cancelled) return;
          setRuns(d);
          setSelected((cur) => cur ?? (d[0]?.run_id ?? null));
        })
        .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Failed"); });
    }
    load();
    const id = setInterval(load, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    function load() {
      fetch(`/api/optimizer/runs/${encodeURIComponent(selected!)}`)
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
        .then((d: RunDetail) => { if (!cancelled) { setDetail(d); setError(null); } })
        .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Failed"); });
    }
    load();
    const id = setInterval(load, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [selected]);

  if (error && !detail) return <main style={page}><p style={{ color: STATUS.critical }}>Cannot load optimizer runs: {error}</p></main>;
  if (!runs.length) return <main style={page}><p style={muted}>No optimizer runs under runs/harness/ on this machine.</p></main>;
  if (!detail) return <main style={page}><p style={muted}>Loading…</p></main>;

  const st = runStatus(detail);
  const budgetFrac = detail.budget_usd ? Math.min(1, detail.spent_usd / detail.budget_usd) : 0;
  const f = detail.in_flight;
  const judged = detail.history.filter((h) => !h.outcome.startsWith("pilot_"));
  const pilot = detail.history.filter((h) => h.outcome.startsWith("pilot_"));

  return (
    <main style={page}>
      {/* Header: run picker + status */}
      <section style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <h1 style={h1}>Harness optimizer</h1>
        <select style={select} value={selected ?? ""} onChange={(e) => { setSelected(e.target.value); setDetail(null); }}>
          {runs.map((r) => (
            <option key={r.run_id} value={r.run_id}>{r.run_id} · {runStatus(r).label.toLowerCase()}</option>
          ))}
        </select>
        <span style={pill(st.color)}>{st.label}</span>
        <span style={muted}>
          round {detail.round} · {detail.phase}
          {detail.heartbeat_age_s !== null && ` · heartbeat ${Math.round(detail.heartbeat_age_s)}s ago`}
        </span>
      </section>

      {detail.stop_reason && (
        <div style={banner(detail.phase === "done" ? STATUS.info : STATUS.warning)}>{detail.stop_reason}</div>
      )}

      {/* Top cards */}
      <section style={grid}>
        <Card title="Spend">
          <Big>{usd(detail.spent_usd)}</Big>
          <span style={muted}> of {usd(detail.budget_usd)}</span>
          <div style={barTrack}><div style={{ ...barFill, width: `${budgetFrac * 100}%`, background: budgetFrac > 0.9 ? STATUS.warning : STATUS.info }} /></div>
        </Card>
        <Card title="Unattended time">
          <Big>{hours(detail.timing.active_hours * 3600)}</Big>
          <div style={muted}>{detail.timing.sessions.length} session{detail.timing.sessions.length === 1 ? "" : "s"} · started {time(detail.timing.started_at)}</div>
          {detail.timing.finished_at && <div style={muted}>finished {time(detail.timing.finished_at)}</div>}
        </Card>
        <Card title="Baseline">
          <Row k="S* (pass rate)" v={fmt(detail.S_star)} />
          <Row k="δ noise band" v={fmt(detail.delta)} />
          <Row k="δ escalation" v={fmt(detail.delta_esc)} />
        </Card>
        <Card title="Edits">
          <Big>{detail.accepted.length}</Big><span style={muted}> accepted of {judged.length} judged</span>
          <div style={muted}>{detail.config.max_rounds} rounds max · stops after {detail.config.max_stall} rejections in a row</div>
        </Card>
        <Card title="Tripwires">
          {detail.health.trips?.length
            ? <div style={{ color: STATUS.critical, fontWeight: 600 }}>{detail.health.trips.length} tripped</div>
            : <div style={{ color: STATUS.good, fontWeight: 600 }}>none tripped</div>}
          <div style={muted}>{detail.health.checks ?? 0} checks{detail.health.last_check && ` · last ${time(detail.health.last_check.time)}`}</div>
          {detail.health.smoke && (
            <div style={muted}>smoke: {detail.health.smoke.verdicts.join(" ")} · {usd(detail.health.smoke.cost_usd)}</div>
          )}
        </Card>
      </section>

      {/* Evaluation in flight */}
      {f && (
        <Card title={`Now: ${f.label}`}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, color: "#cbd5e1" }}>
            <span>{f.done} / {f.total} cases · {f.trials} trial{f.trials === 1 ? "" : "s"} each</span>
            <span>{usd(f.so_far.cost_usd)} so far</span>
          </div>
          <div style={barTrack}><div style={{ ...barFill, width: `${f.total ? (f.done / f.total) * 100 : 0}%`, background: STATUS.good }} /></div>
          {f.done > 0 && (
            <table style={table}>
              <thead><tr><th style={th}></th><th style={th}>Pass rate</th><th style={th}>Cost / trial</th><th style={th}>Escalation</th></tr></thead>
              <tbody>
                <tr><td style={td}>so far</td><td style={td}>{fmt(f.so_far.S)}</td><td style={td}>{usd(f.so_far.C)}</td><td style={td}>{pct(f.so_far.escalation_rate)}</td></tr>
                {f.incumbent_same_cases && (
                  <tr><td style={td}>incumbent, same cases</td><td style={td}>{fmt(f.incumbent_same_cases.S)}</td>
                    <td style={td}>{usd(f.incumbent_same_cases.C)}</td><td style={td}>{pct(f.incumbent_same_cases.escalation_rate)}</td></tr>
                )}
              </tbody>
            </table>
          )}
          {detail.candidate?.hypothesis && (
            <p style={{ ...muted, marginTop: 10, lineHeight: 1.5 }}>
              <b style={{ color: "#94a3b8" }}>{detail.candidate.id} [{detail.candidate.component}]</b> {detail.candidate.hypothesis}
            </p>
          )}
        </Card>
      )}

      {/* Held-out report */}
      {detail.report && (
        <Card title="Held-out report">
          <p style={{ color: "#e2e8f0", fontSize: 14, marginBottom: 10 }}>{detail.report.verdict}</p>
          <table style={table}>
            <thead><tr><th style={th}></th><th style={th}>original</th>{detail.report.evolved && <th style={th}>evolved</th>}</tr></thead>
            <tbody>
              {Object.keys(detail.report.original).filter((k) => k.startsWith("pass") || k.startsWith("cost") || k.startsWith("esc")).map((k) => (
                <tr key={k}><td style={td}>{k}</td><td style={td}>{fmt(detail.report!.original[k])}</td>
                  {detail.report!.evolved && <td style={td}>{fmt(detail.report!.evolved[k])}</td>}</tr>
              ))}
            </tbody>
          </table>
          {detail.report.comparisons && Object.entries(detail.report.comparisons).map(([name, c]) => (
            <div key={name} style={{ ...muted, marginTop: 6 }}>
              <b style={{ color: "#94a3b8" }}>{name.replace(/_/g, " ")}:</b> {signed(c.diff)} (95% CI {signed(c.ci95[0])} to {signed(c.ci95[1])}), {c.direction}
            </div>
          ))}
        </Card>
      )}

      {/* Rounds */}
      <Card title="Rounds">
        {judged.length === 0 && <p style={muted}>No candidate judged yet.</p>}
        {judged.length > 0 && (
          <table style={table}>
            <thead><tr><th style={th}>Round</th><th style={th}>Component</th><th style={th}>Outcome</th><th style={th}>ΔS</th><th style={th}>ΔC</th><th style={th}>Eval cost</th><th style={th}>Reason</th></tr></thead>
            <tbody>
              {judged.map((h) => (
                <React.Fragment key={h.candidate_id}>
                  <tr style={{ cursor: "pointer" }} onClick={() => setOpen(open === h.candidate_id ? null : h.candidate_id)}>
                    <td style={td}>{h.candidate_id}</td>
                    <td style={td}>{h.component}</td>
                    <td style={td}><span style={pill(OUTCOME_COLOR[h.outcome] ?? STATUS.muted)}>{h.outcome}</span></td>
                    <td style={td}>{signed(h.delta_S)}</td>
                    <td style={td}>{signed(h.delta_C, true)}</td>
                    <td style={td}>{usd(h.cost_usd)}</td>
                    <td style={{ ...td, color: "#94a3b8", maxWidth: 480 }}>{h.reason}</td>
                  </tr>
                  {open === h.candidate_id && (
                    <tr><td style={td} colSpan={7}>
                      <p style={{ ...muted, lineHeight: 1.5 }}><b style={{ color: "#94a3b8" }}>Hypothesis:</b> {h.hypothesis}</p>
                      <p style={muted}>Improved: {h.improved.join(", ") || "—"}</p>
                      <p style={muted}>Regressed: {h.regressed.join(", ") || "—"}</p>
                      <pre style={pre}>{h.diff}</pre>
                    </td></tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        )}
        {pilot.length > 0 && <p style={{ ...muted, marginTop: 8 }}>Seeded with {pilot.length} pilot-run candidate{pilot.length === 1 ? "" : "s"} as notes for the proposer (not applied to this run's harness).</p>}
      </Card>

      <section style={grid2}>
        {/* Incumbent by tier */}
        {detail.incumbent_tiers && (
          <Card title="Incumbent by tier">
            <table style={table}>
              <thead><tr><th style={th}>Tier</th><th style={th}>Cases</th><th style={th}>Pass rate</th><th style={th}>Escalation</th><th style={th}>Cost / trial</th></tr></thead>
              <tbody>
                {detail.incumbent_tiers.map((t) => (
                  <tr key={t.tier}><td style={td}>{t.tier}</td><td style={td}>{t.cases}</td><td style={td}>{fmt(t.S)}</td>
                    <td style={td}>{pct(t.escalation_rate)}</td><td style={td}>{usd(t.C)}</td></tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}

        {/* Sessions */}
        <Card title="Sessions">
          <table style={table}>
            <thead><tr><th style={th}>Start</th><th style={th}>Length</th><th style={th}>Replays</th><th style={th}>Ended</th></tr></thead>
            <tbody>
              {detail.timing.sessions.map((s, i) => (
                <tr key={i}><td style={td}>{time(s.start)}</td><td style={td}>{hours(s.seconds)}</td><td style={td}>{s.replays ?? 0}</td>
                  <td style={{ ...td, color: "#94a3b8" }}>{s.ended_because ?? (detail.running && i === detail.timing.sessions.length - 1 ? "running" : "—")}</td></tr>
              ))}
              {detail.timing.sessions.length === 0 && <tr><td style={td} colSpan={4}>Not recorded (run predates timing).</td></tr>}
            </tbody>
          </table>
        </Card>
      </section>

      {/* Log */}
      {detail.log_tail.length > 0 && (
        <Card title="Log (tail)">
          <pre style={pre}>{detail.log_tail.join("\n")}</pre>
        </Card>
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Small pieces
// ---------------------------------------------------------------------------

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={card}>
      <h2 style={cardTitle}>{title}</h2>
      {children}
    </div>
  );
}

function Big({ children }: { children: React.ReactNode }) {
  return <span style={{ fontSize: 24, fontWeight: 700, color: "#e2e8f0" }}>{children}</span>;
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, padding: "2px 0" }}>
      <span style={{ color: "#64748b" }}>{k}</span><span style={{ color: "#e2e8f0", fontVariantNumeric: "tabular-nums" }}>{v}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles (match the rest of the dashboard)
// ---------------------------------------------------------------------------

const page: React.CSSProperties = { maxWidth: 1400, margin: "0 auto", padding: 24, display: "flex", flexDirection: "column", gap: 16 };
const h1: React.CSSProperties = { fontSize: 18, fontWeight: 700, color: "#e2e8f0" };
const muted: React.CSSProperties = { fontSize: 12, color: "#64748b" };
const grid: React.CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))", gap: 16 };
const grid2: React.CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))", gap: 16 };
const card: React.CSSProperties = { background: "#1e2130", border: "1px solid #2d3149", borderRadius: 12, padding: "16px 20px" };
const cardTitle: React.CSSProperties = { fontSize: 12, fontWeight: 700, color: "#94a3b8", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 10 };
const select: React.CSSProperties = { background: "#161927", border: "1px solid #2d3149", color: "#e2e8f0", borderRadius: 6, padding: "4px 8px", fontSize: 13 };
const table: React.CSSProperties = { width: "100%", borderCollapse: "collapse", fontSize: 13, marginTop: 8 };
const th: React.CSSProperties = { textAlign: "left", color: "#64748b", fontWeight: 600, padding: "6px 8px", borderBottom: "1px solid #2d3149" };
const td: React.CSSProperties = { color: "#e2e8f0", padding: "6px 8px", borderBottom: "1px solid #232739", verticalAlign: "top", fontVariantNumeric: "tabular-nums" };
const pre: React.CSSProperties = { background: "#161927", border: "1px solid #2d3149", borderRadius: 6, padding: 10, fontSize: 11, color: "#cbd5e1", overflowX: "auto", whiteSpace: "pre-wrap", maxHeight: 360 };
const barTrack: React.CSSProperties = { height: 6, background: "#161927", borderRadius: 3, marginTop: 8, overflow: "hidden" };
const barFill: React.CSSProperties = { height: "100%", borderRadius: 3 };
const pill = (color: string): React.CSSProperties => ({
  fontSize: 11, fontWeight: 700, color, border: `1px solid ${color}`, borderRadius: 4, padding: "1px 7px", letterSpacing: "0.04em",
});
const banner = (color: string): React.CSSProperties => ({
  fontSize: 13, color: "#e2e8f0", background: "#161927", borderLeft: `3px solid ${color}`, borderRadius: 6, padding: "8px 12px",
});
