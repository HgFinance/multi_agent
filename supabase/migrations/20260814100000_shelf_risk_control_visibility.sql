begin;

-- 서가가 "위험을 통제해 본 적 있나" 를 보이게 한다 (2026-08-14)
--
-- ▶ 무엇이 안 보였나 (실측)
--   `v_signal_shelf` 는 다른 지표를 전부 `max()`(최고)로 요약하면서 낙폭만
--   `min()`(최악)으로 요약했다. 그래서 momentum 이 이렇게 읽혔다:
--
--       best_ir 1.2552 · best_dsr 0.9762 · worst_mdd -95.16%
--       → "수익은 최고인데 못 담을 위험"
--
--   실제 원장은 다르다. 같은 엣지의 `5da381a6` 이 낙폭 정지(-0.25)를 걸고
--   **max_drawdown_pct -25.32%** 를 냈다 - 관문(-35%)을 넘긴 값이다.
--   즉 두 관문을 각각 넘긴 적이 있고 **동시에 넘긴 적이 없을** 뿐인데,
--   서가는 그 사실을 감췄다. 서가의 목적이 "부품의 천장" 이라면 낙폭도
--   천장(가장 얕게 막은 값)을 같이 보여야 한다.
--
-- ▶ 왜 중요한가
--   위험 손잡이는 2026-08-12 에 momentum 의 낙폭 때문에 열렸다. 그런데
--   momentum 실험 14건 중 손잡이를 쓴 것은 **1건**이고, 그 1건은 top_n 을
--   20 -> 200 으로 함께 바꿨다. 손잡이가 듣는지(듣는다 - 낙폭이 -95% 에서
--   -25% 로 갔다) 와 수익이 왜 무너졌는지(top_n 과 엉켰다)가 구분되지 않는다.
--   서가에 손잡이 사용 횟수가 없으면 이 공백 자체가 안 보인다.
--
-- ▶ 판정에 관여하지 않는다
--   전부 select 전용이고 기존 열은 그대로 둔 채 뒤에 덧붙이기만 한다.
--   관문·교훈·시도예산 계산은 기존 경로가 그대로 한다.

-- ── ① 채점표: 그 실험이 위험을 통제했는가 ──────────────────────────────────
--   `config` 는 실제로 돌린 값이다(가설이 아니라). 손잡이가 안 걸렸으면
--   키 자체가 없으므로 null 이 곧 "안 걸었다" 다.
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
       p.llm_model_id,
       -- ▶ 실제로 걸린 위험 손잡이 (2026-08-14 추가, 기존 열 뒤에만 붙인다)
       nullif(e.config->>'max_drawdown_stop', '')::numeric as max_drawdown_stop,
       nullif(e.config->>'vol_target_annual', '')::numeric as vol_target_annual,
       nullif(e.config->>'max_exposure', '')::numeric      as max_exposure,
       nullif(e.config->>'min_adv_krw', '')::numeric       as min_adv_krw,
       (e.config ? 'max_drawdown_stop'
        or e.config ? 'vol_target_annual')                 as risk_controlled
  from research.experiment_outcomes o
  left join quant.experiments e on e.experiment_id::text = o.experiment_id
  left join quant.hypotheses  h on h.hypothesis_id = e.hypothesis_id
  left join research.experiment_proposals p on p.proposal_id = h.proposal_id;

-- ── ② 서가: 낙폭도 천장을 같이 보여준다 ────────────────────────────────────
--
-- ▶ **거래 0 인 실험은 성적 요약에 넣지 않는다** (2026-08-14 실측)
--   유니버스가 비어 한 주도 못 산 실험은 낙폭이 0.0 으로 남는다. 그대로
--   `max(max_drawdown_pct)` 를 하면 **아무것도 안 산 실패가 "완벽한 위험
--   통제" 로 올라온다** - 실제로 6646f45c(illiquidity_premium)와
--   9354f7fd(trend_following) 가 그렇게 best_mdd 0.0 을 만들었다.
--
--   `turnover_total = 0`(명시적으로 잰 0)만 뺀다. **null 은 남긴다** -
--   회전율을 아예 안 재던 옛 실험이 55건 중 48건이라, 같이 빼면 서가가
--   통째로 비어 버린다. 빼는 것은 아는 실패지 모르는 것이 아니다.
create or replace view research.v_signal_shelf as
select s.edge_type,
       count(*)                                       as experiments,
       count(*) filter (where s.decision like 'REJECT%') as rejects,
       max(s.information_ratio) filter (where coalesce(s.turnover_total, 1) <> 0) as best_ir,
       max(s.signal_ic_t)                             as best_ic_t,
       max(s.deflated_sharpe) filter (where coalesce(s.turnover_total, 1) <> 0)   as best_dsr,
       min(s.max_drawdown_pct) filter (where coalesce(s.turnover_total, 1) <> 0)  as worst_mdd,
       max(s.decided_at)                              as last_decided,
       array_agg(distinct s.top_n) filter (where s.top_n is not null) as top_n_tried,
       -- ▶ 낙폭 천장 = **가장 얕게 막은 값**. 다른 지표와 같은 방향으로 읽힌다.
       --   worst_mdd 만 있으면 "이 엣지는 관문을 넘을 수 없다" 로 오독된다.
       max(s.max_drawdown_pct) filter (where coalesce(s.turnover_total, 1) <> 0)  as best_mdd,
       -- ▶ 손잡이를 몇 번이나 써 봤나. 0 이면 "안 통한다" 가 아니라
       --   **아직 안 해 봤다** 는 뜻이다 - 둘은 완전히 다른 결론이다.
       count(*) filter (where s.risk_controlled)      as risk_controlled_runs,
       max(s.information_ratio) filter (
           where s.risk_controlled and coalesce(s.turnover_total, 1) <> 0)        as best_ir_risk_controlled,
       -- ▶ **뺀 것을 조용히 숨기지 않는다.** 0 이 아니면 위 요약이 몇 건을
       --   제외하고 만들어졌는지 읽는 쪽이 알아야 한다.
       count(*) filter (where s.turnover_total = 0)   as no_trade_runs
  from research.v_experiment_scorecard s
 where s.edge_type <> ''
 group by s.edge_type;

comment on view research.v_signal_shelf is
  '신호 서가 - 엣지별로 몇 번 시험했고 최고 IR·IC·DSR 이 얼마였나. '
  '부품(단일 신호)의 천장을 한눈에 본다. '
  'best_mdd=가장 얕게 막은 낙폭(관문 통과 가능성), worst_mdd=최악 낙폭. '
  'risk_controlled_runs=0 은 "안 통한다" 가 아니라 "아직 안 해 봤다" 다. '
  '성적 요약은 거래 0(turnover_total=0) 실험을 뺀 값이고, 뺀 건수는 '
  'no_trade_runs 에 남는다(회전율 미기록 옛 실험은 빼지 않는다).';

commit;
