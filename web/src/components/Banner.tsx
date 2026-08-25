/** A persistent notice. Used for missing tools and low disk space. */
type Props = {
  tone: "warn" | "error" | "info";
  children: React.ReactNode;
};

const TONES: Record<Props["tone"], string> = {
  warn: "border-amber-500/40 bg-amber-500/10 text-amber-200",
  error: "border-rose-500/40 bg-rose-500/10 text-rose-200",
  info: "border-sky-500/40 bg-sky-500/10 text-sky-200",
};

export function Banner({ tone, children }: Props) {
  return (
    <div className={`rounded-lg border px-4 py-3 text-sm ${TONES[tone]}`}>{children}</div>
  );
}
