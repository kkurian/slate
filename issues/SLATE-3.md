---
id: SLATE-3
title: Static HTML export
status: In Review
priority: Medium
assignee:
labels: [viewer]
project: slate
parent:
due:
created: 2026-06-19
updated: 2026-06-19
---

## Description

A `build` command that writes self-contained HTML — one page for the board and one
per issue — that opens without the server and without any runtime. The same
renderer produces both the live and static output so the two cannot drift.

## Acceptance criteria

- [x] `python3 slate.py build out` writes `index.html` and one file per issue
- [x] Links resolve to relative `.html` files in static mode
- [x] CSS is embedded; pages are fully self-contained
- [ ] Confirm pages open correctly under `file://` across browsers

## Notes / decisions

- Static mode omits the live-reload script and emits plain links, so it works with
  no JavaScript runtime assumptions.
