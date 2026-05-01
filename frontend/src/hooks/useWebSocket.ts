import { useState, useEffect, useRef, useCallback } from "react";
import type { AgentSnapshot, Incident, IncidentMetrics, PendingEvent, ScanLogEntry, WSMessage } from "../types";

const PING_MS = 25_000;
const RECONNECT_MS = 3_000;

export function useDashboardWS() {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [metrics, setMetrics] = useState<IncidentMetrics | null>(null);
  const [agentSnapshot, setAgentSnapshot] = useState<AgentSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [scanLog, setScanLog] = useState<ScanLogEntry[]>([]);
  const [pendingEvents, setPendingEvents] = useState<PendingEvent[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const connect = useCallback(() => {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/dashboard`);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      pingRef.current = setInterval(() => ws.send("ping"), PING_MS);
    };

    ws.onmessage = (e) => {
      const msg: WSMessage = JSON.parse(e.data);
      if (msg.type === "init") {
        setIncidents(msg.incidents);
        setMetrics(msg.metrics);
        if (msg.agents) setAgentSnapshot(msg.agents);
        if (msg.pending_events) setPendingEvents(msg.pending_events);
      } else if (msg.type === "incident_update") {
        setIncidents((prev) => {
          const idx = prev.findIndex((i) => i.id === msg.incident.id);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = msg.incident;
            return next;
          }
          return [msg.incident, ...prev];
        });
      } else if (msg.type === "agent_status") {
        setAgentSnapshot(msg.agents);
      } else if (msg.type === "scan_progress") {
        setScanLog((prev) => {
          const entry: ScanLogEntry = { ts: msg.ts, level: msg.level, message: msg.message };
          if (msg.message.startsWith("Scan started")) return [entry];
          return [...prev, entry];
        });
      } else if (msg.type === "pending_event_added") {
        setPendingEvents((prev) => [...prev, msg.event]);
      } else if (msg.type === "pending_event_removed") {
        setPendingEvents((prev) => prev.filter((e) => e.id !== msg.id));
      } else if (msg.type === "pending_events_cleared") {
        setPendingEvents([]);
      } else if (msg.type === "incidents_cleared") {
        setIncidents(msg.incidents);
      }
    };

    ws.onclose = () => {
      setConnected(false);
      if (pingRef.current) clearInterval(pingRef.current);
      setTimeout(connect, RECONNECT_MS);
    };

    ws.onerror = () => ws.close();
  }, []);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
      if (pingRef.current) clearInterval(pingRef.current);
    };
  }, [connect]);

  return { incidents, metrics, agentSnapshot, connected, scanLog, pendingEvents };
}
