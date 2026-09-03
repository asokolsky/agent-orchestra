---
title: "Agent Orchestra Reviewer Meta"
purpose: "Meta documentation for the agent-orchestra-reviewer skill."
audience:
  - reviewers
  - agent-authors
tags:
  - skill-meta
  - agent-orchestra-reviewer
authors:
  - "asokolsky@gmail.com"
created-at: "2026-09-02T14:31:12+0200"
updated-at: "2026-09-03T09:35:00+0200"
---

# Agent Orchestra Reviewer Meta

Meta documentation for the
[agent-orchestra-reviewer skill](./SKILL.md).

## Relationship To Existing Standards

- The skill follows the Agent Skills specification for its directory and
  `SKILL.md` frontmatter.
- The same `SKILL.md` is portable to OpenAI Codex and Anthropic Claude Code;
  neither runtime receives a divergent role contract.
- Version and source metadata, dependency documentation, and this companion
  file follow the conventions established by the DLI SOT skill-authoring
  workflow.
- Review remains read-only and subject to the instructions of the repo being
  reviewed and the agent runtime executing the skill.

## Dependencies

No external skills are required. The workflow uses the Git CLI already required
by agent-orchestra.

| Dependency | Source | Install |
|---|---|---|
| `git` | [Git](https://git-scm.com/) | `brew install git` |

Git establishes the assigned revisions, status, and exact diff. If Git is
unavailable or the review scope cannot be established, return a `blocked`
verdict without findings.

## Departures And Rationale

The SOT companion-document convention uses SOT-specific link syntax. This repo
uses standard Markdown links because it is not rendered by the SOT documentation
toolchain. Product-specific UI metadata is omitted because Anthropic has no
counterpart to `agents/openai.yaml`, and a shared Agent Skills source prevents
the two runtimes from drifting. No other departures are intended.

## Update Cadence

Review this skill whenever the review request/result contract, diff identity or
verdict rules, or upstream Agent Skills specification changes. Otherwise,
review it annually.
