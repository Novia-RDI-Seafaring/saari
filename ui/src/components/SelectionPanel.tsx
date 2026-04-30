import type { PaperCard } from "@/lib/api";
import { PaperRow } from "./PaperRow";
import { X } from "lucide-react";

export function SelectionPanel({
  papers,
  paperId,
  onSelect,
  onClose,
}: {
  papers: PaperCard[];
  paperId?: string | null;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  // Sort: included first, then maybe, then candidate, then excluded; within each, by citations desc.
  const order: Record<string, number> = { included: 0, maybe: 1, candidate: 2, excluded: 3 };
  const sorted = [...papers].sort((a, b) => {
    const da = (order[a.status] ?? 9) - (order[b.status] ?? 9);
    if (da !== 0) return da;
    return (b.cited_by_count ?? 0) - (a.cited_by_count ?? 0);
  });

  return (
    <div className="flex h-full flex-col bg-zinc-900/40 border-l border-zinc-800 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 border-b border-zinc-800 shrink-0">
        <span className="text-xs uppercase tracking-wider text-zinc-500">
          selection ({papers.length})
        </span>
        <button
          onClick={onClose}
          className="text-zinc-500 hover:text-zinc-200"
          aria-label="clear selection"
          title="clear selection (Esc)"
        >
          <X className="size-4" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
        {sorted.length === 0 && (
          <div className="text-xs text-zinc-500 p-3 leading-relaxed">
            Selected papers will appear here. Click a row to inspect, or use the
            bulk-action bar at the bottom of the canvas.
          </div>
        )}
        {sorted.map((p) => (
          <PaperRow
            key={p.id}
            paper={p}
            onSelect={() => onSelect(p.id)}
            selected={paperId === p.id}
          />
        ))}
      </div>
    </div>
  );
}
