import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Check, Lightbulb } from "lucide-react";
import { api, type Stages, type Stage } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

// Stage states are derived server-side from the corpus DB (see
// src/saari/status.py). The stepper never tracks anything itself — it only
// renders /api/stages, the same payload `saari status` and the
// `review_status` MCP tool return.
export function useStages(pollMs = 30000): Stages | null {
  const [data, setData] = useState<Stages | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .stages()
        .then((d) => alive && setData(d))
        .catch(() => {});
    load();
    const t = setInterval(load, pollMs);
    const onFocus = () => load();
    window.addEventListener("focus", onFocus);
    // Views dispatch this after mutations (study saved, bundle regenerated,
    // papers screened) so the stepper reacts immediately, not on next poll.
    window.addEventListener("saari:changed", onFocus);
    return () => {
      alive = false;
      clearInterval(t);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("saari:changed", onFocus);
    };
  }, [pollMs]);
  return data;
}

function Node({ stage, isNext, index }: { stage: Stage; isNext: boolean; index: number }) {
  const navigate = useNavigate();
  return (
    <button
      onClick={() => navigate(stage.view)}
      className="group flex flex-col items-center gap-1.5 min-w-0 flex-1 focus:outline-none"
      title={stage.hint}
    >
      <div
        className={cn(
          "flex size-8 items-center justify-center rounded-full border-2 text-xs font-semibold transition-all",
          stage.state === "done" &&
            "border-emerald-500 bg-emerald-500 text-zinc-950",
          stage.state === "active" &&
            "border-amber-400/80 bg-amber-400/10 text-amber-300",
          stage.state === "todo" &&
            "border-zinc-700 bg-transparent text-zinc-600",
          isNext && "ring-2 ring-emerald-400/50 ring-offset-2 ring-offset-zinc-950",
          "group-hover:scale-105",
        )}
      >
        {stage.state === "done" ? <Check className="size-4" strokeWidth={3} /> : index + 1}
      </div>
      <div
        className={cn(
          "text-xs font-medium",
          stage.state === "done" && "text-emerald-400",
          stage.state === "active" && "text-amber-300",
          stage.state === "todo" && "text-zinc-500",
        )}
      >
        {stage.label}
        {isNext && (
          <span className="ml-1.5 rounded-sm bg-emerald-500/15 px-1 py-px text-[9px] uppercase tracking-wider text-emerald-400">
            next
          </span>
        )}
      </div>
      <div className="text-[11px] leading-tight text-zinc-500 text-center max-w-[9.5rem] truncate w-full">
        {stage.detail}
      </div>
    </button>
  );
}

export function Stepper() {
  const data = useStages();
  if (!data) return null;
  const nextStage = data.stages.find((s) => s.key === data.next) ?? null;

  return (
    <Card className="p-5">
      <div className="mb-4 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-200">
          Systematic review
        </h2>
        <span className="text-[10px] text-zinc-600">
          stages derived live from the corpus — nothing tracked by hand
        </span>
      </div>

      <div className="flex items-start">
        {data.stages.map((s, i) => (
          <div key={s.key} className="contents">
            {i > 0 && (
              <div
                className={cn(
                  "mt-4 h-0.5 w-6 shrink-0 rounded sm:w-10",
                  data.stages[i - 1].state === "done"
                    ? "bg-emerald-500/60"
                    : "bg-zinc-800",
                )}
              />
            )}
            <Node stage={s} isNext={s.key === data.next} index={i} />
          </div>
        ))}
      </div>

      {nextStage ? (
        <div className="mt-4 flex items-start gap-2 rounded-md border border-zinc-800 bg-zinc-900/60 px-3 py-2">
          <Lightbulb className="mt-0.5 size-3.5 shrink-0 text-emerald-400" />
          <p className="text-xs leading-relaxed text-zinc-400">
            <span className="font-medium text-zinc-200">{nextStage.label}: </span>
            {nextStage.hint}
          </p>
        </div>
      ) : (
        <div className="mt-4 rounded-md border border-emerald-900/60 bg-emerald-950/30 px-3 py-2 text-xs text-emerald-300">
          All stages complete — the review is ready to hand to the manuscript.
        </div>
      )}
    </Card>
  );
}

// Compact five-dot progress strip for the sidebar: same data, glanceable
// from every view. Links back to Home where the full stepper lives.
export function StageDots() {
  const data = useStages();
  const navigate = useNavigate();
  if (!data) return null;
  return (
    <button
      onClick={() => navigate("/home")}
      title={data.stages.map((s) => `${s.label}: ${s.detail}`).join("\n")}
      className="mt-auto flex flex-col gap-1.5 rounded-md px-3 py-2 text-left hover:bg-zinc-900 focus:outline-none"
    >
      <div className="flex items-center gap-1.5">
        {data.stages.map((s) => (
          <span
            key={s.key}
            className={cn(
              "size-2 rounded-full",
              s.state === "done" && "bg-emerald-500",
              s.state === "active" && "bg-amber-400",
              s.state === "todo" && "bg-zinc-700",
              s.key === data.next && "ring-1 ring-emerald-400/60 ring-offset-1 ring-offset-zinc-950",
            )}
          />
        ))}
      </div>
      <span className="text-[10px] text-zinc-600">review progress</span>
    </button>
  );
}
