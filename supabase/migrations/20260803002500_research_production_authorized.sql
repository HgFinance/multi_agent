begin;

-- research: Evidence 에 '운영 근거로 써도 되는가' 를 붙인다
--
-- 소유: 재일 (리서치본부)
-- 근거: docs/03-data/DATA_GOVERNANCE_GUIDE.md 18.1(Document Metadata),
--       22절(접근과 Serving), 32절(Production Data Launch Gate).
--
-- ▶ 지금 막는 것과 안 막는 것
--   collectors/source_registry.py 의 UseScope 는 **"이 소스를 저장해도 되는가"**
--   를 막는다(8개 수집기가 check_use 로 통과한다). 그런데 하류에서
--   **"이 Evidence 를 주문 판단의 근거로 써도 되는가"** 는 아무도 판정하지 않는다.
--   그래서 Tier 2 뉴스 스니펫과 승인된 공시 원문이 **동등한 무게로** 나간다.
--
--   저장 허가와 운영 허가는 다른 질문이다. 라이선스상 저장은 되지만 상업적
--   의사결정 근거로는 쓸 수 없는 소스가 실제로 있고(가이드 3.3), 그 구분이
--   코드에 없으면 결국 "다 되는 것" 처럼 취급된다.
--
-- ▶ 기본값은 false 다 (fail-closed)
--   나중에 승인된 소스만 켠다. 기본을 true 로 두면 새 소스가 들어올 때마다
--   조용히 운영 근거가 되고, 그 순간을 아무도 모른다.
--
-- ▶ 소스 단위로 켜고 문서가 물려받는다
--   문서마다 사람이 판단할 수 없다. 승인은 소스(reference.data_sources) 단위이고
--   문서는 그것을 상속한다 - 예외가 필요하면 문서 열을 직접 덮는다.
--
-- 되돌리기: alter table ... drop column production_authorized;

alter table reference.data_sources
  add column if not exists production_authorized boolean not null default false,
  add column if not exists authorization_note text;

comment on column reference.data_sources.production_authorized is
  '이 소스의 Evidence 를 **주문·전략 판단의 근거**로 써도 되는가. 저장 허가'
  '(source_registry.UseScope)와 다른 질문이다. 기본 false - 승인된 것만 켠다.';
comment on column reference.data_sources.authorization_note is
  '승인/미승인 근거(계약 조항, 검토 일자, 담당자). 근거 없는 승인은 승인이 아니다.';

alter table research.documents
  add column if not exists production_authorized boolean;

comment on column research.documents.production_authorized is
  '문서 단위 재정의. NULL 이면 소스 값을 따른다 - 문서마다 사람이 판단할 수 '
  '없으므로 상속이 기본이고, 예외만 여기서 덮는다.';

-- 유효 권한을 한 곳에서 계산한다. 소비자가 각자 coalesce 를 쓰면 언젠가
-- 한쪽이 빠지고, 그 순간 미승인 근거가 조용히 새어 나간다.
create or replace view research.documents_effective_auth as
select d.document_id,
       d.source_id,
       d.external_id,
       d.title,
       d.published_at,
       d.observed_at,
       coalesce(d.production_authorized, s.production_authorized, false)
         as production_authorized,
       s.source_code
from research.documents d
join reference.data_sources s using (source_id);

comment on view research.documents_effective_auth is
  '문서의 **유효** 운영 권한(문서 재정의 > 소스 기본 > false). 조회면이 이 뷰를 '
  '통해서만 권한을 읽는다 - 계산이 흩어지면 한쪽이 빠져도 아무도 모른다.';

commit;
