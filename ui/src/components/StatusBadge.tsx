import { cn } from "@/lib/utils";
import type { ScreenStatus } from "@/lib/api";

const styles: Record<ScreenStatus, string> = {
  included: "bg-emerald-900/40 text-emerald-300 border-emerald-800",
  maybe: "bg-yellow-900/40 text-yellow-300 border-yellow-800",
  excluded: "bg-red-900/40 text-red-300 border-red-800",
  candidate: "bg-zinc-800 text-zinc-400 border-zinc-700",
};

export function StatusBadge({ status, className }: { status: ScreenStatus; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        styles[status],
        className,
      )}
    >
      {status}
    </span>
  );
}

export const STATUS_HEX: Record<ScreenStatus, string> = {
  included: "#10b981",
  maybe: "#eab308",
  excluded: "#ef4444",
  candidate: "#71717a",
};
