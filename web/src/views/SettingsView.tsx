/**
 * Settings.
 *
 * Concurrency applies immediately, because the worker pool is gated by a
 * semaphore rather than its size. The bind host and port are not editable here:
 * changing where the server listens is not a runtime operation, so showing them
 * as editable would be a lie.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError, api, storeToken, storedToken, type Settings } from "../api/client";
import { Banner } from "../components/Banner";

const ENGINE_CHOICES = ["ytdlp", "site", "browser"] as const;

export function SettingsView() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [token, setToken] = useState(storedToken());

  const load = useCallback(async () => {
    try {
      const loaded = await api.settings();
      setSettings(loaded);
      setDraft(loaded);
      setError("");
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!draft) return;
    const previous = settings;
    setSettings(draft);
    setError("");
    setNotice("");
    try {
      const applied = await api.saveSettings({
        output_dir: draft.output_dir,
        concurrency: draft.concurrency,
        engines: draft.engines,
        default_format: draft.default_format,
        ffmpeg_location: draft.ffmpeg_location,
        cookies_from_browser: draft.cookies_from_browser,
      });
      setSettings(applied);
      setDraft(applied);
      setNotice("Saved.");
    } catch (exc) {
      // Optimistic update rolled back so the form does not claim a value the
      // server rejected.
      setSettings(previous);
      setDraft(previous);
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  };

  const toggleEngine = (engine: string) => {
    if (!draft) return;
    const engines = draft.engines.includes(engine)
      ? draft.engines.filter((item) => item !== engine)
      : [...draft.engines, engine];
    setDraft({ ...draft, engines });
  };

  if (!draft) {
    return error ? <Banner tone="error">{error}</Banner> : <p className="text-sm text-slate-500">Loading…</p>;
  }

  return (
    <div className="max-w-xl space-y-5">
      {error && <Banner tone="error">{error}</Banner>}
      {notice && <Banner tone="info">{notice}</Banner>}

      <label className="block text-sm">
        <span className="text-slate-300">Output directory</span>
        <input
          type="text"
          value={draft.output_dir}
          onChange={(event) => setDraft({ ...draft, output_dir: event.target.value })}
          className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm"
        />
      </label>

      <label className="block text-sm">
        <span className="text-slate-300">Concurrent downloads</span>
        <span className="ml-2 text-xs text-slate-500">applies immediately</span>
        <input
          type="number"
          min={1}
          max={16}
          value={draft.concurrency}
          onChange={(event) => setDraft({ ...draft, concurrency: Number(event.target.value) })}
          className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 tabular-nums"
        />
      </label>

      <fieldset className="text-sm">
        <legend className="text-slate-300">Engine order</legend>
        <p className="text-xs text-slate-500">Tried top to bottom; the first that finds media wins.</p>
        <div className="mt-2 space-y-1">
          {ENGINE_CHOICES.map((engine) => (
            <label key={engine} className="flex items-center gap-2">
              <input
                type="checkbox"
                className="accent-sky-500"
                checked={draft.engines.includes(engine)}
                onChange={() => toggleEngine(engine)}
              />
              <span className="font-mono text-xs">{engine}</span>
              {draft.engines.includes(engine) && (
                <span className="text-xs text-slate-500">#{draft.engines.indexOf(engine) + 1}</span>
              )}
            </label>
          ))}
        </div>
      </fieldset>

      <label className="block text-sm">
        <span className="text-slate-300">FFmpeg location</span>
        <span className="ml-2 text-xs text-slate-500">only if it is not on PATH</span>
        <input
          type="text"
          value={draft.ffmpeg_location}
          onChange={(event) => setDraft({ ...draft, ffmpeg_location: event.target.value })}
          placeholder="C:\\ffmpeg\\bin"
          className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm"
        />
      </label>

      <label className="block text-sm">
        <span className="text-slate-300">Reuse cookies from browser</span>
        <input
          type="text"
          value={draft.cookies_from_browser}
          onChange={(event) => setDraft({ ...draft, cookies_from_browser: event.target.value })}
          placeholder="empty, or chrome / firefox / edge"
          className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm"
        />
        <span className="mt-1 block text-xs text-slate-500">
          Off by default and best effort: current Chrome's cookie encryption can defeat it. It reuses
          a session you already hold and bypasses no access control.
        </span>
      </label>

      <label className="block text-sm">
        <span className="text-slate-300">Default format</span>
        <input
          type="text"
          value={draft.default_format}
          onChange={(event) => setDraft({ ...draft, default_format: event.target.value })}
          placeholder="empty means pick per download"
          className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm"
        />
      </label>

      <div className="rounded-md border border-slate-800 p-3 text-sm">
        <p className="text-slate-300">Server</p>
        <p className="mt-1 font-mono text-xs text-slate-500">
          {draft.host}:{draft.port}
        </p>
        <p className="mt-1 text-xs text-slate-500">
          Restart-only. A bind away from localhost requires a token.
        </p>
        <label className="mt-3 block">
          <span className="text-xs text-slate-400">API token, if this server requires one</span>
          <input
            type="password"
            value={token}
            onChange={(event) => {
              setToken(event.target.value);
              storeToken(event.target.value);
            }}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm"
          />
          <span className="mt-1 block text-xs text-slate-500">Kept in this browser only.</span>
        </label>
      </div>

      <button
        type="button"
        onClick={() => void save()}
        className="rounded-md bg-sky-500 px-4 py-2 text-sm font-medium text-slate-950"
      >
        Save settings
      </button>
    </div>
  );
}
