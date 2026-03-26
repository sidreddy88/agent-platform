import React, { useEffect } from "react";
import { useLogs } from "../hooks/useLogs";

const MINUTE_OPTIONS = [15, 30, 60, 180, 360, 1440];

function formatTs(iso: string): string {
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function shortStream(stream: string): string {
  const parts = stream.split("/");
  return parts[parts.length - 1] || stream;
}

export function LogsPage() {
  const { data, loading, error, minutes, setMinutes, fetch } = useLogs();

  useEffect(() => { fetch(minutes); }, []);

  const handleMinutesChange = (m: number) => {
    setMinutes(m);
    fetch(m);
  };

  return (
    <div style={container}>
      {/* Header row */}
      <div style={headerRow}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <h2 style={title}>ECS Error Logs</h2>
          {data && (
            <span style={badge(data.total_errors > 0)}>
              {data.total_errors} error{data.total_errors !== 1 ? "s" : ""}
            </span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={label}>Last</span>
          {MINUTE_OPTIONS.map((m) => (
            <button
              key={m}
              style={timeBtn(m === minutes)}
              onClick={() => handleMinutesChange(m)}
            >
              {m >= 60 ? `${m / 60}h` : `${m}m`}
            </button>
          ))}
          <button style={refreshBtn} onClick={() => fetch(minutes)}>Refresh</button>
        </div>
      </div>

      {loading && <p style={hint}>Loading logs...</p>}
      {error && <p style={errText}>{error}</p>}

      {data && data.log_groups.map((group) => (
        <div key={group.log_group} style={groupCard}>
          <div style={groupHeader}>
            <span style={groupName}>{group.log_group}</span>
            <span style={badge(group.error_count > 0)}>
              {group.error_count} error{group.error_count !== 1 ? "s" : ""}
            </span>
          </div>

          {group.error && <p style={errText}>{group.error}</p>}

          {!group.error && group.events.length === 0 && (
            <p style={hint}>No errors in the last {minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`}.</p>
          )}

          {group.events.length > 0 && (
            <div style={table}>
              <div style={tableHeader}>
                <span style={{ width: 170 }}>Time</span>
                <span style={{ width: 180 }}>Stream</span>
                <span style={{ flex: 1 }}>Message</span>
              </div>
              {group.events.map((ev, i) => (
                <div key={i} style={tableRow(i)}>
                  <span style={{ ...cell, width: 170, color: "#64748b" }}>{formatTs(ev.timestamp)}</span>
                  <span style={{ ...cell, width: 180, color: "#60a5fa", fontFamily: "monospace", fontSize: 11 }}>
                    {shortStream(ev.stream)}
                  </span>
                  <span style={{ ...cell, flex: 1, color: "#fca5a5", fontFamily: "monospace", fontSize: 11, wordBreak: "break-all" }}>
                    {ev.message}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

const container: React.CSSProperties = {
  maxWidth: 1400,
  margin: "0 auto",
  padding: "24px",
  display: "flex",
  flexDirection: "column",
  gap: 20,
};

const headerRow: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
};

const title: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 700,
  color: "#e2e8f0",
  margin: 0,
};

const badge = (hasErrors: boolean): React.CSSProperties => ({
  fontSize: 12,
  fontWeight: 600,
  color: hasErrors ? "#f87171" : "#22c55e",
  background: hasErrors ? "#2a1a1a" : "#1a2a1a",
  border: `1px solid ${hasErrors ? "#7f1d1d" : "#14532d"}`,
  borderRadius: 6,
  padding: "2px 10px",
});

const label: React.CSSProperties = {
  fontSize: 12,
  color: "#64748b",
};

const timeBtn = (active: boolean): React.CSSProperties => ({
  background: active ? "#3b4fd8" : "#2d3149",
  border: `1px solid ${active ? "#4f63f0" : "#3d4166"}`,
  color: active ? "#e2e8f0" : "#94a3b8",
  borderRadius: 6,
  padding: "4px 10px",
  fontSize: 12,
  cursor: "pointer",
});

const refreshBtn: React.CSSProperties = {
  background: "#2d3149",
  border: "1px solid #3d4166",
  color: "#94a3b8",
  borderRadius: 6,
  padding: "4px 12px",
  fontSize: 12,
  cursor: "pointer",
  marginLeft: 4,
};

const groupCard: React.CSSProperties = {
  background: "#1e2130",
  border: "1px solid #2d3149",
  borderRadius: 12,
  padding: "16px 20px",
};

const groupHeader: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  marginBottom: 14,
};

const groupName: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 700,
  color: "#94a3b8",
  fontFamily: "monospace",
  letterSpacing: "0.04em",
};

const table: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 1,
};

const tableHeader: React.CSSProperties = {
  display: "flex",
  gap: 12,
  padding: "6px 10px",
  fontSize: 11,
  fontWeight: 700,
  color: "#475569",
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  borderBottom: "1px solid #2d3149",
  marginBottom: 4,
};

const tableRow = (i: number): React.CSSProperties => ({
  display: "flex",
  gap: 12,
  padding: "6px 10px",
  background: i % 2 === 0 ? "#161927" : "transparent",
  borderRadius: 4,
  alignItems: "flex-start",
});

const cell: React.CSSProperties = {
  fontSize: 12,
  lineHeight: 1.5,
  flexShrink: 0,
};

const hint: React.CSSProperties = {
  fontSize: 13,
  color: "#4b5563",
  fontStyle: "italic",
  textAlign: "center",
  padding: "20px 0",
  margin: 0,
};

const errText: React.CSSProperties = {
  fontSize: 13,
  color: "#f87171",
  margin: 0,
};
