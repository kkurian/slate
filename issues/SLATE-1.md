---
id: SLATE-1
title: Single-file markdown renderer
status: Done
priority: High
assignee:
labels: [viewer]
project: slate
parent:
due:
created: 2026-06-19
updated: 2026-06-19
---

## Description

Render the subset of markdown that issues actually use, with no third-party
library. Headings, paragraphs, bold/italic, inline and fenced code, ordered and
unordered lists, task checkboxes, tables, blockquotes, horizontal rules, links,
and `[[wikilinks]]`. Frontmatter is parsed into the properties panel.

## Acceptance criteria

- [x] Block and inline rendering implemented in the standard library
- [x] Frontmatter parsed (scalars and `[lists]`)
- [x] Wikilinks resolve to issue pages in both live and static modes
- [x] HTML is escaped before formatting is applied

## Notes / decisions

- A full CommonMark parser is out of scope. The renderer targets the markdown we
  write, not arbitrary input.
