/**
 * One queue row.
 *
 * Cancel says "Cancelling" rather than resolving instantly, because it does not:
 * a browser capture has no interruption hook, so cancellation takes effect
 * between stages. Claiming otherwise would be a lie the UI tells every time.
 */
import { useState } from "react";

import { isTerminal, type Job } from "../api/client";
import { ProgressBar } from "./ProgressBar";
import { bytes, duration, speed } from "./format";

type Props = {
  job: Job;
  onCancel: (job: Job) => Promise<void>;
  onRetry: (job: Job) => Promise<void>;
};

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

export function JobRow({ job, onCancel, onRetry }: Props) {
  const [cancelling, setCancelling] = useState(false);
  const [busy, setBusy] = useState(false);

  const finished = isTerminal(job.status);
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
