import { useEffect, useMemo, useRef, useState } from "react";
import { api, type ProjectionPoint, type ScreenStatus } from "@/lib/api";
import { Loader2, RotateCw, MousePointer2, Square, Network } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PaperDetail } from "@/components/PaperDetail";
import { SelectionPanel } from "@/components/SelectionPanel";
import { STATUS_HEX } from "@/components/StatusBadge";
import { cn } from "@/lib/utils";

const ALL_STATUSES: ScreenStatus[] = ["candidate", "maybe", "included", "excluded"];

type SizeBy = "cites" | "links" | "seen";
const SIZE_BY_LABEL: Record<SizeBy, string> = {
  cites: "citations (global)",
  links: "in-corpus links",
  seen: "seen across searches",
};

function rawMetric(p: ProjectionPoint, sizeBy: SizeBy, degree: number): number {
  if (sizeBy === "links") return degree;
  if (sizeBy === "seen") return p.seen_in;
  return p.cited_by_count ?? 0;
}

function radiusFor(p: ProjectionPoint, sizeBy: SizeBy, degree: number): number {
  const v = rawMetric(p, sizeBy, degree);
  return 3.5 + Math.log10(v + 1) * 2.2;
}

interface ViewBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface BoxSel {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

interface Edge {
  source: string;
  target: string;
}

export function Corpus() {
  const [points, setPoints] = useState<ProjectionPoint[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [loading, setLoading] = useState(true);
  const [statuses, setStatuses] = useState(new Set<ScreenStatus>(ALL_STATUSES));
  const [titleQ, setTitleQ] = useState("");
  const [paperId, setPaperId] = useState<string | null>(null);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [hoverPos, setHoverPos] = useState<{ x: number; y: number } | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState<string | null>(null);
  const [boxMode, setBoxMode] = useState(false);
  const [showCitations, setShowCitations] = useState(true);
  const [sizeBy, setSizeBy] = useState<SizeBy>("cites");
  const wrapRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  const dataExtent = useMemo(() => {
    if (points.length === 0) return { x0: 0, x1: 1, y0: 0, y1: 1 };
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    const x0 = Math.min(...xs);
    const x1 = Math.max(...xs);
    const y0 = Math.min(...ys);
    const y1 = Math.max(...ys);
    const padX = (x1 - x0) * 0.05 || 1;
    const padY = (y1 - y0) * 0.05 || 1;
    return { x0: x0 - padX, x1: x1 + padX, y0: y0 - padY, y1: y1 + padY };
  }, [points]);

  const [viewBox, setViewBox] = useState<ViewBox>({ x: 0, y: 0, w: 1, h: 1 });
  useEffect(() => {
    setViewBox({
      x: dataExtent.x0,
      y: dataExtent.y0,
      w: dataExtent.x1 - dataExtent.x0,
      h: dataExtent.y1 - dataExtent.y0,
    });
  }, [dataExtent]);

  const [box, setBox] = useState<BoxSel | null>(null);
  const dragRef = useRef<{ kind: "pan" | "box"; startX: number; startY: number; vb: ViewBox; moved: boolean } | null>(null);

  function load() {
    setLoading(true);
    return Promise.all([api.projection(), api.edges()])
      .then(([proj, e]) => {
        setPoints(proj.points);
        setEdges(e.edges);
      })
      .finally(() => setLoading(false));
  }
  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (!wrapRef.current) return;
    const obs = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      setSize({ w: r.width, h: r.height });
    });
    obs.observe(wrapRef.current);
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (paperId) setPaperId(null);
        else if (selected.size > 0) setSelected(new Set());
      }
      if (e.key.toLowerCase() === "s" && !e.metaKey && !e.ctrlKey) {
        const target = e.target as HTMLElement;
        if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return;
        setBoxMode((m) => !m);
      }
      if (e.key.toLowerCase() === "c" && !e.metaKey && !e.ctrlKey) {
        const target = e.target as HTMLElement;
        if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return;
        setShowCitations((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [paperId, selected.size]);

  // Index points by id, build neighbor sets from edges.
  const pointById = useMemo(() => {
    const m = new Map<string, ProjectionPoint>();
    for (const p of points) m.set(p.id, p);
    return m;
  }, [points]);

  const neighbors = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const e of edges) {
      if (!m.has(e.source)) m.set(e.source, new Set());
      if (!m.has(e.target)) m.set(e.target, new Set());
      m.get(e.source)!.add(e.target);
      m.get(e.target)!.add(e.source);
    }
    return m;
  }, [edges]);

  const degree = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of edges) {
      m.set(e.source, (m.get(e.source) ?? 0) + 1);
      m.set(e.target, (m.get(e.target) ?? 0) + 1);
    }
    return m;
  }, [edges]);

  function passes(p: ProjectionPoint): boolean {
    if (!statuses.has(p.status)) return false;
    if (titleQ && !p.title.toLowerCase().includes(titleQ.toLowerCase())) return false;
    return true;
  }

  function dataToScreen(p: { x: number; y: number }): { sx: number; sy: number } {
    const sx = ((p.x - viewBox.x) / viewBox.w) * size.w;
    const sy = ((p.y - viewBox.y) / viewBox.h) * size.h;
    return { sx, sy };
  }
  function screenToData(sx: number, sy: number): { x: number; y: number } {
    return {
      x: viewBox.x + (sx / size.w) * viewBox.w,
      y: viewBox.y + (sy / size.h) * viewBox.h,
    };
  }
  function svgPos(e: React.MouseEvent): { sx: number; sy: number } {
    const rect = svgRef.current!.getBoundingClientRect();
    return { sx: e.clientX - rect.left, sy: e.clientY - rect.top };
  }

  function onMouseDown(e: React.MouseEvent) {
    if (e.button !== 0) return;
    const { sx, sy } = svgPos(e);
    const useBox = boxMode || e.shiftKey;
    if (useBox) {
      setBox({ x0: sx, y0: sy, x1: sx, y1: sy });
      dragRef.current = { kind: "box", startX: sx, startY: sy, vb: viewBox, moved: false };
    } else {
      dragRef.current = { kind: "pan", startX: sx, startY: sy, vb: viewBox, moved: false };
    }
  }
  function onMouseMove(e: React.MouseEvent) {
    if (!dragRef.current) return;
    const { sx, sy } = svgPos(e);
    const dxScreen = sx - dragRef.current.startX;
    const dyScreen = sy - dragRef.current.startY;
    if (Math.abs(dxScreen) + Math.abs(dyScreen) > 3) dragRef.current.moved = true;
    if (dragRef.current.kind === "box") {
      setBox({ x0: dragRef.current.startX, y0: dragRef.current.startY, x1: sx, y1: sy });
    } else {
      const dx = dxScreen * (dragRef.current.vb.w / size.w);
      const dy = dyScreen * (dragRef.current.vb.h / size.h);
      setViewBox({
        ...dragRef.current.vb,
        x: dragRef.current.vb.x - dx,
        y: dragRef.current.vb.y - dy,
      });
    }
  }
  function onMouseUp() {
    if (!dragRef.current) return;
    if (dragRef.current.kind === "box" && box) {
      const minX = Math.min(box.x0, box.x1);
      const maxX = Math.max(box.x0, box.x1);
      const minY = Math.min(box.y0, box.y1);
      const maxY = Math.max(box.y0, box.y1);
      if (Math.abs(maxX - minX) > 4 && Math.abs(maxY - minY) > 4) {
        const a = screenToData(minX, minY);
        const b = screenToData(maxX, maxY);
        const x0 = Math.min(a.x, b.x);
        const x1 = Math.max(a.x, b.x);
        const y0 = Math.min(a.y, b.y);
        const y1 = Math.max(a.y, b.y);
        const next = new Set(selected);
        for (const p of points) {
          if (p.x >= x0 && p.x <= x1 && p.y >= y0 && p.y <= y1 && passes(p)) {
            next.add(p.id);
          }
        }
        setSelected(next);
      }
      setBox(null);
    }
    dragRef.current = null;
  }

  function onWheel(e: React.WheelEvent) {
    e.preventDefault();
    const { sx, sy } = svgPos(e);
    const k = e.deltaY > 0 ? 1.15 : 1 / 1.15;
    const cx = viewBox.x + (sx / size.w) * viewBox.w;
    const cy = viewBox.y + (sy / size.h) * viewBox.h;
    const newW = viewBox.w * k;
    const newH = viewBox.h * k;
    setViewBox({
      x: cx - (sx / size.w) * newW,
      y: cy - (sy / size.h) * newH,
      w: newW,
      h: newH,
    });
  }

  async function refresh() {
    setRefreshing(true);
    try {
      await api.refresh();
      await load();
    } finally {
      setRefreshing(false);
    }
  }

  async function bulkScreen(decision: ScreenStatus) {
    if (selected.size === 0) return;
    setBulkBusy(decision);
    try {
      await api.screenBatch(Array.from(selected), decision);
      setSelected(new Set());
      await load();
    } finally {
      setBulkBusy(null);
    }
  }

  const visible = points.filter(passes);
  const visibleSelectedCount = points.filter((p) => selected.has(p.id) && passes(p)).length;

  // Focus = hovered (transient) > opened-in-detail (sticky). When the cursor leaves
  // the canvas, the open paper's neighborhood stays lit so you can trace the citation
  // path you just clicked into.
  const focusId = hoverId ?? paperId;
  const focusNeighbors = focusId ? neighbors.get(focusId) ?? new Set<string>() : null;
  const dimOthers = focusNeighbors !== null && (focusNeighbors.size > 0 || focusId !== null);

  return (
    <div className="flex flex-1 min-w-0 min-h-0">
      <div className="w-64 border-r border-zinc-800 p-3 space-y-4 overflow-y-auto shrink-0">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">Filter</div>
          <Input
            placeholder="title contains…"
            value={titleQ}
            onChange={(e) => setTitleQ(e.target.value)}
          />
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">Status</div>
          <div className="space-y-1">
            {ALL_STATUSES.map((s) => {
              const on = statuses.has(s);
              const count = points.filter((p) => p.status === s).length;
              return (
                <label key={s} className="flex items-center justify-between px-2 py-1 rounded hover:bg-zinc-900 cursor-pointer">
                  <span className="flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={(e) => {
                        const next = new Set(statuses);
                        e.target.checked ? next.add(s) : next.delete(s);
                        setStatuses(next);
                      }}
                      className="accent-emerald-500"
                    />
                    <span className="size-2.5 rounded-full" style={{ background: STATUS_HEX[s] }} />
                    <span className="text-zinc-300">{s}</span>
                  </span>
                  <span className="text-zinc-600 text-xs">{count}</span>
                </label>
              );
            })}
          </div>
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">Layers</div>
          <label className="flex items-center justify-between px-2 py-1 rounded hover:bg-zinc-900 cursor-pointer">
            <span className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={showCitations}
                onChange={(e) => setShowCitations(e.target.checked)}
                className="accent-emerald-500"
              />
              <Network className="size-3 text-zinc-400" />
              <span className="text-zinc-300">citations</span>
            </span>
            <span className="text-zinc-600 text-xs">{edges.length}</span>
          </label>
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
            Node size
          </div>
          <select
            value={sizeBy}
            onChange={(e) => setSizeBy(e.target.value as SizeBy)}
            className="w-full h-8 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-xs text-zinc-200"
          >
            <option value="cites">citations (global)</option>
            <option value="links">in-corpus links</option>
            <option value="seen">seen across searches</option>
          </select>
          <div className="text-[10px] text-zinc-500 mt-1.5 leading-relaxed">
            Bigger = higher {SIZE_BY_LABEL[sizeBy]}.
          </div>
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">Tools</div>
          <div className="flex gap-1.5">
            <Button
              size="sm"
              variant={boxMode ? "default" : "outline"}
              className="flex-1"
              onClick={() => setBoxMode((m) => !m)}
              title="Press S to toggle"
            >
              {boxMode ? <Square className="size-3.5" /> : <MousePointer2 className="size-3.5" />}
              {boxMode ? "select" : "pan"}
            </Button>
          </div>
          <div className="text-[10px] text-zinc-500 mt-1.5 leading-relaxed">
            <kbd className="px-1 rounded bg-zinc-800 text-zinc-300">click</kbd> details ·{" "}
            <kbd className="px-1 rounded bg-zinc-800 text-zinc-300">⇧</kbd>+drag select
            <br />
            <kbd className="px-1 rounded bg-zinc-800 text-zinc-300">S</kbd> mode ·{" "}
            <kbd className="px-1 rounded bg-zinc-800 text-zinc-300">C</kbd> edges ·{" "}
            <kbd className="px-1 rounded bg-zinc-800 text-zinc-300">Esc</kbd> clear
          </div>
        </div>

        <div className="pt-2 border-t border-zinc-800">
          <Button size="sm" variant="secondary" className="w-full" onClick={refresh} disabled={refreshing}>
            {refreshing ? <Loader2 className="size-4 animate-spin" /> : <RotateCw className="size-4" />}
            refresh pipeline
          </Button>
        </div>

        <div className="text-[10px] text-zinc-600">
          showing {visible.length} of {points.length}
          {selected.size > 0 && (
            <span className="text-emerald-400"> · {visibleSelectedCount} selected</span>
          )}
        </div>
      </div>

      <div ref={wrapRef} className="flex-1 relative bg-zinc-950 min-w-0 min-h-0 overflow-hidden">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center text-zinc-500 text-sm z-10">
            <Loader2 className="size-4 animate-spin mr-2" /> loading projection…
          </div>
        )}
        {!loading && points.length === 0 && (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-zinc-500 text-sm gap-2 px-8 text-center z-10">
            <div>No projection yet.</div>
            <div className="text-xs">
              Run a search to add papers, then click <span className="text-zinc-300">refresh pipeline</span> to embed and project them.
            </div>
          </div>
        )}
        {!loading && points.length > 0 && (
          <svg
            ref={svgRef}
            width={size.w}
            height={size.h}
            style={{
              cursor: dragRef.current?.kind === "box" || boxMode ? "crosshair" : dragRef.current?.kind === "pan" ? "grabbing" : "grab",
              userSelect: "none",
              touchAction: "none",
            }}
            onMouseDown={onMouseDown}
            onMouseMove={(e) => {
              onMouseMove(e);
              setHoverPos({ x: e.clientX, y: e.clientY });
            }}
            onMouseUp={onMouseUp}
            onMouseLeave={onMouseUp}
            onWheel={onWheel}
          >
            <defs>
              <marker
                id="arrow-out"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="5"
                markerHeight="5"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#10b981" />
              </marker>
              <marker
                id="arrow-in"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="5"
                markerHeight="5"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#38bdf8" />
              </marker>
            </defs>

            {/* Citation edges layer */}
            {showCitations && (
              <g style={{ pointerEvents: "none" }}>
                {edges.map((e, i) => {
                  const a = pointById.get(e.source);
                  const b = pointById.get(e.target);
                  if (!a || !b) return null;
                  // Edge involves the focus paper if either endpoint is the focus.
                  const isFocusEdge = focusId === e.source || focusId === e.target;
                  const inSelection = selected.has(e.source) && selected.has(e.target);
                  if (dimOthers && !isFocusEdge) return null;
                  const A = dataToScreen(a);
                  const B = dataToScreen(b);
                  const mx = (A.sx + B.sx) / 2;
                  const my = (A.sy + B.sy) / 2;
                  const dx = B.sx - A.sx;
                  const dy = B.sy - A.sy;
                  const len = Math.hypot(dx, dy) || 1;
                  const cx = mx - (dy / len) * 6;
                  const cy = my + (dx / len) * 6;
                  // Direction-of-citation: source cites target, so arrow points at target.
                  // Color by relation to focus: green = focus cites this (outgoing); blue = this cites focus (incoming).
                  let stroke = "#52525b";
                  let markerEnd: string | undefined;
                  if (isFocusEdge) {
                    if (focusId === e.source) {
                      stroke = "#10b981"; // focus → other (outgoing)
                      markerEnd = "url(#arrow-out)";
                    } else {
                      stroke = "#38bdf8"; // other → focus (incoming)
                      markerEnd = "url(#arrow-in)";
                    }
                  } else if (inSelection) {
                    stroke = "#10b981";
                  }
                  // Pull the arrowhead back from the circle so it doesn't disappear inside the dot.
                  const targetRadius = radiusFor(b, sizeBy, degree.get(b.id) ?? 0);
                  const tx = B.sx - (dx / len) * (targetRadius + 4);
                  const ty = B.sy - (dy / len) * (targetRadius + 4);
                  return (
                    <path
                      key={i}
                      d={`M ${A.sx} ${A.sy} Q ${cx} ${cy} ${tx} ${ty}`}
                      fill="none"
                      stroke={stroke}
                      strokeWidth={isFocusEdge ? 1.4 : 0.7}
                      opacity={isFocusEdge ? 0.9 : 0.18}
                      markerEnd={markerEnd}
                    />
                  );
                })}
              </g>
            )}

            {/* Points */}
            {points.map((p) => {
              const visible_ = passes(p);
              const isSel = selected.has(p.id);
              const isHover = hoverId === p.id;
              const isFocus = focusId === p.id;
              const isNeighbor = focusNeighbors?.has(p.id) ?? false;
              const { sx, sy } = dataToScreen(p);
              const baseR = radiusFor(p, sizeBy, degree.get(p.id) ?? 0);
              const r = baseR + (isSel ? 1.5 : 0) + (isHover ? 2 : 0) + (isFocus && !isHover ? 1 : 0);
              const showHalo = isSel || isFocus;
              const dimmed = !visible_ || (dimOthers && !isFocus && !isNeighbor);
              return (
                <g key={p.id} style={{ pointerEvents: visible_ ? "auto" : "none" }}>
                  {showHalo && (
                    <circle
                      cx={sx}
                      cy={sy}
                      r={r + 5}
                      fill={isSel ? "#10b981" : STATUS_HEX[p.status]}
                      opacity={0.18}
                      pointerEvents="none"
                    />
                  )}
                  <circle
                    cx={sx}
                    cy={sy}
                    r={r}
                    fill={STATUS_HEX[p.status]}
                    fillOpacity={dimmed ? 0.08 : isSel ? 1 : 0.9}
                    stroke={paperId === p.id ? "#fff" : isSel ? "#10b981" : "transparent"}
                    strokeWidth={paperId === p.id ? 2 : isSel ? 1.5 : 0}
                    style={{ cursor: visible_ ? "pointer" : "default" }}
                    onMouseEnter={() => setHoverId(p.id)}
                    onMouseLeave={() => setHoverId(null)}
                    onClick={(e) => {
                      e.stopPropagation();
                      // Don't fire click after a drag (ambient mouseup).
                      if (dragRef.current?.moved) return;
                      if (e.shiftKey) {
                        const next = new Set(selected);
                        next.has(p.id) ? next.delete(p.id) : next.add(p.id);
                        setSelected(next);
                      } else {
                        setPaperId(p.id);
                      }
                    }}
                  />
                </g>
              );
            })}

            {box && (
              <rect
                x={Math.min(box.x0, box.x1)}
                y={Math.min(box.y0, box.y1)}
                width={Math.abs(box.x1 - box.x0)}
                height={Math.abs(box.y1 - box.y0)}
                fill="#10b981"
                fillOpacity={0.08}
                stroke="#10b981"
                strokeWidth={1}
                strokeDasharray="4 3"
                pointerEvents="none"
              />
            )}
          </svg>
        )}

        {hoverId && hoverPos && !dragRef.current && (() => {
          const p = pointById.get(hoverId);
          if (!p) return null;
          const nCount = neighbors.get(hoverId)?.size ?? 0;
          return (
            <div
              className="pointer-events-none fixed z-20 max-w-sm rounded-md border border-zinc-700 bg-zinc-900/95 px-3 py-2 text-xs shadow-xl"
              style={{ left: hoverPos.x + 12, top: hoverPos.y + 12 }}
            >
              <div className="font-medium text-zinc-100 mb-0.5 leading-snug">{p.title}</div>
              <div className="text-zinc-500 text-[11px]">
                {[
                  p.year,
                  p.cited_by_count != null ? `${p.cited_by_count} cites` : null,
                  p.venue,
                  p.status,
                  showCitations && nCount > 0 ? `${nCount} link${nCount === 1 ? "" : "s"} here` : null,
                ].filter(Boolean).join(" · ")}
              </div>
            </div>
          );
        })()}

        {/* Direction legend, only when something is focused and edges are on */}
        {showCitations && focusId && (
          <div className="absolute top-3 left-3 flex items-center gap-3 rounded-md border border-zinc-700 bg-zinc-900/90 px-3 py-1.5 text-[11px] backdrop-blur-sm">
            <span className="flex items-center gap-1.5 text-zinc-300">
              <svg width="22" height="8">
                <line x1="0" y1="4" x2="16" y2="4" stroke="#10b981" strokeWidth="1.4" />
                <path d="M 16 1 L 22 4 L 16 7 z" fill="#10b981" />
              </svg>
              cites →
            </span>
            <span className="flex items-center gap-1.5 text-zinc-300">
              <svg width="22" height="8">
                <line x1="0" y1="4" x2="16" y2="4" stroke="#38bdf8" strokeWidth="1.4" />
                <path d="M 16 1 L 22 4 L 16 7 z" fill="#38bdf8" />
              </svg>
              cited by ←
            </span>
          </div>
        )}

        {selected.size > 0 && (
          <div className={cn(
            "absolute bottom-3 left-1/2 -translate-x-1/2 flex items-center gap-2",
            "rounded-lg border border-zinc-700 bg-zinc-900/95 px-3 py-2 shadow-xl backdrop-blur-sm z-20",
          )}>
            <span className="text-sm text-zinc-200 mr-1">
              {selected.size} selected
            </span>
            <Button size="xs" variant="success" disabled={bulkBusy !== null}
                    onClick={() => bulkScreen("included")}>include</Button>
            <Button size="xs" variant="secondary" disabled={bulkBusy !== null}
                    onClick={() => bulkScreen("maybe")}>maybe</Button>
            <Button size="xs" variant="destructive" disabled={bulkBusy !== null}
                    onClick={() => bulkScreen("excluded")}>exclude</Button>
            <Button size="xs" variant="ghost" disabled={bulkBusy !== null}
                    onClick={() => bulkScreen("candidate")}>reset</Button>
            <span className="w-px h-4 bg-zinc-700 mx-1" />
            <Button size="xs" variant="ghost" onClick={() => setSelected(new Set())}>
              clear
            </Button>
          </div>
        )}
      </div>

      {(paperId || selected.size > 0) && (
        <div className="w-96 shrink-0">
          {paperId ? (
            <PaperDetail
              paperId={paperId}
              onClose={() => setPaperId(null)}
              onChange={() => load()}
            />
          ) : (
            <SelectionPanel
              papers={points.filter((p) => selected.has(p.id))}
              paperId={paperId}
              onSelect={(id) => setPaperId(id)}
              onClose={() => setSelected(new Set())}
            />
          )}
        </div>
      )}
    </div>
  );
}
