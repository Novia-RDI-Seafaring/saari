import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { FileDown, Loader2, Save, Sparkles } from "lucide-react";
import { api, type ReviewFile } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type Tab = "paper" | "figures";
const EDITABLE: Record<Tab, string> = { paper: "paper.md", figures: "" };

// The generated markdown carries `<!-- WRITE: ... -->` authoring slots and
// provenance comments. In the preview, surface WRITE slots as visible
// callouts and hide the rest — raw comment syntax is editor material.
function prepForPreview(text: string): string {
  return text
    .replace(
      /<!--\s*WRITE:\s*([\s\S]*?)\s*-->/g,
      (_m, slot: string) => `> ✍️ **To write:** ${slot.replace(/\s+/g, " ")}`,
    )
    .replace(/<!--[\s\S]*?-->/g, "");
}

// Shared markdown renderer (GFM tables for the manuscript, dark prose).
function Markdown({ text }: { text: string }) {
  return (
    <div className="text-sm text-zinc-200 leading-relaxed [&_h1]:text-lg [&_h1]:font-semibold [&_h1]:mt-4 [&_h1]:mb-2 [&_h2]:text-base [&_h2]:font-semibold [&_h2]:mt-4 [&_h2]:mb-1.5 [&_h3]:font-semibold [&_h3]:mt-3 [&_h3]:text-zinc-300 [&_p]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:my-2 [&_a]:text-emerald-400 [&_a]:underline [&_blockquote]:border-l-2 [&_blockquote]:border-zinc-700 [&_blockquote]:pl-3 [&_blockquote]:text-zinc-400 [&_code]:text-emerald-300 [&_code]:text-xs [&_table]:my-2 [&_table]:text-xs [&_th]:border [&_th]:border-zinc-700 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_td]:border [&_td]:border-zinc-800 [&_td]:px-2 [&_td]:py-1 [&_img]:my-2 [&_img]:max-w-full">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

export function Review() {
  const [files, setFiles] = useState<ReviewFile[]>([]);
  const [tab, setTab] = useState<Tab>("paper");
  const [loaded, setLoaded] = useState<Record<string, string>>({}); // server content
  const [draft, setDraft] = useState<Record<string, string>>({}); // editor content
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const hasBundle = files.length > 0;

  async function loadList() {
    const r = await api.reviewList();
    setFiles(r.files);
    // pull the editable files' contents
    for (const f of r.files.filter((x) => x.editable)) {
      const text = await api.reviewFile(f.name);
      setLoaded((m) => ({ ...m, [f.name]: text }));
      setDraft((m) => ({ ...m, [f.name]: text }));
    }
  }

  useEffect(() => {
    loadList().catch((e) => setMsg(String(e)));
  }, []);

  async function generate() {
    setGenerating(true);
    setMsg(null);
    try {
      const r = await api.exportSlr();
      setMsg(`Generated bundle · ${r.n_entries} included papers`);
      await loadList();
      setTimeout(() => setMsg(null), 6000);
    } catch (e) {
      setMsg(String(e));
    } finally {
      setGenerating(false);
    }
  }

  async function save(name: string) {
    setSaving(true);
    setMsg(null);
    try {
      await api.saveReviewFile(name, draft[name] ?? "");
      setLoaded((m) => ({ ...m, [name]: draft[name] ?? "" }));
      setMsg(`Saved ${name}`);
      setTimeout(() => setMsg(null), 4000);
    } catch (e) {
      setMsg(String(e));
    } finally {
      setSaving(false);
    }
  }

  const name = EDITABLE[tab];
  const dirty = name ? draft[name] !== loaded[name] : false;

  return (
    <div className="flex flex-1 min-w-0 min-h-0 flex-col">
      {/* toolbar */}
      <div className="flex items-center gap-3 border-b border-zinc-800 px-4 h-11 shrink-0">
        <Button size="sm" variant="success" onClick={generate} disabled={generating}>
          {generating ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
          {hasBundle ? "Regenerate bundle" : "Generate review bundle"}
        </Button>
        <div className="flex gap-1">
          {(["paper", "figures"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={cn(
                "rounded-md px-3 py-1 text-sm capitalize transition-colors",
                tab === t ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:text-zinc-200",
              )}
            >
              {t}
            </button>
          ))}
        </div>
        <div className="ml-auto text-xs text-zinc-400">{msg}</div>
      </div>

      {!hasBundle ? (
        <div className="flex flex-1 items-center justify-center text-sm text-zinc-500">
          No review bundle yet. Set a protocol + screen papers, then Generate.
        </div>
      ) : tab === "figures" ? (
        <FiguresTab files={files} />
      ) : (
        // split pane: editor | preview
        <div className="flex flex-1 min-h-0">
          <div className="flex w-1/2 flex-col border-r border-zinc-800 min-h-0">
            <div className="flex items-center gap-2 px-3 py-1.5 border-b border-zinc-800 text-xs text-zinc-400">
              <span className="font-mono">{name}</span>
              {dirty && <span className="text-amber-400">● unsaved</span>}
              <div className="ml-auto flex gap-1.5">
                <Button size="xs" variant="outline" disabled={!dirty || saving} onClick={() => save(name)}>
                  {saving ? <Loader2 className="size-3 animate-spin" /> : <Save className="size-3" />}
                  save
                </Button>
                <a href={api.reviewFileUrl(name, { download: true })} download>
                  <Button size="xs" variant="ghost">
                    <FileDown className="size-3" />
                    download
                  </Button>
                </a>
              </div>
            </div>
            <textarea
              value={draft[name] ?? ""}
              onChange={(e) => setDraft((m) => ({ ...m, [name]: e.target.value }))}
              spellCheck={false}
              className="flex-1 min-h-0 w-full resize-none bg-zinc-950 px-3 py-2 font-mono text-xs text-zinc-200 focus:outline-none leading-relaxed"
            />
          </div>
          <div className="w-1/2 min-h-0 overflow-auto p-4">
            <Markdown text={prepForPreview(draft[name] ?? "")} />
          </div>
        </div>
      )}
    </div>
  );
}

function FiguresTab({ files }: { files: ReviewFile[] }) {
  const has = (n: string) => files.some((f) => f.name === n);
  return (
    <div className="flex-1 overflow-auto p-6 space-y-6">
      {["prisma.svg", "landscape.svg"].filter(has).map((n) => (
        <div key={n} className="space-y-2">
          <div className="flex items-center gap-2 text-xs text-zinc-400">
            <span className="font-mono">{n}</span>
            <a href={api.reviewFileUrl(n, { download: true })} download className="ml-2">
              <Button size="xs" variant="ghost">
                <FileDown className="size-3" />
                download
              </Button>
            </a>
          </div>
          <img
            src={api.reviewFileUrl(n)}
            alt={n}
            className="max-w-full h-auto rounded-md border border-zinc-800 bg-white p-2"
          />
        </div>
      ))}
      <div className="flex flex-wrap gap-4 pt-2">
        {["slides.md", "refs.bib", "prisma.mmd"].filter(has).map((n) => (
          <a key={n} href={api.reviewFileUrl(n, { download: true })} download>
            <Button size="xs" variant="outline">
              <FileDown className="size-3" />
              {n}
            </Button>
          </a>
        ))}
      </div>
    </div>
  );
}
