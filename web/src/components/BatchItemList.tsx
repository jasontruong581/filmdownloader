/**
 * The enumerated item list.
 *
 * This list existing *is* the gate: the batch control is only enabled once the
 * operator can see what would be queued. Copy rule enforced throughout: the
 * strongest claim available is "found N items", never that a site supports batch
 * download.
 */
import { useMemo } from "react";

import type { BatchItem } from "../api/client";

type Props = {
  items: BatchItem[];
  selected: Set<string>;
  onToggle: (url: string) => void;
  onSelectAll: (select: boolean) => void;
  truncated?: boolean;
  totalEstimate?: number | null;
};

export function BatchItemList({
  items,
  selected,
  onToggle,
  onSelectAll,
  truncated,
  totalEstimate,
}: Props) {
  const allSelected = useMemo(
    () => items.length > 0 && items.every((item) => selected.has(item.url)),
    [items, selected],
  );

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-sm">
        <label className="flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            className="accent-sky-500"
            checked={allSelected}
            onChange={(event) => onSelectAll(event.target.checked)}
          />
          <span>
            Found {items.length} item{items.length === 1 ? "" : "s"}
            {typeof totalEstimate === "number" && totalEstimate > items.length
              ? ` of about ${totalEstimate}`
              : ""}
          </span>
        </label>
        <span className="text-slate-400">{selected.size} selected</span>
      </div>

      {truncated && (
        <p className="text-xs text-amber-300">
          The list was capped, so more items exist than are shown here.
        </p>
      )}

      <ul className="max-h-80 divide-y divide-slate-800 overflow-y-auto rounded-md border border-slate-800">
        {items.map((item, index) => (
          <li key={item.url} className="flex items-center gap-3 px-3 py-2 text-sm">
            <input
              type="checkbox"
              className="accent-sky-500"
              checked={selected.has(item.url)}
              onChange={() => onToggle(item.url)}
            />
            <span className="w-8 shrink-0 text-right font-mono text-xs text-slate-500">
              {index + 1}
            </span>
            <span className="min-w-0 flex-1 truncate" title={item.url}>
              {item.title || item.url}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
