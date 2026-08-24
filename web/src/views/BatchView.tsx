/**
 * Batch download, gated by what the probe could actually enumerate.
 *
 * Three tiers, and the difference between them is the whole point:
 *
 * - Pasting several URLs is always allowed. Nothing is inferred about any site.
 * - One URL that enumerates (a playlist, a collection) enables the control once
 *   its items are listed, because the operator can see what would be queued.
 * - One URL whose host merely has crawl rules is `possible`, not `proven`: those
 *   are page links, not confirmed media, so it takes an explicit confirmation.
 *
 * A probe proves enumeration, not downloadability. No string here says a site
 * supports batch download; the strongest claim is "found N items".
 */
import { useMemo, useState } from "react";

import { ApiError, api, type BatchItem, type BatchProbe } from "../api/client";
import { Banner } from "../components/Banner";
import { BatchItemList } from "../components/BatchItemList";
import { CrawlConfirmDialog } from "../components/CrawlConfirmDialog";

type Props = {
  onQueued: () => void;
};

type ProbeState = "idle" | "probing" | "done";

export function BatchView({ onQueued }: Props) {
  const [pasted, setPasted] = useState("");
  const [probeUrl, setProbeUrl] = useState("");
  const [probeState, setProbeState] = useState<ProbeState>("idle");
  const [probe, setProbe] = useState<BatchProbe | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [verified, setVerified] = useState<string>("");

  const pastedUrls = useMemo(
    () =>
      [...new Set(pasted.split(/\r?\n/).map((line) => line.trim()).filter(Boolean))],
    [pasted],
  );

  const queuePasted = async () => {
    setError("");
    setNotice("");
    try {
      const result = await api.queueBatch({
        items: pastedUrls.map((url) => ({ url, title: "" })),
      });
      setNotice(
        `Queued ${result.jobs.length} job${result.jobs.length === 1 ? "" : "s"}` +
          (result.skipped.length ? `, skipped ${result.skipped.length} already active` : ""),
      );
      setPasted("");
      onQueued();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  };

  const runProbe = async () => {
    const target = probeUrl.trim();
    if (!target) return;
    setProbeState("probing");
    setError("");
    setNotice("");
    setVerified("");
    setProbe(null);
    try {
      const result = await api.probeBatch(target);
      setProbe(result);
      setSelected(new Set(result.items.map((item) => item.url)));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setProbeState("done");
    }
  };

  const selectedItems = (limit?: number): BatchItem[] => {
    const items = (probe?.items ?? []).filter((item) => selected.has(item.url));
    return typeof limit === "number" ? items.slice(0, limit) : items;
  };

  const queueProbed = async (limit?: number) => {
    if (!probe) return;
    setError("");
    setNotice("");
    try {
      const result = await api.queueBatch({
        items: selectedItems(limit),
        source_url: probeUrl.trim(),
        capability: probe.capability,
        confidence: probe.confidence,
      });
      setNotice(
        `Queued ${result.jobs.length} job${result.jobs.length === 1 ? "" : "s"}` +
          (result.skipped.length ? `, skipped ${result.skipped.length} already active` : ""),
      );
      onQueued();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setConfirming(false);
    }
  };

  const verifySample = async () => {
    if (!probe) return;
    setVerifying(true);
    try {
      const result = await api.verifyBatch(selectedItems(2), 2);
      setVerified(
        `${result.verified} of ${result.attempted} sampled item${result.attempted === 1 ? "" : "s"} resolve`,
      );
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setVerifying(false);
    }
  };

  const toggle = (url: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(url)) next.delete(url);
      else next.add(url);
      return next;
    });
  };

  const canQueueProbed = Boolean(probe?.batchable) && selected.size > 0;

  return (
    <div className="space-y-8">
      {error && <Banner tone="error">{error}</Banner>}
      {notice && <Banner tone="info">{notice}</Banner>}

      <section className="space-y-3">
        <div>
          <h2 className="text-sm font-medium">Paste several URLs</h2>
          <p className="text-xs text-slate-400">
            One per line. Each is resolved on its own, so this needs no capability check.
          </p>
        </div>
        <textarea
          value={pasted}
          onChange={(event) => setPasted(event.target.value)}
          rows={5}
          placeholder={"https://example.test/one\nhttps://example.test/two"}
          className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm"
        />
        <button
          type="button"
          disabled={pastedUrls.length === 0}
          onClick={() => void queuePasted()}
          className="rounded-md bg-emerald-500 px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-40"
        >
          Queue {pastedUrls.length || ""} URL{pastedUrls.length === 1 ? "" : "s"}
        </button>
      </section>

      <hr className="border-slate-800" />

      <section className="space-y-3">
        <div>
          <h2 className="text-sm font-medium">Or check one URL for multiple items</h2>
          <p className="text-xs text-slate-400">
            A playlist, a collection, or a listing page. The batch button unlocks only once items
            have actually been found and listed below.
          </p>
        </div>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            void runProbe();
          }}
          className="flex gap-2"
        >
          <input
            type="url"
            value={probeUrl}
            onChange={(event) => setProbeUrl(event.target.value)}
            placeholder="Playlist, collection, or listing URL"
            className="flex-1 rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={probeState === "probing" || !probeUrl.trim()}
            className="rounded-md bg-sky-500 px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-40"
          >
            {probeState === "probing" ? "Checking…" : "Check for batch"}
          </button>
        </form>

        {probe && !probe.batchable && (
          <Banner tone="warn">
            <p className="font-medium">Nothing to batch here.</p>
            <p className="mt-1 text-xs opacity-90">{probe.reason}</p>
          </Banner>
        )}

        {probe?.batchable && probe.confidence === "possible" && (
          <Banner tone="warn">
            These are links found on the page, not confirmed media. Queueing them takes an explicit
            confirmation.
          </Banner>
        )}

        {probe?.batchable && (
          <div className="space-y-3">
            <BatchItemList
              items={probe.items}
              selected={selected}
              onToggle={toggle}
              onSelectAll={(select) =>
                setSelected(select ? new Set(probe.items.map((item) => item.url)) : new Set())
              }
              truncated={probe.truncated}
              totalEstimate={probe.total_estimate}
            />

            {verified && <p className="text-xs text-emerald-300">{verified}</p>}

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={!canQueueProbed}
                onClick={() =>
                  probe.confidence === "possible" ? setConfirming(true) : void queueProbed()
                }
                className="rounded-md bg-emerald-500 px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-40"
              >
                Queue {selected.size} selected
              </button>
              <button
                type="button"
                disabled={verifying || selected.size === 0}
                onClick={() => void verifySample()}
                className="rounded-md border border-slate-700 px-4 py-2 text-sm disabled:opacity-40"
                title="Fully resolve the first two items before committing to the rest"
              >
                {verifying ? "Verifying…" : "Verify first 2"}
              </button>
            </div>
          </div>
        )}
      </section>

      {confirming && probe && (
        <CrawlConfirmDialog
          itemCount={selected.size}
          onCancel={() => setConfirming(false)}
          onConfirm={(limit) => void queueProbed(limit)}
        />
      )}
    </div>
  );
}
