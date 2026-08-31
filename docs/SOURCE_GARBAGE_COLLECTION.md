# Source Garbage Collection

- Generated: `2026-08-30T16:50:23.598114+00:00`
- Scope: three statically orphaned Python candidates identified in the release audit
- Method: tracked Python source references, excluding the candidate itself

## Result

| Candidate | Source refs | Test refs | State |
|---|---:|---:|---|
| `apps/api/fact_router.py` | 0 | 0 | `REMOVED` |
| `apps/api/ceo_hermes_client.py` | 0 | 0 | `REMOVED` |
| `departments/02-trading/contracts/packet_gate.py` | 0 | 0 | `REMOVED` |

All registered retired candidates are absent and have no tracked source or test references. This is cleanup of the working tree and Git history retains recovery. The scanner cannot prove that an external deployment or dynamic import is absent, so future additions must re-run the audit before deleting another compatibility surface.

## Re-run

```bash
python scripts/source_garbage_collector.py
python scripts/source_garbage_collector.py --write
```
