---
id: SLATE-4
title: Configurable issue ID prefix
status: Backlog
priority: Low
assignee:
labels: [viewer]
project: slate
parent:
due:
created: 2026-06-19
updated: 2026-06-19
---

## Description

Issue IDs use a project-chosen prefix (`SLATE-1`, `T-1`). The natural sort that
orders the sidebar already tolerates any prefix, but there is no single place to
declare the project's prefix or to scaffold the next ID. A small `new` command
could read the highest existing number and emit the next issue from the template.

## Acceptance criteria

- [ ] `python3 slate.py new "Title"` scaffolds `issues/<PREFIX>-<next>.md`
- [ ] Prefix read from `project.md` frontmatter, defaulting sensibly
- [ ] Created/updated dates filled in automatically

## Notes / decisions

- Kept out of the first cut to hold the viewer to a single read-only concern.
  Writing issues is a separate, optional capability.
