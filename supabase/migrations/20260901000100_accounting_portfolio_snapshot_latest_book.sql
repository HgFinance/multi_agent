begin;

-- Latest snapshot reads are keyed by one globally unique accounting book.
-- Production installs this index concurrently before deploying this
-- migration. IF NOT EXISTS then makes the canonical transactional migration
-- a no-op there while fresh environments retain the repository-wide
-- all-or-nothing migration contract.
create index if not exists accounting_portfolio_snapshots_latest_book_idx
    on accounting.portfolio_snapshots (book_id, as_of desc, created_at desc);

commit;
