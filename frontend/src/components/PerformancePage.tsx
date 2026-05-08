import React, { useEffect, useMemo, useRef, useState } from "react";

interface SlowQuery {
  namespace: string;
  query_shape: string;
  exec_count: number;
  avg_ms: number;
  total_ms: number;
  latest_at: string;
}

interface SuggestedIndex {
  namespace: string;
  index_def: string;
  impact: string[];
  weight: number;
}

interface Payload {
  slow_queries: SlowQuery[];
  suggested_indexes: SuggestedIndex[];
  fetched_at: string;
  atlas_project_id: string;
  configured: boolean;
  hours: number;
}

type SortKey = "namespace" | "exec_count" | "avg_ms" | "total_ms";

const POLL_MS = 60_000;

export function PerformancePage() {
  const [data, setData] = useState<Payload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("total_ms");
  const [sortAsc, setSortAsc] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [hours, setHours] = useState(24);
  const fetchAbort = useRef<AbortController | null>(null);

  const fetchData = async (refresh: boolean) => {
    fetchAbort.current?.abort();
    const controller = new AbortController();
    fetchAbort.current = controller;
    try {
      setError(null);
      const url = `/performance/heaviest?hours=${hours}${refresh ? "&refresh=true" : ""}`;
      const resp = await fetch(url, { signal: controller.signal });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload: Payload = await resp.json();
      setData(payload);
      setLoading(false);
    } catch (e: any) {
      if (e.name === "AbortError") return;
      setError(e.message ?? String(e));
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData(false);
    const id = setInterval(() => fetchData(false), POLL_MS);
    return () => {
      clearInterval(id);
      fetchAbort.current?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hours]);

  // Map suggested indexes by namespace so we can show inline alongside slow queries.
  const indexByNs = useMemo(() => {
    const map: Record<string, SuggestedIndex[]> = {};
    if (!data) return map;
    for (const ix of data.suggested_indexes) {
      (map[ix.namespace] = map[ix.namespace] || []).push(ix);
    }
    return map;
  }, [data]);

  const sorted = useMemo(() => {
    if (!data) return [];
    const rows = [...data.slow_queries];
    rows.sort((a, b) => {
      let av: any = a[sortKey];
      let bv: any = b[sortKey];
      if (typeof av === "string") {
        return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      return sortAsc ? av - bv : bv - av;
    });
    return rows;
  }, [data, sortKey, sortAsc]);

  function toggleSort(k: SortKey) {
    if (k === sortKey) setSortAsc(!sortAsc);
    else { setSortKey(k); setSortAsc(false); }
  }

  function toggleExpand(i: number) {
    setExpanded((cur) => {
      const next = new Set(cur);
      if (next.has(i)) next.delete(i); else next.add(i);
      return next;
    });
  }

  if (loading && !data) return <div style={loadingStyle}>Loading Atlas Performance Advisor…</div>;
  if (error && !data) return <div style={errorStyle}>Error: {error}</div>;
  if (!data) return null;

  const fetched = new Date(data.fetched_at);

  return (
    <div style={page}>
      <div style={pageHeader}>
        <div>
          <h2 style={titleStyle}>Heaviest API calls</h2>
          <p style={subtitleStyle}>
            From MongoDB Atlas Performance Advisor — slow queries + suggested indexes
            {" · "}
            project <code style={projCode}>{data.atlas_project_id || "(unset)"}</code>
            {" · "}
            updated {fetched.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
          </p>
        </div>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <select
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            style={selectStyle}
          >
            <option value={1}>Last 1h</option>
            <option value={6}>Last 6h</option>
            <option value={24}>Last 24h</option>
            <option value={72}>Last 3d</option>
            <option value={168}>Last 7d</option>
          </select>
          <button style={refreshBtn} onClick={() => fetchData(true)}>Refresh</button>
        </div>
      </div>

      {!data.configured && (
        <div style={warning}>
          ⚠ Atlas API credentials are not configured. Set <code>ATLAS_PUBLIC_KEY</code>,
          <code>ATLAS_PRIVATE_KEY</code>, and <code>ATLAS_PROJECT_ID</code> in <code>.env</code>.
        </div>
      )}

      {data.configured && data.slow_queries.length === 0 && (
        <div style={emptyState}>
          No slow queries reported by Atlas in the last {data.hours}h.
        </div>
      )}

      {data.slow_queries.length > 0 && (
        <table style={tableStyle}>
          <thead>
            <tr style={trHead}>
              <th style={thStyle} onClick={() => toggleSort("namespace")}>
                Namespace {sortKey === "namespace" ? (sortAsc ? "▲" : "▼") : ""}
              </th>
              <th style={thStyle}>Query shape</th>
              <th style={thStyleNum} onClick={() => toggleSort("exec_count")}>
                Exec count {sortKey === "exec_count" ? (sortAsc ? "▲" : "▼") : ""}
              </th>
              <th style={thStyleNum} onClick={() => toggleSort("avg_ms")}>
                Avg ms {sortKey === "avg_ms" ? (sortAsc ? "▲" : "▼") : ""}
              </th>
              <th style={thStyleNum} onClick={() => toggleSort("total_ms")}>
                Total ms {sortKey === "total_ms" ? (sortAsc ? "▲" : "▼") : ""}
              </th>
              <th style={thStyle}>Suggested index</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((q, i) => {
              const isExpanded = expanded.has(i);
              const idxs = indexByNs[q.namespace] || [];
              return (
                <React.Fragment key={`${q.namespace}|${i}`}>
                  <tr style={trBody} onClick={() => toggleExpand(i)}>
                    <td style={tdMono}>{q.namespace}</td>
                    <td style={tdShape}>{truncate(q.query_shape, 80)}</td>
                    <td style={tdNum}>{q.exec_count.toLocaleString()}</td>
                    <td style={tdNum}>{q.avg_ms.toFixed(1)}</td>
                    <td style={tdNumBold}>{q.total_ms.toLocaleString()}</td>
                    <td style={tdMono}>{idxs[0] ? idxs[0].index_def : "—"}</td>
                  </tr>
                  {isExpanded && (
                    <tr style={trDetail}>
                      <td colSpan={6} style={tdDetail}>
                        <div style={detailSection}>
                          <div style={detailLabel}>Full query shape</div>
                          <pre style={pre}>{q.query_shape || "(empty)"}</pre>
                        </div>
                        <div style={detailSection}>
                          <div style={detailLabel}>Last seen</div>
                          <div>{q.latest_at || "—"}</div>
                        </div>
                        {idxs.length > 0 && (
                          <div style={detailSection}>
                            <div style={detailLabel}>
                              Suggested indexes for this namespace ({idxs.length})
                            </div>
                            {idxs.map((ix, k) => (
                              <div key={k} style={ixRow}>
                                <code style={ixCode}>{ix.index_def}</code>
                                <span style={ixWeight}>weight {ix.weight.toFixed(1)}</span>
                                {ix.impact.length > 0 && (
                                  <span style={ixImpact}>
                                    helps {ix.impact.length} query shape{ix.impact.length === 1 ? "" : "s"}
                                  </span>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      )}

      {data.suggested_indexes.length > 0 && (
        <div style={section}>
          <h3 style={sectionTitle}>All suggested indexes ({data.suggested_indexes.length})</h3>
          <table style={tableStyle}>
            <thead>
              <tr style={trHead}>
                <th style={thStyle}>Namespace</th>
                <th style={thStyle}>Index</th>
                <th style={thStyleNum}>Weight</th>
                <th style={thStyleNum}>Query shapes helped</th>
              </tr>
            </thead>
            <tbody>
              {[...data.suggested_indexes]
                .sort((a, b) => b.weight - a.weight)
                .map((ix, i) => (
                  <tr key={i} style={trBody}>
                    <td style={tdMono}>{ix.namespace}</td>
                    <td style={tdMono}>{ix.index_def}</td>
                    <td style={tdNumBold}>{ix.weight.toFixed(1)}</td>
                    <td style={tdNum}>{ix.impact.length}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function truncate(s: string, n: number): string {
  if (!s) return "(empty)";
  return s.length > n ? s.slice(0, n) + "…" : s;
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const page: React.CSSProperties = { padding: 20, color: "#e5e7eb" };
const pageHeader: React.CSSProperties = {
  display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16,
};
const titleStyle: React.CSSProperties = { fontSize: 22, fontWeight: 700, margin: 0, color: "#f1f5f9" };
const subtitleStyle: React.CSSProperties = { fontSize: 13, color: "#94a3b8", marginTop: 4 };
const projCode: React.CSSProperties = { background: "#1e293b", padding: "1px 6px", borderRadius: 3, fontSize: 12 };

const selectStyle: React.CSSProperties = {
  background: "#1e293b", color: "#e5e7eb", border: "1px solid #334155",
  borderRadius: 4, padding: "6px 8px", fontSize: 13,
};
const refreshBtn: React.CSSProperties = {
  background: "#3b4fd8", color: "#fff", border: "none", borderRadius: 4,
  padding: "6px 12px", cursor: "pointer", fontSize: 13,
};

const warning: React.CSSProperties = {
  background: "rgba(245,158,11,0.1)", border: "1px solid rgba(245,158,11,0.3)",
  borderRadius: 6, padding: 12, fontSize: 13, color: "#fbbf24", marginBottom: 16,
};
const emptyState: React.CSSProperties = {
  background: "#1e293b", border: "1px solid #334155", borderRadius: 6,
  padding: 24, textAlign: "center", color: "#94a3b8", fontSize: 14,
};

const tableStyle: React.CSSProperties = {
  width: "100%", borderCollapse: "collapse",
  background: "#0f172a", border: "1px solid #1e293b", borderRadius: 6,
  overflow: "hidden", marginTop: 8,
};
const trHead: React.CSSProperties = { background: "#1e293b" };
const trBody: React.CSSProperties = { borderTop: "1px solid #1e293b", cursor: "pointer" };
const trDetail: React.CSSProperties = { background: "#0a1424" };

const thStyle: React.CSSProperties = {
  textAlign: "left", padding: "10px 12px", fontSize: 12, fontWeight: 600,
  color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.04em",
  cursor: "pointer", userSelect: "none",
};
const thStyleNum: React.CSSProperties = { ...thStyle, textAlign: "right" };
const tdMono: React.CSSProperties = {
  padding: "10px 12px", fontFamily: "monospace", fontSize: 13, color: "#e5e7eb",
};
const tdShape: React.CSSProperties = {
  padding: "10px 12px", fontFamily: "monospace", fontSize: 12, color: "#cbd5e1",
  maxWidth: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
};
const tdNum: React.CSSProperties = {
  padding: "10px 12px", textAlign: "right", fontVariantNumeric: "tabular-nums",
  fontSize: 13, color: "#cbd5e1",
};
const tdNumBold: React.CSSProperties = { ...tdNum, fontWeight: 600, color: "#f1f5f9" };

const tdDetail: React.CSSProperties = {
  padding: "12px 16px", borderBottom: "1px solid #1e293b",
};
const detailSection: React.CSSProperties = { marginBottom: 12 };
const detailLabel: React.CSSProperties = {
  fontSize: 11, color: "#64748b", textTransform: "uppercase",
  letterSpacing: "0.04em", marginBottom: 4,
};
const pre: React.CSSProperties = {
  background: "#0a1424", padding: 8, borderRadius: 4, fontSize: 12,
  color: "#cbd5e1", whiteSpace: "pre-wrap", wordBreak: "break-all", margin: 0,
};
const ixRow: React.CSSProperties = {
  display: "flex", gap: 12, alignItems: "center", padding: "4px 0", fontSize: 13,
};
const ixCode: React.CSSProperties = {
  background: "#1e293b", padding: "2px 6px", borderRadius: 3,
  fontFamily: "monospace", color: "#a5f3fc",
};
const ixWeight: React.CSSProperties = { color: "#94a3b8", fontSize: 12 };
const ixImpact: React.CSSProperties = { color: "#94a3b8", fontSize: 12 };

const section: React.CSSProperties = { marginTop: 28 };
const sectionTitle: React.CSSProperties = {
  fontSize: 16, fontWeight: 600, color: "#f1f5f9", marginBottom: 8,
};

const loadingStyle: React.CSSProperties = { padding: 32, color: "#94a3b8", textAlign: "center" };
const errorStyle: React.CSSProperties = { padding: 32, color: "#f87171", textAlign: "center" };
