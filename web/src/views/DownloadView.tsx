/**
 * Resolve one URL, pick a format, queue it.
 *
 * Queueing sends the resolution id, so the backend reuses the resolve the
 * operator already paid for instead of doing it again. For a browser-resolved
 * page that saves a second full Chrome session.
 */
import { useState } from "react";

import { ApiError, api, type Resolved } from "../api/client";
import { Banner } from "../components/Banner";
import { FormatPicker, recommendedFormat } from "../components/FormatPicker";
import { duration } from "../components/format";

type Props = {
  onQueued: () => void;
};

export function DownloadView({ onQueued }: Props) {
  const [url, setUrl] = useState("");
  const [resolving, setResolving] = useState(false);
  const [resolved, setResolved] = useState<Resolved | null>(null);
  const [formatId, setFormatId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const resolve = async () => {
    const target = url.trim();
    if (!target) return;
    setResolving(true);
    setError("");
    setNotice("");
    setResolved(null);
    try {
      const result = await api.resolve(target);
      setResolved(result);
      setFormatId(recommendedFormat(result.formats));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setResolving(false);
    }
  };

  const queue = async () => {
    if (!resolved) return;
    setError("");
    try {
      await api.queueJob({
        resolution_id: resolved.resolution_id,
        // The URL that was asked for, not where the page ended up. A redirect
        // target is the wrong thing to record and the wrong thing to retry.
        url: resolved.url || url.trim(),
        format_id: formatId || undefined,
        title: resolved.title,
      });
      setNotice("Queued. Watch it in the Queue tab.");
      onQueued();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  };

  return (
    <div className="space-y-4">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void resolve();
        }}
        className="flex gap-2"
      >
        <input
          type="url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="Page URL you are authorized to download"
          className="flex-1 rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={resolving || !url.trim()}
          className="rounded-md bg-sky-500 px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-40"
        >
          {resolving ? "Resolving…" : "Resolve"}
        </button>
      </form>

      {error && <Banner tone="error">{error}</Banner>}
      {notice && <Banner tone="info">{notice}</Banner>}

      {resolved && (
        <div className="space-y-4 rounded-lg border border-slate-800 p-4">
          <div className="flex gap-4">
            {resolved.thumbnail && (
              <img
                src={resolved.thumbnail}
                alt=""
                className="h-24 w-40 shrink-0 rounded object-cover"
                onError={(event) => {
                  event.currentTarget.style.display = "none";
                }}
              />
            )}
            <div className="min-w-0 space-y-1">
              <p className="font-medium">{resolved.title || resolved.final_url}</p>
              <p className="text-xs text-slate-400">
                engine {resolved.engine}
                {resolved.uploader ? ` · ${resolved.uploader}` : ""}
                {duration(resolved.duration) ? ` · ${duration(resolved.duration)}` : ""}
              </p>
              {resolved.item_count > 1 && (
                <p className="text-xs text-amber-300">
                  This URL enumerated {resolved.item_count} items. Use the Batch tab to queue them
                  all.
                </p>
              )}
            </div>
          </div>

          <FormatPicker formats={resolved.formats} selected={formatId} onSelect={setFormatId} />

          <button
            type="button"
            onClick={() => void queue()}
            className="rounded-md bg-emerald-500 px-4 py-2 text-sm font-medium text-slate-950"
          >
            Queue download
          </button>
        </div>
      )}
    </div>
  );
}
