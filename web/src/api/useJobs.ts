/**
 * Live job state.
 *
 * SSE carries live events; REST carries truth. The job list is refetched on
 * mount and on every reconnect, and that snapshot is what guarantees a missed
 * event cannot leave a stale view. It is also why the backend keeps no replay
 * buffer: one consistency mechanism, not two.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, isTerminal, storedToken, type Job } from "./client";

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 30_000;

type JobEventPayload = {
  job_id: string;
  batch_id?: string | null;
  created_at?: string;
  kind: string;
  payload: Record<string, unknown>;
};

export type ConnectionState = "connecting" | "live" | "offline";

export type ActivityEntry = { at: string; text: string };

/**
 * Kept in memory only, and capped. This is a running commentary on work in
 * flight, not a record: the backend persists no event history, so a reload
 * legitimately starts the commentary over rather than pretending to recover it.
 */
const ACTIVITY_LIMIT = 40;

/** A line worth showing a human, or null for events that only move numbers. */
function describeEvent(event: JobEventPayload): string | null {
  const payload = event.payload ?? {};
  const message = typeof payload.message === "string" ? payload.message : "";

  if (event.kind === "progress") return null;

  switch (event.kind) {
    case "info":
      return message || null;
    case "stage_started":
      return message || `Stage ${String(payload.stage ?? "")}`;
    case "candidates_found":
      return `Found ${Number(payload.count ?? 0)} candidate(s).`;
    case "candidate_attempt":
      return `Trying candidate ${Number(payload.index ?? 0)} of ${Number(payload.total ?? 0)}.`;
    case "candidate_rejected": {
      const reason = typeof payload.reason === "string" ? payload.reason : "rejected";
      const detail = typeof payload.error === "string" ? `: ${payload.error}` : "";
      return `Candidate rejected (${reason})${detail}`;
    }
    case "download_completed":
      return "Download finished.";
    case "failed":
      return typeof payload.error === "string" ? `Failed: ${payload.error}` : "Failed.";
    default:
      if (event.kind.startsWith("job_")) {
        const status = typeof payload.status === "string" ? payload.status : event.kind.slice(4);
        return `Job ${status}.`;
      }
      return message || null;
  }
}

function applyEvent(job: Job, event: JobEventPayload): Job {
  const payload = event.payload ?? {};
  const next: Job = { ...job };

  if (event.kind === "progress") {
    if ("phase" in payload) next.phase = String(payload.phase ?? next.phase);
    // An absent percent must stay null: zero would render as a stalled bar.
    next.percent = typeof payload.percent === "number" ? payload.percent : null;
    next.downloaded_bytes =
      typeof payload.downloaded_bytes === "number" ? payload.downloaded_bytes : null;
    next.total_bytes = typeof payload.total_bytes === "number" ? payload.total_bytes : null;
    next.speed_bps = typeof payload.speed_bps === "number" ? payload.speed_bps : null;
    next.eta_seconds = typeof payload.eta_seconds === "number" ? payload.eta_seconds : null;
    return next;
  }

  if (event.kind.startsWith("job_")) {
    const status = typeof payload.status === "string" ? payload.status : event.kind.slice(4);
    next.status = status;
    if (typeof payload.error === "string") next.error = payload.error;
    if (status === "completed") next.percent = 100;
    return next;
  }

  if (event.kind === "download_completed" && typeof payload.path === "string") {
    next.output_path = payload.path;
    return next;
  }

  if (event.kind === "failed" && typeof payload.error === "string") {
    next.error = payload.error;
  }
  return next;
}

export function useJobs() {
  const [jobs, setJobs] = useState<Map<string, Job>>(new Map());
  const [activity, setActivity] = useState<Map<string, ActivityEntry[]>>(new Map());
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [error, setError] = useState<string>("");
  const attemptRef = useRef(0);

  const reconcile = useCallback(async () => {
    try {
      const fetched = await api.jobs();
      setJobs(new Map(fetched.map((job) => [job.id, job])));
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => {
    let source: EventSource | null = null;
    let timer: number | undefined;
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      setConnection("connecting");
      // Reconcile on every connect, not only the first: this is what closes the
      // gap for events missed while the connection was down.
      void reconcile();

      const token = storedToken();
      const url = token
        ? `/api/jobs/events?access_token=${encodeURIComponent(token)}`
        : "/api/jobs/events";
      source = new EventSource(url);

      source.onopen = () => {
        attemptRef.current = 0;
        setConnection("live");
      };

      source.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as JobEventPayload;

          const line = describeEvent(event);
          if (line) {
            setActivity((current) => {
              const next = new Map(current);
              const kept = [...(next.get(event.job_id) ?? []), { at: event.created_at ?? "", text: line }];
              next.set(event.job_id, kept.slice(-ACTIVITY_LIMIT));
              return next;
            });
          }

          setJobs((current) => {
            const existing = current.get(event.job_id);
            if (!existing) {
              // A job this client has not seen yet; the next reconcile fills it in.
              void reconcile();
              return current;
            }
            const next = new Map(current);
            next.set(event.job_id, applyEvent(existing, event));
            return next;
          });
        } catch {
          /* a malformed frame must not break the stream */
        }
      };

      source.onerror = () => {
        source?.close();
        source = null;
        setConnection("offline");
        if (disposed) return;
        attemptRef.current += 1;
        const delay = Math.min(RECONNECT_BASE_MS * 2 ** attemptRef.current, RECONNECT_MAX_MS);
        timer = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (timer) window.clearTimeout(timer);
      source?.close();
    };
  }, [reconcile]);

  const all = useMemo(
    () =>
      [...jobs.values()].sort((a, b) => (a.created_at < b.created_at ? 1 : -1)),
    [jobs],
  );

  const active = useMemo(() => all.filter((job) => !isTerminal(job.status)), [all]);
  const finished = useMemo(() => all.filter((job) => isTerminal(job.status)), [all]);

  const mutate = useCallback((job: Job) => {
    setJobs((current) => {
      const next = new Map(current);
      next.set(job.id, job);
      return next;
    });
  }, []);

  return { all, active, finished, activity, connection, error, refresh: reconcile, mutate };
}
