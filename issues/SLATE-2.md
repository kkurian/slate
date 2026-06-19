---
id: SLATE-2
title: Live-reload server with SPA navigation
status: Done
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

Serve the tracker from the standard-library HTTP server. Navigation between the
board and issues swaps content in place rather than reloading the page. When a file
changes on disk, open pages update and keep their scroll position.

## Acceptance criteria

- [x] `ThreadingHTTPServer` serves the board and each issue
- [x] Internal links fetch and swap the layout — no full reload
- [x] Back/forward navigation works via history state
- [x] File changes push a Server-Sent Event; the page re-fetches in place
- [x] No websocket library — SSE over a 0.5s mtime poll

## Notes / decisions

- SSE was chosen over websockets specifically to avoid a dependency. The standard
  library has no websocket server; it does have everything SSE needs.
