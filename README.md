<h1 align="center">
  s l a t e
  <br>
  <sub>task tracking for AI and their humans</sub>
</h1>

Humans and AIs build software together. Both must track the work. This is where they part: a human reads a project through arrangement — position, grouping, the whole grasped at once. An AI perceives no arrangement. It works in text — parsing, emitting — and visual layout gives it nothing. Worse, layout is not free: rendered markup is noise the agent must still read, and that reading spends context the work itself should hold. Dressing data for an eye the agent does not have is pure waste. I do not tolerate waste.

Most trackers are built for the eye. The work lives in a database behind a visual interface; the machine reaches it through an API, second-class. slate inverts the priority. The record is plain markdown, one file per issue — exactly what an agent reads and writes unassisted. From it, slate renders a board for you. The view serves the human; the source serves the agent. Neither reader is an afterthought.

The markdown is the system of record. The viewer is one Python file, standard library only, and disposable. Delete it; nothing is lost.

<p align="center">
  <img src="docs/screenshot.png" alt="slate rendering a task board: a status-grouped sidebar, count chips, and the project overview in a dark, Linear-style interface" width="820">
</p>

---

## Install

From your repository root:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/bioneural/slate/main/install.sh)
```

This copies slate into `tasks/` (pass a different directory as the argument), writes a starter `project.md`, and makes your agent aware of the tracker. Re-run any time to update; your `project.md`, your issues, and your existing agent instructions (`CLAUDE.md` or `AGENTS.md`) are left untouched.

The only requirements are a Python 3 interpreter and `curl` — both already on your system. The viewer itself uses nothing but the Python 3 standard library: no pip, no npm, no build step.

### How the agent learns about slate

The installer writes a managed block into your repository's **root** agent-instructions file, telling the agent to track work in slate. It targets whichever your repo already uses — `CLAUDE.md`, `AGENTS.md`, or both — and defaults to `CLAUDE.md` when neither exists:

```
<!-- slate:begin -->
## Task tracking (slate)
...
@tasks/AGENTS.md
<!-- slate:end -->
```

This step is required. An agent loads only the **root** instructions file across the whole repo; a copy nested under `tasks/` loads only when the agent happens to work in that subtree. The root block is the one thing that makes an agent working anywhere in the repo aware of the tracker.

`CLAUDE.md` gets an `@`-import (`@tasks/AGENTS.md`), so Claude Code loads the conventions every session. `AGENTS.md` has no import mechanism, so it gets a path reference to the same file instead. Run this step alone any time with `python3 tasks/slate.py install`.

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
