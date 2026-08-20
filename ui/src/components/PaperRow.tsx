import type { PaperCard, ScreenStatus } from "@/lib/api";
import { StatusBadge } from "./StatusBadge";
import { fmtNum } from "@/lib/utils";
import { Check, ExternalLink, HelpCircle, X } from "lucide-react";

export function PaperRow({
  paper,
  onSelect,
  onScreen,
  selected,
}: {
  paper: PaperCard;
  onSelect?: (p: PaperCard) => void;
  /** When set, render inline include/maybe/exclude quick actions. */
  onScreen?: (p: PaperCard, decision: ScreenStatus) => void;
  selected?: boolean;
}) {
  const meta = [
    paper.year ? String(paper.year) : null,
    paper.venue,
    paper.cited_by_count != null ? `${fmtNum(paper.cited_by_count)} cites` : null,
    paper.seen_in > 1 ? `seen×${paper.seen_in}` : null,
    paper.score != null ? `score ${paper.score.toFixed(3)}` : null,
  ].filter(Boolean) as string[];

  const quick = (decision: ScreenStatus, label: string, cls: string, Icon: typeof Check) => (
    <button
      type="button"
      title={`${label} (open the paper to add a reason)`}
      onClick={(e) => {
        e.stopPropagation();
        onScreen?.(paper, decision);
      }}
      className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] border transition-colors ${cls}`}
    >
      <Icon className="size-3" />
      {label}
    </button>
  );

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onSelect?.(paper)}
      onKeyDown={(e) => {
        if (e.key === "Enter") onSelect?.(paper);
      }}
      className={`w-full cursor-pointer text-left rounded-md border px-3 py-2.5 transition-colors ${
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
      <div className="mt-1.5 flex items-center justify-between gap-2">
        {paper.landing_page_url ? (
          <div className="min-w-0 text-[11px] text-zinc-600 flex items-center gap-1">
            <ExternalLink className="size-3 shrink-0" />
            <span className="truncate">{paper.landing_page_url}</span>
          </div>
        ) : (
          <span />
        )}
        {onScreen && (
          <div className="flex shrink-0 items-center gap-1.5">
            {quick(
              "included",
              "include",
              "border-emerald-800/60 text-emerald-400 hover:bg-emerald-500/10",
              Check,
            )}
            {quick(
              "maybe",
              "maybe",
              "border-amber-800/60 text-amber-400 hover:bg-amber-500/10",
              HelpCircle,
            )}
            {quick(
              "excluded",
              "exclude",
              "border-red-900/60 text-red-400 hover:bg-red-500/10",
              X,
            )}
          </div>
        )}
      </div>
    </div>
  );
}
