import { useEffect, useState } from "react";
import type { PaperFull, ScreenStatus } from "@/lib/api";
import { api } from "@/lib/api";
import { Button } from "./ui/button";
import { StatusBadge } from "./StatusBadge";
import { fmtNum } from "@/lib/utils";
import { ExternalLink, FileText, Sparkles, Network, X } from "lucide-react";

export function PaperDetail({
  paperId,
  onClose,
  onChange,
  onShowSimilar,
  onShowGraph,
}: {
  paperId: string;
  onClose: () => void;
  onChange?: () => void;
  onShowSimilar?: (id: string) => void;
  onShowGraph?: (id: string) => void;
}) {
  const [paper, setPaper] = useState<PaperFull | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string>("");
  const [noteDirty, setNoteDirty] = useState(false);

  useEffect(() => {
    let cancel = false;
    setLoading(true);
    setError(null);
    api
      .paper(paperId)
      .then((p) => {
        if (!cancel) {
          setPaper(p);
          setNote(p.screening_note ?? "");
          setNoteDirty(false);
        }
      })
      .catch((e) => {
        if (!cancel) setError(String(e));
      })
      .finally(() => {
        if (!cancel) setLoading(false);
      });
    return () => {
      cancel = true;
    };
  }, [paperId]);

  async function screen(decision: ScreenStatus) {
    if (!paper) return;
    setBusy(decision);
    try {
      await api.screen(paper.id, decision, noteDirty ? note : undefined);
      window.dispatchEvent(new Event("saari:changed"));
      const p = await api.paper(paper.id);
      setPaper(p);
      setNote(p.screening_note ?? "");
      setNoteDirty(false);
      onChange?.();
    } finally {
      setBusy(null);
    }
  }

  async function saveNote() {
    if (!paper) return;
    setBusy("note");
    try {
      await api.screen(paper.id, paper.status, note);
      setNoteDirty(false);
      onChange?.();
    } finally {
      setBusy(null);
    }
  }

  async function snowball() {
    if (!paper) return;
    setBusy("snowball");
    try {
      await api.snowball(paper.id);
      onChange?.();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex h-full flex-col bg-zinc-900/40 border-l border-zinc-800 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 border-b border-zinc-800 shrink-0">
        <span className="text-xs uppercase tracking-wider text-zinc-500">paper</span>
        <button
          onClick={onClose}
          className="text-zinc-500 hover:text-zinc-200"
          aria-label="close"
        >
          <X className="size-4" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {loading && <div className="text-zinc-500 text-sm">loading…</div>}
        {error && <div className="text-red-400 text-sm">{error}</div>}
        {paper && (
          <>
            <div>
              <h2 className="text-base font-semibold text-zinc-100 leading-snug">
                {paper.title}
              </h2>
              <div className="mt-2 flex items-center gap-2 flex-wrap">
                <StatusBadge status={paper.status} />
                {paper.oa_status && paper.oa_status !== "closed" && (
                  <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-900/40 text-blue-300 border border-blue-800">
                    {paper.oa_status}
                  </span>
                )}
              </div>
            </div>

            <dl className="grid grid-cols-3 gap-2 text-xs">
              <div>
                <dt className="text-zinc-500">Year</dt>
                <dd className="text-zinc-200">{paper.year ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-zinc-500">Citations</dt>
                <dd className="text-zinc-200">{fmtNum(paper.cited_by_count)}</dd>
              </div>
              <div>
                <dt className="text-zinc-500">Seen in</dt>
                <dd className="text-zinc-200">{paper.seen_in} searches</dd>
              </div>
            </dl>

            {paper.venue && (
              <div className="text-xs">
                <span className="text-zinc-500">Venue: </span>
                <span className="text-zinc-200">{paper.venue}</span>
              </div>
            )}

            {paper.authors.length > 0 && (
              <div className="text-xs">
                <span className="text-zinc-500">Authors: </span>
                <span className="text-zinc-300">
                  {paper.authors.slice(0, 5).map((a) => a.name).join(", ")}
                  {paper.authors.length > 5 ? `, +${paper.authors.length - 5} more` : ""}
                </span>
              </div>
            )}

            {paper.abstract && (
              <div className="text-xs leading-relaxed text-zinc-300 whitespace-pre-wrap">
                {paper.abstract}
              </div>
            )}

            <div className="flex flex-wrap gap-1.5">
              <Button size="sm" variant="success" disabled={busy !== null}
                      onClick={() => screen("included")}>include</Button>
              <Button size="sm" variant="secondary" disabled={busy !== null}
                      onClick={() => screen("maybe")}>maybe</Button>
              <Button size="sm" variant="destructive" disabled={busy !== null}
                      onClick={() => screen("excluded")}>exclude</Button>
              <Button size="sm" variant="ghost" disabled={busy !== null}
                      onClick={() => screen("candidate")}>reset</Button>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-[10px] uppercase tracking-wider text-zinc-500">
                  Screening note
                </label>
                {noteDirty && (
                  <Button size="xs" variant="outline" disabled={busy !== null} onClick={saveNote}>
                    save note
                  </Button>
                )}
              </div>
              <textarea
                value={note}
                onChange={(e) => {
                  setNote(e.target.value);
                  setNoteDirty(e.target.value !== (paper.screening_note ?? ""));
                }}
                placeholder="Why include / exclude / maybe? Saved with the next decision, or via Save."
                className="w-full min-h-[64px] rounded-md border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-zinc-600 resize-y leading-relaxed"
              />
            </div>

            <div className="flex flex-wrap gap-1.5 pt-1 border-t border-zinc-800">
              <Button size="sm" variant="outline" disabled={busy !== null}
                      onClick={snowball}>
                <Network className="size-3.5" /> snowball
              </Button>
              {onShowSimilar && (
                <Button size="sm" variant="outline"
                        onClick={() => onShowSimilar(paper.id)}>
                  <Sparkles className="size-3.5" /> similar
                </Button>
              )}
              {onShowGraph && (
                <Button size="sm" variant="outline"
                        onClick={() => onShowGraph(paper.id)}>
                  <Network className="size-3.5" /> graph
                </Button>
              )}
            </div>

            <div className="space-y-1 pt-1 border-t border-zinc-800">
              {paper.landing_page_url && (
                <a href={paper.landing_page_url} target="_blank" rel="noreferrer"
                   className="flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300">
                  <ExternalLink className="size-3" /> publisher page
                </a>
              )}
              {paper.pdf_url && (
                <a href={paper.pdf_url} target="_blank" rel="noreferrer"
                   className="flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300">
                  <FileText className="size-3" /> PDF
                </a>
              )}
              {paper.doi && (
                <div className="text-[11px] text-zinc-500">
                  DOI <code className="text-zinc-300">{paper.doi}</code>
                </div>
              )}
              <div className="text-[11px] text-zinc-600">
                <code>{paper.id}</code>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
