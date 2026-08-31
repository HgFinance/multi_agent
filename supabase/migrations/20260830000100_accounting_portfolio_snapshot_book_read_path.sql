begin;

-- portfolio_snapshot_latest is commonly filtered by book_id. Keep the
-- existing fund_id-leading PIT index for its callers and add this separate
-- read path for book_id lookups. IF NOT EXISTS makes a deployment after the
-- live concurrent installation a no-op.
create index if not exists accounting_portfolio_snapshots_book_pit_idx
    on accounting.portfolio_snapshots (book_id, fund_id, as_of desc, created_at desc);

commit;
