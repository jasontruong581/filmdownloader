/**
 * The tier-2 gate.
 *
 * A crawl probe finds links on a page, which is not the same as finding media.
 * That distinction has to reach the operator before anything is queued, and a
 * max-items bound has to be chosen deliberately rather than defaulted to
 * something large enough to be careless.
 */
import { useState } from "react";

type Props = {
  itemCount: number;
  onCancel: () => void;
  onConfirm: (limit: number) => void;
};

const DEFAULT_LIMIT = 10;

export function CrawlConfirmDialog({ itemCount, onCancel, onConfirm }: Props) {
  const [limit, setLimit] = useState(Math.min(DEFAULT_LIMIT, itemCount));
  const [understood, setUnderstood] = useState(false);

  const valid = understood && limit >= 1 && limit <= itemCount;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4">
      <div className="w-full max-w-lg space-y-4 rounded-lg border border-amber-500/40 bg-slate-900 p-6">
        <h2 className="text-lg font-medium">Queue crawl-derived links?</h2>

        <div className="space-y-2 text-sm text-slate-300">
          <p>
            These {itemCount} results are <strong>links found on the page</strong>, not media that
            has been confirmed downloadable. Each one will be resolved on its own when its job runs,
            and some may find nothing.
          </p>
          <p className="text-slate-400">
            Every item becomes an independent job, so a failure affects only that item.
          </p>
        </div>

        <label className="block text-sm">
          <span className="text-slate-300">How many to queue</span>
          <input
            type="number"
            min={1}
            max={itemCount}
            value={limit}
            onChange={(event) => setLimit(Number(event.target.value))}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 tabular-nums"
          />
        </label>

        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5 accent-amber-500"
            checked={understood}
            onChange={(event) => setUnderstood(event.target.checked)}
          />
          <span>I understand these are page links, not confirmed media.</span>
        </label>

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-slate-700 px-4 py-2 text-sm hover:border-slate-600"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!valid}
            onClick={() => onConfirm(limit)}
            className="rounded-md bg-amber-500 px-4 py-2 text-sm font-medium text-slate-950 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Queue {Math.max(1, Math.min(limit, itemCount))}
          </button>
        </div>
      </div>
    </div>
  );
}
