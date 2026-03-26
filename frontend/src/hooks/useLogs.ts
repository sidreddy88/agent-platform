import { useState, useCallback } from "react";

export interface LogEvent {
  timestamp: string;
  stream: string;
  message: string;
  log_group: string;
}

export interface LogGroup {
  log_group: string;
  error_count: number;
  events: LogEvent[];
  error: string | null;
}

export interface LogsData {
  log_groups: LogGroup[];
  total_errors: number;
  minutes: number;
}

export function useLogs() {
  const [data, setData] = useState<LogsData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [minutes, setMinutes] = useState(60);

  const fetch_ = useCallback(async (mins: number = minutes) => {
    setLoading(true);
    try {
      const res = await fetch(`/api/logs/ecs?minutes=${mins}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Fetch failed");
    } finally {
      setLoading(false);
    }
  }, [minutes]);

  return { data, loading, error, minutes, setMinutes, fetch: fetch_ };
}
