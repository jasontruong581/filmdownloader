/**
 * Shell and routing.
 *
 * Routing is hash-based on purpose: it needs no server rewrite rules, and a hard
 * refresh on any tab works with nothing more than the index fallback the backend
 * already provides.
 */
import { useCallback, useEffect, useState } from "react";

import { api, type Health } from "./api/client";
import { useJobs } from "./api/useJobs";
import { HealthBanner } from "./components/HealthBanner";
import { BatchView } from "./views/BatchView";
import { DownloadView } from "./views/DownloadView";
import { LibraryView } from "./views/LibraryView";
import { QueueView } from "./views/QueueView";
import { SettingsView } from "./views/SettingsView";

const TABS = [
  { id: "download", label: "Download" },
  { id: "batch", label: "Batch" },
  { id: "queue", label: "Queue" },
  { id: "library", label: "Library" },
  { id: "settings", label: "Settings" },
] as const;

type TabId = (typeof TABS)[number]["id"];

function currentTab(): TabId {
  const hash = window.location.hash.replace(/^#\/?/, "");
  return (TABS.find((tab) => tab.id === hash)?.id ?? "download") as TabId;
}

export function App() {
  const [tab, setTab] = useState<TabId>(currentTab);
  const [health, setHealth] = useState<Health | null>(null);
  const jobs = useJobs();

  useEffect(() => {
    const onHashChange = () => setTab(currentTab());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  const go = useCallback((next: TabId) => {
    window.location.hash = `#/${next}`;
    setTab(next);
  }, []);

  const activeCount = jobs.active.length;

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <header className="mb-6 space-y-4">
        <div className="flex items-baseline justify-between">
          <h1 className="text-xl font-semibold">FilmDownloader</h1>
          <p className="text-xs text-slate-500">
            Use only with content you are authorized to download.
          </p>
        </div>

        <HealthBanner health={health} />

        <nav className="flex gap-1 border-b border-slate-800">
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => go(item.id)}
              className={`-mb-px border-b-2 px-3 py-2 text-sm transition ${
                tab === item.id
                  ? "border-sky-500 text-slate-100"
                  : "border-transparent text-slate-400 hover:text-slate-200"
              }`}
            >
              {item.label}
              {item.id === "queue" && activeCount > 0 && (
                <span className="ml-2 rounded-full bg-sky-500/20 px-2 py-0.5 text-xs text-sky-300">
                  {activeCount}
                </span>
              )}
            </button>
          ))}
        </nav>
      </header>

      <main>
        {tab === "download" && <DownloadView onQueued={() => go("queue")} />}
        {tab === "batch" && <BatchView onQueued={() => go("queue")} />}
        {tab === "queue" && (
          <QueueView
            active={jobs.active}
            finished={jobs.finished}
            connection={jobs.connection}
            error={jobs.error}
            onChanged={jobs.mutate}
            onRefresh={() => void jobs.refresh()}
          />
        )}
        {tab === "library" && <LibraryView />}
        {tab === "settings" && <SettingsView />}
      </main>
    </div>
  );
}
