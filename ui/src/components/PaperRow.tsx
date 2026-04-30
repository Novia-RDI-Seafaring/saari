import type { PaperCard } from "@/lib/api";
import { StatusBadge } from "./StatusBadge";
import { fmtNum } from "@/lib/utils";
import { ExternalLink } from "lucide-react";

export function PaperRow({
  paper,
  onSelect,
  selected,
}: {
  paper: PaperCard;
  onSelect?: (p: PaperCard) => void;
  selected?: boolean;
}) {
  const meta = [
    paper.year ? String(paper.year) : null,
    paper.venue,
    paper.cited_by_count != null ? `${fmtNum(paper.cited_by_count)} cites` : null,
    paper.seen_in > 1 ? `seen×${paper.seen_in}` : null,
    paper.score != null ? `score ${paper.score.toFixed(3)}` : null,
  ].filter(Boolean) as string[];

  return (
    <button
      type="button"
      onClick={() => onSelect?.(paper)}
      className={`w-full text-left rounded-md border px-3 py-2.5 transition-colors ${
        selected
          ? "border-zinc-500 bg-zinc-800/50"
          : "border-zinc-800 bg-zinc-900/30 hover:border-zinc-700 hover:bg-zinc-900/60"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="font-medium text-zinc-100 leading-snug text-sm">{paper.title}</div>
        <StatusBadge status={paper.status} />
      </div>
      {meta.length > 0 && (
        <div className="mt-1 text-xs text-zinc-500">{meta.join(" · ")}</div>
      )}
      {paper.abstract_excerpt && (
        <div className="mt-2 text-xs text-zinc-400 leading-relaxed line-clamp-3">
          {paper.abstract_excerpt}
        </div>
      )}
      {paper.landing_page_url && (
        <div className="mt-1.5 text-[11px] text-zinc-600 flex items-center gap-1">
          <ExternalLink className="size-3" />
          <span className="truncate">{paper.landing_page_url}</span>
        </div>
      )}
    </button>
  );
}
