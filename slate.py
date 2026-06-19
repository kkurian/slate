#!/usr/bin/env python3
"""slate — a read-only web view of a task tracker kept in plain markdown.

The tracker lives in plain markdown (project.md + issues/*.md). This file is just
a *viewer*; if it ever breaks, every file is still readable in any editor or on
GitHub. Python 3 standard library only — no pip, no npm, no build step.

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

# Sidebar grouping order, mirroring Linear's workflow states.
STATUS_ORDER = ["In Progress", "Todo", "In Review", "Backlog", "Done", "Canceled"]
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
                    if v.startswith("[") and v.endswith("]"):
                        v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
                    meta[k] = v
            return meta, "\n".join(lines[end + 1:])
    return meta, text


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


def url_for(kind, ident=None):
    if MODE == "static":
        return "index.html" if kind == "project" else f"{ident}.html"
    return "/" if kind == "project" else f"/issue/{ident}"


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
            })
    items.sort(key=_id_key)
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
.layout{display:grid;grid-template-columns:244px minmax(0,1fr) 244px;min-height:100vh}
.layout.no-props{grid-template-columns:244px minmax(0,1fr)}
.sidebar{background:var(--sidebar);border-right:1px solid var(--line);padding:14px 8px;
  position:sticky;top:0;height:100vh;overflow:auto}
.brand{display:flex;align-items:center;gap:9px;font-weight:600;color:var(--ink);padding:6px 8px 16px}
.group{margin:2px 0 12px}
.group-h{display:flex;align-items:center;gap:8px;padding:5px 8px;font-size:12px;font-weight:600;color:var(--mut)}
.group-h .n{margin-left:auto;color:var(--faint);font-weight:500}
.item{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:6px;color:var(--ink);font-size:13px}
.item:hover{background:var(--hover)}
.item.active{background:var(--active)}
.iid{color:var(--faint);font-variant-numeric:tabular-nums;flex:none;font-size:12.5px}
.ititle{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#c4c6cc}
.item.active .ititle{color:var(--ink)}
.content{padding:42px 60px;max-width:780px}
.props{background:var(--sidebar);border-left:1px solid var(--line);padding:28px 22px;
  position:sticky;top:0;height:100vh;overflow:auto}
.props h3{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 14px;font-weight:600}
.props-dl{display:grid;grid-template-columns:82px 1fr;gap:12px;margin:0;font-size:13px;align-items:center}
.props-dl dt{color:var(--mut)}
.props-dl dd{margin:0;color:var(--ink);display:flex;align-items:center;gap:7px}
.board{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 32px}
.chip{display:flex;align-items:center;gap:9px;border:1px solid var(--line);border-radius:8px;
  padding:9px 14px;background:var(--panel);min-width:104px}
.chip b{font-size:18px;font-weight:600}.chip span{font-size:12px;color:var(--mut)}
.issue-head{margin-bottom:28px}
.issue-head .crumb{color:var(--faint);font-size:12.5px;font-variant-numeric:tabular-nums}
.issue-head h1{margin:6px 0 14px;font-size:24px;font-weight:600;letter-spacing:-.012em}
.badges{display:flex;gap:8px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:500;
  padding:4px 11px 4px 8px;border-radius:7px;border:1px solid var(--line);background:var(--panel)}
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
    document.querySelector('.layout').replaceWith(document.importNode(next, true));
    window.scrollTo(0, keepScroll ? y : 0);
  }
  document.addEventListener('click', function(e){
    if(e.metaKey||e.ctrlKey||e.shiftKey||e.button) return;
    var a = e.target.closest('a');
    if(!a) return;
    var u = new URL(a.href, location.href);
    if(u.origin !== location.origin) return;                 // external link → default
    if(u.pathname !== '/' && !u.pathname.startsWith('/issue/')) return;
    if(u.pathname === location.pathname && u.hash) return;    // in-page anchor → default
    e.preventDefault();
    history.pushState({}, '', u.pathname);
    load(u.pathname, false);
  });
  window.addEventListener('popstate', function(){ load(location.pathname, true); });
  try{
    var es = new EventSource('/events');
    es.onmessage = function(){ load(location.pathname, true); };   // live reload, snappy + keeps scroll
  }catch(e){}
})();
</script>"""


def _svg(inner):
    return f'<svg class="ico" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">{inner}</svg>'


def status_icon(status):
    """Linear-style status ring/disc."""
    s = slug(status)

    def ring(c, extra=""):
        return f'<circle cx="8" cy="8" r="6" fill="none" stroke="{c}" stroke-width="1.6"{extra}/>'

    def pie(c, frac):
        circ = 18.85  # 2*pi*3
        return (f'<circle cx="8" cy="8" r="3" fill="none" stroke="{c}" stroke-width="6" '
                f'stroke-dasharray="{round(frac * circ, 2)} {circ}" transform="rotate(-90 8 8)"/>')

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


def page(title, sidebar, main, props="", live=True):
    cls = "layout" if props else "layout no-props"
    props_html = f'<aside class="props">{props}</aside>' if props else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
        f'<div class="{cls}"><aside class="sidebar">{sidebar}</aside>'
        f'<main class="content">{main}</main>{props_html}</div>'
        f"{SSE_SCRIPT if live else ''}</body></html>"
    )


def sidebar_html(active=None):
    issues = list_issues()
    parts = [f'<a class="brand" href="{url_for("project")}"><span class="logo"></span>'
             f'{html.escape(project_title())}</a>']
    for status in STATUS_ORDER:
        rows = [it for it in issues if it["status"] == status]
        if not rows and status not in ALWAYS_SHOW:
            continue
        parts.append(f'<div class="group"><div class="group-h">{status_icon(status)}'
                     f'{html.escape(status)}<span class="n">{len(rows)}</span></div>')
        for it in rows:
            cls = "item active" if it["id"] == active else "item"
            parts.append(
                f'<a class="{cls}" href="{url_for("issue", it["id"])}">'
                f'{priority_icon(it["priority"])}'
                f'<span class="iid">{html.escape(it["id"])}</span>'
                f'<span class="ititle">{html.escape(it["title"])}</span></a>'
            )
        parts.append("</div>")
    return "".join(parts)


def render_board():
    issues = list_issues()
    chips = []
    for status in STATUS_ORDER:
        c = sum(1 for it in issues if it["status"] == status)
        if c or status in ALWAYS_SHOW:
            chips.append(f'<div class="chip">{status_icon(status)}<b>{c}</b>'
                         f'<span>{html.escape(status)}</span></div>')
    return f'<div class="board">{"".join(chips)}</div>'


def render_props(meta):
    fields = [("Status", "status"), ("Priority", "priority"), ("Assignee", "assignee"),
              ("Labels", "labels"), ("Project", "project"), ("Parent", "parent"),
              ("Due", "due"), ("Created", "created"),
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
            elif key == "parent":
                v = render_inline(str(v))
            else:
                v = html.escape(str(v))
            rows.append(f"<dt>{label}</dt><dd>{v}</dd>")
    rows.append("</dl>")
    return "".join(rows)


def render_project_page(live=True):
    path = ROOT / "project.md"
    text = path.read_text(encoding="utf-8") if path.exists() else "# Project\n"
    meta, body = parse_doc(text)
    main = render_board() + '<article class="md">' + render_blocks(body) + "</article>"
    return page(meta.get("title", "slate"), sidebar_html(None), main, live=live)


def render_issue_page(p, meta, body, live=True):
    iid = meta.get("id", p.stem)
    title = meta.get("title", p.stem)
    status = meta.get("status", "Backlog")
    pri = meta.get("priority", "No priority")
    head = (
        f'<div class="issue-head"><div class="crumb">{html.escape(iid)}</div>'
        f"<h1>{html.escape(title)}</h1>"
        f'<div class="badges"><span class="badge">{status_icon(status)}{html.escape(status)}</span>'
        f'<span class="badge">{priority_icon(pri)}{html.escape(pri)}</span></div></div>'
    )
    main = '<article class="md issue">' + head + render_blocks(body) + "</article>"
    return page(f"{iid} · {title}", sidebar_html(iid), main, props=render_props(meta), live=live)


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


def watch():
    mtimes, init = {}, False
    while True:
        snap = {}
        for p in _watched_files():
            try:
                snap[p] = p.stat().st_mtime
            except FileNotFoundError:
                continue
        if init and snap != mtimes:
            changed = [p.name for p in set(snap) | set(mtimes) if snap.get(p) != mtimes.get(p)]
            STATE["changed"] = changed[0] if changed else ""
            STATE["version"] += 1
        mtimes, init = snap, True
        time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/events":
            return self._sse()
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/":
            return self._html(render_project_page())
        m = re.match(r"^/issue/(.+)$", path)
        if m:
            res = find_issue(urllib.parse.unquote(m.group(1)))
            if res:
                return self._html(render_issue_page(*res))
        self.send_error(404)

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
    count = 0
    for it in list_issues():
        res = find_issue(it["id"])
        if res:
            (out / f"{it['id']}.html").write_text(
                render_issue_page(*res, live=False), encoding="utf-8")
            count += 1
    print(f"built static site → {out}/  (index.html + {count} issues)")


SLATE_BEGIN = "<!-- slate:begin -->"
SLATE_END = "<!-- slate:end -->"


def _git_root(start):
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def install(target=None):
    """Make the host repo's agent aware of slate.

    Idempotently writes a managed block into the repo's root CLAUDE.md that (1)
    instructs the agent to track work here and (2) imports AGENTS.md so Claude Code
    loads the conventions every session. Re-running updates the block in place.
    """
    root = Path(target).resolve() if target else (_git_root(ROOT) or ROOT.parent)
    claude = root / "CLAUDE.md"
    rel = os.path.relpath(ROOT / "AGENTS.md", root).replace(os.sep, "/")
    block = (
        f"{SLATE_BEGIN}\n"
        "## Task tracking (slate)\n\n"
        "This repository tracks tasks with slate — a task tracker kept in plain "
        "markdown. Track all work here: when you start, advance, or finish a task, "
        "create or update the corresponding issue and keep its `status` current. "
        "The conventions follow.\n\n"
        f"@{rel}\n"
        f"{SLATE_END}"
    )
    existing = claude.read_text(encoding="utf-8") if claude.exists() else ""
    if SLATE_BEGIN in existing and SLATE_END in existing:
        pre = existing[:existing.index(SLATE_BEGIN)]
        post = existing[existing.index(SLATE_END) + len(SLATE_END):]
        new, action = pre + block + post, "updated slate block in"
    elif existing.strip():
        new, action = existing.rstrip() + "\n\n" + block + "\n", "added slate block to"
    else:
        new, action = block + "\n", "created"
    claude.write_text(new, encoding="utf-8")
    print(f"slate: {action} {claude}")
    print(f"slate: agent will load @{rel} every session")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "build":
        build(sys.argv[2] if len(sys.argv) > 2 else "_site")
    elif cmd == "install":
        install(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        serve()
