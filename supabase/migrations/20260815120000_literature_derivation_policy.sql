begin;

-- Existing AST-ready leads predate the public-baseline/derived-candidate split.
-- We cannot reconstruct an unrecorded derivation safely, so classify them
-- conservatively as replication controls.  New leads are stamped by
-- lead_intake.py and only alpha_candidate_eligible=true reaches the planner.
update research.methodology_leads
   set ast_contract = ast_contract || jsonb_build_object(
       'novelty_policy_version', 'literature-derivation-v1',
       'derivation_mode', 'DIRECT_REPLICATION',
       'derivation_transforms', '[]'::jsonb,
       'novelty_rationale', '',
       'source_baseline_expr', ast_contract->'candidate_signal_expr',
       'source_baseline_fingerprint', coalesce(ast_contract->>'ast_fingerprint', ''),
       'source_baseline_shape_fingerprint',
           coalesce(ast_contract->>'ast_shape_fingerprint', ''),
       'candidate_vs_source_similarity', 1.0,
       'alpha_candidate_eligible', false,
       'novelty_classification', 'PUBLIC_BASELINE_CONTROL'
   )
 where ast_contract->>'ast_readiness' = 'AST_READY'
   and not (ast_contract ? 'novelty_policy_version');

comment on column research.methodology_leads.ast_contract is
  'Executable AST readiness plus public-baseline derivation lineage. DIRECT_REPLICATION is a control; only alpha_candidate_eligible=true may seed a new alpha proposal.';

commit;
