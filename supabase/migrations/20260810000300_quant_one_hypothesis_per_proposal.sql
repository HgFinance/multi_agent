-- 기획안 하나 = 가설 하나.
--
-- Gate 0 접수가 두 번 돌면 같은 기획안이 가설을 두 개 만든다. 그러면 같은
-- 실험이 서로 다른 hypothesis_id 로 두 번 계수되고, trial_family 의 분모가
-- 부풀어 DSR 감가가 실제보다 세지거나(같은 시도를 두 번 셈) 예산이 헛되이
-- 소진된다. 접수 코드가 조심하는 것으로는 부족하고 - 다른 경로(수동 등록,
-- 재실행, 동시 실행)로도 들어오므로 DB 가 막아야 한다.
--
-- 부분 유니크 인덱스인 이유: proposal_id 가 NULL 인 가설이 이미 15건 넘게 있다
-- (기획안 경로가 생기기 전에 만들어진 것들). 그것들은 서로 충돌하면 안 된다.
create unique index if not exists quant_hypotheses_proposal_id_uniq
    on quant.hypotheses (proposal_id)
 where proposal_id is not null;

comment on index quant.quant_hypotheses_proposal_id_uniq is
  '기획안 하나는 가설 하나만 만든다 - 중복 접수가 시도 계수를 부풀리는 것을 막는다';
