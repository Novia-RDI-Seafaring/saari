import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type PaperCard, type ScreenStatus } from "@/lib/api";
import { PaperRow } from "@/components/PaperRow";
import { PaperDetail } from "@/components/PaperDetail";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Sparkles } from "lucide-react";

const STATUSES: (ScreenStatus | "all")[] = ["all", "candidate", "maybe", "included", "excluded"];
const SORTS = ["recent", "cited", "seen_in"] as const;
type Sort = (typeof SORTS)[number];

export function Papers() {
  // Deep links (e.g. the Home stepper) can pre-filter: /papers?status=candidate
  const [searchParams] = useSearchParams();
  const statusParam = searchParams.get("status");
  const initialStatus: ScreenStatus | "all" = STATUSES.includes(
    statusParam as ScreenStatus,
  )
    ? (statusParam as ScreenStatus)
    : "all";
  const sortParam = searchParams.get("sort");
  const initialSort: Sort = SORTS.includes(sortParam as Sort)
    ? (sortParam as Sort)
    : "cited";

  const [list, setList] = useState<PaperCard[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState<ScreenStatus | "all">(initialStatus);
  const [titleGrep, setTitleGrep] = useState("");
  const [sort, setSort] = useState<Sort>(initialSort);
  const [paperId, setPaperId] = useState<string | null>(null);
  const [semantic, setSemantic] = useState("");
  const [searching, setSearching] = useState(false);

  function load() {
    api
      .papers({
        limit: 200,
        status: status === "all" ? undefined : status,
        title_grep: titleGrep || undefined,
        sort,
      })
      .then((r) => {
        setList(r.papers);
        setTotal(r.total);
      });
  }

  useEffect(() => {
    load();
  }, [status, titleGrep, sort]);

  async function runSemantic() {
    if (!semantic.trim()) return;
    setSearching(true);
    try {
      const r = await api.query({ text: semantic.trim(), k: 30 });
      setList(r.papers);
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="flex flex-1 min-w-0 min-h-0">
      <div className="flex-1 min-w-0 flex flex-col">
        <div className="border-b border-zinc-800 p-3 space-y-2 shrink-0">
          <div className="flex gap-2 items-center">
            <Input
              placeholder="title contains…"
              value={titleGrep}
              onChange={(e) => setTitleGrep(e.target.value)}
              className="flex-1"
            />
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value as ScreenStatus | "all")}
              className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-200"
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as Sort)}
              className="h-9 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-sm text-zinc-200"
            >
              {SORTS.map((s) => (
                <option key={s} value={s}>sort: {s}</option>
              ))}
            </select>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              runSemantic();
            }}
            className="flex gap-2"
          >
            <Input
              placeholder="semantic query (cosine over embeddings)…"
              value={semantic}
              onChange={(e) => setSemantic(e.target.value)}
              className="flex-1"
            />
            <Button type="submit" variant="secondary" disabled={searching || !semantic.trim()}>
              <Sparkles className="size-4" /> query
            </Button>
            <Button type="button" variant="ghost" onClick={() => { setSemantic(""); load(); }}>
              clear
            </Button>
          </form>
          <div className="text-[10px] text-zinc-600">
            showing {list.length} of {total}
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-3 space-y-1.5">
          {list.map((p) => (
            <PaperRow
              key={p.id}
              paper={p}
              onSelect={(p) => setPaperId(p.id)}
              onScreen={async (p, decision) => {
                await api.screen(p.id, decision);
                window.dispatchEvent(new Event("saari:changed"));
                load();
              }}
              selected={paperId === p.id}
            />
          ))}
        </div>
      </div>
      {paperId && (
        <div className="w-96 shrink-0">
          <PaperDetail
            paperId={paperId}
            onClose={() => setPaperId(null)}
            onChange={load}
          />
        </div>
      )}
    </div>
  );
}
