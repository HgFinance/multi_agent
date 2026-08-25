-- Bound per-session analytical memory in the market-data database.
--
-- PostgreSQL may allocate work_mem once per sort/hash node and once per
-- parallel worker.  The previous 32MB default, combined with unrestricted
-- analytical statements, allowed a single research query plan to multiply
-- memory well beyond the 4GiB container boundary.  The market database has
-- also accumulated tens of GiB of temporary spill files, so increasing
-- work_mem would make the failure mode worse.
--
-- These settings are intentionally database-scoped.  Trading, conditional
-- order, accounting, and control-plane sessions use the separate `control`
-- database and are not given an analytical statement timeout here.

alter database market set work_mem = '8MB';
alter database market set maintenance_work_mem = '128MB';
alter database market set temp_file_limit = '512MB';
alter database market set idle_in_transaction_session_timeout = '60s';
