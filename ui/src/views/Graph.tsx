import { useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Node,
  type Edge,
} from "reactflow";
import "reactflow/dist/style.css";
import { api, type PaperFull } from "@/lib/api";
import { PaperDetail } from "@/components/PaperDetail";
import { STATUS_HEX } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Loader2, Network, RefreshCw } from "lucide-react";

function shortTitle(s: string, n = 60): string {
  const t = s.replace(/\s+/g, " ").trim();
  return t.length <= n ? t : t.slice(0, n - 1) + "…";
}

export function Graph() {
  const [seed, setSeed] = useState<string | null>(null);
  const [seedQuery, setSeedQuery] = useState("");
  const [allIds, setAllIds] = useState<Set<string>>(new Set());
  const [papers, setPapers] = useState<PaperFull[]>([]);
  const [loading, setLoading] = useState(true);
  const [snowballing, setSnowballing] = useState(false);
  const [paperId, setPaperId] = useState<string | null>(null);

  // Pick the seed with the most in-corpus referenced_works (densest local graph).
  // Falls back to most-cited paper if no candidate has refs in corpus.
  useEffect(() => {
    (async () => {
      const all = await api.papers({ limit: 5000, sort: "cited" });
      const ids = new Set(all.papers.map((p) => p.id));
      setAllIds(ids);

      // Look at the top-50 cited papers and pick the one with most in-corpus refs.
      const top = all.papers.slice(0, 50);
      let best: { id: string; n: number } | null = null;
      for (const card of top) {
        const full = await api.paper(card.id);
        const inCorpus = full.referenced_works.filter((r) => ids.has(`openalex:${r}`)).length;
        if (!best || inCorpus > best.n) best = { id: full.id, n: inCorpus };
        if (best.n >= 5) break; // good enough
      }
      if (best) setSeed(best.id);
    })();
  }, []);

  useEffect(() => {
    if (!seed || allIds.size === 0) return;
    setLoading(true);
    (async () => {
      const visited = new Set<string>();
      const queue: string[] = [seed];
      const collected: PaperFull[] = [];
      while (queue.length > 0 && collected.length < 60) {
        const id = queue.shift()!;
        if (visited.has(id)) continue;
        visited.add(id);
        try {
          const p = await api.paper(id);
          collected.push(p);
          for (const ref of p.referenced_works.slice(0, 10)) {
            const fullId = `openalex:${ref}`;
            if (allIds.has(fullId) && !visited.has(fullId)) queue.push(fullId);
          }
        } catch {
          /* skip */
        }
      }
      setPapers(collected);
      setLoading(false);
    })();
  }, [seed, allIds]);

  const { nodes, edges } = useMemo<{ nodes: Node[]; edges: Edge[] }>(() => {
    if (papers.length === 0) return { nodes: [], edges: [] };
    const inCorpus = new Set(papers.map((p) => p.id));
    const N = papers.length;
    const cx = 400;
    const cy = 300;
    const r = Math.max(180, N * 8);
    const nodes: Node[] = papers.map((p, i) => {
      const angle = (i / N) * Math.PI * 2;
      const isSeed = p.id === seed;
      return {
        id: p.id,
        position: { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r },
        data: { label: shortTitle(p.title) },
        style: {
          background: isSeed ? "#0c2620" : "#18181b",
          border: `2px solid ${STATUS_HEX[p.status]}`,
          color: "#f4f4f5",
          fontSize: 11,
          padding: 6,
          borderRadius: 6,
          width: 200,
          fontWeight: isSeed ? 600 : 400,
        },
      };
    });
    const edges: Edge[] = [];
    for (const p of papers) {
      for (const ref of p.referenced_works) {
        const fullId = `openalex:${ref}`;
        if (inCorpus.has(fullId)) {
          edges.push({
            id: `${p.id}->${fullId}`,
            source: p.id,
            target: fullId,
            style: { stroke: "#3f3f46", strokeWidth: 1 },
          });
        }
      }
    }
    return { nodes, edges };
  }, [papers, seed]);

  async function snowballSeed() {
    if (!seed) return;
    setSnowballing(true);
    try {
      await api.snowball(seed, "both", 25);
      // refresh the in-corpus set
      const all = await api.papers({ limit: 5000 });
      setAllIds(new Set(all.papers.map((p) => p.id)));
    } finally {
      setSnowballing(false);
    }
  }

  async function onSeedSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!seedQuery.trim()) return;
    try {
      const r = await api.query({ text: seedQuery.trim(), k: 1 });
      if (r.papers.length > 0) setSeed(r.papers[0].id);
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="flex flex-1 min-w-0 min-h-0">
      <div className="w-64 border-r border-zinc-800 p-3 space-y-3 shrink-0">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">Seed paper</div>
          <form onSubmit={onSeedSubmit} className="space-y-1.5">
            <Input
              placeholder="describe seed (semantic)…"
              value={seedQuery}
              onChange={(e) => setSeedQuery(e.target.value)}
            />
            <Button type="submit" size="sm" variant="outline" className="w-full">
              <Network className="size-3.5" /> set seed
            </Button>
          </form>
          <div className="text-[10px] text-zinc-500 mt-1.5 leading-relaxed">
            Picks the most-similar paper in the corpus and re-roots the citation graph there.
          </div>
        </div>
        <div className="pt-2 border-t border-zinc-800">
          <Button
            size="sm"
            variant="secondary"
            className="w-full"
            onClick={snowballSeed}
            disabled={!seed || snowballing}
          >
            {snowballing ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
            snowball seed
          </Button>
          <div className="text-[10px] text-zinc-500 mt-1.5 leading-relaxed">
            Pulls the seed's references + citers into the corpus. Dense graphs need this.
          </div>
        </div>
        <div className="pt-2 border-t border-zinc-800 text-[10px] text-zinc-500">
          {nodes.length} nodes · {edges.length} edges
        </div>
      </div>

      <div className="flex-1 min-w-0 relative bg-zinc-950">
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center text-zinc-500 text-sm">
            <Loader2 className="size-4 animate-spin mr-2" /> building graph…
          </div>
        )}
        {!loading && nodes.length === 0 && (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-zinc-500 text-sm gap-3 px-8 text-center">
            <div>No citation edges yet — your corpus papers don't reference each other.</div>
            <div className="text-xs max-w-md">
              Snowball the seed to pull in its referenced works and citers, then you'll see real structure.
            </div>
            {seed && (
              <Button size="sm" variant="secondary" onClick={snowballSeed} disabled={snowballing}>
                {snowballing ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                snowball seed
              </Button>
            )}
          </div>
        )}
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodeClick={(_, n) => setPaperId(n.id)}
          fitView
          minZoom={0.1}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="#27272a" gap={24} />
          <Controls className="!bg-zinc-900 !border-zinc-800" />
          <MiniMap pannable zoomable maskColor="rgba(0,0,0,0.5)" />
        </ReactFlow>
      </div>
      {paperId && (
        <div className="w-96 shrink-0">
          <PaperDetail
            paperId={paperId}
            onClose={() => setPaperId(null)}
            onShowGraph={(id) => setSeed(id)}
          />
        </div>
      )}
    </div>
  );
}
