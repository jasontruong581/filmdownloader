/**
 * A progress bar that can honestly say "I do not know".
 *
 * Some sources report neither a duration nor a total size, so percent is
 * genuinely unknown. Rendering that as zero would look like a stalled download,
 * so it becomes an indeterminate bar instead.
 */
type Props = {
  percent: number | null | undefined;
  phase?: string | null;
};

const PHASE_LABELS: Record<string, string> = {
  downloading: "Downloading",
  "downloading:video": "Downloading video",
  "downloading:audio": "Downloading audio",
  merging: "Merging",
  postprocessing: "Finishing",
};

export function phaseLabel(phase: string | null | undefined): string {
  if (!phase) return "";
  return PHASE_LABELS[phase] ?? phase;
}

export function ProgressBar({ percent, phase }: Props) {
  const known = typeof percent === "number" && Number.isFinite(percent);
  const clamped = known ? Math.min(100, Math.max(0, percent)) : 0;

  return (
    <div className="w-full">
      <div className="flex items-baseline justify-between text-xs text-slate-400">
        <span>{phaseLabel(phase)}</span>
        <span className="tabular-nums">{known ? `${clamped.toFixed(1)}%` : "unknown"}</span>
      </div>
      <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-slate-800">
        {known ? (
          <div
            className="h-full rounded-full bg-sky-500 transition-[width] duration-300"
            style={{ width: `${clamped}%` }}
            role="progressbar"
            aria-valuenow={clamped}
            aria-valuemin={0}
            aria-valuemax={100}
          />
        ) : (
          <div
            className="h-full w-1/3 animate-pulse rounded-full bg-sky-500/60"
            role="progressbar"
            aria-label="Progress unknown"
          />
        )}
      </div>
    </div>
  );
}
