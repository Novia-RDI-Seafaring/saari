import { NavLink, Outlet } from "react-router-dom";
import { useEffect, useState } from "react";
import { api, type ProjectInfo } from "@/lib/api";
import { Home, Search, ScatterChart, Network, Database, FileDown, FileText, Loader2 } from "lucide-react";
import { ChatPanel } from "./ChatPanel";
import { StageDots } from "./Stepper";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

export function Layout() {
  const [info, setInfo] = useState<ProjectInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportMsg, setExportMsg] = useState<string | null>(null);

  useEffect(() => {
    api.project().then(setInfo).catch((e) => setError(String(e)));
  }, []);

  async function exportBibtex() {
    setExporting(true);
    setExportMsg(null);
    try {
      const r = await api.exportBibtex("included");
      setExportMsg(`wrote ${r.n_entries} entries → ${r.path}`);
      setTimeout(() => setExportMsg(null), 6000);
    } catch (e) {
      setExportMsg(String(e));
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-zinc-800 px-4 h-12 shrink-0">
        <div className="flex items-center gap-3">
          <div className="size-5 rounded bg-gradient-to-br from-emerald-400 to-emerald-700" />
          <h1 className="font-semibold text-zinc-100">saari</h1>
          {info && (
            <>
              <span className="text-zinc-600">/</span>
              <span className="text-zinc-300 text-sm">{info.name}</span>
              <span className="text-zinc-600 text-xs ml-2">
                {info.n_papers} papers · {info.n_searches} searches
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-3">
          {info && (
            <div className="flex items-center gap-3 text-xs text-zinc-500">
              <span>embedded {info.pipeline.embedded}/{info.n_papers}</span>
              <span>·</span>
              <span>projected {info.pipeline.projected}/{info.n_papers}</span>
              {Object.entries(info.by_status).map(([k, v]) => (
                <span key={k} className="text-zinc-400">
                  <span className="text-zinc-600">{k}</span> {v}
                </span>
              ))}
            </div>
          )}
          {error && <div className="text-red-400 text-xs">{error}</div>}
          <Button
            size="xs"
            variant="outline"
            onClick={exportBibtex}
            disabled={exporting}
            title="Export included papers to BibTeX"
          >
            {exporting ? <Loader2 className="size-3 animate-spin" /> : <FileDown className="size-3" />}
            bibtex
          </Button>
        </div>
      </header>
      {exportMsg && (
        <div className="border-b border-zinc-800 bg-zinc-900/50 px-4 py-1.5 text-xs text-zinc-300">
          {exportMsg}
        </div>
      )}
      <div className="flex flex-1 min-h-0">
        <nav className="w-48 border-r border-zinc-800 p-2 shrink-0 flex flex-col gap-0.5">
          {[
            { to: "/home", icon: Home, label: "Home" },
            { to: "/searches", icon: Search, label: "Searches" },
            { to: "/corpus", icon: ScatterChart, label: "Corpus" },
            { to: "/graph", icon: Network, label: "Graph" },
            { to: "/papers", icon: Database, label: "Papers" },
            { to: "/review", icon: FileText, label: "Review" },
          ].map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                  isActive
                    ? "bg-zinc-800 text-zinc-100"
                    : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200",
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
          <StageDots />
        </nav>
        <main className="flex-1 min-w-0 min-h-0 flex">
          <Outlet />
        </main>
        <ChatPanel />
      </div>
    </div>
  );
}
