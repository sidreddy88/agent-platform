import React, { useState } from "react";
import { useDashboard } from "./hooks/useDashboard";
import { useDashboardWS } from "./hooks/useWebSocket";
import { LogsPage } from "./components/LogsPage";
import { ECSPillar } from "./components/ECSPillar";
import { ECSTaskPillar } from "./components/ECSTaskPillar";
import { EC2Pillar } from "./components/EC2Pillar";
import { ALBPillar } from "./components/ALBPillar";
import { DOPillar } from "./components/DOPillar";
import { CloudfarePillar } from "./components/CloudfarePillar";
import { IncidentFeed } from "./components/IncidentFeed";

function formatTs(d: Date) {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

type Tab = "dashboard" | "logs";

export default function App() {
  const { data, loading, error, lastUpdated, refetch } = useDashboard();
  const { incidents, metrics, connected } = useDashboardWS();
  const [tab, setTab] = useState<Tab>("dashboard");

  return (
    <div style={layout}>
      {/* Header */}
      <header style={headerBar}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={logo}>⚡</span>
          <span style={logoText}>Agent Platform</span>
          <div style={{ display: "flex", gap: 4, marginLeft: 16 }}>
            <button style={tabBtn(tab === "dashboard")} onClick={() => setTab("dashboard")}>Dashboard</button>
            <button style={tabBtn(tab === "logs")} onClick={() => setTab("logs")}>Logs</button>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          {lastUpdated && (
            <span style={lastUpdatedText}>Updated {formatTs(lastUpdated)}</span>
          )}
          <button style={refreshBtn} onClick={refetch}>Refresh</button>
          {/* Queue stats */}
          {data && (
            <span style={queueBadge}>
              Queue: {data.queue.queue_size} · Enqueued: {data.queue.total_enqueued}
            </span>
          )}
        </div>
      </header>

      {/* Main content */}
      {tab === "logs" && <LogsPage />}
      <main style={main} hidden={tab !== "dashboard"}>
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

            {/* Incident Feed */}
            <section>
              <IncidentFeed
                incidents={incidents}
                metrics={metrics ?? data.incidents}
                connected={connected}
              />
            </section>
          </>
        )}
      </main>
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
