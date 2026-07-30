---
document_id: evidence-news-symbol-b-001
document_type: news_article
version: "1.0.0"
effective_from: "2026-06-02"
effective_to: "2026-09-02"
status: SAMPLE_PLACEHOLDER
---

# Sample News Article — SYMBOL_B Regulatory Review (Sample — replace before production use)

## Event
On 2026-06-02, the sector regulator announced a preliminary review of SYMBOL_B's Q1 2026 disclosure
practices, following complaints about the timing of a material contract announcement. The review is
described as preliminary and non-punitive at this stage; no finding of wrongdoing has been made.

## Company Response
SYMBOL_B issued a statement saying it is cooperating fully with the review and that it believes its
Q1 2026 disclosures complied with applicable rules.

## Why `effective_to` Is Set
Unlike the earnings release and analyst note in this sample corpus, this article is time-bound: it
describes the state of an ongoing review as of early June 2026, which the reviewing agent must not
treat as still current if asked about a later `as_of` date without a follow-up article. This exists
so the Point-in-Time filter in `src/nodes.py` has at least one expiring document to filter out in
this second persona's corpus, the same way `corpus/compliance/restricted_list.md` does for
`compliance-policy-agent`.

## Source and Review
This is a sample placeholder standing in for a real news wire item. Any Evidence QA Agent citing
this document must reference `effective_from`/`effective_to` and confirm the query's `as_of` date
falls within that window.
