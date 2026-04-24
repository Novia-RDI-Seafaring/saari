"""Corpus visualizations: Obsidian .canvas + self-contained HTML scatter.

Reads from paper + paper_projection. The user runs `saari project` (or we do
it implicitly) to compute 2D UMAP coords, then `saari canvas` to write the
artifact. No heavy deps in this module — just the stdlib + the DB.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saari import db, paths
from saari.models import Paper

# Obsidian JSON Canvas color slots (1-6). Custom hex also allowed.
_STATUS_COLOR = {
    "included": "4",  # green
    "maybe": "3",     # yellow
    "excluded": "1",  # red
    "candidate": None,  # default (no color)
}

# HTML palette (for the SPA-lite viewer)
_STATUS_HEX = {
    "included": "#16a34a",  # green
    "maybe": "#eab308",     # yellow
    "excluded": "#dc2626",  # red
    "candidate": "#94a3b8", # slate
}


@dataclass
class _Point:
    paper: Paper
    x: float  # raw projection coord
    y: float


def _load_points(
    status: str | None, project_root: Path | None
) -> list[_Point]:
    root = project_root or paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        sql = (
            "SELECT p.*, pp.x, pp.y, "
            "(SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
            "FROM paper p JOIN paper_projection pp ON pp.paper_id = p.id"
        )
        args: list[Any] = []
        if status:
            sql += " WHERE p.status = ?"
            args.append(status)
        rows = con.execute(sql, args).fetchall()
    pts: list[_Point] = []
    for r in rows:
        paper = db._row_to_paper(r)
        pts.append(_Point(paper=paper, x=float(r["x"]), y=float(r["y"])))
    return pts


def _normalize(
    points: list[_Point], width: int, height: int, margin: int = 120
) -> list[tuple[float, float]]:
    if not points:
        return []
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    avail_w = width - 2 * margin
    avail_h = height - 2 * margin
    return [
        (
            margin + (p.x - min_x) / span_x * avail_w,
            margin + (p.y - min_y) / span_y * avail_h,
        )
        for p in points
    ]


def _abstract_excerpt(paper: Paper, n: int = 240) -> str:
    if not paper.abstract:
        return ""
    s = " ".join(paper.abstract.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _node_size(paper: Paper) -> tuple[int, int]:
    """Width scales gently with citations; height fixed. Never overlap-proof, but stable."""
    base_w = 300
    bonus = int(min(140, math.log10((paper.cited_by_count or 0) + 1) * 40))
    return base_w + bonus, 170


# ---------- Obsidian JSON Canvas ----------


def write_obsidian_canvas(
    out: Path,
    status: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Write an Obsidian JSON Canvas from the current projection."""
    points = _load_points(status, project_root)
    if not points:
        return {"n_nodes": 0, "path": str(out), "note": "No projected papers. Run `saari project` first."}

    canvas_w, canvas_h = 4800, 3400
    coords = _normalize(points, canvas_w, canvas_h)

    nodes = []
    for pt, (cx, cy) in zip(points, coords):
        p = pt.paper
        w, h = _node_size(p)
        body_lines = [f"# {p.title}"]
        meta_bits = []
        if p.year is not None:
            meta_bits.append(str(p.year))
        if p.cited_by_count is not None:
            meta_bits.append(f"cited={p.cited_by_count}")
        if p.venue:
            meta_bits.append(f"*{p.venue}*")
        if meta_bits:
            body_lines.append(" · ".join(meta_bits))
        flag_bits = []
        if p.status != "candidate":
            flag_bits.append(f"**{p.status}**")
        if p.oa_status and p.oa_status != "closed":
            flag_bits.append(p.oa_status)
        if p.arxiv_id:
            flag_bits.append("arxiv")
        if p.pmcid:
            flag_bits.append("pmc")
        if p.seen_in > 1:
            flag_bits.append(f"seen_in={p.seen_in}")
        if flag_bits:
            body_lines.append(" · ".join(flag_bits))
        excerpt = _abstract_excerpt(p, n=200)
        if excerpt:
            body_lines.append("")
            body_lines.append(f"> {excerpt}")
        body_lines.append("")
        body_lines.append(f"`{p.id}`")

        node: dict[str, Any] = {
            "id": p.id.replace(":", "-"),
            "type": "text",
            "text": "\n".join(body_lines),
            "x": int(cx - w / 2),
            "y": int(cy - h / 2),
            "width": w,
            "height": h,
        }
        color = _STATUS_COLOR.get(p.status)
        if color is not None:
            node["color"] = color
        nodes.append(node)

    canvas = {"nodes": nodes, "edges": []}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(canvas, indent=2, ensure_ascii=False))
    return {
        "n_nodes": len(nodes),
        "path": str(out),
        "size_kb": round(out.stat().st_size / 1024, 1),
    }


# ---------- Self-contained HTML viewer ----------


def write_html_viewer(
    out: Path,
    status: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Write a standalone HTML file: SVG scatter, hover tooltip, status filter.

    Zero external dependencies at view time — just open in a browser.
    """
    points = _load_points(status, project_root)
    if not points:
        return {"n_nodes": 0, "path": str(out), "note": "No projected papers. Run `saari project` first."}

    # Build per-point data for embedding as JSON in the HTML
    data = []
    for pt in points:
        p = pt.paper
        data.append(
            {
                "id": p.id,
                "title": p.title,
                "year": p.year,
                "cited": p.cited_by_count,
                "venue": p.venue,
                "status": p.status,
                "oa": p.oa_status,
                "arxiv": p.arxiv_id,
                "doi": p.doi,
                "seen_in": p.seen_in,
                "abstract_excerpt": _abstract_excerpt(p, n=400),
                "x": pt.x,
                "y": pt.y,
                "url": p.landing_page_url or (p.pdf_url or ""),
            }
        )

    payload = json.dumps(data, ensure_ascii=False)
    palette = json.dumps(_STATUS_HEX)

    template = _HTML_TEMPLATE.replace("__PAYLOAD__", payload).replace(
        "__PALETTE__", palette
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(template)
    return {
        "n_nodes": len(data),
        "path": str(out),
        "size_kb": round(out.stat().st_size / 1024, 1),
    }


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>saari — corpus landscape</title>
<style>
  :root {
    --bg: #0b0e12;
    --fg: #e2e8f0;
    --dim: #64748b;
    --panel: rgba(20, 25, 33, 0.92);
    --border: #1f2937;
  }
  html, body { margin: 0; padding: 0; height: 100%; background: var(--bg); color: var(--fg); font-family: -apple-system, BlinkMacSystemFont, 'Inter', system-ui, sans-serif; }
  #root { display: grid; grid-template-columns: 300px 1fr; height: 100vh; }
  #sidebar { padding: 16px; border-right: 1px solid var(--border); overflow-y: auto; }
  #sidebar h1 { margin: 0 0 8px 0; font-size: 16px; }
  #sidebar .meta { color: var(--dim); font-size: 12px; margin-bottom: 16px; }
  .filter { margin-bottom: 8px; }
  .filter label { display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; padding: 4px 6px; border-radius: 4px; }
  .filter label:hover { background: #141a22; }
  .filter .swatch { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  #query { width: 100%; padding: 6px 8px; background: #0f141b; color: var(--fg); border: 1px solid var(--border); border-radius: 4px; font-size: 13px; box-sizing: border-box; margin-bottom: 12px; }
  #detail { margin-top: 16px; padding: 12px; background: #0f141b; border-radius: 6px; border: 1px solid var(--border); font-size: 13px; max-height: 60vh; overflow-y: auto; }
  #detail h2 { margin: 0 0 6px 0; font-size: 14px; }
  #detail .meta2 { color: var(--dim); font-size: 11px; margin-bottom: 8px; }
  #detail .excerpt { color: #cbd5e1; line-height: 1.4; font-size: 12px; white-space: pre-wrap; }
  #detail a { color: #7dd3fc; text-decoration: none; font-size: 11px; }
  #canvas-wrap { position: relative; overflow: hidden; }
  svg { display: block; width: 100%; height: 100%; cursor: grab; }
  svg.dragging { cursor: grabbing; }
  circle { cursor: pointer; transition: stroke-width 0.1s; }
  circle:hover { stroke: #fff; stroke-width: 2; }
  circle.selected { stroke: #fff; stroke-width: 3; }
  circle.dimmed { opacity: 0.15; }
  #tooltip { position: absolute; pointer-events: none; background: var(--panel); border: 1px solid var(--border); padding: 8px 10px; border-radius: 6px; font-size: 12px; max-width: 340px; display: none; z-index: 10; }
  #tooltip .ttitle { font-weight: 600; margin-bottom: 2px; }
  #tooltip .tmeta { color: var(--dim); font-size: 11px; }
</style>
</head>
<body>
<div id="root">
  <div id="sidebar">
    <h1>saari corpus</h1>
    <div class="meta" id="stats"></div>
    <input id="query" type="text" placeholder="title filter…" />
    <div id="filters"></div>
    <div id="detail" style="display: none;"></div>
  </div>
  <div id="canvas-wrap">
    <svg id="viz" viewBox="0 0 1000 800" preserveAspectRatio="xMidYMid meet"></svg>
    <div id="tooltip"></div>
  </div>
</div>
<script>
const DATA = __PAYLOAD__;
const PALETTE = __PALETTE__;

const svg = document.getElementById('viz');
const tooltip = document.getElementById('tooltip');
const wrap = document.getElementById('canvas-wrap');
const detail = document.getElementById('detail');
const statsEl = document.getElementById('stats');

// Normalize to 0..1000 x 0..800 with margin
const MARGIN = 40;
const W = 1000, H = 800;
const xs = DATA.map(d => d.x), ys = DATA.map(d => d.y);
const minX = Math.min(...xs), maxX = Math.max(...xs);
const minY = Math.min(...ys), maxY = Math.max(...ys);
const spanX = Math.max(maxX - minX, 1e-9), spanY = Math.max(maxY - minY, 1e-9);
function xPx(d) { return MARGIN + (d.x - minX) / spanX * (W - 2*MARGIN); }
function yPx(d) { return MARGIN + (d.y - minY) / spanY * (H - 2*MARGIN); }
function radius(d) { return 4 + Math.log10((d.cited || 0) + 1) * 2.5; }

// State
const active = { statuses: new Set(['included', 'maybe', 'candidate', 'excluded']), q: '' };

function passes(d) {
  if (!active.statuses.has(d.status)) return false;
  if (active.q && !d.title.toLowerCase().includes(active.q.toLowerCase())) return false;
  return true;
}

function render() {
  svg.innerHTML = '';
  const nShown = DATA.filter(passes).length;
  statsEl.textContent = `${nShown} of ${DATA.length} shown · UMAP 384→2`;
  for (const d of DATA) {
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('cx', xPx(d));
    c.setAttribute('cy', yPx(d));
    c.setAttribute('r', radius(d));
    c.setAttribute('fill', PALETTE[d.status] || PALETTE.candidate);
    c.setAttribute('fill-opacity', 0.85);
    c.dataset.id = d.id;
    if (!passes(d)) c.classList.add('dimmed');
    c.addEventListener('mouseenter', (e) => showTip(d, e));
    c.addEventListener('mousemove', moveTip);
    c.addEventListener('mouseleave', hideTip);
    c.addEventListener('click', () => select(d));
    svg.appendChild(c);
  }
}

function showTip(d, e) {
  const metaBits = [];
  if (d.year) metaBits.push(d.year);
  if (d.cited != null) metaBits.push('cited=' + d.cited);
  if (d.venue) metaBits.push(d.venue);
  tooltip.innerHTML = `<div class="ttitle">${escapeHtml(d.title)}</div>
    <div class="tmeta">${metaBits.join(' · ')} · <strong>${d.status}</strong></div>`;
  tooltip.style.display = 'block';
  moveTip(e);
}
function moveTip(e) {
  const rect = wrap.getBoundingClientRect();
  tooltip.style.left = (e.clientX - rect.left + 12) + 'px';
  tooltip.style.top = (e.clientY - rect.top + 12) + 'px';
}
function hideTip() { tooltip.style.display = 'none'; }

function select(d) {
  document.querySelectorAll('circle.selected').forEach(el => el.classList.remove('selected'));
  const c = svg.querySelector(`circle[data-id="${d.id.replaceAll('"', '\\\\"')}"]`);
  if (c) c.classList.add('selected');
  const meta = [];
  if (d.year) meta.push(d.year);
  if (d.cited != null) meta.push('cited=' + d.cited);
  if (d.venue) meta.push(d.venue);
  if (d.oa) meta.push('oa=' + d.oa);
  const idLink = d.url ? `<a href="${d.url}" target="_blank">open →</a>` : '';
  detail.style.display = 'block';
  detail.innerHTML = `<h2>${escapeHtml(d.title)}</h2>
    <div class="meta2">${meta.join(' · ')} · status=${d.status}${d.seen_in > 1 ? ' · seen_in=' + d.seen_in : ''}</div>
    <div class="excerpt">${escapeHtml(d.abstract_excerpt || '(no abstract)')}</div>
    <div style="margin-top:8px;"><code style="font-size:10px;">${d.id}</code> ${idLink}</div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
}

// Filters
const statuses = ['included', 'maybe', 'candidate', 'excluded'];
const filtersEl = document.getElementById('filters');
for (const s of statuses) {
  const count = DATA.filter(d => d.status === s).length;
  const label = document.createElement('label');
  label.innerHTML = `<input type="checkbox" checked data-status="${s}" />
    <span class="swatch" style="background:${PALETTE[s]}"></span>
    ${s} <span style="color:var(--dim);margin-left:auto;">${count}</span>`;
  label.style.display = 'flex';
  label.style.justifyContent = 'space-between';
  filtersEl.appendChild(label);
}
filtersEl.addEventListener('change', (e) => {
  const s = e.target.dataset.status;
  if (e.target.checked) active.statuses.add(s); else active.statuses.delete(s);
  render();
});

document.getElementById('query').addEventListener('input', (e) => {
  active.q = e.target.value;
  render();
});

// Pan/zoom (simple)
let vb = { x: 0, y: 0, w: W, h: H };
svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
let isPanning = false, lastX = 0, lastY = 0;
svg.addEventListener('mousedown', (e) => {
  if (e.target === svg) { isPanning = true; svg.classList.add('dragging'); lastX = e.clientX; lastY = e.clientY; }
});
window.addEventListener('mouseup', () => { isPanning = false; svg.classList.remove('dragging'); });
svg.addEventListener('mousemove', (e) => {
  if (!isPanning) return;
  const dx = (e.clientX - lastX) * vb.w / svg.clientWidth;
  const dy = (e.clientY - lastY) * vb.h / svg.clientHeight;
  vb.x -= dx; vb.y -= dy;
  lastX = e.clientX; lastY = e.clientY;
  svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
});
svg.addEventListener('wheel', (e) => {
  e.preventDefault();
  const k = e.deltaY > 0 ? 1.1 : 0.9;
  const rect = svg.getBoundingClientRect();
  const mx = vb.x + (e.clientX - rect.left) * vb.w / rect.width;
  const my = vb.y + (e.clientY - rect.top) * vb.h / rect.height;
  vb.w *= k; vb.h *= k;
  vb.x = mx - (e.clientX - rect.left) * vb.w / rect.width;
  vb.y = my - (e.clientY - rect.top) * vb.h / rect.height;
  svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
}, { passive: false });

render();
</script>
</body>
</html>
"""
