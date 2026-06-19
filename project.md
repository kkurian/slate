---
title: slate
status: Active
updated: 2026-06-19
---

# slate

A local task tracker kept in plain markdown. This project file references one
markdown file per issue under `issues/`. The board above counts issues by status;
the sidebar lists them.

slate is the viewer, not the data. The markdown files are the source of truth —
readable in any editor or on GitHub. If the viewer breaks, nothing is lost.

## Use it

```
python3 slate.py            # live server at http://localhost:8787
python3 slate.py build out  # standalone HTML into ./out/
```

The live server reloads open pages when a file changes on disk. See `README.md`.

## Add an issue

Copy `templates/issue.md` to `issues/<ID>.md`, give it the next number, fill in the
frontmatter. It appears on the board automatically.

## Conventions

- `status`: Backlog, Todo, In Progress, In Review, Done, Canceled.
- `priority`: Urgent, High, Medium, Low, No priority.
- Link issues to each other with `[[SLATE-1]]` wikilinks.

The issues below track slate's own development. Delete them and start your own.
