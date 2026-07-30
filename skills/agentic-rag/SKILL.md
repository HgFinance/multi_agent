---
name: agentic-rag
description: "Grounded retrieve-grade-generate-verify loop over versioned policy/evidence documents, for compliance and evidence-verification checks."
version: 0.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [rag, compliance, risk, qa, langgraph]
    related_skills: []
---

# Agentic RAG: Grounded Policy/Evidence Verification

## Overview

This skill runs a small LangGraph pipeline — retrieve → grade → generate → hallucination-check, with a bounded retry loop — over a local document corpus, and returns a structured JSON verdict. It exists so that `compliance-policy-agent` (and later `evidence-qa-agent` / `hallucination-critic`) never answer from the model's own memory of "what the Mandate probably says" — every claim must trace back to a retrieved, Point-in-Time-valid document chunk.

Baseline scope only (see HEDGE_FUND_MASTER_PLAN.md 5.10 and 13.1's "don't over-build early" principle): no query rewriting, reranking, fusion, or semantic cache yet. Those are backlog items once this loop is proven in real use.

## When to use

Use this skill whenever a persona needs to answer a question that must be grounded in a specific, versioned policy document rather than general knowledge — currently: `compliance-policy-agent` checking a proposed order against the Mandate, Restricted List, or Policy Store.

## Prerequisites

- `OPENAI_API_KEY` set in the environment (this profile's `.env` already has it — risk-management uses OpenAI per its `env:` assignment).
- Python 3.9+ with `openai`, `numpy`, `langgraph`, `python-dotenv` installed (see the project's `requirements.txt`).

## How to run it

Invoke via the terminal tool. Resolve the path from the repo root with `git rev-parse
--show-toplevel` instead of hardcoding a machine-specific path — this works no matter
where the `multi_agent` repo is cloned or which machine/profile is running it:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
python3 "$REPO_ROOT/skills/agentic-rag/main.py" \
  --persona compliance-policy-agent \
  --query "Can we open a new long position in SYMBOL_A today?" \
  --as-of 2026-07-29
```

(This only works from a working directory inside the repo. If the terminal tool's cwd
is outside it, `cd` into the repo clone first — check `terminal.cwd` in the profile's
`config.yaml`.)

Output is JSON on stdout:

```json
{
  "answer": {
    "verdict": "breach" ,
    "cited_documents": ["policy-restricted-list-001"],
    "rationale": "...",
    "confidence": 0.9,
    "escalate": true
  },
  "grounded": true,
  "attempts": 1,
  "relevant_documents": [{"document_id": "...", "title": "...", "version": "...", "score": 0.83}]
}
```

Present `answer.rationale` and `answer.cited_documents` to the user; never restate the verdict without also surfacing which document(s) it came from. If `grounded` is `false` after the retry budget is exhausted, treat the verdict as inconclusive and escalate rather than acting on it — do not silently proceed as if it were a clean answer (this is the deterministic fallback the graph already enforces: an ungrounded answer defaults toward `ambiguous`/`escalate: true`, not a confident pass).

## Corpus

`corpus/compliance/` holds **sample placeholder** policy documents (Mandate, Restricted List, Concentration Policy) so the pipeline is testable today. Replace their content with the real, sourced versions before relying on this for anything beyond development — they are marked `status: SAMPLE_PLACEHOLDER` in their frontmatter for exactly this reason. Each document's frontmatter (`document_id`, `version`, `effective_from`/`effective_to`) drives the deterministic Point-in-Time filter in `src/nodes.py` — keep that frontmatter accurate when you add real documents.

## Architecture

```
retrieve (local cosine-sim over OpenAI embeddings, PIT-filtered)
   -> grade (LLM: which retrieved chunks are actually relevant)
   -> generate (LLM: structured verdict, grounded only in graded-relevant chunks)
   -> hallucination_check (deterministic: every citation must be in the relevant set)
   -> retry (loop back to retrieve, up to 3 attempts) or done
```

Retrieval math and citation-grounding checks are plain Python (`src/nodes.py`, `src/retriever.py`) — the LLM is only used for relevance judgment and drafting the verdict text, consistent with HEDGE_FUND_MASTER_PLAN.md 5.9 (deterministic checks stay out of the LLM's hands).

## Known backlog (not yet implemented — do not assume these exist)

- Step-size cap on `risk-supervisor`'s recommended position changes.
- Explicit Stop Rule (conflicting signals -> default reject/hold) on `risk-supervisor`.
- Automatic Block on incomplete evidence, for `evidence-qa-agent`.

## Extending to other personas

To wire in `evidence-qa-agent` / `hallucination-critic`, add a corpus directory (e.g. `corpus/evidence/`) and register it in `PERSONA_CORPUS` in `main.py`. The graph and retriever are already generic — nothing persona-specific is hardcoded outside `main.py`'s corpus mapping and the compliance-flavored system prompts in `nodes.py` (those two prompts should move to a small per-persona prompt table when a second persona is added, rather than being copy-pasted).
