begin;

-- Kanban QA projection inserts deterministic finding UUIDs with
-- ON CONFLICT (finding_id) DO NOTHING. PostgreSQL requires read access to the
-- conflict target for that idempotent insert, but the audit API role should
-- not gain broad read access to finding payloads.
grant select (finding_id) on audit.findings to svc_audit_api;

commit;
