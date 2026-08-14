-- Library Layer 조회 면 (2026-08-14)
--
-- ▶ 왜 필요한가 (코드 실측)
--   공장은 원장에 계속 적재하는데 **읽을 면이 없었다**: factory 테이블 위
--   view 0건, apps/·orchestration/·ai-office/ 전체에서 그 테이블 참조 0건.
--   사용자 질의 창구(liaison)가 가진 것은 `factory_outcomes`(최근 N건 덤프)
--   하나여서 "저변동 계열은 어디까지 갔나" 같은 질문에 답할 수 없었다.
--
--   질의는 Library 를 먼저 읽고, 없을 때만 공장에 비동기로 요청해야 한다.
--   그 "먼저 읽는" 대상이 여기다.
--
-- ▶ 뷰만 만든다(요약 테이블 아님)
--   실험 수십~수백 건 규모라 집계 비용이 무의미하고, 요약 테이블을 두면
--   원장과 어긋나는 두 번째 진실이 생긴다. 원장이 정본이고 뷰는 창이다.
--
-- ▶ 판정에 관여하지 않는다
--   전부 select 전용이다. 관문·교훈·시도예산 계산은 기존 경로가 그대로 한다.

-- ── ① 계열 현황: 같은 아이디어가 어디까지 갔나 ──────────────────────────
create or replace view research.v_trial_family_status as
with last_out as (
    select distinct on (trial_family_id)
           trial_family_id, decision, decided_at, lesson_codes,
           root_cause, notes, oos_summary, experiment_id
      from research.experiment_outcomes
     where trial_family_id is not null
     order by trial_family_id, decided_at desc
), agg as (
    select trial_family_id,
           count(*)                                             as outcomes,
           count(*) filter (where decision like 'REJECT%')       as rejects,
           count(*) filter (where decision = 'GATE_HOLD')        as holds,
           count(*) filter (where decision in ('PROMOTED','SUPPORTED','SUBMIT_TO_QA'))
                                                                as advanced,
           min(decided_at)                                       as first_decided,
           max(decided_at)                                       as last_decided,
           array_agg(distinct lc) filter (where lc is not null)   as all_lessons
      from research.experiment_outcomes,
           lateral unnest(coalesce(lesson_codes, '{}')) as lc
     where trial_family_id is not null
     group by trial_family_id
)
select a.trial_family_id,
       a.outcomes, a.rejects, a.holds, a.advanced,
       a.first_decided, a.last_decided, a.all_lessons,
       l.decision                                  as last_decision,
       l.root_cause                                as last_root_cause,
       l.lesson_codes                              as last_lessons,
       l.notes                                     as last_note,
       l.oos_summary                               as last_metrics,
       l.experiment_id                             as last_experiment_id
  from agg a
  left join last_out l on l.trial_family_id = a.trial_family_id;

comment on view research.v_trial_family_status is
  '계열별 현황 - 몇 번 시도했고 마지막 판정이 무엇이며 어떤 교훈이 쌓였나. '
  '사용자 질의(liaison)와 다음 기획이 같은 창으로 본다.';

-- ── ② 실험 성적표: 한 실험이 관문 어디에서 멈췄나 ────────────────────────
create or replace view research.v_experiment_scorecard as
select o.experiment_id,
       o.trial_family_id,
       o.decision,
       o.decided_at,
       coalesce(h.expected_edge->>'type', '')            as edge_type,
       coalesce(h.expected_edge->>'universe_key', '')    as universe_key,
       nullif(e.config->>'top_n', '')::int               as top_n,
       -- 관문이 보는 지표
       (o.oos_summary->>'excess_return_pct')::numeric    as excess_return_pct,
       (o.oos_summary->>'information_ratio')::numeric    as information_ratio,
       (o.oos_summary->>'max_drawdown_pct')::numeric     as max_drawdown_pct,
       (o.oos_summary->>'deflated_sharpe')::numeric      as deflated_sharpe,
       (o.oos_summary->>'pbo')::numeric                  as pbo,
       -- 위험조정 계측 (2026-08-14 신설). 명목 초과가 vol 차이에 오염됐는지
       -- 이 열들이 말해 준다.
       (o.oos_summary->>'m2_excess_ann_pct')::numeric    as m2_excess_ann_pct,
       (o.oos_summary->>'alpha_ann_pct')::numeric        as alpha_ann_pct,
       (o.oos_summary->>'appraisal_ratio')::numeric      as appraisal_ratio,
       (o.oos_summary->>'strategy_ann_vol_pct')::numeric as strategy_ann_vol_pct,
       (o.oos_summary->>'benchmark_ann_vol_pct')::numeric as benchmark_ann_vol_pct,
       -- 부품 채점표
       (o.oos_summary->>'signal_ic')::numeric            as signal_ic,
       (o.oos_summary->>'signal_ic_t')::numeric          as signal_ic_t,
       (o.oos_summary->>'turnover_total')::numeric       as turnover_total,
       o.lesson_codes,
       o.root_cause,
       o.notes,
       h.mapping_loss,
       p.llm_model_id
  from research.experiment_outcomes o
  left join quant.experiments e on e.experiment_id::text = o.experiment_id
  left join quant.hypotheses  h on h.hypothesis_id = e.hypothesis_id
  left join research.experiment_proposals p on p.proposal_id = h.proposal_id;

comment on view research.v_experiment_scorecard is
  '실험 성적표 - 관문 지표 + 위험조정 계측(M²·alpha·appraisal) + 부품 채점표'
  '(IC·회전율) + 번역 손실 각인 + 모델 스탬프를 한 행으로.';

-- ── ③ 신호 서가: 어떤 엣지가 무엇을 보였나 ───────────────────────────────
create or replace view research.v_signal_shelf as
select s.edge_type,
       count(*)                                       as experiments,
       count(*) filter (where s.decision like 'REJECT%') as rejects,
       max(s.information_ratio)                       as best_ir,
       max(s.signal_ic_t)                             as best_ic_t,
       max(s.deflated_sharpe)                         as best_dsr,
       min(s.max_drawdown_pct)                        as worst_mdd,
       max(s.decided_at)                              as last_decided,
       array_agg(distinct s.top_n) filter (where s.top_n is not null) as top_n_tried
  from research.v_experiment_scorecard s
 where s.edge_type <> ''
 group by s.edge_type;

comment on view research.v_signal_shelf is
  '신호 서가 - 엣지별로 몇 번 시험했고 최고 IR·IC·DSR 이 얼마였나. '
  '부품(단일 신호)의 천장을 한눈에 본다.';
