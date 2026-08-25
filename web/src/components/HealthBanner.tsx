/**
 * Warns before the operator queues anything.
 *
 * FFmpeg is commonly absent from PATH on Windows, and finding that out only when
 * a download fails is a worse experience than being told up front.
 */
import { Banner } from "./Banner";
import { bytes } from "./format";
import type { Health } from "../api/client";

const LOW_DISK_BYTES = 2 * 1024 * 1024 * 1024;

export function HealthBanner({ health }: { health: Health | null }) {
  if (!health) return null;

  const missing = health.tools.filter((tool) => tool.required && !tool.available);
  const lowDisk = typeof health.free_bytes === "number" && health.free_bytes < LOW_DISK_BYTES;

  if (missing.length === 0 && !lowDisk) return null;

  return (
    <div className="space-y-2">
      {missing.length > 0 && (
        <Banner tone="error">
          <p className="font-medium">
            Missing required {missing.length === 1 ? "tool" : "tools"}:{" "}
            {missing.map((tool) => tool.name).join(", ")}
          </p>
          <p className="mt-1 text-xs opacity-90">
            Downloads will fail until it is installed. If it is installed somewhere else, set
            FILMDOWNLOADER_FFMPEG to its directory or executable, or set the FFmpeg location in
            Settings.
          </p>
        </Banner>
      )}
      {lowDisk && (
        <Banner tone="warn">
          Only {bytes(health.free_bytes)} free in {health.output_dir}.
        </Banner>
      )}
    </div>
  );
}
