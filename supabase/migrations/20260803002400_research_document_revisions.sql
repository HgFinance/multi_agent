-- research: 문서 정정 이력 - 탐지한 것을 버리지 않는다
--
-- 소유: 재일 (리서치본부)
-- 근거: WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md 6.2 P0 4항
--       ("뉴스·공시 수정과 삭제 이력, 원문 Hash, 수집 Version") 및 6.2 중단 조건
--       ("수정 이력 없는 문서는 전략 학습과 거래 근거에 사용하지 않는다").
--
-- ▶ 지금 무슨 일이 벌어지고 있나
--   repository/reference_repository.py 는 공급자가 제목을 바꾼 것을 **실제로
--   탐지한다**(upsert_news_documents 가 저장본과 대조). 그런데 그 결과를
--   self.last_revisions 라는 **메모리 튜플**에만 담고, 프로세스가 끝나면
--   사라진다. 저장 자체는 최초 관측본을 유지하므로(PIT 규율) 정정 사실은
--   그 튜플이 유일한 흔적인데 그것마저 휘발된다.
--
--   그래서 6.2 의 중단 조건을 판정할 데이터가 없다. "이 문서는 수정된 적이
--   있는가"를 물으면 답할 수 없고, 정정 전 판단을 재현할 때 무엇이 달라졌는지
--   모른다.
--
-- ▶ 왜 문서 행을 고치지 않고 별도 이력인가
--   research.documents 의 title/published_at 을 덮지 않는 것이 PIT 규율이다
--   (upsert 주석). 덮으면 observed_at <= as_of 재현이 미래 문장을 보게 된다.
--   그래서 원본은 그대로 두고 **변경 사실만 append** 한다 - 재무제표를
--   revision 으로 쌓은 것(002300 이전 768ad3a)과 같은 판단이다.
--
-- 되돌리기: drop table research.document_revisions;

create table if not exists research.document_revisions (
  revision_id uuid primary key default gen_random_uuid(),
  document_id uuid not null references research.documents(document_id)
    on delete cascade,
  -- 무엇이 바뀌었나. title 로 시작하고 published_at·status 로 넓힌다.
  field text not null,
  old_value text,
  new_value text,
  -- **관측 시각이다. 공급자가 언제 고쳤는지가 아니다.** 우리가 알 수 있는 것은
  -- 우리가 본 시점뿐이고, 그것을 공급자 시각인 척하면 PIT 가 거짓이 된다.
  observed_at timestamptz not null default now(),
  detected_by text not null,          -- 어느 수집기가 잡았는지
  unique (document_id, field, new_value, observed_at)
);

comment on table research.document_revisions is
  '문서 정정 이력(append-only). research.documents 의 원본은 절대 덮지 않고 '
  '변경 사실만 여기 쌓는다 - 덮으면 PIT 재현이 미래 문장을 본다.';
comment on column research.document_revisions.observed_at is
  '**우리가 변경을 관측한 시각.** 공급자가 고친 시각이 아니다 - 알 수 없는 것을 '
  '아는 척하지 않는다.';

create index if not exists document_revisions_doc_idx
  on research.document_revisions (document_id, observed_at desc);
create index if not exists document_revisions_time_idx
  on research.document_revisions (observed_at desc);

-- 정정된 적 있는 문서를 한눈에 - 거래 근거로 쓰기 전 확인하는 면이다
create or replace view research.documents_with_revisions as
select d.document_id, d.source_id, d.external_id, d.title, d.published_at,
       d.observed_at,
       count(r.revision_id)      as revisions,
       max(r.observed_at)        as last_revised_at
from research.documents d
join research.document_revisions r using (document_id)
group by d.document_id, d.source_id, d.external_id, d.title, d.published_at,
         d.observed_at;

comment on view research.documents_with_revisions is
  '정정 이력이 있는 문서만. 6.2 중단 조건("수정 이력 없는 문서는 거래 근거에 '
  '쓰지 않는다")을 판정하려면 이 면이 필요하다.';
