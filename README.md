<h1 align="center">
  s l a t e
  <br>
  <sub>long-horizon project tracking for agents and their humans</sub>
</h1>

<p align="center">
  <strong>Agents can run whole projects now. slate is where the project lives — and how you watch it move.</strong>
</p>

A project outlives every context window. The plan, the open threads, what shipped, what's next — that state needs a home every session can reach, or the project starts over each morning. slate gives it one: markdown files in your repo, the one medium agents read and write natively. Claude Code plans into them, works from them, and closes them out across as many sessions as the project takes; you watch a live board, down to which issue an agent is touching this second.

<p align="center">
  <img src="docs/screenshot.png" alt="the slate board mid-flight: a sidebar with status counts and '2 agents active · 5 workers', and an In Progress list with pulsing dots on the issues agents are touching right now" width="820">
</p>

The failure modes of long-horizon work are familiar:

- The agent lays out a plan, the session ends, and the next session has never heard of it.
- Sessions run long — or in parallel — and the only record of what's in flight is buried in transcripts.
- The project state sits in a legacy tracker, and the agent burns context on API calls to reach it — or quietly stops updating the board.

---

## How it works

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/how-it-works-dark.png">
    <img src="docs/how-it-works-light.png" alt="Claude Code sessions read and write tasks/issues/*.md — markdown, the source of truth. slate.py renders the files into a live board in your browser; the board's only writes back are reorder and restatus." width="820">
  </picture>
</p>

- **Claude Code works the files directly.** No API, no MCP server, no credentials — the installer puts a managed block in your root `CLAUDE.md`/`AGENTS.md` that teaches it the conventions, and from then on it files, updates, and closes issues as it works.
- **`slate.py` renders the files into a live board.** Any change on disk appears in place, scroll held. One Python file, stdlib only, disposable — the tracker is the markdown.
- **The board writes back exactly two fields.** Drag a row to reorder or click the status chip on an issue: the viewer rewrites `order:` / `status:` frontmatter, nothing else, ever.
- **Presence.** The viewer watches Claude Code's session transcripts and puts a pulsing dot on the issues an agent is touching *right now* — like cursors in a shared doc, never written to the files.

slate is built for **Claude Code** today: the installer wires it up, and presence reads its transcripts. The files are plain markdown any agent could adopt, but others aren't wired up automatically yet.

---

## Start

**1. Install** — from your repository root:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/kkurian/slate/main/install.sh)
```

**2. Hand the agent a project:**

```sh
claude "plan the payment-retry work as slate issues"
```

**3. Open the board:**

```sh
python3 tasks/slate.py    # → http://localhost:8787
```

That's it. Issues appear as the agent files them, dots pulse on what it's touching, and tomorrow's session inherits today's plan — the project keeps moving instead of starting over. The only requirements are Python 3 and `curl`: no account, no database, no pip, no build step.

---

## Why markdown-first

Humans and AIs run projects together. Both must track the work. This is where they part: a human reads a project through arrangement — position, grouping, the whole grasped at once. An AI perceives no arrangement. It works in text — parsing, emitting — and visual layout gives it nothing. Worse, layout is not free: rendered markup is noise the agent must still read, and that reading spends context the work itself should hold. Dressing data for an eye the agent does not have is pure waste. I do not tolerate waste.

slate inverts the usual priorities of a task tracker. Most are built for the eye: the work sits in a database behind a visual interface; the machine reaches it through an API, second-class. slate makes the markdown primary — one file per issue, exactly what an agent reads and writes unassisted. The board is rendered from those files, for you. The view serves the human; the source serves the agent. Neither reader is an afterthought.

The markdown is the system of record — it lives in your repo, branches with it, diffs in review, and outlasts any session, any viewer, any vendor. Delete `slate.py`; the project is still all there.

---

## What the installer does

One command from the repo root leaves you with this:

```
your-repo/
├── CLAUDE.md          ← managed block appended (or AGENTS.md — whichever you use)
└── tasks/             ← or any directory you pass as the argument
    ├── project.md     ← starter overview; yours from here on, never overwritten
    ├── issues/        ← one markdown file per issue
    ├── templates/
    │   └── issue.md   ← copy to create an issue
    ├── AGENTS.md      ← the tracker conventions the agent loads
    └── slate.py       ← the viewer
```

- **Re-run any time to update slate.** Your `project.md`, your issues, and your existing agent instructions are never touched.
- **The managed block is what makes the agent show up.** It goes in the **root** instructions file because that's the one an agent loads no matter where in the repo it works:

  ```
  <!-- slate:begin -->
  ## Task tracking (slate)
  ...
  @tasks/AGENTS.md
  <!-- slate:end -->
  ```

  `CLAUDE.md` gets an `@`-import, which Claude Code loads every session; `AGENTS.md` gets a path reference, since it has no import mechanism. Re-run just this step with `python3 tasks/slate.py install`.

---

## The viewer

```sh
python3 slate.py            # live board at http://localhost:8787
python3 slate.py build out  # standalone HTML in ./out/ — no server needed, no write paths
```

- **Live, everywhere.** `project.md` is the overview, each status a view, each issue a page. Navigation swaps in place; a file edited on disk updates the open page and holds your scroll. Even `slate.py` reloads itself: edit it and the server re-execs — a syntax error leaves the old server running until you fix it.
- **Agent presence.** The viewer watches Claude Code's transcripts (`~/.claude/projects/<project-slug>/`, workflow subagents included; override with `SLATE_TRANSCRIPTS`). A transcript written in the last 90 seconds is a live agent: the sidebar counts agents — and workers, when a workflow fans out — an issue pulses while its file is the target of a tool call, and its page shows an "agent working" badge. Presence is ephemeral: never written to the markdown, absent from static builds, a hint rather than an audit log.
- **Two write paths, no more.** Drag a row within a status view to reorder it (Esc cancels), or click the status chip on an issue page to move it. Each rewrites only the `order:` / `status:` frontmatter of the issues involved.
- **Knobs.** `SLATE_PORT` sets the port; drag the sidebar's edge to resize it.

---

## Format

A project file and one file per issue — markdown with YAML frontmatter. Copy `templates/issue.md` to `issues/<ID>.md` and it's on the board, no rebuild.

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

| field | values | drives |
|---|---|---|
| `status` | Backlog · Todo · In Progress · In Review · Done · Canceled | which view the issue is in, sidebar counts |
| `priority` | Urgent · High · Medium · Low · No priority | the priority marks |
| `order` | integer, optional | position within the status — set by dragging, or by hand |

Link issues with `[[T-2]]` wikilinks. The sidebar brand shows the `title` from your `project.md`, so slate reads as native to the project it sits in.

---

## Design

- **The markdown is the source of truth.** The viewer is disposable; the project outlasts it.
- **Nearly read-only by construction.** The server's only writes are the `order` and `status` frontmatter fields; it cannot alter the tracker beyond that.
- **One renderer, two outputs.** The live server and the static build share the same rendering, so they cannot drift.
- **Zero dependencies.** Python standard library only.

The viewer renders a focused subset of markdown — headings, lists, task checkboxes, tables, code, blockquotes, links, and wikilinks — enough for issues.

---

## License

MIT. See [LICENSE](LICENSE).
