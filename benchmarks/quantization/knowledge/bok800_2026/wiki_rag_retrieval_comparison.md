# BOK 800 Wiki vs RAG Retrieval Diagnostic

> Retrieval-only diagnostic. No model quality score is claimed.

- Questions: `50`
- Wiki documents: `789`
- RAG glossary entries: `391`
- Glossary SHA256: `d3da5743695146b492835d1e71b7d373ffc268686917e8e4494378b7e823f369`
- Wiki non-empty hit rate: `0.94`
- RAG non-empty hit rate: `0.24`

| Question | Wiki top page | RAG matched terms | Model quality |
|---|---|---|---|
| v2-001 | `224-719fcbbe5b` | `시장평균환율(MAR), 주가수익비율(PER)` | NOT EXECUTED |
| v2-002 | `279-e3459a7868` | `MISS` | NOT EXECUTED |
| v2-003 | `560-0abfc8b270` | `MISS` | NOT EXECUTED |
| v2-004 | `638-c766bc09cc` | `MISS` | NOT EXECUTED |
| v2-005 | `638-c766bc09cc` | `주가수익비율(PER)` | NOT EXECUTED |
| v2-006 | `577-6ed387a0df` | `주가수익비율(PER), 주당순이익(EPS)` | NOT EXECUTED |
| v2-007 | `001-fb347c1d2f` | `MISS` | NOT EXECUTED |
| v2-008 | `001-fb347c1d2f` | `교환사채(EB), 주가수익비율(PER)` | NOT EXECUTED |
| v2-009 | `638-c766bc09cc` | `주가수익비율(PER)` | NOT EXECUTED |
| v2-010 | `279-e3459a7868` | `MISS` | NOT EXECUTED |
| v2-011 | `442-96006e974b` | `MISS` | NOT EXECUTED |
| v2-012 | `478-a2d29ded21` | `MISS` | NOT EXECUTED |
| v2-013 | `458-e15c5a8264` | `G2(Group of Two)` | NOT EXECUTED |
| v2-014 | `560-0abfc8b270` | `MISS` | NOT EXECUTED |
| v2-015 | `MISS` | `MISS` | NOT EXECUTED |
| v2-016 | `279-e3459a7868` | `MISS` | NOT EXECUTED |
| v2-017 | `169-2b90c63462` | `MISS` | NOT EXECUTED |
| v2-018 | `169-2b90c63462` | `MISS` | NOT EXECUTED |
| v2-019 | `008-fba8a12f2a` | `MISS` | NOT EXECUTED |
| v2-020 | `008-fba8a12f2a` | `MISS` | NOT EXECUTED |
| v2-021 | `371-975a98eba1` | `MISS` | NOT EXECUTED |
| v2-022 | `449-9af076a28c` | `MISS` | NOT EXECUTED |
| v2-023 | `169-2b90c63462` | `MISS` | NOT EXECUTED |
| v2-024 | `169-2b90c63462` | `MISS` | NOT EXECUTED |
| v2-025 | `169-2b90c63462` | `MISS` | NOT EXECUTED |
| v2-026 | `MISS` | `국민계정체계(SNA)` | NOT EXECUTED |
| v2-027 | `682-c274e5f75e` | `MISS` | NOT EXECUTED |
| v2-028 | `682-c274e5f75e` | `MISS` | NOT EXECUTED |
| v2-029 | `159-ce49f5ce66` | `MISS` | NOT EXECUTED |
| v2-030 | `078-d0e139a982` | `주가수익비율(PER)` | NOT EXECUTED |
| v2-031 | `449-9af076a28c` | `주가수익비율(PER)` | NOT EXECUTED |
| v2-032 | `001-fb347c1d2f` | `MISS` | NOT EXECUTED |
| v2-033 | `653-c497d3bad7` | `MISS` | NOT EXECUTED |
| v2-034 | `078-d0e139a982` | `주가수익비율(PER)` | NOT EXECUTED |
| v2-035 | `001-fb347c1d2f` | `환매조건부매매/RP/Repo` | NOT EXECUTED |
| v2-036 | `130-083bf395bc` | `MISS` | NOT EXECUTED |
| v2-037 | `130-083bf395bc` | `MISS` | NOT EXECUTED |
| v2-038 | `130-083bf395bc` | `MISS` | NOT EXECUTED |
| v2-039 | `140-d570036488` | `MISS` | NOT EXECUTED |
| v2-040 | `752-9ce3afa302` | `MISS` | NOT EXECUTED |
| v2-041 | `130-083bf395bc` | `MISS` | NOT EXECUTED |
| v2-042 | `653-c497d3bad7` | `MISS` | NOT EXECUTED |
| v2-043 | `653-c497d3bad7` | `MISS` | NOT EXECUTED |
| v2-044 | `MISS` | `MISS` | NOT EXECUTED |
| v2-045 | `653-c497d3bad7` | `MISS` | NOT EXECUTED |
| v2-046 | `638-c766bc09cc` | `MISS` | NOT EXECUTED |
| v2-047 | `638-c766bc09cc` | `MISS` | NOT EXECUTED |
| v2-048 | `638-c766bc09cc` | `MISS` | NOT EXECUTED |
| v2-049 | `638-c766bc09cc` | `주가수익비율(PER)` | NOT EXECUTED |
| v2-050 | `638-c766bc09cc` | `MISS` | NOT EXECUTED |

Quality remains unexecuted until both paths run against the same AWQ endpoint.
