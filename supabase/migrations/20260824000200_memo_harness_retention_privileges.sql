begin;

-- The retention worker uses the bounded generic control-plane runtime login.
-- Grant only relation-scoped deletion; it must not receive schema-wide or
-- financial-domain write privileges. VACUUM remains disabled for this login
-- in the runtime overlay because the relation owner is postgres.
grant delete on experience.workflow_experiences to service_role;

commit;
