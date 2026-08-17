begin;

-- 최초 리드는 출처 해시를 유지한다. 같은 출처에서 다른 AST 계약이 파생되면
-- lead_intake가 `<source_lead_id>_r<contract_hash>`를 발급한다. 원 행을 UPDATE하지
-- 않는 이유는 이미 그 리드를 인용한 proposal/experiment의 PIT 의미를 보존하기 위해서다.
comment on column research.methodology_leads.lead_id is
  '최초 해석은 url+title 출처 해시. 같은 출처의 다른 AST 계약은 '
  '<source_lead_id>_r<canonical_contract_hash> revision ID로 별도 보존한다.';

comment on table research.methodology_leads is
  '스카우트 방법론 리드의 불변 원장. 같은 출처·같은 AST 계약 재수집은 '
  'independent_mentions로 접고, 다른 AST 계약은 결정론적 revision lead로 분기한다.';

commit;
