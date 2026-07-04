<h1 align="center">
  s l a t e
  <br>
  <sub>task tracking for AI and their humans</sub>
</h1>

<p align="center">
  <strong>Know what your agents are doing. Let them pick up where they left off.</strong>
</p>

Agents do more of the work now — but the work itself goes invisible: plans buried in chat scrollback, state that dies with the context window. slate makes the work durable and visible. Claude Code tracks it the one way agents natively can — markdown files in your repo — and you watch a live board, down to which issue an agent is touching this second.

<p align="center">
  <img src="docs/screenshot.png" alt="the slate board mid-flight: a sidebar with status counts and '2 agents active · 5 workers', and an In Progress list with pulsing dots on the issues agents are touching right now" width="820">
</p>

You already know the symptoms:

- Your agent makes a plan, the session ends, and the plan is gone.
- You run long or parallel sessions and can't tell what's in flight without reading transcripts.
- You pointed the agent at a legacy task tracker and watched it burn context on API calls — or quietly stop updating the board.

---

## How it works

```
              reads & writes                      renders, live
 Claude Code ───────────────►  tasks/issues/*.md ───────────────►  the board in your browser
   sessions  ◄───────────────    (markdown —         slate.py           localhost:8787
                                source of truth)
                                       ▲                                      │
                                       │     drag to reorder · status chip    │
                                       └──────── the viewer's only writes ────┘
```

- **Claude Code works the files directly.** No API, no MCP server, no credentials — the installer puts a managed block in your root `CLAUDE.md`/`AGENTS.md` that teaches it the conventions, and from then on it files, updates, and closes issues as it works.
- **`slate.py` renders the files into a live board.** Any change on disk appears in place, scroll held. One Python file, stdlib only, disposable — the tracker is the markdown.
- **The board writes back exactly two fields.** Drag a row to reorder or click the status chip on an issue: the viewer rewrites `order:` / `status:` frontmatter, nothing else, ever.
- **Presence.** The viewer watches Claude Code's session transcripts and puts a pulsing dot on the issues an agent is touching *right now* — like cursors in a shared doc, never written to the files.

slate is built for **Claude Code** today: the installer wires it up, and presence reads its transcripts. The files are plain markdown any agent could adopt, but others aren't wired up automatically yet.

---

## Start

**1. Install** — from your repository root:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/bioneural/slate/main/install.sh)
```

**2. Hand the agent some work:**

```sh
claude "plan the payment-retry work as slate issues"
```

**3. Open the board:**

```sh
python3 tasks/slate.py    # → http://localhost:8787
```

That's it. Issues appear as the agent files them, dots pulse on what it's touching, and the next session picks the plan back up — no re-explaining. The only requirements are Python 3 and `curl`: no account, no database, no pip, no build step.

---

## Why markdown-first

Humans and AIs build software together. Both must track the work. This is where they part: a human reads a project through arrangement — position, grouping, the whole grasped at once. An AI perceives no arrangement. It works in text — parsing, emitting — and visual layout gives it nothing. Worse, layout is not free: rendered markup is noise the agent must still read, and that reading spends context the work itself should hold. Dressing data for an eye the agent does not have is pure waste. I do not tolerate waste.

slate inverts the usual priorities of a task tracker. Most are built for the eye: the work sits in a database behind a visual interface; the machine reaches it through an API, second-class. slate makes the markdown primary — one file per issue, exactly what an agent reads and writes unassisted. The board is rendered from those files, for you. The view serves the human; the source serves the agent. Neither reader is an afterthought.

The markdown is the system of record. The viewer is one Python file, standard library only, and disposable. Delete it; nothing is lost.

---

## Install notes

The installer copies slate into `tasks/` (pass a different directory as the argument) and writes a starter `project.md`. Re-run any time to update; your `project.md`, your issues, and your existing agent instructions are left untouched.

It also writes a managed block into your repository's **root** agent-instructions file — `CLAUDE.md`, `AGENTS.md`, or both, defaulting to `CLAUDE.md`:

```
<!-- slate:begin -->
## Task tracking (slate)
...
@tasks/AGENTS.md
<!-- slate:end -->
```

The root file is the one an agent loads no matter where in the repo it works, so this block is what makes every session aware of the tracker. `CLAUDE.md` gets an `@`-import, which Claude Code loads every session; `AGENTS.md` gets a path reference, since it has no import mechanism. Run this step alone with `python3 tasks/slate.py install`.

---

## The viewer

```sh
python3 slate.py            # live server at http://localhost:8787
python3 slate.py build out  # write standalone HTML into ./out/
```

The live server renders `project.md` as the overview, a list view per status, and each `issues/*.md` as an issue. Navigation is instant; file changes appear in place with scroll held. That includes `slate.py` itself: edit the viewer and it re-execs in place — and if the edit has a syntax error, the old server keeps running until you fix it.

**Agent presence** comes from Claude Code's transcripts (`~/.claude/projects/<project-slug>/`, recursing into workflow subagents; override with `SLATE_TRANSCRIPTS`). A transcript written in the last 90 seconds is a live agent; the sidebar counts active agents (and workers, when a workflow fans out), an issue gets a pulsing dot while its file is the target of a tool call, and its page shows an "agent working" badge. Presence is ephemeral display state — never written to the markdown, absent from static builds, and a heuristic hint, not an audit log.

Drag a row within a status view to reorder it (Esc cancels), or click the status chip on an issue page to move it — the viewer's only write paths. `build` emits self-contained HTML that needs no server and gets neither write path. Set `SLATE_PORT` to change the port; drag the sidebar's edge to resize it.

---

## Format

A project file and one file per issue. Both are markdown with YAML frontmatter.

```markdown
---
id: T-1
title: Short imperative summary
status: In Progress
priority: High
assignee: Ada
labels: [backend]
---

## Description
What this is and why it matters. Link issues with [[T-2]] wikilinks.

## Acceptance criteria
- [ ] A concrete, checkable outcome
```

- `status`: Backlog, Todo, In Progress, In Review, Done, Canceled — drives the status views and sidebar counts.
- `priority`: Urgent, High, Medium, Low, No priority — drives the priority marks.
- `order` (optional): integer position within the status group, lowest first; set by dragging, or by hand.
- Link issues with `[[T-2]]` wikilinks.

Copy `templates/issue.md` to `issues/<ID>.md` to create an issue; it appears on the board with no rebuild. The sidebar brand shows the `title` from your `project.md`, so slate reads as native to the project it sits in.

---

## Design

- The markdown is the source of truth. The viewer is disposable.
- Nearly read-only by construction. The server's only writes are the `order` and `status` frontmatter fields; it cannot alter the tracker beyond that.
- One renderer, two outputs. The live server and the static build share the same rendering, so they cannot drift.
- Zero dependencies. Standard library only.

The viewer renders a focused subset of markdown — headings, lists, task checkboxes, tables, code, blockquotes, links, and wikilinks — enough for issues.

---

## License

MIT. See [LICENSE](LICENSE).
