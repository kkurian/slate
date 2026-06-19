# Working with slate

slate is a task tracker kept in plain markdown — a project file and one file per
issue, rendered by a read-only web viewer. The markdown is the source of truth. You
maintain the tracker by editing the files, never through the rendered interface.

## Where things are

- `project.md` — project overview and board.
- `issues/<ID>.md` — one file per issue.
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
- `status`: Backlog, Todo, In Progress, In Review, Done, Canceled. Drives the board
  grouping and counts.
- `priority`: Urgent, High, Medium, Low, No priority.
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

## Rules

1. Edit the markdown files. The web viewer is read-only.
2. The markdown is canonical. Do not store task state anywhere else.
3. One issue per file; `id` matches the filename stem.
4. Keep `status` accurate — the board is only as correct as the frontmatter.
