begin;

-- GOV-02 Case Root — governance.cases.status 허용 값 명시
--
-- 소유: 영주 (CEO Office). 근거: docs/01-product/MINIMUM_SERVICE_UNIT_SPEC.md 12절
--   ("governance.cases는 현재 상태 조회용 Projection이고 governance.case_events가
--    변경 이력의 기준이다"), docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 2.2절
--
-- ## 왜 이 마이그레이션이 필요한가
--
-- governance 스키마의 모든 status 컬럼에는 check 제약이 있는데 governance.cases.status만
-- 없다(2026-08-03 실측: approvals/escalations/committee_sessions/department_handoffs 전부 있음).
-- 즉 "전사 Case Root가 어떤 상태를 가질 수 있는가"가 DDL에도 문서에도 정의돼 있지 않았다.
-- 이 값이 없으면 Case를 만드는 코드가 각자 다른 문자열을 넣게 되고, 나중에 통일하려면
-- 데이터 마이그레이션이 된다.
--
-- ## 제안하는 값과 그 근거 (리뷰 요청 대상)
--
-- OPEN -> ACKNOWLEDGED -> RESOLVED / CANCELLED
--
-- 새로 지어낸 어휘가 아니라 **같은 스키마의 governance.escalations가 이미 쓰는 값 그대로**다.
-- escalations도 Case에 붙는 범용 업무 항목이라 같은 어휘를 쓰면 두 테이블이 일관된다.
-- (참고로 department_handoffs는 REQUESTED/ACCEPTED/COMPLETED/REJECTED/EXPIRED,
--  committee_sessions는 SCHEDULED/OPEN/DECIDED/CANCELLED를 쓴다 - 후보로 검토했으나
--  전자는 '요청-수락' 성격이 강하고 후자는 회의 전용이라 Case Root에는 escalations 쪽이 맞다.)
--
-- ## 왜 Investment Case의 19단계를 여기 넣지 않는가
--
-- MINIMUM_SERVICE_UNIT_SPEC 4절의 DETECTED/QUALIFIED/.../EVALUATED는 **투자 Case 전용**이다.
-- 채용(HIRING) Case에 POSITION_OPEN은 의미가 없다. 12절이 cases를 "현재 상태 조회용
-- Projection"으로 정의했으므로 Root는 굵은 단위만 갖고, 세부 단계는 하위타입
-- (governance.investment_cases)과 변경 이력(governance.case_events)이 소유한다.
-- 투자 Case의 Root 행도 이 4개 값을 쓰고, 19단계는 investment_cases와 case_events에 남는다.
--
-- ## case_type에는 의도적으로 제약을 걸지 않는다
--
-- API 스펙 2.2가 MANDATE_CHANGE|COMMITTEE|INCIDENT|HIRING|IMPROVEMENT를 예시로 들지만
-- 투자 Case의 Root가 쓸 값은 어디에도 없다. 여기서 목록을 확정하면 트레이딩·리서치본부의
-- 투자 Case 생성을 막을 수 있어 자유 텍스트로 남긴다(그쪽 계약이 정해지면 별도 마이그레이션).
--
-- ## 적용 안전성
--
-- governance.cases는 현재 0건이고(2026-08-04 실측) 이 테이블에 쓰는 코드도 아직 없어
-- 기존 데이터를 깨뜨릴 위험이 없다. 반대로 다른 본부가 각자 Case를 만들기 시작한 뒤에는
-- 같은 변경이 데이터 마이그레이션이 되므로 지금이 비용이 가장 낮은 시점이다.
--
-- 리뷰에서 다른 어휘가 낫다고 판단되면 이 제약만 교체하면 된다 - 애플리케이션 쪽
-- 상태 머신(departments/00-ceo-office/src/case/case.py)도 같은 값을 쓰므로 함께 수정한다.
alter table governance.cases
  add constraint cases_status_check
  check (status in ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'CANCELLED'));

comment on column governance.cases.status is
  'Case Root의 굵은 진행 상태: OPEN -> ACKNOWLEDGED -> RESOLVED/CANCELLED. '
  'governance.escalations.status와 같은 어휘. 세부 단계는 하위타입(investment_cases)과 '
  'case_events가 소유한다 (MINIMUM_SERVICE_UNIT_SPEC 12절 Projection 정의).';

commit;
