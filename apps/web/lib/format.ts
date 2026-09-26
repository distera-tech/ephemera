import type { JobStatus } from "./api";

export function clock(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return [h, m, s % 60].map((n) => String(n).padStart(2, "0")).join(":");
}

export function secs(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return clock(seconds);
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function time(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export type Tone = "ok" | "warn" | "bad" | "neutral" | "info";

export function statusTone(status: JobStatus): Tone {
  switch (status) {
    case "COMPLETED":
      return "ok";
    case "FAILED":
    case "CLEANUP_FAILED":
      return "bad";
    case "QUEUED":
    case "CANCELLED":
      return "neutral";
    case "INFERENCING":
    case "TRANSFERRING":
    case "READY":
      return "info";
    default:
      return "warn";
  }
}

export const toneClasses: Record<Tone, string> = {
  ok: "text-ok bg-ok-bg border-ok/30",
  warn: "text-warn bg-warn-bg border-warn/30",
  bad: "text-bad bg-bad-bg border-bad/30",
  info: "text-info bg-info/10 border-info/30",
  neutral: "text-muted bg-panel-2 border-line-strong",
};

export function label(status: string): string {
  return status.replaceAll("_", " ");
}
