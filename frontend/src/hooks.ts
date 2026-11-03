import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { EventItem, LlmStatus, PhaseKey, Run } from "./types";

/** Hash routing: #/ (home), #/run/<id>, #/run/<id>/<phase | results>. */
export interface Route {
  name: "home" | "run";
  runId?: string;
  view?: PhaseKey | "results";
}

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "run" && parts[1]) {
    const v = parts[2];
    const view = v === "results" ? "results" : v && "ABCDT".includes(v) && v.length === 1 ? (v as PhaseKey) : undefined;
    return { name: "run", runId: parts[1], view };
  }
  return { name: "home" };
}

export function navigate(to: string): void {
  window.location.hash = to;
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

/** Fetches a run and keeps it fresh: fast while a job runs, slow otherwise. */
export function useRun(runId: string) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const alive = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const r = await api.getRun(runId);
      if (alive.current) {
        setRun(r);
        setError(null);
      }
    } catch (e) {
      if (alive.current && e instanceof ApiError) setError(e);
    }
  }, [runId]);

  useEffect(() => {
    alive.current = true;
    setRun(null);
    void refresh();
    return () => {
      alive.current = false;
    };
  }, [refresh]);

  const busy = run?.job != null || run?.phases.some((p) => p.status === "running");
  useEffect(() => {
    if (error?.code === "run_not_found") return;
    const id = window.setInterval(() => void refresh(), busy ? 1200 : 6000);
    return () => window.clearInterval(id);
  }, [busy, refresh, error?.code]);

  return { run, error, refresh, setRun };
}

/** The run's live events, accumulated. `forPhase` returns the latest job's events of one phase. */
export function useEvents(runId: string, active: boolean) {
  const [events, setEvents] = useState<EventItem[]>([]);
  const cursor = useRef(0);

  useEffect(() => {
    cursor.current = 0;
    setEvents([]);
  }, [runId]);

  useEffect(() => {
    let stop = false;
    const pull = async () => {
      try {
        const r = await api.events(runId, cursor.current);
        if (stop || r.events.length === 0) return;
        cursor.current = r.cursor;
        setEvents((prev) => [...prev, ...r.events].slice(-4000));
      } catch {
        /* the run poll shows connection problems */
      }
    };
    void pull();
    if (!active) return;
    const id = window.setInterval(pull, 1000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [runId, active]);

  const forPhase = useCallback(
    (phase: PhaseKey): EventItem[] => {
      const mine = events.filter((e) => e.phase === phase);
      const last = mine.length ? mine[mine.length - 1].job : null;
      return mine.filter((e) => e.job === last);
    },
    [events],
  );
  return { events, forPhase };
}

export function useLlmStatus(intervalMs = 15000) {
  const [status, setStatus] = useState<LlmStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const check = useCallback(async (fresh = false) => {
    try {
      setStatus(await api.llmStatus(fresh));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);
  useEffect(() => {
    void check();
    const id = window.setInterval(() => void check(), intervalMs);
    return () => window.clearInterval(id);
  }, [check, intervalMs]);
  return { status, failed, check };
}

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
