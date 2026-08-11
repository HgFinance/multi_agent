# 호가·체결 일일 이관 - Trading_bot -> 헤지펀드 시세 평면
#
# 담당: 재일 (리서치본부 RES 수집)
# 근거: 재일님 결정 2026-08-11 "우리 수집기 당연히 교체해야 하고 ... 내일부터
#       수집은 교체 수집기로". 우리 자체 수집기(hedgefund-ls-realtime)는
#       `K3_`/`S3_` 만 구독해 **KRX 전용**이었다 - NXT 하루 340~380만 행이
#       통째로 빠져 있었다. 저쪽 수집기는 KRX+NXT 를 다 받는다.
#
# ▶ 왜 컨테이너 스케줄러(collector_scheduler.py)에 못 넣나
#   이관기는 `docker exec` 로 두 DB 컨테이너를 잇는다(trading-timescaledb 와
#   hedgefund-timescaledb). batch-collectors 컨테이너 안에는 docker 소켓이
#   없어서 그 안에서는 실행되지 않는다. 그래서 호스트 작업 스케줄러에 건다.
#
# ▶ 시각 20:30 인 이유
#   저쪽 원천은 19:59 까지 채워진다(시간외 단일가 포함, 실측). 20:00 의
#   document-archive 가 끝나는 20:14 뒤이고, 21:00 chart-daily-universe
#   앞이다. 하루치 이관은 약 9분이라 21:00 을 밀지 않는다.
#
# ▶ 멱등
#   이관기가 `done_days()` 로 이미 들어온 날을 건너뛴다. 하루를 놓쳐도
#   --date 없이 돌리면 남은 날을 전부 메운다. 실패해도 다음 날이 메운다.

$ErrorActionPreference = 'Stop'
$repo = 'c:\Users\wodlf\OneDrive\Desktop\Multi agent-hedge fund\multi_agent'
$log  = Join-Path $env:USERPROFILE 'microstructure_import.log'

Set-Location $repo
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$stamp] 일일 이관 시작" -Encoding utf8

# --date 를 주지 않는다. 어제 놓친 날이 있으면 같이 메우는 것이 맞다 -
# 사람이 백필을 기억해야 하는 구조는 결국 안 채워진다.
$env:PYTHONIOENCODING = 'utf-8'
$out = & python 'departments/01-research/collectors/import_external_microstructure.py' --run 2>&1
$code = $LASTEXITCODE

Add-Content -Path $log -Value ($out | Out-String) -Encoding utf8
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
if ($code -eq 0) {
    Add-Content -Path $log -Value "[$stamp] 일일 이관 종료 OK" -Encoding utf8
} else {
    # 실패를 조용히 넘기지 않는다 - 0건 이관이 며칠 지나도 아무도 모르는 것이
    # 이 계층에서 가장 흔한 사고다.
    Add-Content -Path $log -Value "[$stamp] ⚠ 일일 이관 실패 exit=$code" -Encoding utf8
}
exit $code
