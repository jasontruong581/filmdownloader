/** Finished files, played through the ranged endpoint so seeking works. */
import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type LibraryItem } from "../api/client";
import { Banner } from "../components/Banner";
import { bytes, timestamp } from "../components/format";

export function LibraryView() {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [error, setError] = useState("");
  const [playing, setPlaying] = useState<string>("");

  const load = useCallback(async () => {
    try {
      setItems(await api.library());
      setError("");
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium">{items.length} file{items.length === 1 ? "" : "s"}</h2>
        <button type="button" onClick={() => void load()} className="text-xs text-slate-400 hover:text-slate-200">
          Refresh
        </button>
      </div>

      {error && <Banner tone="error">{error}</Banner>}

      {playing && (
        <div className="space-y-2 rounded-lg border border-slate-800 p-3">
          <video src={api.fileUrl(playing)} controls autoPlay className="w-full rounded" />
          <button type="button" onClick={() => setPlaying("")} className="text-xs text-slate-400 hover:text-slate-200">
            Close player
          </button>
        </div>
      )}

      {items.length === 0 ? (
        <p className="text-sm text-slate-500">Nothing downloaded yet.</p>
      ) : (
        <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
          {items.map((item) => (
            <li key={item.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <span className="min-w-0 flex-1 truncate" title={item.id}>
                {item.name}
              </span>
              <span className="shrink-0 tabular-nums text-xs text-slate-500">{bytes(item.size_bytes)}</span>
              <span className="hidden shrink-0 text-xs text-slate-500 sm:inline">
                {timestamp(item.modified_at)}
              </span>
              <button
                type="button"
                onClick={() => setPlaying(item.id)}
                className="shrink-0 rounded border border-slate-700 px-2 py-1 text-xs hover:border-slate-600"
              >
                Play
              </button>
              <a
                href={api.fileUrl(item.id)}
                download={item.name}
                className="shrink-0 rounded border border-slate-700 px-2 py-1 text-xs hover:border-slate-600"
              >
                Download
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
