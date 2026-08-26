/**
 * One queue row.
 *
 * Cancel says "Cancelling" rather than resolving instantly, because it does not:
 * a browser capture has no interruption hook, so cancellation takes effect
 * between stages. Claiming otherwise would be a lie the UI tells every time.
 */
import { useState } from "react";

import { isTerminal, type Job } from "../api/client";
import type { ActivityEntry } from "../api/useJobs";
import { ProgressBar } from "./ProgressBar";
import { bytes, duration, speed } from "./format";

type Props = {
  job: Job;
  activity?: ActivityEntry[];
  onCancel: (job: Job) => Promise<void>;
  onRetry: (job: Job) => Promise<void>;
};

/** Just the clock part of an ISO timestamp; the date is never in question here. */
function clock(at: string): string {
  const time = at.includes("T") ? at.split("T")[1] ?? "" : at;
  return time.slice(0, 8);
}

const STATUS_STYLES: Record<string, string> = {
  queued: "bg-slate-700 text-slate-200",
  resolving: "bg-sky-500/20 text-sky-300",
  downloading: "bg-sky-500/20 text-sky-300",
  postprocessing: "bg-indigo-500/20 text-indigo-300",
  completed: "bg-emerald-500/20 text-emerald-300",
  failed: "bg-rose-500/20 text-rose-300",
  cancelled: "bg-slate-700 text-slate-300",
  interrupted: "bg-amber-500/20 text-amber-300",
};

export function JobRow({ job, activity = [], onCancel, onRetry }: Props) {
  const [cancelling, setCancelling] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showLog, setShowLog] = useState(false);

  const finished = isTerminal(job.status);
  const latest = activity.length > 0 ? activity[activity.length - 1] : null;
  const retryable = job.status === "failed" || job.status === "interrupted" || job.status === "cancelled";

  return (
    <li className="space-y-2 rounded-lg border border-slate-800 p-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium" title={job.title || job.url}>
            {job.title || job.url}
          </p>
          <p className="truncate text-xs text-slate-500" title={job.url}>
            {job.url}
          </p>
        </div>
        <span
          className={`shrink-0 rounded px-2 py-0.5 text-xs ${
            STATUS_STYLES[job.status] ?? "bg-slate-700 text-slate-200"
          }`}
        >
          {cancelling && !finished ? "cancelling" : job.status}
        </span>
      </div>

      {!finished && <ProgressBar percent={job.percent} phase={job.phase} />}

      {latest && (
        <div className="space-y-1">
          <div className="flex items-baseline gap-2">
            {/* The current step, in words. A percent alone cannot say that the
                browser is waiting on a page or that a candidate was refused. */}
            <p className="min-w-0 flex-1 truncate text-xs text-slate-300" title={latest.text}>
              {latest.text}
            </p>
            {activity.length > 1 && (
              <button
                type="button"
                onClick={() => setShowLog((open) => !open)}
                className="shrink-0 text-xs text-slate-500 hover:text-slate-300"
              >
                {showLog ? "hide log" : `log (${activity.length})`}
              </button>
            )}
          </div>

          {showLog && (
            <ol className="max-h-48 overflow-y-auto rounded bg-slate-900/60 p-2 font-mono text-xs text-slate-400">
              {activity.map((entry, index) => (
                <li key={`${entry.at}-${index}`} className="break-words">
                  <span className="mr-2 text-slate-600">{clock(entry.at)}</span>
                  {entry.text}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-400">
        {job.engine && <span>engine {job.engine}</span>}
        {job.format_id && <span>format {job.format_id}</span>}
        {bytes(job.downloaded_bytes) && (
          <span className="tabular-nums">
            {bytes(job.downloaded_bytes)}
            {bytes(job.total_bytes) ? ` / ${bytes(job.total_bytes)}` : ""}
          </span>
        )}
        {speed(job.speed_bps) && <span className="tabular-nums">{speed(job.speed_bps)}</span>}
        {duration(job.eta_seconds) && <span className="tabular-nums">eta {duration(job.eta_seconds)}</span>}
      </div>

      {job.error && (
        <p className="whitespace-pre-wrap break-words rounded bg-rose-500/10 p-2 font-mono text-xs text-rose-300">
          {job.error}
        </p>
      )}

      {job.output_path && job.status === "completed" && (
        <p className="truncate font-mono text-xs text-slate-500" title={job.output_path}>
          {job.output_path}
        </p>
      )}

      <div className="flex gap-2">
        {!finished && (
          <button
            type="button"
            disabled={cancelling}
            onClick={async () => {
              setCancelling(true);
              try {
                await onCancel(job);
              } finally {
                setCancelling(false);
              }
            }}
            className="rounded border border-slate-700 px-3 py-1 text-xs hover:border-slate-600 disabled:opacity-50"
          >
            {cancelling ? "Cancelling…" : "Cancel"}
          </button>
        )}
        {retryable && (
          <button
            type="button"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await onRetry(job);
              } finally {
                setBusy(false);
              }
            }}
            className="rounded border border-slate-700 px-3 py-1 text-xs hover:border-slate-600 disabled:opacity-50"
          >
            Retry
          </button>
        )}
      </div>
    </li>
  );
}
