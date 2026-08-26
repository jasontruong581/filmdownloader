/**
 * The live queue.
 *
 * State comes from the SSE hook, which reconciles against the REST list on
 * connect and reconnect. The connection indicator is shown because a silent
 * reconnect that lost events would otherwise be indistinguishable from an idle
 * queue.
 */
import { useMemo } from "react";

import { api, type Job } from "../api/client";
import { Banner } from "../components/Banner";
import { JobRow } from "../components/JobRow";
import type { ActivityEntry, ConnectionState } from "../api/useJobs";

type Props = {
  active: Job[];
  finished: Job[];
  activity: Map<string, ActivityEntry[]>;
  connection: ConnectionState;
  error: string;
  onChanged: (job: Job) => void;
  onRefresh: () => void;
};

const CONNECTION_LABELS: Record<ConnectionState, string> = {
  connecting: "connecting…",
  live: "live",
  offline: "reconnecting…",
};

export function QueueView({
  active,
  finished,
  activity,
  connection,
  error,
  onChanged,
  onRefresh,
}: Props) {
  const batches = useMemo(() => {
    const map = new Map<string, Job[]>();
    for (const job of [...active, ...finished]) {
      if (!job.batch_id) continue;
      const list = map.get(job.batch_id) ?? [];
      list.push(job);
      map.set(job.batch_id, list);
    }
    return map;
  }, [active, finished]);

  const cancel = async (job: Job) => {
    try {
      onChanged(await api.cancelJob(job.id));
    } finally {
      onRefresh();
    }
  };

  const retry = async (job: Job) => {
    try {
      onChanged(await api.retryJob(job.id));
    } finally {
      onRefresh();
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between text-xs text-slate-400">
        <span>
          <span
            className={`mr-2 inline-block h-2 w-2 rounded-full ${
              connection === "live" ? "bg-emerald-500" : "bg-amber-500"
            }`}
          />
          {CONNECTION_LABELS[connection]}
        </span>
        <button type="button" onClick={onRefresh} className="hover:text-slate-200">
          Refresh
        </button>
      </div>

      {error && <Banner tone="error">{error}</Banner>}

      {batches.size > 0 && (
        <section className="space-y-1 text-xs text-slate-400">
          {[...batches.entries()].map(([id, jobs]) => {
            const done = jobs.filter((job) => job.status === "completed").length;
            const failed = jobs.filter((job) => job.status === "failed").length;
            return (
              <p key={id}>
                batch {id.slice(0, 8)}: {done}/{jobs.length} complete
                {failed > 0 ? `, ${failed} failed` : ""}
              </p>
            );
          })}
        </section>
      )}

      <section>
        <h2 className="mb-2 text-sm font-medium">Active ({active.length})</h2>
        {active.length === 0 ? (
          <p className="text-sm text-slate-500">Nothing running.</p>
        ) : (
          <ul className="space-y-2">
            {active.map((job) => (
              <JobRow
                key={job.id}
                job={job}
                activity={activity.get(job.id)}
                onCancel={cancel}
                onRetry={retry}
              />
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium">Finished ({finished.length})</h2>
        {finished.length === 0 ? (
          <p className="text-sm text-slate-500">Nothing finished yet.</p>
        ) : (
          <ul className="space-y-2">
            {finished.map((job) => (
              <JobRow
                key={job.id}
                job={job}
                activity={activity.get(job.id)}
                onCancel={cancel}
                onRetry={retry}
              />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
