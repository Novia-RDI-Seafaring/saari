import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Study, type Funnel } from "@/lib/api";
import { Stepper } from "@/components/Stepper";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Pencil, Save, Search, ScatterChart, Network, Database, FileText, Loader2, X } from "lucide-react";
import { cn } from "@/lib/utils";

export function Home() {
  const [study, setStudy] = useState<Study | null>(null);
  const [funnel, setFunnel] = useState<Funnel | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ question: "", criteria: "", tags: "" });
  const [saving, setSaving] = useState(false);

  function load() {
    api.study().then(setStudy);
    api.funnel().then(setFunnel);
  }
  useEffect(() => {
    load();
  }, []);

  function startEdit() {
    if (!study) return;
    setDraft({
      question: study.question,
      criteria: study.criteria,
      tags: study.tags.join(", "),
    });
    setEditing(true);
  }

  async function save() {
    setSaving(true);
    try {
      const tags = draft.tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);
      const updated = await api.studyUpdate({
        question: draft.question,
        criteria: draft.criteria,
        tags,
      });
      setStudy(updated);
      setEditing(false);
      window.dispatchEvent(new Event("saari:changed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex-1 min-w-0 overflow-y-auto">
      <div className="max-w-4xl mx-auto p-6 space-y-6">
        <Stepper />

        {/* Study card */}
        <Card className="p-5">
          <div className="flex items-start justify-between gap-3 mb-3">
            <h2 className="text-sm font-semibold text-zinc-200 uppercase tracking-wider">
              Study
            </h2>
            {!editing ? (
              <Button size="xs" variant="ghost" onClick={startEdit}>
                <Pencil className="size-3" /> edit
              </Button>
            ) : (
              <div className="flex gap-1.5">
                <Button size="xs" variant="ghost" onClick={() => setEditing(false)} disabled={saving}>
                  <X className="size-3" /> cancel
                </Button>
                <Button size="xs" variant="success" onClick={save} disabled={saving}>
                  {saving ? <Loader2 className="size-3 animate-spin" /> : <Save className="size-3" />}
                  save
                </Button>
              </div>
            )}
          </div>

          {editing ? (
            <div className="space-y-3">
              <div>
                <label className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1 block">
                  Research question
                </label>
                <textarea
                  className="w-full min-h-[60px] rounded-md border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-600 resize-y"
                  placeholder="What are you trying to answer?"
                  value={draft.question}
                  onChange={(e) => setDraft((d) => ({ ...d, question: e.target.value }))}
                />
              </div>
              <div>
                <label className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1 block">
                  Inclusion / exclusion criteria (markdown OK)
                </label>
                <textarea
                  className="w-full min-h-[120px] rounded-md border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-zinc-600 resize-y font-mono"
                  placeholder="- Peer-reviewed&#10;- Published since 2015&#10;- English"
                  value={draft.criteria}
                  onChange={(e) => setDraft((d) => ({ ...d, criteria: e.target.value }))}
                />
              </div>
              <div>
                <label className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1 block">
                  Tags (comma-separated)
                </label>
                <Input
                  placeholder="maritime, digital-twins"
                  value={draft.tags}
                  onChange={(e) => setDraft((d) => ({ ...d, tags: e.target.value }))}
                />
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              {study?.question ? (
                <p className="text-zinc-100 leading-relaxed">{study.question}</p>
              ) : (
                <p className="text-zinc-500 italic text-sm">
                  No research question yet. Click <span className="text-zinc-300">edit</span> to write one.
                </p>
              )}
              {study?.criteria && (
                <div className="border-t border-zinc-800 pt-3">
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5">Criteria</div>
                  <pre className="text-xs text-zinc-300 whitespace-pre-wrap font-mono leading-relaxed">
                    {study.criteria}
                  </pre>
                </div>
              )}
              {study?.tags && study.tags.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {study.tags.map((t) => (
                    <span
                      key={t}
                      className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-zinc-800 text-zinc-300 border border-zinc-700"
                    >
                      {t}
                    </span>
                  ))}
                </div>
              )}
              {study?.started_at && (
                <div className="text-[10px] text-zinc-600">
                  started {study.started_at.slice(0, 10)}
                  {study.updated_at && study.updated_at !== study.started_at && (
                    <> · updated {study.updated_at.slice(0, 10)}</>
                  )}
                </div>
              )}
            </div>
          )}
        </Card>

        {/* PRISMA-style funnel */}
        <Card className="p-5">
          <h2 className="text-sm font-semibold text-zinc-200 uppercase tracking-wider mb-4">
            Progress
          </h2>
          {funnel ? (
            <div className="space-y-3">
              <FunnelRow
                label="Searches"
                value={funnel.n_searches}
                detail={funnel.searches.map((s) => s.query).slice(0, 3).join(" · ") +
                  (funnel.searches.length > 3 ? ` · +${funnel.searches.length - 3} more` : "")}
              />
              <FunnelRow
                label="Records fetched (with overlap)"
                value={funnel.n_fetched}
                detail={`across ${funnel.n_searches} searches`}
              />
              <FunnelRow
                label="Unique papers in corpus"
                value={funnel.n_unique}
                detail={`${funnel.embedded} embedded · ${funnel.projected} projected`}
              />
              <div className="border-t border-zinc-800 pt-3 grid grid-cols-4 gap-3">
                <StatusTile label="candidate" n={funnel.by_status.candidate ?? 0} color="#71717a" />
                <StatusTile label="maybe" n={funnel.by_status.maybe ?? 0} color="#eab308" />
                <StatusTile label="included" n={funnel.by_status.included ?? 0} color="#10b981" />
                <StatusTile label="excluded" n={funnel.by_status.excluded ?? 0} color="#ef4444" />
              </div>
              {funnel.n_unscreened > 0 && (
                <div className="text-xs text-zinc-500 pt-1">
                  <span className="text-zinc-300">{funnel.n_unscreened}</span> still to screen.
                </div>
              )}
            </div>
          ) : (
            <div className="text-zinc-500 text-sm">loading…</div>
          )}
        </Card>

        {/* Next actions */}
        <Card className="p-5">
          <h2 className="text-sm font-semibold text-zinc-200 uppercase tracking-wider mb-3">
            Next actions
          </h2>
          <div className="grid grid-cols-2 gap-2.5">
            <ActionCard
              to="/searches"
              icon={Search}
              title="Run a search"
              hint="Query OpenAlex with year filter · adds papers to the corpus."
            />
            <ActionCard
              to="/corpus"
              icon={ScatterChart}
              title="Triage the cluster"
              hint={
                funnel && funnel.embedded > 0
                  ? "Lasso a region in UMAP, bulk include/exclude."
                  : "Run a search first, then refresh to embed + project."
              }
              dim={!funnel || funnel.embedded === 0}
            />
            <ActionCard
              to="/graph"
              icon={Network}
              title="Map the citations"
              hint="See which papers cite which. Snowball seeds to densify."
              dim={!funnel || funnel.n_unique < 5}
            />
            <ActionCard
              to="/papers"
              icon={Database}
              title="Browse + filter"
              hint="Sortable table with filters and semantic query."
              dim={!funnel || funnel.n_unique === 0}
            />
          </div>
        </Card>
      </div>
    </div>
  );
}

function FunnelRow({ label, value, detail }: { label: string; value: number; detail?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <div>
        <div className="text-zinc-300 text-sm">{label}</div>
        {detail && <div className="text-zinc-500 text-[11px] mt-0.5">{detail}</div>}
      </div>
      <div className="text-2xl font-semibold tabular-nums text-zinc-100">{value}</div>
    </div>
  );
}

function StatusTile({ label, n, color }: { label: string; n: number; color: string }) {
  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-950/50 p-2.5">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-zinc-500">
        <span className="size-2 rounded-full" style={{ background: color }} />
        {label}
      </div>
      <div className="text-xl font-semibold tabular-nums text-zinc-100 mt-0.5">{n}</div>
    </div>
  );
}

function ActionCard({
  to,
  icon: Icon,
  title,
  hint,
  dim = false,
}: {
  to: string;
  icon: typeof FileText;
  title: string;
  hint: string;
  dim?: boolean;
}) {
  return (
    <Link
      to={to}
      className={cn(
        "rounded-md border p-3 transition-colors group",
        dim
          ? "border-zinc-900 bg-zinc-950/50 hover:border-zinc-800"
          : "border-zinc-800 bg-zinc-900/40 hover:border-zinc-700 hover:bg-zinc-900/70",
      )}
    >
      <div className="flex items-center gap-2">
        <Icon className={cn("size-4", dim ? "text-zinc-600" : "text-emerald-400")} />
        <span className={cn("text-sm font-medium", dim ? "text-zinc-500" : "text-zinc-100")}>
          {title}
        </span>
      </div>
      <div className={cn("text-[11px] mt-1.5 leading-relaxed", dim ? "text-zinc-600" : "text-zinc-400")}>
        {hint}
      </div>
    </Link>
  );
}
