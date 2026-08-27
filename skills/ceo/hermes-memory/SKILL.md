---
name: hermes-memory
description: "Use the single governed CEO memory path for bounded experience retrieval and safe terminal learning."
---

# Hermes Memory

CEO memory is an advisory experience loop, not model-weight training and not a
second database. Use this skill when a CEO workflow needs prior routing or
operational lessons, or when a terminal outcome is eligible to become a lesson.

## Canonical path

- `orchestration.experience_bank.ExperienceBank` and the
  `experience.workflow_experiences` relation are the only durable CEO
  experience store. Do not create another JSON, SQLite, Notion, or profile
  memory store.
- The BFF performs the bounded D5 lookup before creating a workflow root. The
  CEO supervisor records one aggregate after response-plane finalization, and
  the portfolio pipeline records its own terminal outcome. Reuse these paths;
  do not add a second recorder or a second planner call.
- D5 is advisory. It may suggest a previously successful department set or
  orchestration policy, but it never authorizes an order, risk decision,
  accounting value, approval, promotion, or response rewrite.

## Source boundaries

Use sources by role and keep their raw content out of memory:

1. Current authoritative department/API data answers current market,
   portfolio, mandate, order, ledger, and risk questions.
2. D5 supplies bounded prior workflow outcomes: case type, departments,
   policy, success, safe failure codes, latency, and a short lesson.
3. LangSmith contributes only metadata-only observations and QA-approved,
   benchmark-passed feedback. Pending, failed, or unapproved feedback is not
   a lesson and must not enter planning hints.
4. Discord/Kanban supplies the terminal root identity and structured workflow
   status. Notion is a projection/audit surface, not a second memory source.
   Hermes logs supply bounded counters and error classes only.

Never persist prompts, answers, tool arguments/results, page blocks, Discord
message bodies, credentials, raw log lines, prices, positions, cash, NAV,
mandate limits, or policy text. Keep source IDs and bounded metadata only.

## Retrieval and learning rules

- Fresh evidence wins over memory. If current state is required, delegate to
  the owning department and read its authoritative result; do not answer from
  a remembered value.
- Use memory for routing and process improvement, not factual substitution.
  A missing or unavailable memory lookup is inconclusive and must not change a
  safe workflow into an approval.
- Write only after a terminal workflow has a stable root/source identity.
  Record success or a safe hold plus deterministic failure codes; do not write
  running, partial, or speculative lessons.
- Deduplicate by the existing `experience_identity` (one logical root/run =
  one record). Replays and repeated observations must update coordination
  state or be ignored, never create another lesson row.
- Treat a lesson as a candidate operating pattern. It becomes a skill/code
  change only through the existing QA-approved LangSmith feedback and governed
  skill-evolution benchmark; this skill never self-edits code or profiles.

## Safety check

Before using or writing memory, verify: source role is known, identity is
present, payload is bounded/redacted, terminal status is known, and the result
cannot cross an authority boundary. If any check fails, omit the memory item,
log only a safe error category, and continue with the normal fail-closed path.
