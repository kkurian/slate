#!/usr/bin/env python3
"""slate — a web view of a task tracker kept in plain markdown.

The tracker lives in plain markdown (project.md + issues/*.md). This file is a
*viewer* with two deliberate write paths: dragging issue rows within a status
view to reorder them (Esc cancels) rewrites the `order:` frontmatter of the
affected issues, and picking a state from an issue's status chip rewrites its
`status:`. Everything else is read-only; if the viewer ever breaks, every file
is still readable in any editor or on GitHub. Python 3 standard library only —
no pip, no npm, no build step.

The live server also shows agent presence: it watches Claude Code's session
transcripts (under ~/.claude/projects/<slug>/, including workflow subagents;
override with SLATE_TRANSCRIPTS) and marks the issue files an active agent is
touching. Display-only, never written back.

Usage:
    python3 slate.py            # live server at http://localhost:8787
    python3 slate.py serve      # same
    python3 slate.py build out  # write standalone HTML into ./out/
    python3 slate.py install    # make the host repo's agent aware of slate

Env:
    SLATE_PORT   override the port (default 8787)
"""

import html
import json
import os
import re
import sys
import time
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # the plan/ directory
ISSUES = ROOT / "issues"
TEMPLATES = ROOT / "templates"
PORT = int(os.environ.get("SLATE_PORT", "8787"))

# 'live' (server) generates /issue/<id> links; 'static' (build) generates <id>.html
MODE = "live"

# Sidebar nav order, mirroring Linear's workflow states.
STATUS_ORDER = ["In Progress", "In Review", "Todo", "Backlog", "Done", "Canceled"]
# Lifecycle order — how the status menu lists them.
LIFECYCLE = ["Backlog", "Todo", "In Progress", "In Review", "Done", "Canceled"]
ALWAYS_SHOW = {"In Progress", "Todo", "Backlog"}


# --------------------------------------------------------------------------- #
# Markdown parsing (small, focused on the markdown we actually write)
# --------------------------------------------------------------------------- #

def parse_doc(text):
    """Split optional YAML-ish frontmatter from the body. Returns (meta, body)."""
    meta = {}
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        end = None
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                end = j
                break
        if end is not None:
            for line in lines[1:end]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    k, v = k.strip(), v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                        v = v[1:-1]          # drop one pair of matching YAML quotes
                    if v.startswith("[") and v.endswith("]"):
                        v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
                    meta[k] = v
            return meta, "\n".join(lines[end + 1:])
    return meta, text


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


def url_for(kind, ident=None):
    if MODE == "static":
        if kind == "project":
            return "index.html"
        if kind == "status":
            return f"status-{slug(ident)}.html"
        if kind == "waves":
            return "waves.html"
        return f"{ident}.html"
    if kind == "project":
        return "/"
    if kind == "status":
        return f"/status/{slug(ident)}"
    if kind == "waves":
        return "/waves"
    return f"/issue/{ident}"


def render_inline(s):
    s = html.escape(s, quote=False)
    code = []

    def stash(m):
        code.append(m.group(1))
        return f"\x00{len(code) - 1}\x00"

    s = re.sub(r"`([^`]+)`", stash, s)
    # [[wikilink]] or [[ID|label]] -> internal issue link
    s = re.sub(
        r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
        lambda m: f'<a class="wl" href="{url_for("issue", m.group(1).strip())}">'
                  f'{(m.group(2) or m.group(1)).strip()}</a>',
        s,
    )
    # [text](url)
    s = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        s,
    )
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{code[int(m.group(1))]}</code>", s)
    return s


def split_row(r):
    r = r.strip()
    if r.startswith("|"):
        r = r[1:]
    if r.endswith("|"):
        r = r[:-1]
    return [c.strip() for c in r.split("|")]


def render_table(rows):
    head = split_row(rows[0])
    out = ["<table><thead><tr>"]
    out += [f"<th>{render_inline(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for r in rows[2:]:
        out.append("<tr>" + "".join(f"<td>{render_inline(c)}</td>" for c in split_row(r)) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def render_list(lines):
    parsed = []  # [indent, ordered(bool), checked(None|bool), [content lines]]
    for ln in lines:
        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", ln)
        if m:
            indent = len(m.group(1).expandtabs(4))
            ordered = bool(re.match(r"\d+\.", m.group(2)))
            content = m.group(3)
            checked = None
            cm = re.match(r"\[([ xX])\]\s+(.*)$", content)
            if cm:
                checked = cm.group(1).lower() == "x"
                content = cm.group(2)
            parsed.append([indent, ordered, checked, [content]])
        elif parsed:
            parsed[-1][3].append(ln.strip())
    if not parsed:
        return ""
    return _emit_list(parsed, 0, parsed[0][0])[0]


def _emit_list(parsed, pos, level):
    tag = "ol" if parsed[pos][1] else "ul"
    out = [f"<{tag}>"]
    while pos < len(parsed):
        indent, _, checked, content = parsed[pos]
        if indent < level:
            break
        text = render_inline(" ".join(content).strip())
        if checked is not None:
            box = "checked" if checked else ""
            li_open = '<li class="task">'
            text = f'<input type="checkbox" disabled {box}> {text}'
        else:
            li_open = "<li>"
        pos += 1
        child = ""
        if pos < len(parsed) and parsed[pos][0] > level:
            child, pos = _emit_list(parsed, pos, parsed[pos][0])
        out.append(li_open + text + child + "</li>")
    out.append(f"</{tag}>")
    return "\n".join(out), pos


def render_blocks(text):
    lines = text.split("\n")
    out, para, i, n = [], [], 0, len(lines)

    def flush():
        nonlocal para
        if para:
            out.append("<p>" + render_inline(" ".join(para).strip()) + "</p>")
            para = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue
        if not stripped:
            flush()
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", stripped)
        if m:
            flush()
            lvl, txt = len(m.group(1)), m.group(2).strip()
            out.append(f'<h{lvl} id="{slug(txt)}">{render_inline(txt)}</h{lvl}>')
            i += 1
            continue
        if re.match(r"^(\*\s*){3,}$|^(-\s*){3,}$|^(_\s*){3,}$", stripped):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith(">"):
            flush()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + render_blocks("\n".join(quote)) + "</blockquote>")
            continue
        if ("|" in line and i + 1 < n
                and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]) and "-" in lines[i + 1]):
            flush()
            tbl = []
            while i < n and "|" in lines[i] and lines[i].strip():
                tbl.append(lines[i])
                i += 1
            out.append(render_table(tbl))
            continue
        if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
            flush()
            lst = []
            while i < n and (re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i])
                             or (lines[i].strip() and lines[i][:1] in (" ", "\t"))):
                lst.append(lines[i])
                i += 1
            out.append(render_list(lst))
            continue
        para.append(stripped)
        i += 1
    flush()
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Issue model
# --------------------------------------------------------------------------- #

def _id_key(item):
    m = re.match(r"([A-Za-z]+)-?(\d+)", item["id"])
    return (m.group(1), int(m.group(2))) if m else (item["id"], 0)


def _sort_key(item):
    # Issues with an explicit `order` come first (ascending); the rest follow in id order.
    try:
        return (0, int(item["order"]), _id_key(item))
    except (TypeError, ValueError):
        return (1, 0, _id_key(item))


def _wave_sort_key(value):
    # Numeric waves first, ascending (wave 2 before wave 10); string waves after,
    # alphabetical. Grouping is by the raw value, so the key only orders the groups.
    s = str(value).strip()
    try:
        return (0, float(s), "")
    except ValueError:
        return (1, 0.0, s.lower())


def list_issues():
    items = []
    if ISSUES.exists():
        for p in ISSUES.glob("*.md"):
            meta, _ = parse_doc(p.read_text(encoding="utf-8"))
            items.append({
                "id": meta.get("id", p.stem),
                "title": meta.get("title", p.stem),
                "status": meta.get("status", "Backlog"),
                "priority": meta.get("priority", "No priority"),
                "order": meta.get("order"),
                "wave": meta.get("wave"),
                "assignee": meta.get("assignee", ""),
                "updated": meta.get("updated", ""),
            })
    items.sort(key=_sort_key)
    return items


def find_issue(issue_id):
    if not ISSUES.exists():
        return None
    for p in ISSUES.glob("*.md"):
        meta, body = parse_doc(p.read_text(encoding="utf-8"))
        if meta.get("id") == issue_id or p.stem == issue_id:
            return p, meta, body
    return None


def project_title():
    path = ROOT / "project.md"
    if path.exists():
        meta, _ = parse_doc(path.read_text(encoding="utf-8"))
        if meta.get("title"):
            return meta["title"]
    return "slate"


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #

CSS = """
:root{
  --bg:#0e0e10;--sidebar:#0b0b0d;--panel:#161618;--hover:rgba(255,255,255,.045);
  --active:rgba(255,255,255,.08);--line:rgba(255,255,255,.07);
  --ink:#ededf0;--mut:#888b94;--faint:#5c5f66;--accent:#7d87e0;--code:rgba(255,255,255,.07)}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--ink);
  font:13.5px/1.6 "Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--ink);text-decoration:none}
.ico{flex:none;vertical-align:-3px}
.logo{width:18px;height:18px;border-radius:5px;flex:none;
  background:linear-gradient(135deg,#7d87e0,#a64ce8)}
.layout{display:grid;grid-template-columns:var(--sbw,220px) minmax(0,1fr) 244px;min-height:100vh}
.layout.no-props{grid-template-columns:var(--sbw,220px) minmax(0,1fr)}
.sidebar{background:var(--sidebar);border-right:1px solid var(--line);padding:14px 8px 52px;
  position:sticky;top:0;height:100vh;overflow:auto}
.sash{position:fixed;top:0;bottom:0;left:calc(var(--sbw,220px) - 1px);width:5px;
  cursor:col-resize;z-index:30}
.sash:hover,.sash.on{background:rgba(255,255,255,.08)}
.brand{display:flex;align-items:center;gap:9px;font-weight:600;color:var(--ink);padding:6px 8px 16px}
.nav-row{display:flex;align-items:center;gap:9px;padding:4px 8px;border-radius:6px;
  color:var(--mut);font-size:13px;font-weight:500;line-height:1.5}
.nav-row:hover{background:var(--hover);color:var(--ink)}
.nav-row.active{background:var(--active);color:var(--ink)}
.nav-row .n{margin-left:auto;color:var(--faint);font-weight:500;font-size:12px;
  font-variant-numeric:tabular-nums}
.group-h{display:flex;align-items:center;gap:9px;padding:5px 8px;font-size:12px;font-weight:600;color:var(--mut)}
.item{position:relative;display:flex;align-items:center;gap:9px;padding:4px 8px;border-radius:6px;color:var(--ink);font-size:13px;line-height:1.5}
.item:hover{background:var(--hover)}
.item:hover .ititle{color:var(--ink)}
.item.dragging{opacity:.45}
.grip{position:absolute;left:-14px;top:50%;transform:translateY(-50%);display:flex;
  align-items:center;color:#6e7178;opacity:0;cursor:grab;transition:opacity .12s}
.item:hover .grip{opacity:.75}
.item.dragging .grip{cursor:grabbing}
.iid{color:var(--faint);font-variant-numeric:tabular-nums;flex:none;font-size:12.5px}
.ititle{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#c4c6cc}
.idate{margin-left:auto;flex:none;padding-left:14px;color:var(--faint);font-size:12.5px;
  font-variant-numeric:tabular-nums}
.idate .k{opacity:.7}
.av{width:17px;height:17px;border-radius:50%;flex:none;display:inline-flex;
  align-items:center;justify-content:center;font-size:9px;font-weight:600;
  color:rgba(255,255,255,.92);letter-spacing:.02em;line-height:1}
.item .av{margin-left:auto}
.item .av+.idate{margin-left:0;padding-left:12px}
.content{padding:42px 60px 64px;max-width:860px;display:flex;flex-direction:column}
.view-head h1{display:flex;align-items:center;gap:9px;margin:0 0 18px;
  font-size:18px;font-weight:600;letter-spacing:-.01em}
.view-head .vcount{color:var(--faint);font-weight:500;font-size:14px}
.list{display:flex;flex-direction:column;margin:0 -8px}
section.active{margin:6px 0 10px}
section.active .group-h{padding-left:0}
.empty{margin:2px 0;font-size:13px;color:var(--faint)}
.pulse{width:8px;height:8px;border-radius:50%;background:#4cb782;flex:none;
  animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.agents-live{display:flex;align-items:center;gap:8px;padding:6px 8px;margin-top:12px;
  font-size:12px;color:var(--mut)}
.badge.live{border-color:rgba(76,183,130,.35)}
@media (max-width:700px){
  .content{padding:32px 22px}
  .idate{display:none}
}
.foot{position:fixed;bottom:0;left:0;right:0;z-index:20;padding:10px 0;
  background:var(--bg);border-top:1px solid var(--line);
  font-size:12px;color:var(--faint);text-align:center}
.foot a{color:var(--mut)}
.foot a:hover{color:var(--ink)}
.props{background:var(--sidebar);border-left:1px solid var(--line);padding:28px 22px 52px;
  position:sticky;top:0;height:100vh;overflow:auto}
.props h3{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 14px;font-weight:600}
.props-dl{display:grid;grid-template-columns:82px 1fr;gap:12px;margin:0;font-size:13px;align-items:center}
.props-dl dt{color:var(--mut)}
.props-dl dd{margin:0;color:var(--ink);display:flex;align-items:center;gap:7px}
.issue-head{margin-bottom:28px}
.issue-head .crumb{color:var(--faint);font-size:12.5px;font-variant-numeric:tabular-nums}
.issue-head h1{margin:6px 0 14px;font-size:24px;font-weight:600;letter-spacing:-.012em}
.badges{display:flex;gap:8px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:500;
  padding:4px 11px 4px 8px;border-radius:7px;border:1px solid var(--line);background:var(--panel)}
.badge-menu{position:relative}
.badge-menu>summary.badge{cursor:pointer;list-style:none;user-select:none}
.badge-menu>summary.badge::-webkit-details-marker{display:none}
.badge-menu>summary.badge:hover{border-color:var(--mut)}
.menu{position:absolute;top:calc(100% + 6px);left:0;z-index:20;min-width:170px;
  display:flex;flex-direction:column;padding:4px;background:var(--panel);
  border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 28px rgba(0,0,0,.45)}
.menu-item{display:flex;align-items:center;gap:9px;padding:6px 10px;border:0;background:none;
  color:var(--ink);font:inherit;font-size:13px;border-radius:6px;cursor:pointer;text-align:left}
.menu-item:hover{background:var(--hover)}
.menu-item.sel{background:var(--active)}
.md{font-size:14.5px;line-height:1.7;color:#cfd1d7}
.md h1,.md h2,.md h3{color:var(--ink);line-height:1.3;letter-spacing:-.01em}
.md h2{margin-top:34px;font-size:17px;border-bottom:1px solid var(--line);padding-bottom:7px}
.md h3{font-size:15px;margin-top:24px}
.md a{color:var(--accent)}.md a:hover{text-decoration:underline}
.md code{background:var(--code);padding:1.5px 5px;border-radius:5px;font-size:12.5px;
  font-family:"SF Mono",ui-monospace,Menlo,monospace}
.md pre{background:var(--panel);border:1px solid var(--line);padding:14px 16px;border-radius:9px;overflow:auto}
.md pre code{background:none;padding:0}
.md table{border-collapse:collapse;width:100%;margin:16px 0;font-size:13.5px}
.md th,.md td{border:1px solid var(--line);padding:8px 11px;text-align:left;vertical-align:top}
.md th{background:var(--panel);font-weight:600;color:var(--ink)}
.md blockquote{border-left:2px solid var(--line);margin:14px 0;padding:2px 16px;color:var(--mut)}
.md li{margin:3px 0}
.md li.task{list-style:none;margin-left:-22px}
.md li.task input{margin-right:8px;accent-color:var(--accent)}
.md hr{border:none;border-top:1px solid var(--line);margin:26px 0}
.wl{color:var(--accent)!important}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:6px;
  border:2px solid transparent;background-clip:content-box}
"""

SSE_SCRIPT = """<script>
(function(){
  // SPA-style navigation: fetch the target and swap <div.layout> in place — no full reload.
  async function load(url, keepScroll){
    var y = window.scrollY;
    var res = await fetch(url);
    var html = await res.text();
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var next = doc.querySelector('.layout');
    if(!next) { location.href = url; return; }
    document.title = doc.title;
    var st = doc.querySelector('head style');                     // CSS changes when slate.py does
    if(st) document.querySelector('head style').replaceWith(document.importNode(st, true));
    document.querySelector('.layout').replaceWith(document.importNode(next, true));
    window.scrollTo(0, keepScroll ? y : 0);
  }
  // Status chip: pick a state from the dropdown → POST → files change → SSE re-renders.
  document.addEventListener('click', function(e){
    var btn = e.target.closest('.badge-menu .menu-item');
    if(btn){
      var menu = btn.closest('.badge-menu');
      menu.removeAttribute('open');
      fetch('/status', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id: menu.dataset.issue, status: btn.dataset.status})});
      return;
    }
    document.querySelectorAll('details.badge-menu[open]').forEach(function(d){
      if(!d.contains(e.target)) d.removeAttribute('open');   // click-away closes
    });
  });
  document.addEventListener('keydown', function(e){
    if(e.key !== 'Escape') return;
    document.querySelectorAll('details.badge-menu[open]').forEach(function(d){
      d.removeAttribute('open');
    });
  });

  document.addEventListener('click', function(e){
    if(e.metaKey||e.ctrlKey||e.shiftKey||e.button) return;
    var a = e.target.closest('a');
    if(!a) return;
    var u = new URL(a.href, location.href);
    if(u.origin !== location.origin) return;                 // external link → default
    if(u.pathname !== '/' && u.pathname !== '/waves'
       && !u.pathname.startsWith('/issue/')
       && !u.pathname.startsWith('/status/')) return;
    if(u.pathname === location.pathname && u.hash) return;    // in-page anchor → default
    e.preventDefault();
    history.pushState({}, '', u.pathname);
    load(u.pathname, false);
  });
  window.addEventListener('popstate', function(){ load(location.pathname, true); });

  // Drag-to-reorder within a status view's list. The drop POSTs the list's new
  // id sequence to /reorder; the server renumbers `order:` in the issue files and
  // the SSE reload below re-renders everything from the markdown.
  var dragEl = null, dragIds = '', dragHome = null, dragStatus = null;
  function listIds(list){
    return Array.prototype.map.call(
      list.querySelectorAll('.item[data-id]'), function(el){ return el.dataset.id; });
  }
  // A status view may render as several wave sections, each its own list sharing the
  // status. The persisted order is the concatenation of every such list in the view,
  // in document order — so a within-section reorder saves, a cross-section move can't.
  function viewIds(status){
    var ids = [];
    document.querySelectorAll('.list[data-status]').forEach(function(l){
      if(l.dataset.status === status)
        Array.prototype.push.apply(ids, listIds(l));
    });
    return ids;
  }
  document.addEventListener('dragstart', function(e){
    var it = e.target.closest('.list .item[data-id]');
    if(!it) return;
    dragEl = it;
    dragStatus = it.closest('.list').dataset.status;
    dragIds = viewIds(dragStatus).join(',');
    dragHome = { parent: it.parentNode, next: it.nextSibling };   // for snap-back on cancel
    it.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    try{ e.dataTransfer.setData('text/plain', it.dataset.id); }catch(_){}
  });
  document.addEventListener('dragover', function(e){
    if(!dragEl || !e.target.closest) return;
    // Accept the drop anywhere in the row's own list — including over the dragged
    // row itself and the gaps. The row re-slots under the cursor while dragging, so
    // release usually happens over it; an unaccepted target there would make WebKit
    // play its fly-back animation (and read as a canceled drag). Outside the list
    // stays unaccepted: dropping there cancels.
    if(e.target.closest('.list') !== dragEl.closest('.list')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    var over = e.target.closest('.list .item[data-id]');
    if(!over || over === dragEl) return;
    var r = over.getBoundingClientRect();
    over.parentNode.insertBefore(dragEl, e.clientY < r.top + r.height/2 ? over : over.nextSibling);
  });
  document.addEventListener('drop', function(e){ if(dragEl) e.preventDefault(); });
  document.addEventListener('dragend', function(e){
    if(!dragEl) return;
    var it = dragEl, home = dragHome, status = dragStatus, list = it.closest('.list');
    dragEl = null; dragHome = null; dragStatus = null;
    it.classList.remove('dragging');
    // Esc or a drop outside any valid target cancels: snap back, save nothing.
    if(e.dataTransfer && e.dataTransfer.dropEffect === 'none' && home){
      home.parent.insertBefore(it, home.next);
      return;
    }
    if(!list) return;
    var ids = viewIds(status);
    if(ids.join(',') === dragIds) return;                             // nothing moved
    fetch('/reorder', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({status: status, ids: ids})});
  });

  // Sidebar width: drag the sash, persisted per browser.
  try{
    var w = localStorage.getItem('slate-sbw');
    if(w) document.documentElement.style.setProperty('--sbw', w);
  }catch(_){}
  var sashing = false;
  document.addEventListener('mousedown', function(e){
    if(!e.target.closest || !e.target.closest('.sash')) return;
    sashing = true;
    e.target.closest('.sash').classList.add('on');
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e){
    if(!sashing) return;
    e.preventDefault();
    var px = Math.min(400, Math.max(160, e.clientX)) + 'px';
    document.documentElement.style.setProperty('--sbw', px);
  });
  document.addEventListener('mouseup', function(){
    if(!sashing) return;
    sashing = false;
    var s = document.querySelector('.sash');
    if(s) s.classList.remove('on');
    try{
      localStorage.setItem('slate-sbw',
        getComputedStyle(document.documentElement).getPropertyValue('--sbw').trim());
    }catch(_){}
  });

  try{
    var es = new EventSource('/events');
    es.onmessage = function(){
      // Never yank the DOM out from under an in-progress drag or an open menu;
      // the next presence heartbeat (≤15s) or file change re-syncs afterwards.
      if(dragEl || document.querySelector('details.badge-menu[open]')) return;
      load(location.pathname, true);                               // live reload, keeps scroll
    };
    // Editing slate.py re-execs the server, dropping this stream. EventSource
    // retries on its own; reload on the reconnect so new CSS/markup applies.
    var lost = false;
    es.onerror = function(){ lost = true; };
    es.onopen = function(){ if(lost){ lost = false; load(location.pathname, true); } };
  }catch(e){}
})();
</script>"""


def _svg(inner):
    return f'<svg class="ico" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">{inner}</svg>'


def _ring(c, extra=""):
    return f'<circle cx="8" cy="8" r="6" fill="none" stroke="{c}" stroke-width="1.6"{extra}/>'


def _pie(c, frac):
    circ = 18.85  # 2*pi*3
    return (f'<circle cx="8" cy="8" r="3" fill="none" stroke="{c}" stroke-width="6" '
            f'stroke-dasharray="{round(frac * circ, 2)} {circ}" transform="rotate(-90 8 8)"/>')


def status_icon(status):
    """Linear-style status ring/disc."""
    s = slug(status)
    ring, pie = _ring, _pie

    if s == "backlog":
        return _svg(ring("#6e7178", ' stroke-dasharray="1.6 1.8"'))
    if s == "todo":
        return _svg(ring("#6e7178"))
    if s == "in-progress":
        return _svg(ring("#f2c94c") + pie("#f2c94c", 0.5))
    if s == "in-review":
        return _svg(ring("#4cb782") + pie("#4cb782", 0.75))
    if s == "done":
        return _svg('<circle cx="8" cy="8" r="6.5" fill="#7d87e0"/>'
                    '<path d="M4.8 8.2 L7 10.3 L11.2 5.9" fill="none" stroke="#fff" '
                    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>')
    if s == "canceled":
        return _svg('<circle cx="8" cy="8" r="6.5" fill="#585b62"/>'
                    '<path d="M5.8 5.8 L10.2 10.2 M10.2 5.8 L5.8 10.2" stroke="#fff" '
                    'stroke-width="1.5" stroke-linecap="round"/>')
    return _svg(ring("#6e7178"))


def priority_icon(pri):
    """Linear-style priority bars (urgent gets the orange exclamation box)."""
    p = slug(pri)
    if p == "urgent":
        return _svg('<rect x="1" y="1" width="14" height="14" rx="3.5" fill="#f2994a"/>'
                    '<rect x="7" y="3.5" width="2" height="5.5" rx="1" fill="#fff"/>'
                    '<rect x="7" y="10.5" width="2" height="2" rx="1" fill="#fff"/>')
    n = {"high": 3, "medium": 2, "low": 1}.get(p, 0)
    bars = []
    for idx, (x, y, h) in enumerate([(2, 9, 4), (6.5, 6, 7), (11, 3, 10)]):
        col = "#9498a1" if idx < n else "#3a3d44"
        bars.append(f'<rect x="{x}" y="{y}" width="2.5" height="{h}" rx="0.6" fill="{col}"/>')
    return _svg("".join(bars))


def waves_icon():
    """Stacked wavy lines — the marker for the Waves view."""
    return _svg('<path d="M2 6 q1.5 -2 3 0 t3 0 t3 0" fill="none" stroke="#6e7178" '
                'stroke-width="1.4" stroke-linecap="round"/>'
                '<path d="M2 10 q1.5 -2 3 0 t3 0 t3 0" fill="none" stroke="#6e7178" '
                'stroke-width="1.4" stroke-linecap="round"/>')


def progress_icon(done, total):
    """Fractional pie showing a wave's completion (done/total), muted track behind."""
    frac = (done / total) if total else 0
    return _svg(_ring("#3a3d44") + _pie("#7d87e0", frac))


# Mid-tone hues that carry a near-white initial (the Done disc and the urgent box
# already set this precedent). A disc's color is its assignee's palette slot.
ASSIGNEE_HUES = ["#7d87e0", "#4cb782", "#f2994a", "#b06fd4", "#6e7178"]


def _assignee_hue(name):
    """Deterministic color per assignee: sorted distinct assignees → palette index."""
    names = sorted({it["assignee"] for it in list_issues() if it.get("assignee")})
    idx = names.index(name) if name in names else 0
    return ASSIGNEE_HUES[idx % len(ASSIGNEE_HUES)]


def assignee_icon(name):
    """A solid disc carrying the assignee's initial in white — the same visual
    weight as the Done disc. An HTML span (not SVG text) keeps the letter crisp.
    Empty name → nothing; in a row, CSS right-aligns it beside the date."""
    if not name:
        return ""
    return (f'<span class="av" style="background:{_assignee_hue(name)}" '
            f'title="{html.escape(name, quote=True)}">'
            f'{html.escape(name.strip()[:1].upper())}</span>')


FOOTER = ('<footer class="foot">rendered by '
          '<a href="https://github.com/kkurian/slate">slate</a>. built by '
          '<a href="https://github.com/kkurian">kkurian</a>.</footer>')


def page(title, sidebar, main, props="", live=True):
    cls = "layout" if props else "layout no-props"
    props_html = f'<aside class="props">{props}</aside>' if props else ""
    sash = '<div class="sash" title="Drag to resize"></div>' if live else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
        f'<div class="{cls}"><aside class="sidebar">{sidebar}</aside>'
        f'<main class="content">{main}</main>{props_html}{sash}</div>'
        f"{FOOTER}{SSE_SCRIPT if live else ''}</body></html>"
    )


GRIP = ('<span class="grip" title="Drag to reorder">'
        '<svg viewBox="0 0 10 16" width="10" height="16" aria-hidden="true">'
        + "".join(f'<circle cx="{x}" cy="{y}" r="1.1" fill="currentColor"/>'
                  for y in (4, 8, 12) for x in (3, 7))
        + "</svg></span>")


def sidebar_html(active_status=None):
    """Navigation only — status views with counts, never issue titles (they'd truncate)."""
    issues = list_issues()
    parts = [f'<a class="brand" href="{url_for("project")}"><span class="logo"></span>'
             f'{html.escape(project_title())}</a>']
    for status in STATUS_ORDER:
        count = sum(1 for it in issues if it["status"] == status)
        if not count and status not in ALWAYS_SHOW:
            continue
        cls = "nav-row active" if status == active_status else "nav-row"
        parts.append(
            f'<a class="{cls}" href="{url_for("status", status)}">{status_icon(status)}'
            f'{html.escape(status)}<span class="n">{count}</span></a>')
    # Waves is a lens across the statuses, not a status — set it apart with a divider,
    # and count distinct waves (not issues carrying one). The divider carries its style
    # inline: it only exists in wave mode, so the shared CSS (and no-wave builds) is left
    # byte-identical.
    waves = {str(it["wave"]) for it in issues if it.get("wave")}
    if waves:                                # the Waves view only exists once a wave is set
        cls = "nav-row active" if active_status == "Waves" else "nav-row"
        parts.append('<div style="height:1px;background:var(--line);margin:9px 8px 8px">'
                     '</div>')
        parts.append(
            f'<a class="{cls}" href="{url_for("waves")}">{waves_icon()}'
            f'Waves<span class="n">{len(waves)}</span></a>')
    pres = agent_presence()
    n, workers = pres["sessions"], pres["workers"]
    if n:
        label = "1 agent active" if n == 1 else f"{n} agents active"
        if workers > n:                      # a workflow fanned out — say how wide
            label += f' · {workers} workers'
        parts.append(f'<div class="agents-live"><span class="pulse"></span>{label}</div>')
    return "".join(parts)


def issue_row(it, drag=False):
    """One full-width list row: icons, id, title, updated date pinned right."""
    attrs = (f' draggable="true" data-id="{html.escape(it["id"], quote=True)}"'
             if drag else "")
    date = (f'<span class="idate" title="last updated"><span class="k">updated</span> '
            f'{html.escape(str(it["updated"]))}</span>'
            if it.get("updated") else "")
    hit = agent_presence()["issues"].get(it["id"])
    dot = (f'<span class="pulse" title="agent active · {_age_label(hit["age"])}"></span>'
           if hit else "")
    disc = assignee_icon(it.get("assignee", ""))
    return (
        f'<a class="item"{attrs} href="{url_for("issue", it["id"])}">'
        f'{GRIP if drag else ""}'
        f'{status_icon(it["status"])}{priority_icon(it["priority"])}'
        f'<span class="iid">{html.escape(it["id"])}</span>'
        f'<span class="ititle">{html.escape(it["title"])}</span>{dot}{disc}{date}</a>'
    )


def _wave_groups(issues):
    """Partition issues (already in _sort_key order) into ([(value, rows)...], nowave),
    wave groups ordered by _wave_sort_key and the No-wave rows returned separately."""
    groups = {}
    for it in issues:
        if it.get("wave"):
            groups.setdefault(str(it["wave"]), []).append(it)
    nowave = [it for it in issues if not it.get("wave")]
    ordered = [(value, groups[value]) for value in sorted(groups, key=_wave_sort_key)]
    return ordered, nowave


def render_status_page(status, live=True):
    issues = [it for it in list_issues() if it["status"] == status]
    head = (f'<div class="view-head"><h1>{status_icon(status)}{html.escape(status)}'
            f'<span class="vcount">{len(issues)}</span></h1></div>')
    if not issues:
        return page(f"{status} · {project_title()}", sidebar_html(status),
                    head + '<p class="empty">No issues.</p>', live=live)
    drag = live
    data = f' data-status="{html.escape(status, quote=True)}"' if drag else ""

    def rows(items):
        return "".join(issue_row(it, drag=drag) for it in items)

    if any(it.get("wave") for it in issues):
        # Group by wave. Each section is its own draggable list carrying the same
        # data-status; the drop handler persists the concatenation of all sections in
        # displayed order, so dragging reorders within a section but never across one.
        ordered, nowave = _wave_groups(issues)
        parts = [_group_section(f"Wave {html.escape(str(value))}", rows(items), data)
                 for value, items in ordered]
        if nowave:
            parts.append(_group_section("No wave", rows(nowave), data))
        body = "".join(parts)
    else:
        body = f'<section class="list"{data}>{rows(issues)}</section>'
    return page(f"{status} · {project_title()}", sidebar_html(status), head + body, live=live)


def _group_section(label, rows_html, data=""):
    """A titled group: the group-h header treatment over a (optionally draggable) list."""
    return (f'<section class="active"><div class="group-h">{label}</div>'
            f'<div class="list"{data}>{rows_html}</div></section>')


def _wave_status_key(it):
    """Order a wave's issues by workflow position (In Progress first), then the usual
    sort. Unknown statuses fall after the known ones."""
    try:
        rank = STATUS_ORDER.index(it["status"])
    except ValueError:
        rank = len(STATUS_ORDER)
    return (rank, _sort_key(it))


def render_waves_page(live=True):
    """A progress dashboard: one section per wave (sorted), its header carrying a
    fractional pie and a done/total count; issues within a wave lead with the ones in
    flight. A trailing 'No wave' section holds issues without the field so none
    disappear. Display-only — no drag reorder here (that write path is per-status)."""
    issues = list_issues()                   # already in _sort_key order within each group
    ordered, nowave = _wave_groups(issues)
    head = (f'<div class="view-head"><h1>{waves_icon()}Waves'
            f'<span class="vcount">{len(ordered)}</span></h1></div>')

    parts = []
    for value, items in ordered:
        done = sum(1 for it in items if it["status"] == "Done")
        rows = sorted(items, key=_wave_status_key)
        # Inline style so the shared CSS stays byte-identical to a no-wave build.
        label = (f'{progress_icon(done, len(items))}Wave {html.escape(str(value))}'
                 f'<span style="color:var(--faint);font-weight:500;'
                 f'font-variant-numeric:tabular-nums">{done}/{len(items)}</span>')
        parts.append(_group_section(label, "".join(issue_row(it) for it in rows)))
    if nowave:
        parts.append(_group_section("No wave", "".join(issue_row(it) for it in nowave)))
    body = "".join(parts) if parts else '<p class="empty">No issues.</p>'
    return page(f"Waves · {project_title()}", sidebar_html("Waves"), head + body, live=live)


def render_props(meta):
    fields = [("Status", "status"), ("Priority", "priority"), ("Wave", "wave"),
              ("Assignee", "assignee"), ("Labels", "labels"), ("Project", "project"),
              ("Parent", "parent"), ("Due", "due"), ("Created", "created"),
              ("Updated", "updated")]
    rows = ['<h3>Properties</h3><dl class="props-dl">']
    for label, key in fields:
        if meta.get(key):
            v = meta[key]
            if isinstance(v, list):
                v = ", ".join(v)
            if key == "status":
                v = status_icon(str(v)) + html.escape(str(v))
            elif key == "priority":
                v = priority_icon(str(v)) + html.escape(str(v))
            elif key == "assignee":
                v = assignee_icon(str(v)) + html.escape(str(v))
            elif key == "parent":
                v = render_inline(str(v))
            else:
                v = html.escape(str(v))
            rows.append(f"<dt>{label}</dt><dd>{v}</dd>")
    rows.append("</dl>")
    return "".join(rows)


def render_active():
    issues = list_issues()
    rows = [issue_row(it) for status in ("In Progress", "In Review")
            for it in issues if it["status"] == status]
    body = (f'<div class="list">{"".join(rows)}</div>' if rows
            else '<p class="empty">Nothing in progress.</p>')
    return f'<section class="active"><div class="group-h">Active</div>{body}</section>'


def render_project_page(live=True):
    path = ROOT / "project.md"
    text = path.read_text(encoding="utf-8") if path.exists() else "# Project\n"
    meta, body = parse_doc(text)
    main = (render_active()
            + '<article class="md">' + render_blocks(body) + "</article>")
    return page(meta.get("title", "slate"), sidebar_html(None), main, live=live)


def render_issue_page(p, meta, body, live=True):
    iid = meta.get("id", p.stem)
    title = meta.get("title", p.stem)
    status = meta.get("status", "Backlog")
    pri = meta.get("priority", "No priority")
    if live:
        opts = "".join(
            f'<button class="menu-item{" sel" if s == status else ""}" '
            f'data-status="{html.escape(s, quote=True)}">{status_icon(s)}{html.escape(s)}</button>'
            for s in LIFECYCLE)
        status_badge = (
            f'<details class="badge-menu" data-issue="{html.escape(iid, quote=True)}">'
            f'<summary class="badge">{status_icon(status)}{html.escape(status)}</summary>'
            f'<div class="menu">{opts}</div></details>')
    else:
        status_badge = f'<span class="badge">{status_icon(status)}{html.escape(status)}</span>'
    hit = agent_presence()["issues"].get(iid)
    live_badge = ""
    if hit:
        live_badge = (f'<span class="badge live"><span class="pulse"></span>'
                      f'agent working · {_age_label(hit["age"])}</span>')
    wave = meta.get("wave")
    # Non-interactive chip (a plain span, like the priority chip) — the wave is set by
    # editing the file, never from here.
    wave_badge = (f'<span class="badge">{waves_icon()}Wave {html.escape(str(wave))}</span>'
                  if wave else "")
    head = (
        f'<div class="issue-head"><div class="crumb">{html.escape(iid)}</div>'
        f"<h1>{html.escape(title)}</h1>"
        f'<div class="badges">{status_badge}'
        f'<span class="badge">{priority_icon(pri)}{html.escape(pri)}</span>'
        f'{wave_badge}{live_badge}</div></div>'
    )
    main = '<article class="md issue">' + head + render_blocks(body) + "</article>"
    return page(f"{iid} · {title}", sidebar_html(status), main, props=render_props(meta), live=live)


# --------------------------------------------------------------------------- #
# Agent presence (read-only) — which issues have an agent on them, right now
# --------------------------------------------------------------------------- #
# Claude Code appends each session's transcript under ~/.claude/projects/<slug>/,
# with workflow subagents nested a few levels down. A fresh mtime means an agent is
# live; the tail's tool calls name the issue files it is touching. Presence is
# ephemeral display state — never written to the markdown, absent from static builds,
# fail-soft if transcripts move or the format changes (no transcripts → no
# indicators, nothing else breaks).

AGENT_FRESH = 90       # seconds of transcript silence before an agent is "gone"
_TAIL_BYTES = 65536
_PRESENCE = {"at": 0.0, "val": None}


def _transcript_dirs():
    """[(dir, cwd_guards)] to scan. The slug mapping is lossy (foo.bar and foo-bar
    collide), so auto-discovered dirs carry guards: a transcript only counts if its
    events' cwd sits under one of our roots. An explicit SLATE_TRANSCRIPTS is trusted."""
    env = os.environ.get("SLATE_TRANSCRIPTS")
    if env:
        d = Path(env)
        return [(d, None)] if d.is_dir() else []
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"
    roots = {ROOT, _git_root(ROOT) or ROOT}
    guards = tuple(f'"cwd":{sp}"{r}' for r in roots for sp in ("", " "))
    dirs = []
    for root in roots:
        d = base / re.sub(r"[^A-Za-z0-9-]", "-", str(root))
        if d.is_dir():
            dirs.append((d, guards))
    return dirs


def _tail(path, n=_TAIL_BYTES):
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - n))
        return f.read().decode("utf-8", "ignore")


def _touched_issues(text, id_pat):
    """Issue ids a session is actually acting on — its markdown file (issues/<ID>.md)
    is the target of a tool call — not ids merely mentioned in passing (grep output,
    git status, a wikilink), which would light spurious dots. id_pat matches
    'issues/<ID>.md' and captures <ID>. Only tool_use path/command args are scanned,
    so a bare 'BZ-14' echoed anywhere in the tail no longer counts as work."""
    hit = set()
    for line in text.split("\n"):
        if '"tool_use"' not in line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        content = (evt.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            for field in ("file_path", "command"):
                v = inp.get(field)
                if isinstance(v, str):
                    hit.update(id_pat.findall(v))
    return hit


def _session_id(path, base):
    """Collapse a transcript path to its owning session. A plain session writes
    <slug>/<uuid>.jsonl; a workflow writes its subagents to
    <slug>/<uuid>/subagents/workflows/<wf>/agent-*.jsonl. Both belong to session
    <uuid>, so the first path component under base is the key (minus any .jsonl)."""
    try:
        first = path.relative_to(base).parts[0]
    except (ValueError, IndexError):
        return str(path)
    return first[:-6] if first.endswith(".jsonl") else first


def agent_presence():
    """{'sessions': n, 'workers': m, 'issues': {id: {'age': s}}} — cached ~1s.
    A session may fan out into workflow subagents; each fresh transcript is one
    worker, and workers collapse to their parent session for the session count."""
    if MODE != "live":
        return {"sessions": 0, "workers": 0, "issues": {}}
    now = time.time()
    if _PRESENCE["val"] is not None and now - _PRESENCE["at"] < 1.0:
        return _PRESENCE["val"]
    out = {"sessions": 0, "workers": 0, "issues": {}}
    try:
        fresh = []
        for d, guards in _transcript_dirs():
            for f in d.rglob("*.jsonl"):     # recurse: workflow subagents nest under <session>/
                if not f.is_file():          # a dir or FIFO named *.jsonl must not wedge us
                    continue
                try:
                    age = int(now - f.stat().st_mtime)
                except OSError:
                    continue
                if age <= AGENT_FRESH:
                    fresh.append((age, f, d, guards))
        if fresh:                            # only touch the tracker when someone is live
            ids = [it["id"] for it in list_issues()]
            pat = (re.compile(r"issues/(" + "|".join(
                       re.escape(i) for i in sorted(ids, key=len, reverse=True))
                   + r")\.md") if ids else None)
            sessions = set()
            for age, f, d, guards in sorted(fresh, key=lambda t: t[0]):
                text = _tail(f)
                if guards and not any(g in text for g in guards):
                    continue                 # slug collision: another project's session
                out["workers"] += 1
                sessions.add(_session_id(f, d))
                for iid in _touched_issues(text, pat) if pat else ():
                    cur = out["issues"].get(iid)
                    if cur is None or age < cur["age"]:
                        out["issues"][iid] = {"age": age}
            out["sessions"] = len(sessions)
    except Exception:
        out = {"sessions": 0, "workers": 0, "issues": {}}
    _PRESENCE["at"], _PRESENCE["val"] = now, out
    return out


def _age_label(s):
    return f"{s}s ago" if s < 60 else f"{s // 60}m ago"


# --------------------------------------------------------------------------- #
# Reorder write path (the one place the viewer writes markdown)
# --------------------------------------------------------------------------- #

def _rewrite_meta(path, updates):
    """Update frontmatter keys in place, preserving everything else verbatim.
    A value of None removes the key."""
    lines = path.read_text(encoding="utf-8").split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path.name}: no frontmatter")
    end = next((j for j in range(1, len(lines)) if lines[j].strip() == "---"), None)
    if end is None:
        raise ValueError(f"{path.name}: unterminated frontmatter")
    pending, out = dict(updates), []
    for j, line in enumerate(lines):
        if 0 < j < end and ":" in line:
            k = line.split(":", 1)[0].strip()
            if k in pending:
                v = pending.pop(k)
                if v is None:
                    continue
                line = f"{k}: {v}"
        out.append(line)
    tail = [f"{k}: {v}" for k, v in pending.items() if v is not None]
    close = next(j for j in range(1, len(out)) if out[j].strip() == "---")
    out[close:close] = tail
    path.write_text("\n".join(out), encoding="utf-8")


def apply_reorder(status, ids):
    """Renumber `order:` 1..n for the given ids, which must all sit in `status`."""
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("ids must be a list of issue ids")
    current = {it["id"] for it in list_issues() if it["status"] == status}
    if set(ids) != current:
        raise ValueError(f"ids do not match the issues currently in {status!r}")
    today = time.strftime("%Y-%m-%d")
    for pos, iid in enumerate(ids, 1):
        p, meta, _ = find_issue(iid)
        if str(meta.get("order", "")) == str(pos):
            continue   # already in place — don't churn the file or its updated date
        _rewrite_meta(p, {"order": pos, "updated": today})


def apply_status(iid, status):
    """Move an issue to a new status. Its `order` belonged to the old group, so drop it."""
    if status not in STATUS_ORDER:
        raise ValueError(f"unknown status {status!r}")
    res = find_issue(iid) if isinstance(iid, str) else None
    if not res:
        raise ValueError(f"unknown issue {iid!r}")
    p, meta, _ = res
    _rewrite_meta(p, {"status": status, "order": None,
                      "updated": time.strftime("%Y-%m-%d")})


# --------------------------------------------------------------------------- #
# Live server + file watcher
# --------------------------------------------------------------------------- #

STATE = {"version": 0, "changed": ""}


def _watched_files():
    files = []
    if (ROOT / "project.md").exists():
        files.append(ROOT / "project.md")
    for d in (ISSUES, TEMPLATES):
        if d.exists():
            files.extend(d.glob("*.md"))
    return files


SELF = Path(__file__).resolve()


def _restart():
    """Re-exec the server so an edited slate.py takes effect without a manual restart."""
    try:
        compile(SELF.read_text(encoding="utf-8"), str(SELF), "exec")
    except SyntaxError as e:
        print(f"slate.py changed but has a syntax error (line {e.lineno}); keeping the old server")
        return
    print("slate.py changed — restarting server")
    os.execv(sys.executable, [sys.executable, str(SELF)] + sys.argv[1:])


def watch():
    mtimes, init, tick, agents_key, agents_beat = {}, False, 0, None, 0.0
    while True:
        snap = {}
        for p in [SELF] + _watched_files():
            try:
                snap[p] = p.stat().st_mtime
            except FileNotFoundError:
                continue
        if init and snap.get(SELF) != mtimes.get(SELF):
            _restart()   # returns only if the new source doesn't compile
        elif init and snap != mtimes:
            changed = [p.name for p in set(snap) | set(mtimes) if snap.get(p) != mtimes.get(p)]
            STATE["changed"] = changed[0] if changed else ""
            STATE["version"] += 1
        mtimes, init = snap, True
        tick += 1
        if tick % 4 == 0:
            # Agent presence. Re-render on material change (who is live, which issues),
            # NOT on every tool call — that would swap the layout every couple of
            # seconds while an agent works. A 15s heartbeat while agents are active
            # keeps the age labels and tool lines from freezing on screen.
            pres = agent_presence()
            key = (pres["sessions"], tuple(sorted(pres["issues"])))
            now = time.time()
            material = agents_key is not None and key != agents_key
            heartbeat = pres["sessions"] and now - agents_beat > 15
            if material or heartbeat:
                STATE["changed"] = "agents"
                STATE["version"] += 1
                agents_beat = now
            agents_key = key
        time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if not self._local():
            return self.send_error(403)
        path = urllib.parse.urlparse(self.path).path
        if path == "/events":
            return self._sse()
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/":
            return self._html(render_project_page())
        if path == "/waves":
            if any(it.get("wave") for it in list_issues()):
                return self._html(render_waves_page())
            return self.send_error(404)
        m = re.match(r"^/status/([a-z0-9-]+)$", path)
        if m:
            status = {slug(s): s for s in STATUS_ORDER}.get(m.group(1))
            if status:
                return self._html(render_status_page(status))
        m = re.match(r"^/issue/(.+)$", path)
        if m:
            res = find_issue(urllib.parse.unquote(m.group(1)))
            if res:
                return self._html(render_issue_page(*res))
        self.send_error(404)

    def _local(self):
        """Only the user's own browser tab may talk to us: a foreign Host means DNS
        rebinding; a foreign Origin (or a CORS-safelisted Content-Type on POST) means
        some other website is poking our write endpoints."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("localhost", "127.0.0.1", "[::1]", ""):
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urllib.parse.urlparse(origin)
            if o.hostname not in ("localhost", "127.0.0.1", "::1"):
                return False
        return True

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/reorder", "/status"):
            return self.send_error(404)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if not self._local() or ctype != "application/json":
            return self.send_error(403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n))
            if path == "/reorder":
                apply_reorder(payload["status"], payload["ids"])
            else:
                apply_status(payload["id"], payload["status"])
        except (ValueError, KeyError, TypeError) as e:
            return self.send_error(400, explain=str(e))
        self.send_response(204)
        self.end_headers()

    def _html(self, body):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last = STATE["version"]
        try:
            while True:
                if STATE["version"] != last:
                    last = STATE["version"]
                    self.wfile.write(f'data: {STATE["changed"]}\n\n'.encode())
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(0.6)
        except Exception:
            pass


def serve():
    threading.Thread(target=watch, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"plan viewer → http://localhost:{PORT}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


def build(outdir):
    global MODE
    MODE = "static"
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render_project_page(live=False), encoding="utf-8")
    issues = list_issues()
    views = 0
    for status in STATUS_ORDER:
        if any(it["status"] == status for it in issues) or status in ALWAYS_SHOW:
            (out / f"status-{slug(status)}.html").write_text(
                render_status_page(status, live=False), encoding="utf-8")
            views += 1
    if any(it.get("wave") for it in issues):
        (out / "waves.html").write_text(render_waves_page(live=False), encoding="utf-8")
        views += 1
    count = 0
    for it in issues:
        res = find_issue(it["id"])
        if res:
            (out / f"{it['id']}.html").write_text(
                render_issue_page(*res, live=False), encoding="utf-8")
            count += 1
    print(f"built static site → {out}/  (index.html + {views} views + {count} issues)")


SLATE_BEGIN = "<!-- slate:begin -->"
SLATE_END = "<!-- slate:end -->"


def _git_root(start):
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def _agent_block(pointer):
    body = (
        f"{SLATE_BEGIN}\n"
        "## Task tracking (slate)\n\n"
        "This repository tracks tasks with slate — a task tracker kept in plain "
        "markdown. Track all work here: when you start, advance, or finish a task, "
        "create or update the corresponding issue and keep its `status` current.\n"
    )
    if pointer:
        body += f"\n{pointer}\n"
    return body + SLATE_END


def _write_block(path, pointer):
    block = _agent_block(pointer)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if SLATE_BEGIN in existing and SLATE_END in existing:
        pre = existing[:existing.index(SLATE_BEGIN)]
        post = existing[existing.index(SLATE_END) + len(SLATE_END):]
        new, verb = pre + block + post, "updated"
    elif existing.strip():
        new, verb = existing.rstrip() + "\n\n" + block + "\n", "updated"
    else:
        new, verb = block + "\n", "created"
    path.write_text(new, encoding="utf-8")
    return verb


def install(target=None):
    """Make the host repo's agent aware of slate.

    Writes a managed block into the repo's root agent-instructions file telling the
    agent to track work in slate. Targets CLAUDE.md and/or AGENTS.md — whichever the
    repo already uses (Claude Code reads CLAUDE.md; other tools read AGENTS.md) — and
    defaults to CLAUDE.md when neither exists. CLAUDE.md gets an @-import of the
    conventions; AGENTS.md, which has no import mechanism, gets a path reference.
    Idempotent: re-running updates the block in place.
    """
    root = Path(target).resolve() if target else (_git_root(ROOT) or ROOT.parent)
    conventions = (ROOT / "AGENTS.md").resolve()
    rel = os.path.relpath(conventions, root).replace(os.sep, "/")

    files = [n for n in ("CLAUDE.md", "AGENTS.md") if (root / n).exists()] or ["CLAUDE.md"]
    for name in files:
        path = root / name
        if path.resolve() == conventions:
            pointer = ""                                  # conventions are in this file
        elif name == "CLAUDE.md":
            pointer = f"@{rel}"                            # Claude Code resolves the import
        else:
            pointer = f"Read `{rel}` before creating or updating issues."
        verb = _write_block(path, pointer)
        print(f"slate: {verb} block in {path}")
    print("slate: agent will track work in slate")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "build":
        build(sys.argv[2] if len(sys.argv) > 2 else "_site")
    elif cmd == "install":
        install(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        serve()
