<h1 align="center">
  s l a t e
  <br>
  <sub>a task tracker in plain markdown</sub>
</h1>

Software is increasingly built by humans and machines together. Both need the same view of a project — what the work is, what state it is in — and the two do not read alike. A human reads rendered structure at a glance. A machine reads structured text it can parse and edit. A tracker that serves one makes the other translate.

slate serves both from a single source. A project and its issues are plain markdown on disk — the form a machine reads and writes natively. One read-only viewer renders that same markdown as a live board — the form a human reads at a glance. The work is kept as one set of files and presented in two forms at once, each optimized for the reader it serves. The agent edits the files; you watch the board; neither translates for the other.

The markdown is the system of record. slate is only the viewer — a single Python file using nothing but the standard library. Delete it and nothing is lost.

<p align="center">
  <img src="docs/screenshot.png" alt="slate rendering a task board: a status-grouped sidebar, count chips, and the project overview in a dark, Linear-style interface" width="820">
</p>

---

## Install

Copy `slate.py`, `AGENTS.md`, `templates/`, and a starter `project.md` into your repository — under a `tasks/` or `plan/` directory, or at the root. Replace the demo `issues/` with your own. There is nothing else to install.

No dependencies. The viewer uses only the Python 3 standard library — no pip, no npm, no build step. The one requirement is a Python 3 interpreter, which your system already has.

### Tell your agent

slate ships `AGENTS.md` — operating instructions an AI coding agent follows to create and update issues. Run the installer once, from wherever you placed slate:

```sh
python3 slate.py install
```

This writes a managed block into your repository's **root** `CLAUDE.md` (creating it if absent) that imports `AGENTS.md` and instructs the agent to track work in slate. It is idempotent — re-running updates the block in place. The block looks like:

```
<!-- slate:begin -->
## Task tracking (slate)

This repository tracks tasks with slate ...

@slate/AGENTS.md
<!-- slate:end -->
```

Why this step is required: Claude Code always loads the repository's **root** `CLAUDE.md`, but a `CLAUDE.md` nested inside `slate/` only loads when the agent happens to work in that subtree, and `AGENTS.md` is not auto-loaded at all. The root import is the only thing that makes an agent working anywhere in the repo aware of the tracker. The installer adds it for you; you can also add the `@`-import line by hand. Other agent tools can reference `AGENTS.md` directly.

---

## Use

```sh
python3 slate.py            # live server at http://localhost:8787
python3 slate.py build out  # write standalone HTML into ./out/
```

The live server renders `project.md` as a board and each `issues/*.md` as an issue. Navigation is instant — pages swap without a full reload. When any file changes on disk, open pages update in place and hold their scroll position.

`build` emits self-contained HTML you can open without the server, or hand to someone who has no runtime at all.

Set `SLATE_PORT` to override the default port.

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

- `status`: Backlog, Todo, In Progress, In Review, Done, Canceled — drives the sidebar grouping and the board counts.
- `priority`: Urgent, High, Medium, Low, No priority — drives the priority marks.
- Link issues to each other with `[[T-2]]` wikilinks.

Copy `templates/issue.md` to `issues/<ID>.md` to create an issue. It appears on the board with no rebuild. The sidebar brand shows whatever `title` you set in `project.md`, so slate reads as native to the project it sits in.

---

## Design

- The markdown is the source of truth. The viewer is disposable.
- Read-only by construction. The server answers GET and nothing else; it cannot alter the tracker.
- One renderer, two outputs. The live server and the static build share the same rendering, so they cannot drift.
- Zero dependencies. Standard library only.

The viewer renders a focused subset of markdown — headings, lists, task checkboxes, tables, code, blockquotes, links, and wikilinks — enough for issues.

---

## License

MIT. See [LICENSE](LICENSE).
