begin;

-- quant-api exposes /seeds without retaining the broad shared DATABASE_URL.
-- Its scoped role needs exactly the research source queried by
-- pipeline/research_bridge.py and no write privilege in the research schema.
grant usage on schema research to svc_quant;
grant select on table research.packet_claims to svc_quant;

commit;
