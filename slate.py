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
    SLATE_REPO   owner/repo the `pr:` numbers reference (default: ROOT's git origin)
"""

import calendar
import html
import json
import os
import re
import subprocess
import sys
import time
import threading
import urllib.parse
from datetime import date
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
        if kind == "prs":
            return "prs.html"
        return f"{ident}.html"
    if kind == "project":
        return "/"
    if kind == "status":
        return f"/status/{slug(ident)}"
    if kind == "waves":
        return "/waves"
    if kind == "prs":
        return "/prs"
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
    # Numeric waves sort ascending (wave 2 before wave 10). A labeled wave may pin
    # its position with a leading numeric prefix ("0 — hotfix" sorts before wave 1);
    # labels without one come after every numbered wave, alphabetical. Grouping is
    # by the raw value, so the key only orders the groups.
    s = str(value).strip()
    try:
        return (0, float(s), "")
    except ValueError:
        head = s.split(None, 1)[0] if s else ""
        try:
            return (0, float(head), s.lower())
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
                "pr": meta.get("pr"),
                "review_hold": meta.get("review_hold"),
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
.av{position:relative;width:17px;height:17px;border-radius:50%;flex:none;display:inline-flex;
  align-items:center;justify-content:center;font-size:9px;font-weight:600;
  color:rgba(255,255,255,.92);letter-spacing:.02em;line-height:1;cursor:default}
.av:hover::after{content:attr(data-name);position:absolute;bottom:calc(100% + 7px);right:-2px;
  background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:6px;
  padding:3px 8px;font-size:11.5px;font-weight:500;letter-spacing:0;white-space:nowrap;
  pointer-events:none;z-index:5}
/* Right-hand cluster ordering: [pulse][idle][rev][av]. The first present takes
   the auto-margin that right-aligns the whole cluster; the ones after it reset to
   a plain gap. */
.item .pulse,.item .idle,.item .rev,.item .av{margin-left:auto}
.item .pulse~.idle,.item .pulse~.rev,.item .pulse~.av,
.item .idle~.rev,.item .idle~.av,.item .rev~.av{margin-left:0}
/* Idle age — a faint Nd on an active-status row untouched for days (see
   _idle_days); the same quiet register as the PR number, two chars wide. */
.idle{color:var(--faint);font-size:12.5px;font-variant-numeric:tabular-nums;flex:none}
/* Doctor chips — the stale-review warning (every linked PR merged while the
   issue still sits in review) in a warm warning register, and the muted held
   acknowledgment when review_hold records the state as intentional. */
.stale{color:#d9973b;font-size:11px;font-weight:500;border:1px solid rgba(217,151,59,.4);
  border-radius:5px;padding:1px 6px;flex:none;white-space:nowrap;line-height:1.6}
.hold{color:var(--faint);font-size:11px;font-weight:500;border:1px solid var(--line);
  border-radius:5px;padding:1px 6px;flex:none;white-space:nowrap;line-height:1.6}
/* PR review state — aggregate glyph + PR number per row; detail lives on the
   issue page's Pull requests block, so the row carries no tooltip. */
.rev{display:inline-flex;align-items:center;gap:5px;flex:none}
.rev.draft{opacity:.55}
.rev .prn{color:var(--faint);font-size:12.5px;font-variant-numeric:tabular-nums}
/* Pull requests block in the Properties panel. The PR line is the object —
   glyph, number, chip at full size; its reviewers are subordinate detail and
   hang beneath it on a thread rule dropped from the glyph's centerline, one
   register smaller and quieter, so parent and children can't be misread as
   siblings. Merged and closed PRs are settled business: the entry collapses
   to its one line, dimmed. The .pr-sub standing line survives only where
   there is nothing else to hang — a pending PR nobody has been asked to
   review — because there the absence is the news. */
.prb{margin:0 0 4px}
.prb+.prb{margin-top:14px}
.pr-line{display:flex;align-items:center;gap:7px;font-size:13px}
.pr-line a{color:var(--ink);font-variant-numeric:tabular-nums}
.pr-line a:hover{color:var(--accent)}
.mini{font-size:10.5px;font-weight:500;padding:1px 6px;border:1px solid rgba(255,255,255,.09);
  border-radius:5px;color:var(--mut);line-height:1.5}
.prb.done .pr-line{opacity:.8}
.prb.done .pr-line a{color:var(--mut)}
.prb.done .pr-line a:hover{color:var(--accent)}
.pr-sub{margin:2px 0 0 23px;font-size:12px;color:var(--faint)}
.rvs{margin:4px 0 0 7px;padding-left:15px;border-left:1px solid var(--line)}
.rvr{display:flex;align-items:center;gap:6px;padding:2px 0;font-size:12px;color:var(--mut)}
.rvr .ico{width:13px;height:13px}
.rvr .who{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.rvr .age{margin-left:auto;flex:none;color:var(--faint);font-size:11px;font-variant-numeric:tabular-nums}
.rvr.bot{opacity:.5}
.checked{margin-top:12px;font-size:11px;color:var(--faint)}
/* Pull requests view — one two-line ledger row per distinct PR: identity on line
   1 (number, title, chip, owning issues), standing on line 2 (phrase, reviewers,
   ages). Rows never lead with the review glyph — the section heading carries it.
   Merged/closed collapse to one muted line; a gh outage degrades to a flat list. */
.prv .gcount{color:var(--faint);font-weight:500;font-variant-numeric:tabular-nums}
.prv .pritem{padding:5px 8px 6px;border-radius:6px}
.prv .pritem:hover{background:var(--hover)}
.prv .pritem.dim .ptitle{color:var(--mut)}
.prv .pritem.draft{opacity:.55}
.prv .l1{display:flex;align-items:center;gap:7px;font-size:13px;line-height:1.5}
.prv .l2{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:0 0 0 2px;
  font-size:12px;color:var(--mut)}
.prv .prn{color:var(--faint);font-size:12.5px;font-variant-numeric:tabular-nums;flex:none}
.prv a.prn:hover{color:var(--accent)}
.prv .ptitle{color:#c4c6cc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.prv .ptitle.deg{color:var(--mut)}
.prv .mini{flex:none}
.prv .right{margin-left:auto;display:inline-flex;align-items:center;gap:7px;flex:none;
  padding-left:14px}
.prv .right-in{display:inline-flex;gap:7px;flex:none}
.prv .ph{color:var(--mut)}
.prv .rv{display:inline-flex;align-items:center;gap:5px;color:#c4c6cc}
.prv .rv.bot{opacity:.5}
.prv .a{color:var(--faint);font-size:11.5px;font-variant-numeric:tabular-nums}
.prv .sep{color:#3a3d44}
.prv .notice{margin:-6px 0 16px;font-size:12.5px;color:var(--mut);max-width:64ch}
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
.badge.stale{color:#d9973b;border-color:rgba(217,151,59,.4)}
.badge.hold{color:var(--mut);border-color:var(--line)}
/* Doctor strip — the review view's lead finding: which issues here have every
   PR merged. One quiet line in the warning register, above the list. */
.doctor-strip{margin:10px 0 2px;padding:7px 10px;font-size:12.5px;color:#d9973b;
  border:1px solid rgba(217,151,59,.35);border-radius:7px;background:rgba(217,151,59,.06)}
@media (max-width:700px){
  .content{padding:32px 22px}
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
.props-dl dd{margin:0;color:var(--ink);display:flex;align-items:center;gap:7px;
  font-variant-numeric:tabular-nums}
.props-dl dd .src{color:var(--faint);white-space:nowrap}
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
    if(u.pathname !== '/' && u.pathname !== '/waves' && u.pathname !== '/prs'
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


def prs_icon():
    """Merge motif — two source nodes joining a target — drawn in the Waves icon's
    gray stroke language: the marker for the Pull requests view."""
    return _svg('<circle cx="5" cy="4.1" r="1.7" fill="none" stroke="#6e7178" stroke-width="1.4"/>'
                '<circle cx="5" cy="11.9" r="1.7" fill="none" stroke="#6e7178" stroke-width="1.4"/>'
                '<circle cx="11.5" cy="9.7" r="1.7" fill="none" stroke="#6e7178" stroke-width="1.4"/>'
                '<path d="M5 5.9 v4.2 M5 6.4 a4.4 4.4 0 0 0 4.7 3.2" fill="none" '
                'stroke="#6e7178" stroke-width="1.4" stroke-linecap="round"/>')


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
    Empty name → nothing; in a row, CSS right-aligns it beside the date.
    Hover shows the full name via a CSS tooltip (instant, unlike title=)."""
    if not name:
        return ""
    return (f'<span class="av" style="background:{_assignee_hue(name)}" '
            f'data-name="{html.escape(name, quote=True)}">'
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
    # Waves and Pull requests are lenses across the statuses, not statuses — set them
    # apart with a single divider below the status list, and each exists only once its
    # field appears on some issue (Waves counts distinct waves; Pull requests counts
    # open PRs). One divider serves both, present when either view is. The divider
    # carries its style inline so the shared CSS (and no-lens builds) stays byte-identical.
    waves = {str(it["wave"]) for it in issues if it.get("wave")}
    pr_issues = [it for it in issues if _pr_refs(it)]
    if waves or pr_issues:
        parts.append('<div style="height:1px;background:var(--line);margin:9px 8px 8px">'
                     '</div>')
    if waves:                                # the Waves view only exists once a wave is set
        cls = "nav-row active" if active_status == "Waves" else "nav-row"
        parts.append(
            f'<a class="{cls}" href="{url_for("waves")}">{waves_icon()}'
            f'Waves<span class="n">{len(waves)}</span></a>')
    if pr_issues:                            # exists once some issue carries a pr:
        cls = "nav-row active" if active_status == "Pull requests" else "nav-row"
        parts.append(
            f'<a class="{cls}" href="{url_for("prs")}">{prs_icon()}'
            f'Pull requests<span class="n">{_pr_open_count(pr_issues)}</span></a>')
    pres = agent_presence()
    n, workers = pres["sessions"], pres["workers"]
    if n:
        label = "1 agent active" if n == 1 else f"{n} agents active"
        if workers > n:                      # a workflow fanned out — say how wide
            label += f' · {workers} workers'
        parts.append(f'<div class="agents-live"><span class="pulse"></span>{label}</div>')
    return "".join(parts)


# Statuses where silence is abnormal, so a days-old `updated:` earns an idle tag.
IDLE_STATUSES = {"In Progress", "In Review"}
IDLE_DAYS = 3          # calendar days of silence before the tag appears


def _idle_days(updated):
    """Whole calendar days between an `updated:` YYYY-MM-DD stamp and today's local
    date — the day-scale age the idle tag and the panel both read. None if the field
    is missing or unparseable, so a bad date fails soft to no tag / the raw string."""
    try:
        t = time.strptime(str(updated).strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return (date.today() - date(t.tm_year, t.tm_mon, t.tm_mday)).days


def issue_row(it, drag=False):
    """One full-width list row: icons, id, title; idle age, PR state and assignee
    pinned right."""
    attrs = (f' draggable="true" data-id="{html.escape(it["id"], quote=True)}"'
             if drag else "")
    rev = review_row_glyph(_pr_refs(it))
    hit = agent_presence()["issues"].get(it["id"])
    dot = (f'<span class="pulse" title="agent active · {_age_label(hit["age"])}"></span>'
           if hit else "")
    # An active-status row gone quiet for IDLE_DAYS grows a faint Nd — unless an
    # agent is on it now (the pulse), which outranks a day-old stamp.
    idle = ""
    if not hit and it["status"] in IDLE_STATUSES:
        days = _idle_days(it.get("updated"))
        if days is not None and days >= IDLE_DAYS:
            idle = f'<span class="idle">{days}d</span>'
    # Doctor verdict, from the same cached PR state the glyph just read — a
    # reviewing issue whose every PR has merged wears a warning chip; a recorded
    # review_hold wears a muted one; indeterminate stays off the row.
    doc = ""
    verdict = review_verdict(it)
    if verdict and verdict[0] == "stale":
        doc = ('<span class="stale" title="every linked PR has merged — '
               'the status may need a flip">all PRs merged</span>')
    elif verdict and verdict[0] == "held":
        doc = (f'<span class="hold" title="review hold: '
               f'{html.escape(verdict[2], quote=True)}">held</span>')
    disc = assignee_icon(it.get("assignee", ""))
    return (
        f'<a class="item"{attrs} href="{url_for("issue", it["id"])}">'
        f'{GRIP if drag else ""}'
        f'{status_icon(it["status"])}{priority_icon(it["priority"])}'
        f'<span class="iid">{html.escape(it["id"])}</span>'
        f'<span class="ititle">{html.escape(it["title"])}</span>{dot}{idle}{doc}{rev}{disc}</a>'
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
    # A review view leads with the doctor's finding when it has one: the issues
    # here whose every PR has merged, named up front so the smell is visible the
    # moment the view opens. Held and indeterminate issues stay out of the strip.
    if status in REVIEW_STATUSES:
        stale = [it for it in issues
                 if (v := review_verdict(it)) and v[0] == "stale"]
        if stale:
            ids = ", ".join(html.escape(it["id"]) for it in stale)
            n = len(stale)
            noun = "issue has" if n == 1 else "issues have"
            head += (f'<div class="doctor-strip">{n} {noun} every linked PR '
                     f'merged — likely awaiting a status flip: {ids}</div>')
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
            elif key == "updated":
                # Age first (the question), source date faint after (the anchor).
                # A bad date fails soft to the raw string.
                days = _idle_days(v)
                if days is not None:
                    age = "today" if days <= 0 else f"{days}d ago"
                    v = f'{age} <span class="src">&middot; {html.escape(str(v))}</span>'
                else:
                    v = html.escape(str(v))
            else:
                v = html.escape(str(v))
            rows.append(f"<dt>{label}</dt><dd>{v}</dd>")
    rows.append("</dl>")
    rows.append(render_pr_block(_pr_refs(meta)))
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
    # Doctor verdict on the issue itself — the warning next to the very status
    # chip that needs the flip. Held shows its recorded reason, muted.
    doctor_badge = ""
    if live:
        verdict = review_verdict(meta)
        if verdict and verdict[0] == "stale":
            doctor_badge = ('<span class="badge stale">all PRs merged — '
                            'status may need a flip</span>')
        elif verdict and verdict[0] == "held":
            doctor_badge = (f'<span class="badge hold">review hold &middot; '
                            f'{html.escape(verdict[2])}</span>')
    head = (
        f'<div class="issue-head"><div class="crumb">{html.escape(iid)}</div>'
        f"<h1>{html.escape(title)}</h1>"
        f'<div class="badges">{status_badge}'
        f'<span class="badge">{priority_icon(pri)}{html.escape(pri)}</span>'
        f'{wave_badge}{doctor_badge}{live_badge}</div></div>'
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
            inp = raw if isinstance(raw := block.get("input"), dict) else {}
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
# PR review state (read-only) — where each issue's pull request review stands
# --------------------------------------------------------------------------- #
# An issue may carry a `pr:` frontmatter field — a bare number or a list — naming
# the host repo's pull request(s). We ask `gh` for each PR's review state and show
# an aggregate glyph on the row plus per-reviewer detail in the Properties panel.
# Same terms as presence: cached, display-only, never written back, absent from
# static builds, and fail-soft — no gh binary, not authed, offline, or an unknown
# PR number simply drops that PR's data and the row renders as if `pr:` were unset.

PR_TTL = 120           # seconds a PR's fetched state is trusted before a refresh
PR_TIMEOUT = 8         # seconds we wait on gh before giving up on a PR
PR_FIELDS = "number,title,url,isDraft,state,reviewDecision,reviewRequests,latestReviews"
# Worst-active-state wins the row aggregate: changes outranks pending outranks
# approved; the done states (merged/closed) only surface when nothing is active.
_PR_RANK = {"changes": 0, "pending": 1, "approved": 2, "merged": 3, "closed": 4}
_PR_CACHE = {}         # num(str) -> {"at": float, "val": dict|None}
_PR_FETCHING = set()   # nums with a background refresh in flight
_PR_LOCK = threading.Lock()


def _pr_refs(item):
    """PR number strings from an issue's `pr:` field (scalar or list) → []."""
    v = item.get("pr")
    if not v:
        return []
    values = v if isinstance(v, list) else [v]
    return [s for s in (str(x).strip() for x in values) if s]


def _gh_repo_args():
    """How to point gh at the host repo. SLATE_REPO (owner/repo) wins as an
    explicit `-R` and runs from anywhere; otherwise we run from ROOT's git root so
    gh resolves the origin — that repo is the one the bare PR numbers reference."""
    repo = os.environ.get("SLATE_REPO")
    if repo:
        return ["-R", repo], None
    return [], str(_git_root(ROOT) or ROOT)


def _fetch_pr(num):
    """One gh call for one PR → its JSON record, or None on any failure."""
    args, cwd = _gh_repo_args()
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(num), *args, "--json", PR_FIELDS],
            cwd=cwd, capture_output=True, text=True, timeout=PR_TIMEOUT)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None


def _refresh_pr(num):
    """Background body of one PR refresh: the gh round-trip, then the cache write.
    A failure is cached too (as None), so a missing gh or a bad number can't make
    every pass spawn the subprocess again until the TTL turns over."""
    val = _fetch_pr(num)
    with _PR_LOCK:
        _PR_CACHE[num] = {"at": time.time(), "val": val}
        _PR_FETCHING.discard(num)


def pr_info(num):
    """Last-known gh record for one PR number — never blocks the caller. A fresh
    entry (~120s TTL) answers directly; a missing or expired one answers with
    whatever is cached (None on first sight) while a daemon thread refreshes it,
    one thread per PR so a multi-PR issue refreshes in parallel. gh lives off the
    request path entirely: the watcher's _pr_signature pass notices each landed
    refresh and live-reloads open pages over SSE, so stale data self-corrects
    within seconds. Live mode only; None on any failure, same fail-soft terms as
    before."""
    if MODE != "live":
        return None
    num = str(num).strip()
    now = time.time()
    with _PR_LOCK:
        ent = _PR_CACHE.get(num)
        if ent and now - ent["at"] < PR_TTL:
            return ent["val"]
        stale = ent["val"] if ent else None
        if num in _PR_FETCHING:
            return stale
        _PR_FETCHING.add(num)
    threading.Thread(target=_refresh_pr, args=(num,), daemon=True).start()
    return stale


def pr_pending(num):
    """True while a PR's first fetch is still in flight — nothing has ever landed
    for it, so its state is unknown rather than absent. Distinct from a landed
    failure (bad number, gh missing), which is cached as None and stays silently
    dropped, the same fail-soft terms as always. Live mode only."""
    if MODE != "live":
        return False
    with _PR_LOCK:
        return str(num).strip() not in _PR_CACHE


def _is_bot(login):
    """A review from a bot (Copilot, or any [bot] account) — excluded from the row
    aggregate and listed muted in the panel."""
    s = (login or "").lower()
    return "copilot" in s or s.endswith("[bot]")


def _req_login(req):
    """A reviewRequests entry names a user (login) or a team (name/slug)."""
    if not isinstance(req, dict):
        return ""
    return req.get("login") or req.get("name") or req.get("slug") or ""


def _short_age(iso):
    """Compact relative age of an ISO-8601 UTC timestamp: '5m', '2h', '3d'."""
    try:
        secs = int(time.time() - calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return ""
    if secs < 3600:
        return f"{max(secs // 60, 0)}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _pr_state(info):
    """Reduce a gh record to (kind, chip): kind is the display state
    (pending/approved/changes/merged/closed), chip the ready/draft/merged label."""
    state = (info.get("state") or "").upper()
    if state == "MERGED":
        return "merged", "merged"
    if state == "CLOSED":
        return "closed", "closed"
    decision = (info.get("reviewDecision") or "").upper()
    kind = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes"}.get(decision, "pending")
    return kind, ("draft" if info.get("isDraft") else "ready")


def _request_logins(info):
    """Human reviewers asked but not yet heard from — for the pending tooltip."""
    return [x for x in (_req_login(r) for r in info.get("reviewRequests") or [])
            if x and not _is_bot(x)]


def _pr_phrase(kind, info):
    """The one-line standing shown under a PR in the panel (and in tooltips)."""
    if kind == "changes":
        return "changes requested"
    if kind == "approved":
        return "approved"
    if kind == "merged":
        return "merged"
    if kind == "closed":
        return "closed"
    return "review requested" if _request_logins(info) else "no reviewers requested"


def _pr_reviewers(info):
    """[(login, disp, age, is_bot)] for the panel — each reviewer's latest verdict
    first (disp in approved/changes/commented), then still-pending requests. Bots
    sort last so they list muted beneath the humans."""
    rows, seen = [], set()
    for rv in info.get("latestReviews") or []:
        login = (rv.get("author") or {}).get("login") or ""
        if not login:
            continue
        disp = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes"}.get(
            (rv.get("state") or "").upper(), "commented")
        rows.append((login, disp, _short_age(rv.get("submittedAt")), _is_bot(login)))
        seen.add(login.lower())
    for req in info.get("reviewRequests") or []:
        login = _req_login(req)
        if login and login.lower() not in seen:
            rows.append((login, "pending", "", _is_bot(login)))
            seen.add(login.lower())
    rows.sort(key=lambda r: r[3])            # bots last, otherwise stable
    return rows


def _review_inner(kind):
    """SVG innards for a disposition glyph, sized to the 16px icon grid."""
    if kind == "approved":
        return ('<circle cx="8" cy="8" r="5.4" fill="none" stroke="#4cb782" stroke-width="1.5"/>'
                '<path d="M5.3 8.2 L7.1 10 L10.7 6.2" fill="none" stroke="#4cb782" '
                'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>')
    if kind == "changes":
        return ('<circle cx="8" cy="8" r="5.4" fill="none" stroke="#e0655f" stroke-width="1.5"/>'
                '<rect x="5.2" y="7.25" width="5.6" height="1.5" rx="0.75" fill="#e0655f"/>')
    if kind == "commented":
        return ('<circle cx="8" cy="8" r="5.4" fill="none" stroke="#888b94" stroke-width="1.5"/>'
                '<circle cx="5.5" cy="8" r="0.95" fill="#888b94"/>'
                '<circle cx="8" cy="8" r="0.95" fill="#888b94"/>'
                '<circle cx="10.5" cy="8" r="0.95" fill="#888b94"/>')
    if kind == "merged":
        return ('<circle cx="8" cy="8" r="6.5" fill="#7d87e0"/>'
                '<circle cx="5.7" cy="5" r="1.2" fill="#fff"/>'
                '<circle cx="5.7" cy="11" r="1.2" fill="#fff"/>'
                '<circle cx="10.5" cy="9.6" r="1.2" fill="#fff"/>'
                '<path d="M5.7 6.1 v4.9 M5.7 6.6 a3.6 3.6 0 0 0 3.4 3" fill="none" '
                'stroke="#fff" stroke-width="1.3" stroke-linecap="round"/>')
    if kind == "closed":
        # Closed-but-not-merged, GitHub's red semantic, in slate's disc language:
        # the merged disc recolored red with the merge motif negated to a white X.
        return ('<circle cx="8" cy="8" r="6.5" fill="#e0655f"/>'
                '<path d="M5.6 5.6 L10.4 10.4 M10.4 5.6 L5.6 10.4" stroke="#fff" '
                'stroke-width="1.5" stroke-linecap="round"/>')
    if kind == "unknown":
        # State not yet known — a first fetch still in flight. The dashed ring
        # the degraded PRs view already speaks: an outline that hasn't resolved,
        # quieter than pending's dotted ring, resolving on the next repaint.
        return ('<circle cx="8" cy="8" r="5.4" fill="none" stroke="#5c5f66" '
                'stroke-width="1.5" stroke-dasharray="1.6 1.8"/>')
    # pending: a faint hollow ring with a center dot
    return ('<circle cx="8" cy="8" r="5.4" fill="none" stroke="#5c5f66" stroke-width="1.5"/>'
            '<circle cx="8" cy="8" r="1.5" fill="#5c5f66"/>')


def review_icon(kind):
    return _svg(_review_inner(kind))


def review_row_glyph(prnums):
    """The row's PR cluster — aggregate glyph plus the winning PR's number — or ''
    when no PR has live data. The winning PR (lowest _PR_RANK) sets the glyph and
    number, its draft flag the opacity; extra PRs collapse to a +n. While nothing
    has resolved yet but a first fetch is in flight, the cluster shows the dashed
    unknown ring instead of vanishing — the next repaint trades it for the real
    state. No tooltip: the detail lives in the issue page's Pull requests block."""
    infos = [(n, pr_info(n)) for n in prnums]
    states = [(n, info, _pr_state(info)[0]) for n, info in infos if info]
    if not states:
        checking = [n for n, info in infos if info is None and pr_pending(n)]
        if not checking:
            return ""
        more = f" +{len(checking) - 1}" if len(checking) > 1 else ""
        return (f'<span class="rev">{review_icon("unknown")}'
                f'<span class="prn">#{checking[0]}{more}</span></span>')
    num, info, kind = min(states, key=lambda s: _PR_RANK.get(s[2], 9))
    draft = info.get("isDraft") and kind in ("pending", "approved", "changes")
    cls = "rev draft" if draft else "rev"
    more = f" +{len(states) - 1}" if len(states) > 1 else ""
    return (f'<span class="{cls}">{review_icon(kind)}'
            f'<span class="prn">#{num}{more}</span></span>')


# Doctor verdicts — the stale-review check the served board and the doctor CLI
# share. An issue sitting in a review status while every pull request it names
# has merged is usually a missed status flip: the work landed but the issue
# never left review. The `pr:` frontmatter is the authoritative PR list (numbers
# in body prose are out of scope — a task citing a merged precedent PR is not
# the same as its own work having merged), and `review_hold:` records the reason
# when the state is intentional: awaiting a human flip, follow-up work still
# owed, or a review happening off GitHub.
REVIEW_STATUSES = {"In Review"}


def _pr_merge_partition(refs, fetch):
    """Split PR refs into (merged, unmerged, unknown) via fetch — pr_info for the
    served board (cached, the same data the review glyphs read), _fetch_pr for a
    one-shot CLI run. 'unmerged' is anything still open or closed-without-merge;
    'unknown' a PR gh could not resolve, so the caller can decline to assert
    'all merged' rather than guess."""
    merged, unmerged, unknown = [], [], []
    for n in refs:
        info = fetch(n)
        if info is None:
            unknown.append(n)
        elif _pr_state(info)[0] == "merged":
            merged.append(n)
        else:
            unmerged.append(n)
    return merged, unmerged, unknown


def review_verdict(item, fetch=pr_info):
    """Classify one issue: ('stale', merged, None) when it sits in a review status
    with every named PR merged and no hold recorded; ('held', merged, reason) when
    `review_hold:` acknowledges that state; ('indeterminate', merged, unknown) when
    some PRs resolved merged but others couldn't be fetched; None otherwise — not
    in review, no PRs, real review work remaining, or no merge signal at all (the
    fail-soft default, so a dead gh or a static build stays silent)."""
    if item.get("status") not in REVIEW_STATUSES:
        return None
    refs = _pr_refs(item)
    if not refs:
        return None
    merged, unmerged, unknown = _pr_merge_partition(refs, fetch)
    if unmerged or not merged:
        return None
    if unknown:
        return ("indeterminate", merged, unknown)
    hold = item.get("review_hold")
    if hold:
        return ("held", merged, str(hold))
    return ("stale", merged, None)


def render_pr_block(prnums):
    """The 'Pull requests' block for the Properties panel: one entry per PR. The
    PR line is the object — glyph, number, ready/draft chip — and its reviewers
    hang beneath it on a thread rule, each with a verdict glyph and age. Merged
    and closed PRs are settled, so reviewer detail would inform nothing; the
    entry collapses to its one dimmed line. A standing phrase survives in just
    one place — a pending PR with no human reviewer in play says so, because
    there the absence is the only news; every other standing is already spoken
    by the glyph and chip. A PR whose first fetch is still in flight holds its
    place with the dashed unknown ring and a 'checking' chip rather than being
    absent. Empty when no PR has data or a fetch in flight (so static builds and
    landed failures show nothing)."""
    fetched = [(n, pr_info(n)) for n in prnums]
    infos = [(n, info) for n, info in fetched if info]
    checking = [n for n, info in fetched if info is None and pr_pending(n)]
    if not infos and not checking:
        return ""
    parts = ['<h3 style="margin-top:26px">Pull requests</h3>']
    for num, info in infos:
        kind, chip = _pr_state(info)
        url = html.escape(str(info.get("url") or ""), quote=True)
        label = f"#{num}"
        link = f'<a href="{url}" target="_blank" rel="noopener">{label}</a>' if url else label
        done = kind in ("merged", "closed")
        parts.append(f'<div class="prb{" done" if done else ""}">'
                     f'<div class="pr-line">{review_icon(kind)}{link}'
                     f'<span class="mini">{html.escape(chip)}</span></div>')
        if not done:
            rows = _pr_reviewers(info)
            if kind == "pending" and not any(not bot for *_, bot in rows):
                parts.append(f'<div class="pr-sub">{html.escape(_pr_phrase(kind, info))}</div>')
            if rows:
                rvs = []
                for login, disp, age, bot in rows:
                    age_html = f'<span class="age">{html.escape(age)}</span>' if age else ""
                    rvs.append(f'<div class="rvr{" bot" if bot else ""}">{review_icon(disp)}'
                               f'<span class="who">{html.escape(login)}</span>{age_html}</div>')
                parts.append(f'<div class="rvs">{"".join(rvs)}</div>')
        parts.append('</div>')
    for num in checking:
        url = _pr_github_url(num)
        label = f"#{num}"
        link = (f'<a href="{html.escape(url, quote=True)}" target="_blank" '
                f'rel="noopener">{label}</a>' if url else label)
        parts.append(f'<div class="prb done"><div class="pr-line">'
                     f'{review_icon("unknown")}{link}'
                     f'<span class="mini">checking</span></div></div>')
    ages = [int(time.time() - _PR_CACHE[n]["at"]) for n, _ in infos if n in _PR_CACHE]
    if ages:
        parts.append(f'<div class="checked">checked {_age_label(min(ages))}</div>')
    return "".join(parts)


def _pr_signature():
    """A hashable snapshot of every issue's PR review state, so the watcher can
    bump the SSE version (and live-reload open pages) only on a material change."""
    sig = []
    for it in list_issues():
        for n in _pr_refs(it):
            info = pr_info(n)
            if not info:
                sig.append((n, None))
                continue
            revs = tuple(sorted((r[0], r[1]) for r in _pr_reviewers(info)))
            sig.append((n, _pr_state(info), revs))
    return tuple(sig)


# --------------------------------------------------------------------------- #
# Pull requests view — one deduped entry per PR, grouped by review standing
# --------------------------------------------------------------------------- #
# A lens across the issues, like Waves: the same `pr:` field the row aggregate and
# the Properties panel already read, gathered into one distinct-PR-per-row list so
# the human question — what is in flight, and where does each PR stand — is answered
# in the layout itself. Actionable sections first (changes/pending/approved), the
# terminal Merged and Closed sections trailing muted. Fail-soft: with gh down the
# frontmatter still yields every number and its issues, so the view degrades to a
# flat list that keeps its links. No tooltips anywhere here.

# Standing sections, in display order; each renders only when non-empty.
_PR_SECTIONS = [("changes", "Changes requested"), ("pending", "Awaiting review"),
                ("approved", "Approved"), ("merged", "Merged"), ("closed", "Closed")]


def _pr_num_key(n):
    """Sort key for a PR number string; non-numeric refs sort as 0."""
    try:
        return int(n)
    except (TypeError, ValueError):
        return 0


def _distinct_prs(issues):
    """(order, owners): the distinct PR numbers across issues in first-seen order,
    and {num: [issue,...]} naming every issue that references each (so PRs shared by
    two issues — e.g. a split task — appear once, listing both)."""
    order, owners = [], {}
    for it in issues:
        for n in _pr_refs(it):
            if n not in owners:
                owners[n] = []
                order.append(n)
            owners[n].append(it)
    return order, owners


def _pr_open_count(issues):
    """The sidebar/head badge: PRs whose gh state is OPEN (drafts counted,
    merged/closed excluded). With gh unavailable for every PR, fall back to the
    count of distinct pr: numbers so the badge still reads as PRs in flight."""
    nums, _ = _distinct_prs(issues)
    infos = [pr_info(n) for n in nums]
    if not any(infos):
        return len(nums)
    return sum(1 for i in infos if i and (i.get("state") or "").upper() == "OPEN")


def _pr_repo_slug():
    """owner/repo the pr: numbers reference, for building GitHub links when gh is
    down: SLATE_REPO wins; else parse ROOT's git origin. None if neither is known."""
    repo = (os.environ.get("SLATE_REPO") or "").strip()
    if repo:
        return repo
    root = str(_git_root(ROOT) or ROOT)
    try:
        proc = subprocess.run(
            ["git", "-C", root, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=PR_TIMEOUT)
        if proc.returncode != 0:
            return None
        url = proc.stdout.strip()
    except Exception:
        return None
    # git@github.com:owner/repo.git  or  https://github.com/owner/repo(.git)
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def _pr_github_url(num):
    """A github.com pull URL for a PR number when the repo is known, else None —
    so a degraded row links out only when it can."""
    repo = _pr_repo_slug()
    return f"https://github.com/{repo}/pull/{num}" if repo else None


def _pr_issue_links(items):
    """The owning issues of a PR as internal links (the wikilink accent style)."""
    return "".join(f'<a class="wl" href="{url_for("issue", it["id"])}">'
                   f'{html.escape(it["id"])}</a>' for it in items)


def _pr_ledger_row(num, info, owners):
    """A live PR's row. Identity on line 1: number linked to GitHub, PR title, a
    draft/ready chip, owning issues pinned right. Standing on line 2: the phrase,
    then each reviewer's verdict glyph, login and age (bots muted, last). No leading
    review glyph — the section heading carries it. Merged/closed drop line 2 and
    read as a single muted line (their standing is terminal)."""
    kind, chip = _pr_state(info)
    draft = bool(info.get("isDraft")) and kind in ("pending", "approved", "changes")
    terminal = kind in ("merged", "closed")
    url = html.escape(str(info.get("url") or ""), quote=True)
    label = f"#{num}"
    numhtml = (f'<a class="prn" href="{url}" target="_blank" rel="noopener">{label}</a>'
               if url else f'<span class="prn">{label}</span>')
    title = html.escape(str(info.get("title") or ""))
    cls = "pritem" + (" dim" if terminal else "") + (" draft" if draft else "")
    l1 = (f'<div class="l1">{numhtml}<span class="ptitle">{title}</span>'
          f'<span class="mini">{html.escape(chip)}</span>'
          f'<span class="right">{_pr_issue_links(owners)}</span></div>')
    if terminal:
        return f'<div class="{cls}">{l1}</div>'
    l2 = [f'<span class="ph">{html.escape(_pr_phrase(kind, info))}</span>']
    for login, disp, age, bot in _pr_reviewers(info):
        age_html = f' <span class="a">{html.escape(age)}</span>' if age else ""
        l2.append('<span class="sep">·</span>')
        l2.append(f'<span class="rv{" bot" if bot else ""}">{review_icon(disp)}'
                  f'{html.escape(login)}{age_html}</span>')
    return f'<div class="{cls}">{l1}<div class="l2">{"".join(l2)}</div></div>'


def _pr_degraded_row(num, owners):
    """A degraded row (gh down): a dashed unknown ring, the PR number (linked to
    GitHub only when the repo is known, plain text otherwise), the owning issues,
    and the first owner's issue title standing in for the PR title."""
    ring = review_icon("unknown")
    url = _pr_github_url(num)
    label = f"#{num}"
    numhtml = (f'<a class="prn" href="{html.escape(url, quote=True)}" target="_blank" '
               f'rel="noopener">{label}</a>'
               if url else f'<span class="prn">{label}</span>')
    title = html.escape(str(owners[0]["title"])) if owners else ""
    return (f'<div class="pritem"><div class="l1">{ring}{numhtml}'
            f'<span class="right-in">{_pr_issue_links(owners)}</span>'
            f'<span class="ptitle deg">{title}</span></div></div>')


def render_prs_page(live=True):
    """The Pull requests view: one entry per distinct PR across all issues, grouped
    by review standing. Live PRs render a two-line ledger row; merged/closed collapse
    to one muted line. With gh unavailable for every PR (a gh outage, or a static
    build where pr_info is always None) the view degrades to a flat list with a
    notice — the frontmatter still yields every number, its issues, and a GitHub link."""
    issues = [it for it in list_issues() if _pr_refs(it)]
    order, owners = _distinct_prs(issues)
    infos = {n: pr_info(n) for n in order}
    live_nums = [n for n in order if infos[n]]
    checking = [n for n in order if infos[n] is None and pr_pending(n)]
    head = (f'<div class="view-head"><h1>{prs_icon()}Pull requests'
            f'<span class="vcount">{_pr_open_count(issues)}</span></h1></div>')
    if not live_nums:                        # gh down / first fetch / static build
        notice = (('<div class="notice">Checking GitHub — first fetch in flight. '
                   'Standings and reviewers appear as it lands.</div>')
                  if checking else
                  ('<div class="notice">GitHub state unavailable — listing the '
                   '<code>pr:</code> links from the issue files. Each number still '
                   'links to GitHub; standings and reviewers return when '
                   '<code>gh</code> does.</div>'))
        rows = "".join(_pr_degraded_row(n, owners[n])
                       for n in sorted(order, key=_pr_num_key, reverse=True))
        body = f'<div class="prv">{notice}<section>{rows}</section></div>'
        return page(f"Pull requests · {project_title()}", sidebar_html("Pull requests"),
                    head + body, live=live)
    by_kind = {}
    for n in live_nums:
        by_kind.setdefault(_pr_state(infos[n])[0], []).append(n)
    parts = []
    for kind, label in _PR_SECTIONS:
        nums = sorted(by_kind.get(kind, []), key=_pr_num_key, reverse=True)
        if not nums:
            continue
        rows = "".join(_pr_ledger_row(n, infos[n], owners[n]) for n in nums)
        parts.append(f'<section><div class="group-h">{review_icon(kind)}{label}'
                     f'<span class="gcount">{len(nums)}</span></div>{rows}</section>')
    if checking:                             # first fetches in flight — hold a place
        rows = "".join(_pr_degraded_row(n, owners[n])
                       for n in sorted(checking, key=_pr_num_key, reverse=True))
        parts.append(f'<section><div class="group-h">{review_icon("unknown")}Checking'
                     f'<span class="gcount">{len(checking)}</span></div>{rows}</section>')
    ages = [int(time.time() - _PR_CACHE[n]["at"]) for n in live_nums if n in _PR_CACHE]
    if ages:
        parts.append(f'<div class="checked">checked {_age_label(min(ages))}</div>')
    body = f'<div class="prv">{"".join(parts)}</div>'
    return page(f"Pull requests · {project_title()}", sidebar_html("Pull requests"),
                head + body, live=live)


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
        res = find_issue(iid)
        if res is None:                      # issue file vanished since the check above
            raise ValueError(f"unknown issue {iid!r}")
        p, meta, _ = res
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
    p, _, _ = res
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
    # Seed the PR baseline before the loop: this both kicks off the background
    # fetches at server start (warming the cache ahead of the first page load)
    # and anchors the signature at its unknown state, so the very first landed
    # fetch reads as a change and repaints the unknown rings away.
    pr_key = _pr_signature()
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
        if tick % 4 == 0:
            # PR review state. Pure cache reads — pr_info never blocks, and only
            # enqueues a background refresh once per TTL — so this can ride the
            # 2s cadence: a landed fetch repaints open pages (trading unknown
            # rings for real glyphs) within seconds of arriving.
            key = _pr_signature()
            if key != pr_key:
                STATE["changed"] = "reviews"
                STATE["version"] += 1
            pr_key = key
        time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):   # match the base signature; stay silent
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
        if path == "/prs":
            if any(_pr_refs(it) for it in list_issues()):
                return self._html(render_prs_page())
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
    if any(_pr_refs(it) for it in issues):
        (out / "prs.html").write_text(render_prs_page(live=False), encoding="utf-8")
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


# --------------------------------------------------------------------------- #
# doctor — the stale-review audit as a one-shot CLI, sharing review_verdict
# --------------------------------------------------------------------------- #
# The served board evaluates the same check on every render (row chips, the
# review view's strip, the issue-page badge) from cached PR state; this is the
# scriptable surface — a fresh gh fetch per PR, a printed report, and a nonzero
# exit when anything is flagged, so it slots into a check run. Read-only, like
# the board: doctor never rewrites a file.


def doctor():
    """Audit the board and print findings; return an exit status (1 if anything
    is flagged, else 0)."""
    flagged, held, indeterminate = [], [], []
    for it in list_issues():
        verdict = review_verdict(it, fetch=_fetch_pr)
        if not verdict:
            continue
        kind, merged, extra = verdict
        if kind == "stale":
            flagged.append((it, merged))
        elif kind == "held":
            held.append((it, merged, extra))
        else:
            indeterminate.append((it, merged, extra))
    _print_doctor(flagged, held, indeterminate)
    return 1 if flagged else 0


def _pr_list(nums):
    return ", ".join(f"#{n}" for n in nums)


def _doctor_entry(it, merged):
    return (f"  {it['id']}  {it['title']}\n"
            f"        status: {it['status']}   merged PRs: {_pr_list(merged)}")


def _print_doctor(flagged, held, indeterminate):
    if flagged:
        print(f"slate doctor: {len(flagged)} issue(s) in a review status with every "
              f"PR merged\n")
        for it, merged in flagged:
            print(_doctor_entry(it, merged))
        print("\n  Likely stale: the work merged but the issue never left review. Move")
        print("  each to Done (or the status that fits), or add `review_hold: <reason>`")
        print("  to its frontmatter if the review status is intentional.")
    else:
        print("slate doctor: no stale review issues — every reviewing issue still has "
              "unmerged or unresolved PRs.")
    if held:
        print(f"\nslate doctor: {len(held)} held (every PR merged, review_hold set) — "
              f"not flagged")
        for it, merged, hold in held:
            print(f"{_doctor_entry(it, merged)}\n        held: {hold}")
    if indeterminate:
        print(f"\nslate doctor: {len(indeterminate)} indeterminate — gh could not resolve "
              f"some PRs, so 'all merged' can't be asserted")
        for it, merged, unknown in indeterminate:
            print(f"  {it['id']}  {it['title']}\n"
                  f"        merged: {_pr_list(merged) or 'none'}   "
                  f"unresolved: {_pr_list(unknown)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "build":
        build(sys.argv[2] if len(sys.argv) > 2 else "_site")
    elif cmd == "doctor":
        sys.exit(doctor())
    elif cmd == "install":
        install(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        serve()
