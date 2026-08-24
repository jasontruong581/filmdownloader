/** Small formatters shared across views. */

export function bytes(value: number | null | undefined): string {
  if (typeof value !== "number" || value <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit <= 1 ? 0 : 1)} ${units[unit]}`;
}

export function speed(bps: number | null | undefined): string {
  const rendered = bytes(bps);
  return rendered ? `${rendered}/s` : "";
}

export function duration(seconds: number | null | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}

export function timestamp(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString();
}
