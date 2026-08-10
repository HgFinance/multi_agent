-- 회의론자가 **어떤 지시 아래** 서명했는지를 남긴다.
--
-- 2026-08-11 회고에서 드러난 구멍이다. 1·2회차에 회의론자가 기획안을 전부
-- 기각하자 프롬프트를 바꿔("표본이 짧다는 그 자체로 REJECT 사유가 아니다")
-- 3회차에 통과시켰다. skeptic_sign 은 **누가** 서명했는지만 남기므로 그 조율이
-- 원장에 잡히지 않는다.
--
-- 실험 층에서는 "결과를 본 뒤 가설 수정" 을 사전등록으로 막는데, 기획 층에서는
-- 판정 기준 자체를 갈아가며 통과할 때까지 돌릴 수 있었다. 그러면 회의론자는
-- 게이트가 아니라 장식이다. 프롬프트 판을 기록해 대조 가능하게 만든다.
alter table research.experiment_proposals
  add column if not exists skeptic_prompt_version text not null default '',
  add column if not exists planner_prompt_version text not null default '';

comment on column research.experiment_proposals.skeptic_prompt_version is
  '회의론자가 어떤 지시 아래 서명했는가. 서명자만 남기면 프롬프트를 갈아가며 통과할 때까지 돌릴 수 있다';
