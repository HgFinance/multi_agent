begin;

alter table research.methodology_leads
    add column if not exists ast_contract jsonb not null default '{}'::jsonb;

-- Move metadata written by the short-lived refs-embedded implementation into its
-- own contract column, then restore refs to the strict SourceRef shape.
update research.methodology_leads
   set ast_contract = jsonb_build_object(
           'ast_readiness', refs->0->'ast_readiness',
           'observables', coalesce(refs->0->'observables', '[]'::jsonb),
           'candidate_signal_expr', refs->0->'candidate_signal_expr',
           'missing_data', coalesce(refs->0->'missing_data', '""'::jsonb),
           'mapping_loss', coalesce(refs->0->'mapping_loss', '""'::jsonb)
       ),
       refs = jsonb_set(
           refs, '{0}',
           (refs->0) - 'ast_readiness' - 'observables' - 'candidate_signal_expr'
                     - 'missing_data' - 'mapping_loss'
       )
 where refs->0 ? 'ast_readiness';

create index if not exists idx_methodology_leads_ast_readiness
    on research.methodology_leads ((ast_contract->>'ast_readiness'));

commit;
