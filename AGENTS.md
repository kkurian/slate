# Working with slate

slate is a task tracker kept in plain markdown — a project file and one file per
issue, rendered by a web viewer. The markdown is the source of truth. You maintain
the tracker by editing the files; the viewer's only writes are drag-to-reorder
within a status view (rewrites `order`) and the status chip on an issue page
(rewrites `status`).

## Where things are

- `project.md` — project overview and board.
- `issues/<ID>.md` — one file per issue.
- `todos/<person>.md` — optional per-person day todos: `person:` frontmatter, then
  dated `## YYYY-MM-DD` sections of `- [ ]` / `- [x]` items with `[[ID]]` wikilinks;
  drives the Today panel and the Today day view.
- `templates/issue.md` — copy this to create an issue.
- `slate.py` — the viewer. You do not need to run it to read or edit issues.

When slate is installed under a subdirectory, these paths are relative to that
directory.

## Issue format

Markdown with YAML frontmatter:

```markdown
---
id: T-1
title: Short imperative summary
status: In Progress
priority: High
assignee: Ada
labels: [backend]
project: example
parent:
due:
created: 2026-06-19
updated: 2026-06-19
---

## Description
What this is and why it matters. Link issues with [[T-2]] wikilinks.

## Acceptance criteria
- [ ] A concrete, checkable outcome

## Sub-issues
- [ ] [[T-2]] — child work, if any

## Notes / decisions
- Running log of decisions and findings.
```

- `id` must equal the filename stem. `issues/T-1.md` has `id: T-1`.
- `status`: Backlog, Todo, In Progress, In Review, Done, Canceled. Drives which
  status view an issue appears in and the sidebar counts.
- `priority`: Urgent, High, Medium, Low, No priority.
- `order` (optional): integer position within the status group, lowest first. Issues
  without it sort by id after the ordered ones. Usually set by dragging in the
  viewer; you may also set or renumber it directly.
- Reference another issue with a `[[T-2]]` wikilink.

## Create an issue

1. Copy `templates/issue.md` to `issues/<PREFIX>-<next-number>.md`, using the
   project's existing prefix and the next free number.
2. Set `id` to match the filename stem.
3. Fill `title`, `status`, `priority`, and the body.
4. Set `created` and `updated` to today's date.

## Update an issue

- Change `status` as the work moves through the lifecycle.
- Check acceptance-criteria boxes (`- [x]`) as they are met.
- Set `updated` to today's date on any edit.
- Record decisions and findings under Notes.

## Audit

The board audits itself: the live server flags each **In Review** issue whose every
`pr:` has merged — the usual sign of a status flip that was missed — with a warning
chip on the row, a strip atop the In Review view, and a badge on the issue page.
`python3 slate.py doctor` runs the same check as a one-shot CLI, printing a report
and exiting nonzero when anything is flagged. Both are read-only — no file is ever
edited. When a review status is intentional even with every PR merged — awaiting a
human flip, follow-up work, or a review happening off GitHub — record
`review_hold: <short reason>` in the issue's frontmatter; the board and doctor then
show it as held rather than flagging it.

## Rules

1. Edit the markdown files. The web viewer writes nothing except `order` and
   `status` (plus `updated`) when the human reorders or restatuses an issue.
2. The markdown is canonical. Do not store task state anywhere else.
3. One issue per file; `id` matches the filename stem.
4. Keep `status` accurate — the board is only as correct as the frontmatter.
