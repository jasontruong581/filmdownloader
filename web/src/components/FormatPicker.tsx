/**
 * Format selection.
 *
 * Grouped by track because the choice is genuinely three different things: a
 * complete stream, video without audio, and audio alone. The recommended pick is
 * the best complete stream, since that is what needs no merge.
 */
import { useMemo } from "react";

import type { Format } from "../api/client";

type Props = {
  formats: Format[];
  selected: string;
  onSelect: (formatId: string) => void;
};

const GROUP_ORDER = ["both", "video-only", "audio-only", "unknown"] as const;

const GROUP_LABELS: Record<string, string> = {
  both: "Video with audio",
  "video-only": "Video only",
  "audio-only": "Audio only",
  unknown: "Other",
};

export function recommendedFormat(formats: Format[]): string {
  const complete = formats.find((format) => format.track === "both");
  return (complete ?? formats[0])?.format_id ?? "";
}

export function FormatPicker({ formats, selected, onSelect }: Props) {
  const groups = useMemo(() => {
    const map = new Map<string, Format[]>();
    for (const format of formats) {
      const list = map.get(format.track) ?? [];
      list.push(format);
      map.set(format.track, list);
    }
    return GROUP_ORDER.filter((group) => map.has(group)).map((group) => ({
      group,
      formats: map.get(group) ?? [],
    }));
  }, [formats]);

  const recommended = useMemo(() => recommendedFormat(formats), [formats]);

  if (formats.length === 0) {
    return (
      <p className="text-sm text-slate-400">
        This engine reports no selectable formats; it found direct media only.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {groups.map(({ group, formats: items }) => (
        <fieldset key={group}>
          <legend className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
            {GROUP_LABELS[group] ?? group}
          </legend>
          <div className="space-y-1">
            {items.map((format) => (
              <label
                key={format.format_id}
                className={`flex cursor-pointer items-center gap-3 rounded-md border px-3 py-2 text-sm transition ${
                  selected === format.format_id
                    ? "border-sky-500 bg-sky-500/10"
                    : "border-slate-800 hover:border-slate-700"
                }`}
              >
                <input
                  type="radio"
                  name="format"
                  className="accent-sky-500"
                  checked={selected === format.format_id}
                  onChange={() => onSelect(format.format_id)}
                />
                <span className="flex-1">{format.label}</span>
                {format.format_id === recommended && (
                  <span className="rounded bg-sky-500/20 px-2 py-0.5 text-xs text-sky-300">
                    recommended
                  </span>
                )}
                <span className="font-mono text-xs text-slate-500">{format.format_id}</span>
              </label>
            ))}
          </div>
        </fieldset>
      ))}
    </div>
  );
}
