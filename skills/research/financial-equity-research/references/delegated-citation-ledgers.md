# Delegated citation-ledger isolation

## Why this matters

Parallel research workers can share a profile-level default citation ledger even when their conversational contexts are separate. A worker that resets or appends to the default ledger can silently renumber another worker’s sources, making copied `[n]` citations invalid or point to the wrong URLs.

## Reproduction pattern

1. Worker A resets the default ledger and registers sources `[1]`–`[7]`.
2. Worker B resets or appends to the same default ledger.
3. Worker A renders/validates later; source IDs may now refer to Worker B’s URLs.

## Reliable procedure

Use a unique ledger for each worker and task:

```bash
S=/path/to/grounded-citations/scripts/sources.py
L=/tmp/<root-task>-<assignee>-citations.json
python3 "$S" --ledger "$L" reset
python3 "$S" --ledger "$L" add <url> --title "..."
python3 "$S" --ledger "$L" render --replace-in report.md
python3 "$S" --ledger "$L" verify report.md --strict --min-coverage 0.60
```

Before parent merge, either re-register all URLs in the parent’s canonical ledger and rewrite citations, or hand off the worker report with its explicit Sources block and ledger path. Never assume citation numbers are portable between ledgers.

## Handoff checklist

- report contains URLs rendered from the worker ledger;
- worker ledger path is recorded for audit/debugging;
- parent knows whether IDs must be remapped;
- primary source PDFs/pages and retrieval dates are preserved;
- verification output is included in the handoff summary.
