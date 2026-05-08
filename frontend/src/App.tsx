import React, { useState, useEffect } from "react";
import { useDashboard } from "./hooks/useDashboard";
import { useDashboardWS } from "./hooks/useWebSocket";
import { LogsPage } from "./components/LogsPage";
import { ECSPillar } from "./components/ECSPillar";
import { ECSTaskPillar } from "./components/ECSTaskPillar";
import { EC2Pillar } from "./components/EC2Pillar";
import { ALBPillar } from "./components/ALBPillar";
import { DOPillar } from "./components/DOPillar";
import { CloudfarePillar } from "./components/CloudfarePillar";
import { MongoDBPillar } from "./components/MongoDBPillar";
import { GitHubPillar } from "./components/GitHubPillar";
import { IncidentsPage } from "./components/IncidentsPage";
import { IncidentsTablePage } from "./components/IncidentsTablePage";
import { EventApprovalPage } from "./components/EventApprovalPage";
import { PRsPage } from "./components/PRsPage";
import { StatsPage } from "./components/StatsPage";
import { DatasetPage } from "./components/DatasetPage";
import { PerformancePage } from "./components/PerformancePage";

function formatTs(d: Date) {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

type Tab = "dashboard" | "events" | "incidents" | "table" | "prs" | "stats" | "performance" | "logs" | "dataset";

interface CBInfo { name: string; state: string; failure_count: number; total_rejected: number }

export default function App() {
  const { data, loading, error, lastUpdated, refetch } = useDashboard();
  const {
    incidents,
    metrics,
    agentSnapshot,
    connected,
    scanLog,
    pendingEvents,
    refreshPendingEvents,
  } = useDashboardWS();
  const [tab, setTab] = useState<Tab>("dashboard");
  const [breakers, setBreakers] = useState<CBInfo[]>([]);
  const [resetting, setResetting] = useState<string | null>(null);
  const [datasetVisited, setDatasetVisited] = useState(false);

  useEffect(() => {
    if (tab === "dataset") setDatasetVisited(true);
  }, [tab]);

  useEffect(() => {
    function fetchBreakers() {
      fetch("/api/circuit-breakers")
        .then((r) => r.json())
        .then((data: CBInfo[]) => setBreakers(data))
        .catch(() => {});
    }
    fetchBreakers();
    const id = setInterval(fetchBreakers, 10_000);
    return () => clearInterval(id);
  }, []);

  async function resetBreaker(name: string) {
    setResetting(name);
    try {
      await fetch(`/api/circuit-breakers/${name}/reset`, { method: "POST" });
      setBreakers((prev) => prev.map((b) => b.name === name ? { ...b, state: "closed", failure_count: 0 } : b));
    } finally {
      setResetting(null);
    }
  }

  const openBreakers = breakers.filter((b) => b.state === "open");

  return (
    <div style={layout}>
      {/* Header */}
      <header style={headerBar}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={logo}>⚡</span>
          <span style={logoText}>Agent Platform</span>
          <div style={{ display: "flex", gap: 4, marginLeft: 16 }}>
            <button style={tabBtn(tab === "dashboard")}  onClick={() => setTab("dashboard")}>Dashboard</button>
            <button style={tabBtn(tab === "events")}    onClick={() => setTab("events")}>
              Events{pendingEvents.length > 0 && <span style={pendingDot}>{pendingEvents.length}</span>}
            </button>
            <button style={tabBtn(tab === "incidents")} onClick={() => setTab("incidents")}>Incidents</button>
            <button style={tabBtn(tab === "table")}     onClick={() => setTab("table")}>Table</button>
            <button style={tabBtn(tab === "prs")}       onClick={() => setTab("prs")}>Agent PRs</button>
            <button style={tabBtn(tab === "stats")}     onClick={() => setTab("stats")}>Stats</button>
            <button style={tabBtn(tab === "performance")} onClick={() => setTab("performance")}>Performance</button>
            <button style={tabBtn(tab === "logs")}      onClick={() => setTab("logs")}>Logs</button>
            <button style={tabBtn(tab === "dataset")}   onClick={() => setTab("dataset")}>Dataset</button>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          {tab === "dashboard" && (
            <>
              {lastUpdated && (
                <span style={lastUpdatedText}>Updated {formatTs(lastUpdated)}</span>
              )}
              <button style={refreshBtn} onClick={refetch}>Refresh</button>
            </>
          )}
          {tab === "dashboard" && data && (
            <span style={queueBadge}>
              Queue: {data.queue.queue_size} · Enqueued: {data.queue.total_enqueued}
            </span>
          )}
        </div>
      </header>

      {/* Circuit breaker alert banner */}
      {openBreakers.length > 0 && (
        <div style={cbBanner}>
          <span style={cbBannerIcon}>⚡</span>
          <span style={cbBannerText}>
            Circuit breaker{openBreakers.length > 1 ? "s" : ""} OPEN:{" "}
            {openBreakers.map((b) => b.name).join(", ")} — LLM calls are being rejected
          </span>
          <div style={{ display: "flex", gap: 6 }}>
            {openBreakers.map((b) => (
              <button
                key={b.name}
                style={cbResetBtn(resetting === b.name)}
                onClick={() => resetBreaker(b.name)}
                disabled={resetting === b.name}
              >
                {resetting === b.name ? "Resetting…" : `Reset ${b.name}`}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Main content */}
      {tab === "logs" && <LogsPage />}
      {tab === "events" && <EventApprovalPage events={pendingEvents} />}
      {tab === "table" && <IncidentsTablePage incidents={incidents} />}
      {tab === "incidents" && (
        <IncidentsPage
          incidents={incidents}
          metrics={metrics ?? data?.incidents ?? null}
          connected={connected}
          scanLog={scanLog}
          onPendingEventsChanged={refreshPendingEvents}
        />
      )}
      {tab === "prs" && (
        <main style={main}>
          <PRsPage />
        </main>
      )}
      {tab === "stats" && <StatsPage />}
      {tab === "performance" && <PerformancePage />}
      {datasetVisited && <DatasetPage hidden={tab !== "dataset"} />}
      {tab === "dashboard" && <main style={main}>
        {loading && !data && (
          <div style={centered}>
            <span style={{ fontSize: 24 }}>⏳</span>
            <p style={{ color: "#64748b", marginTop: 12 }}>Loading infrastructure data...</p>
          </div>
        )}

        {error && !data && (
          <div style={centered}>
            <span style={{ fontSize: 24 }}>⚠</span>
            <p style={{ color: "#f87171", marginTop: 12 }}>
              Cannot reach API: {error}
            </p>
            <p style={{ color: "#64748b", fontSize: 13, marginTop: 6 }}>
              Make sure the FastAPI server is running on port 8000.
            </p>
          </div>
        )}

        {data && (
          <>
            {/* 4 Pillars */}
            <section style={pillarsGrid}>
              <ECSPillar services={data.ecs} />
              <ECSTaskPillar clusters={data.ecs_task_clusters ?? []} />
              <EC2Pillar data={data.ec2} />
              <ALBPillar albs={data.alb ?? []} />
              <div style={{ gridColumn: "span 2" }}>
                <DOPillar data={data.digital_ocean} />
              </div>
              <div style={{ gridColumn: "span 2" }}>
                <CloudfarePillar data={data.cloudflare} />
              </div>
              <div style={{ gridColumn: "span 2" }}>
                <MongoDBPillar data={data.mongodb} />
              </div>
              <div style={{ gridColumn: "span 2" }}>
                <GitHubPillar data={data.github} />
              </div>

              {/* PR Agent pillar */}
              <div style={prCard}>
                <div style={cardHeader}>
                  <h2 style={cardTitle}>PR Review Agent</h2>
                  <span style={{ fontSize: 11, color: "#22c55e", fontWeight: 600 }}>LIVE</span>
                </div>
                <div style={{ color: "#64748b", fontSize: 13, lineHeight: 1.6 }}>
                  <p>Webhook: <code style={code}>POST /webhooks/github</code></p>
                  <p style={{ marginTop: 8 }}>
                    Auto-reviews PRs on open/synchronize. Add this URL to your GitHub repo webhook settings.
                  </p>
                </div>
              </div>
            </section>

            {/* Agent pipeline strip — shown while pipeline active or recently ran */}
            {(agentSnapshot?.pipeline_activity.length ?? 0) > 0 && (
              <section style={activeAgentsStrip}>
                <span style={activeAgentsLabel}>
                  {(agentSnapshot?.active_runs.length ?? 0) > 0 ? "Agents running" : "Pipeline"}
                </span>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                  {agentSnapshot!.pipeline_activity.map((r) => (
                    <div key={r.run_id} style={activeAgentPill}>
                      <span style={r.status === "running" ? activeDot : doneDot(r.status)} />
                      <span style={activeAgentName}>{r.agent_name}</span>
                      <span style={activeAgentMeta}>
                        {r.status === "running"
                          ? `${r.tool_calls} calls`
                          : r.status === "failed" ? "failed" : `${r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + "s" : "done"}`}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}

          </>
        )}
      </main>}
    </div>
  );
}

const layout: React.CSSProperties = {
  minHeight: "100vh",
  background: "#0f1117",
};

const headerBar: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "12px 24px",
  background: "#1e2130",
  borderBottom: "1px solid #2d3149",
  position: "sticky",
  top: 0,
  zIndex: 10,
};

const logo: React.CSSProperties = {
  fontSize: 20,
};

const logoText: React.CSSProperties = {
  fontSize: 16,
  fontWeight: 700,
  color: "#e2e8f0",
  letterSpacing: "0.02em",
};

const lastUpdatedText: React.CSSProperties = {
  fontSize: 12,
  color: "#4b5563",
};

const tabBtn = (active: boolean): React.CSSProperties => ({
  background: active ? "#3b4fd8" : "transparent",
  border: `1px solid ${active ? "#4f63f0" : "#2d3149"}`,
  color: active ? "#e2e8f0" : "#64748b",
  borderRadius: 6,
  padding: "4px 14px",
  fontSize: 13,
  fontWeight: active ? 600 : 400,
  cursor: "pointer",
});

const pendingDot: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", justifyContent: "center",
  marginLeft: 6, minWidth: 16, height: 16, borderRadius: 8,
  background: "#f59e0b", color: "#000",
  fontSize: 10, fontWeight: 700, padding: "0 4px",
};

const refreshBtn: React.CSSProperties = {
  background: "#2d3149",
  border: "1px solid #3d4166",
  color: "#94a3b8",
  borderRadius: 6,
  padding: "5px 12px",
  fontSize: 12,
  cursor: "pointer",
};

const queueBadge: React.CSSProperties = {
  fontSize: 11,
  color: "#64748b",
  background: "#161927",
  border: "1px solid #2d3149",
  borderRadius: 6,
  padding: "4px 10px",
};

const main: React.CSSProperties = {
  maxWidth: 1400,
  margin: "0 auto",
  padding: "24px",
  display: "flex",
  flexDirection: "column",
  gap: 24,
};

const pillarsGrid: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
  gap: 16,
};

const prCard: React.CSSProperties = {
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
};

const code: React.CSSProperties = {
  background: "#161927",
  border: "1px solid #2d3149",
  borderRadius: 4,
  padding: "1px 6px",
  fontSize: 12,
  color: "#60a5fa",
  fontFamily: "monospace",
};

const centered: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  padding: "80px 0",
  textAlign: "center",
};

const activeAgentsStrip: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 16,
  background: "#1e2130",
  border: "1px solid #2d3149",
  borderRadius: 10,
  padding: "10px 16px",
  flexWrap: "wrap",
};

const activeAgentsLabel: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  color: "#22c55e",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  whiteSpace: "nowrap",
};

const activeAgentPill: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 7,
  background: "#161927",
  border: "1px solid #2d3149",
  borderRadius: 6,
  padding: "4px 10px",
};

const activeDot: React.CSSProperties = {
  width: 7,
  height: 7,
  borderRadius: "50%",
  background: "#22c55e",
  flexShrink: 0,
  boxShadow: "0 0 0 2px rgba(34,197,94,0.25)",
};

const doneDot = (status: string): React.CSSProperties => ({
  width: 7,
  height: 7,
  borderRadius: "50%",
  background: status === "failed" ? "#ef4444" : "#1e4d2b",
  border: `1px solid ${status === "failed" ? "#ef4444" : "#22c55e"}`,
  flexShrink: 0,
});

const activeAgentName: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: "#e2e8f0",
};

const activeAgentMeta: React.CSSProperties = {
  fontSize: 11,
  color: "#4b5563",
};

const cbBanner: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
  background: "rgba(239,68,68,0.08)", borderBottom: "1px solid rgba(239,68,68,0.3)",
  padding: "10px 24px",
};

const cbBannerIcon: React.CSSProperties = { fontSize: 14 };

const cbBannerText: React.CSSProperties = {
  fontSize: 13, color: "#fca5a5", fontWeight: 600, flex: 1,
};

const cbResetBtn = (busy: boolean): React.CSSProperties => ({
  background: busy ? "transparent" : "rgba(239,68,68,0.12)",
  border: "1px solid rgba(239,68,68,0.4)",
  color: busy ? "#6b7280" : "#f87171",
  borderRadius: 6, padding: "4px 12px", fontSize: 12, fontWeight: 700,
  cursor: busy ? "not-allowed" : "pointer",
});
