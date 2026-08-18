begin;

-- 사용자 화면에서 저장하는 Mandate의 현재 상태를 한 행에 보관한다.
-- 기존 mandate_versions는 승인/Case FK가 참조하므로 삭제하지 않고 legacy/read-only로 남긴다.
alter table governance.mandates
  add column if not exists metadata jsonb not null default '{}'::jsonb;

comment on column governance.mandates.metadata is
  '현재 사용자 Mandate 메타데이터. UI 저장은 이 JSONB를 갱신하며 version 이력을 만들지 않는다.';

-- 기존 데이터가 있으면 mandates.current_version이 가리키는 정책을 새 현재 메타데이터로 한 번만 옮긴다.
with current_rows as (
  select m.mandate_id,
         v.objective_text,
         v.objective,
         v.allowed_assets,
         v.forbidden_assets,
         v.universe_policy,
         v.risk_bounds,
         v.approval_rules,
         v.execution_rules,
         v.content_hash,
         v.created_by,
         v.created_at
    from governance.mandates m
    join governance.mandate_versions v
      on v.mandate_id = m.mandate_id
     and v.version = m.current_version
   where m.current_version > 0
)
update governance.mandates m
   set metadata = jsonb_build_object(
         'objective_text', c.objective_text,
         'objective', c.objective,
         'policy', jsonb_build_object(
           'allowed_assets', c.allowed_assets,
           'forbidden_assets', c.forbidden_assets,
           'universe_policy', c.universe_policy,
           'risk_bounds', c.risk_bounds,
           'approval_rules', c.approval_rules,
           'execution_rules', c.execution_rules
         ),
         'content_hash', c.content_hash,
         'updated_by', c.created_by,
         'updated_at', c.created_at
       )
  from current_rows c
 where m.mandate_id = c.mandate_id
   and m.metadata = '{}'::jsonb;

commit;
