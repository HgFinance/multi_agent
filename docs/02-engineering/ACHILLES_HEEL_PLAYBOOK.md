# 아킬레스건 해결 방책 — 2차 문헌 조사 (해결 편)

> **작성일**: 2026-08-13 · **자매 문서**: [LITERATURE_BREAKTHROUGH_MAP.md](LITERATURE_BREAKTHROUGH_MAP.md) (1차: 어디가 약한가의 지도) — 이 문서는 2차: **그 약점을 어떻게 뚫는가의 방책**.
> **방법론**: 1차에서 "위협 확인"까지만 간 아킬레스건 4개에 대해 해결책 중심 문헌 조사를 병렬 수행. 검증 소스 ~80편. 이번 조사는 소스 나열이 아니라 **공식·숫자·단계가 붙은 실행 프로토콜**을 산출물로 요구했고, 공식이 중요한 논문 3편(Triple Penance, CUSUM, Maximum Drawdown)은 원문 전문을 추출해 공식을 원문 그대로 옮겼다.

---

## 목차

1. [한 장 요약 — 4개 방책의 핵심 설계 결정](#1-한-장-요약)
2. [방책 A — PAPER 검증을 통계적으로 결정적으로 만들기](#2-방책-a--paper-검증의-통계-설계)
3. [방책 B — 신호 블렌딩·포트폴리오 계층 청사진](#3-방책-b--신호-블렌딩포트폴리오-계층)
4. [방책 C — 소형 워커 신뢰성 + Multi-LoRA 활용 플레이북](#4-방책-c--소형-워커-신뢰성)
5. [방책 D — 전략 생애주기 거버넌스 (열화 탐지·트립·은퇴·레짐)](#5-방책-d--전략-생애주기-거버넌스)
6. [통합 — 하나의 파이프라인으로](#6-통합--하나의-파이프라인으로)
7. [실행 순서 제안](#7-실행-순서-제안)

---

## 1. 한 장 요약

| 아킬레스건 | 문헌이 강제하는 핵심 설계 결정 | 결정적 근거 |
|---|---|---|
| **A. PAPER가 짧아서 증명이 안 됨** | 고정 검정 포기: MinTRL상 SR 1.0 인증에 2.7년 필요. 대신 ① 스카우트 컷오프 이후 과거를 증거로 백필, ② 매일 봐도 무벌점인 **베팅 e-process** 순차검정, ③ 전략 스트림엔 **e-LOND** 온라인 FDR, ④ **공장 수준 포트폴리오 검정**이 몇 달 안에 해소되는 진짜 지름길 | Bailey-LdP 2012 (MinTRL) · Waudby-Smith & Ramdas 2024 (베팅 CS) · Xu & Ramdas 2024 (e-LOND) |
| **B. IR 벽 — 블렌딩 계층 부재** | 블렌드 가중을 **추정하지 않는다**(1/N이 기본값, 폴백 아님). 슬리브 9개가 아니라 **통합 단일 포트폴리오**(AQR 실측: +1%/yr, IR +40%). 회전은 Gârleanu-Pedersen 부분 조정 + 무거래 밴드. 리스크 모델은 1팩터+제약이면 충분(Jagannathan-Ma 동치 정리) | DeMiguel 2009 (1/N) · Fitzgibbons 2017 (통합) · GP 2013 (aim) · Jagannathan-Ma 2003 |
| **C. 워커 계층이 최약 고리, Multi-LoRA 유휴** | 폐쇄 스키마 태스크에선 소형 파인튜닝이 프런티어를 이긴다(LoRA Land: 310개 중 224개가 GPT-4 초과). 무훈련 Stage 0(2단계 출력·검증기·합의 격상)부터 시작, 헤드 모델 트레이스 500–2,000개로 역할별 어댑터, 섀도→카나리 게이트 | LoRA Land 2024 · Distilling Step-by-Step 2023 · LIMA 2023 · Kolawole (합의 캐스케이드) |
| **D. "언제 믿기를 멈출까"의 규칙 부재** | 3중 탐지(CUSUM·BOCPD·드로다운 정합성 검정) + **오탐률이 명시된 트립 문턱**(Triple Penance 공식으로 역산) + 변동성 타기팅(유일하게 증거 강한 기계적 디리스킹) + 레짐 플래그. **원시 수익률 랭크로 은퇴 금지**(연기금 3,400개 실증: 가치 파괴) | Philips-Yashchin-Stein 2003 (CUSUM) · Bailey-LdP Triple Penance · Goyal-Wahal 2008 · Moreira-Muir 2017 |

**교차 발견 2가지**:
- **방책 A와 D가 같은 수학을 공유한다** — PSR/MinTRL, DSR, 드로다운 분위수는 하나의 통계 라이브러리로 구현되고, D의 트립 문턱은 1차 지도 12위(자동 트립)의 "임의 숫자가 아닌 문턱" 요구를 정확히 채운다.
- **방책 전부가 "추정을 줄이는 방향"이다** — 블렌드 가중 추정 금지(1/N), 워커 판단 축소(검증기·합의), 트립 문턱은 공식 역산, 검정은 가정 최소(e-process는 유계 수익률만 요구). 작은 표본·짧은 이력 체제에서 문헌의 일관된 처방은 자유도 삭감이다.

---

## 2. 방책 A — PAPER 검증의 통계 설계

**아킬레스건**: LLM 훈련 오염 때문에 컷오프 이후 + PAPER만 결정적 증거인데(1차 지도 2-7), 그 창이 몇 달뿐이다. 몇 달로 무엇을 증명할 수 있는가?

### 2.1 먼저 불가능을 정량화한다 — MinTRL

PSR/MinTRL 공식 (Bailey & López de Prado 2012, *Journal of Risk*):

```
PSR(SR*) = Φ[ (SR̂ − SR*)·√(T−1) / √(1 − γ₃·SR̂ + ((γ₄−1)/4)·SR̂²) ]
MinTRL(SR*) = 1 + [1 − γ₃·SR̂ + ((γ₄−1)/4)·SR̂²] · ( z₁₋α / (SR̂ − SR*) )²
```

γ₃=왜도, γ₄=첨도 — 점추정은 안 바꾸고 신뢰대를 넓힌다(음의 왜도+두꺼운 꼬리 = MinTRL 연장). 일별 관측, SR*=0, 단측 검정 실계산:

| 실현 연 SR | MinTRL @95% | MinTRL @90% |
|---:|---|---|
| 0.5 | ~2,727일 ≈ **10.8년** | ~6.6년 |
| 1.0 | ~683일 ≈ **2.7년** | ~414일 ≈ 19.7개월 |
| 1.5 | ~304일 ≈ **14.5개월** | ~184일 ≈ 8.8개월 |
| 2.0 | ~171일 ≈ **8.1개월** | ~104일 ≈ 5개월 |

**결론: 전형 SR 범위(0.5–1.5)에서 3–6개월 고정 검정은 어떤 문턱으로도 95% 인증 불가.** 설계는 이 사실을 우회해야 한다 — 아래 3개 장치가 그 우회다.

### 2.2 장치 ① — 컷오프부터 증거를 백필한다

스카우트 모델 훈련 컷오프 이후의 **과거** 데이터는, 가설 스펙이 그 데이터를 보기 전에 동결됐다면 정직한 OOS다. 컷오프가 ~2025라면 지금 이미 12–18개월치가 존재 — 페이퍼 전용 대비 결정 창 ~3배. 오염 문헌이 이 경계를 직접 지지한다:

- **Gao, Jiang & Yan (2025), "A Test of Lookahead Bias in LLM Forecasts"** ([arXiv:2512.23847](https://arxiv.org/abs/2512.23847)): **LAP(Lookahead Propensity)** — 날짜만 주는 회상 프로브로 LLM이 실현 결과를 내재화했을 확률을 측정. LAP는 표본 내에서 유의하게 양수이고 겉보기 예측력을 증폭시키다가 **훈련 컷오프 직후 ~0으로 붕괴**.
- **Glasserman & Lin (2023)** ([arXiv:2309.17322](https://arxiv.org/abs/2309.17322)): 종목 식별자 **익명화**로 암기 회상을 차단 — 익명화된 헤드라인이 오히려 표본 내 성과가 좋았다(암기가 노이즈였다는 뜻). 컷오프 이후엔 룩어헤드가 구조적으로 소멸.
- **He et al., ChronoBERT/ChronoGPT** ([arXiv:2502.21206](https://arxiv.org/abs/2502.21206), [HuggingFace 공개](https://huggingface.co/manelalab/chrono-bert-v1-20181231)): 연도별 컷오프 훈련 모델 — 컷오프 이전 평가가 필요해지면 이 계열이 대안.

실무 반영: ① 가설별 **LAP 프로브**를 싼 오염 진단으로 추가(높은 LAP 날짜에 근거한 가설은 게이트 전 추가 할인), ② 스카우트 프롬프트의 **종목명 익명화**, ③ 모델 ID+컷오프를 사전등록 지문에(1차 지도 2-1과 연결).

### 2.3 장치 ② — 베팅 e-process: 매일 봐도 무벌점인 순차검정

근거: Ramdas-Grünwald-Vovk-Shafer, "Game-Theoretic Statistics and Safe Anytime-Valid Inference" (*Statistical Science* 2023, [arXiv:2210.01948](https://arxiv.org/abs/2210.01948)) — e-process/신뢰열은 **임의 정지 시점에서 유효**(Ville 부등식). Waudby-Smith & Ramdas (*JRSS-B* 2024) — 유계 확률변수의 베팅 구성. **KRX ±30% 가격제한으로 long-only 일별 수익률이 진짜 유계라 조리법이 문자 그대로 적용된다**:

```
H₀: 비용 차감 일평균 초과수익 μ ≤ 0
부  K₀ = 1;  K_t = K_{t−1} · (1 + λ_t · r_t)
베팅 λ_t ∈ [0, 1/0.30) — 예: 절단 Kelly λ_t = min( max(μ̂_{t−1},0)/σ̂²_{t−1}, c/0.30 ), c ≈ 0.5–0.75
판정: K_t ≥ 1/α 가 되는 첫날 H₀ 기각(전략 승격). 매일 들여다봐도 보장 유지.
부수: 베팅 족을 후보 평균 m에 대해 반전하면 μ의 신뢰열(CS) — 상한이 경제 허들(연 SR 0.5 상당) 아래로 떨어지면 무용 판정(futility kill).
```

정직한 비용: 근사 Kelly 하에서 기대 log-부 증가율 ≈ SR_daily²/2 이므로 기대 승격 소요 ≈ 2·ln(1/α)/SR_daily² — 연 SR 1.0, α=0.05면 ~1,500 거래일(고정 검정의 ~2.2배). 보상: **낙오자는 수주 내 사멸**(음수 평균 전략의 CS 상한은 빨리 붕괴), 운 좋은 경로의 승자는 조기 인증, 그리고 검정이 멈출 필요가 없어 **페이퍼→소액 실전→전액의 티어 사이를 증거가 끊김 없이 복리 누적**한다.

### 2.4 장치 ③ — 전략 스트림의 온라인 FDR: e-LOND

전략들이 KOSPI 시장 팩터를 공유하므로 p-값 기반 LORD++/SAFFRON의 독립 가정이 깨진다. **e-LOND** (Xu & Ramdas, *AISTATS 2024*, [arXiv:2311.06412](https://arxiv.org/abs/2311.06412))가 정답: **임의 종속 + 각 전략의 임의 정지 시점**에서 FDR 통제, 입력은 장치 ②의 e-값 그대로.

```
전략 t 기각 조건: E_t ≥ 1/α_t,  α_t = α · γ_t · (D(t−1)+1)
γ_j ∝ j^(−1.6) (기본), D = 누적 발견 수 — 발견이 예산을 재충전
```

α=0.1 기준 감각: 1번째 전략은 E ≥ ~23, (발견 없이) 10번째는 E ≥ ~900 — **상류 처리량 규율이 중요**하고, 진짜 발견 하나가 스트림 전체를 완화한다. 다중성 청구는 단계당 정확히 1회: 백테스트 탐색은 DSR(오염 창 담당), 라이브 스트림은 e-LOND — **이중 청구 금지**.

백테스트 게이트의 재보정 — False Strategy Theorem (Bailey-LdP 2014; 유효 시도 수는 López de Prado & Lewis 2019의 클러스터링으로):

```
E[max_N SR] ≈ E[SR] + √V[SR] · [ (1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)) ],  γ ≈ 0.5772
예: N=100, 시도 간 SR 분산 0.5 → 순수 노이즈 family의 기대 최대 SR ≈ 1.27 — 우리 전형 대역 한복판
DSR ≥ 0.95는 SR* = 이 기대 최대값 기준으로 계산(0 기준 아님)
```

### 2.5 장치 ④ — 공장 수준 검정이 진짜 지름길

개별 전략은 몇 달로 못 밝혀도, **페이퍼 중인 후보 전체의 동일가중 포트폴리오**는 분산 효과로 SR이 올라가 "공장이 알파를 만드는가"는 몇 달 안에 해소 가능하다(e-값들의 평균도 e-값 — 전역 귀무가설 검정으로 유효). 서비스 로드맵의 실제 질문("전략 1호를 믿을 수 있나"의 상류 질문)이 바로 이것이다.

### 2.6 페이퍼 현실성 — 낙관을 구조적으로 제거

- **Perold (1988), "The Implementation Shortfall: Paper Versus Reality"** (*JPM*): 페이퍼 북과 실전 북을 동일 신호로 병행, 차이(집행 비용+미체결 기회비용)를 분해 — 실계좌가 생기는 즉시 **Perold 섀도 북**을 돌리고 측정된 shortfall을 매월 페이퍼 시뮬레이터 비용 모델에 환류.
- **Suhonen et al. (2017)** (*JPM*): 은행 전략 215개 — **백테스트 SR 중앙값 1.20 → 실전 0.31 (73% 헤어컷)**, 백테스트 재현은 8.4%. 복잡도가 높을수록 +30%p 악화.
- 보수적 페이퍼 체결 규칙: 지정가 터치 체결 금지(다음 봉/스프레드 관통 VWAP), 지연 패딩, 참여율 ≤ 1–5% ADV, 한국 비용 전액(매도 거래세 포함).

### 2.7 방책 A 프로토콜 요약

```
0. 동결·스탬프: 제안 시점에 스펙 불변 등록 + 스카우트 컷오프 스탬프 + LAP 프로브 + 익명화.
   증거 적격 데이터 = max(컷오프, 제안일) 이후 전부. 공장 누적 시도 수 N(유효 N은 클러스터링) 기록.
1. 백테스트 게이트(지명만, 인증 아님): DSR ≥ 0.95 vs SR*=E[max SR](유효 N), PBO ≤ 0.2.
2. 증거 창 구축: 컷오프 이후 과거 구간을 순차검정의 첫 블록으로 백필 → 같은 e-process가 페이퍼로 연속.
3. 일일 측정: 보수적 모의 체결 후 순 초과수익. 왜도·첨도 동시 추적(PSR 보고용).
4. 전략별 검정: 베팅 e-process + 신뢰열.
5. 판정: [사멸] CS 상한 < 경제 허들 / [소액 실전 승격] K ≥ 1/α_t (e-LOND, 스트림 FDR 0.10)
   — 승격 후에도 e-process는 계속(전액 문턱 K ≥ 4/α_t 등) / [시간 상한] ~MinTRL(SR 1.0, 90%) ≈ 20개월 미해소 시 재활용.
6. 공장 수준: 후보 전체 동일가중 포트폴리오에 e-process 1개 — "공장이 알파를 만드는가".
7. 기대치 규율: 통과분에도 ~50% SR 헤어컷으로 사이징(Harvey-Liu·Suhonen·McLean-Pontiff). 실전 개시 후 Perold 섀도 북.
```

**시스템 반영 지점**: `experiment_orchestrator` 뒤에 신규 `live_evidence` 모듈(e-process 누적은 일일 배치 — 수학은 누적 곱 하나), 판정 어휘에 `PAPER_ACCUMULATING / PROMOTED_SMALL / CERTIFIED / FUTILITY_KILLED` 추가, autopilot이 일일 갱신을 구동.

---

## 3. 방책 B — 신호 블렌딩·포트폴리오 계층

**아킬레스건**: 1차 지도 10위 — SUPPORTED 전략의 개별 배포는 breadth를 버린다. 계층이 아예 없다. 문헌으로 처음부터 설계한다.

### 3.1 설계를 결정짓는 4개의 부정적 결과

1. **가중 최적화는 진다** — DeMiguel-Garlappi-Uppal (*RFS* 2009): 최적화 모델 14개 × 데이터셋 7개에서 어느 것도 1/N을 일관되게 못 이김. 평균 기반 MV가 이기는 데 필요한 표본: **자산 25개에 3,000개월+, 50개에 6,000개월+**. 테마 4–5개를 섞는 우리에게 결정적. Michaud(1989): MV는 "추정 오차 최대화기".
2. **추정 가중 결합은 50년째 단순 평균에 진다** — 예측 결합 퍼즐 (Claeskens et al., *IJF* 2016): 가중을 추정하는 순간 결합이 편향·고분산이 됨.
3. **슬리브 방식은 돈을 흘린다** — AQR "Don't Just Mix, Integrate" (Fitzgibbons-Friedman-Pomorski-Serban, *JOI* 2017): 신호별 슬리브 합산 대비 **통합 단일 포트폴리오가 long-only 초과수익 +~1%/yr, IR +~40%** — 핵심 채널은 트레이드 네팅.
4. **정교한 리스크 모델은 필요 없다** — Jagannathan-Ma (*JF* 2003): **long-only 제약 자체가 공분산 수축과 수학적 동치** — 제약 하에서 표본 공분산이 팩터 모델·수축 추정과 동등 성능.

### 3.2 청사진 (5단계)

**① 신호 정규화·정화** (Grinold 1994 "Alpha = IC × vol × score"; Barra 관행): 리밸런스 일자마다 각 템플릿을 winsorize → 횡단면 z-score(소형주 강건성 위해 rank→정규 점수) → **베타·log사이즈·섹터 더미에 잔차화**(선택적으로 타 테마에도) — 결합기가 진짜 구별되는 베팅만 보게.

**② 결합 규칙**: 테마 내 템플릿 동일가중 → **테마 4–5개 동일가중** = 단일 합성 점수. 업그레이드 경로(라이브 IC 이력 3–5년 축적 후에만): walk-forward 역-IC-분산 틸트(Qian-Hua-Sorensen: IC 공간의 MV — 직교화하면 가중이 거의 대각), Tu-Zhou(2011) 방식으로 1/N과 볼록 결합해 증거가 쌓이는 만큼만 1/N에서 이탈. 싼 중간 단계: 테마 수익 스트림의 실현 변동성 역가중(Kirby-Ostdiek 2012 — 평균 불요, 고비용 하에서도 1/N을 이긴 유일 계열).

**③ 기대수익·공분산 수축**: 신호 프리미엄은 원시 백테스트 평균 금지 — JKP식 경험적 베이즈로 테마 평균·0으로 수축. 조립 규칙은 Black-Litterman(He-Litterman 1999): 동일가중 유니버스 프라이어 + 수축된 IC를 신뢰도로 하는 뷰 — 신호가 무정보면 벤치마크로 우아하게 퇴화. 종목 공분산은 Ledoit-Wolf 상수상관 수축(2004).

**④ 회전 통제** (Gârleanu-Pedersen, *JF* 2013 + Sun et al. 2006 + FIM 2018): 매일 목표로 전량 리밸런스 금지 — **aim 포트폴리오 방향으로 보정된 비율만 이동**(신규 = 기존 + λ·(aim − 기존)). 감쇠 빠른 신호(리버설·브레이크아웃)는 aim에서 자동 할인 — 느린 신호가 어차피 원하는 거래의 타이밍만 조절(네팅). 종목별 무거래 밴드(밴드 경계까지만 거래 — Sun et al.: 리밸런스 비용 ~50% 절감). λ는 실현 회전이 종목별 임팩트 곡선 기반 예산과 일치하도록 보정 — 소형 KOSDAQ은 참여율 캡 타이트하게.

**⑤ 리스크 제약 (최소 생존 모델)**: 시장 베타 1팩터(일별 수익률로 추정) + 대각 잔차 + 제약 세트 — 섹터 비중 유니버스 대비 ±X%, 포트폴리오 베타 밴드, 사이즈 틸트 한도, 종목 캡. 현실적 실패 모드(모멘텀+저변동+비유동성이 전부 KOSDAQ 소형주에 적재)를 막는 목적. 업그레이드는 필요 시에만: 비대칭 PCA + Bai-Ng 팩터 수 선택(일별 수익률만으로 가능).

### 3.3 블렌더 자체의 메타 과적합 방지 (결합기 검증 프로토콜)

1. 결합기 파라미터는 **엄격히 walk-forward** — 신호를 검증한 그 이력 전체에 가중을 적합하는 것은 이중 청구.
2. **자유도 ≈ 0 유지** — 동일가중 = 파라미터 0, 역변동성 = 분산만. 이러면 다중검정 부담이 자명하게 작다.
3. 시도한 결합 변형(IC 가중, 랭크, 최적화, 하이퍼파라미터 각각)은 **전부 trial로 기록** — 최종 합성은 그 시도 수 기준 DSR + Harvey-Liu 헤어컷(균일 50% 금지, 비선형) 통과 필수.
4. **동결 후 1회만 평가하는 최종 홀드아웃**(최근 1–2년).

문헌의 가장 깊은 교훈: **블렌딩 계층의 수익 원천은 영리한 가중 추정이 아니라 비용 네팅·리스크 제약·수축 규율이다.**

**시스템 반영 지점**: 신규 `portfolio/` 모듈(합성 점수·aim·밴드), `release_gate` 뒤 D4(승격 파이프라인)와 함께 설계 — SUPPORTED의 목적지가 "개별 strategy.versions"에서 "합성 점수의 구성 요소"로 바뀐다.

---

## 4. 방책 C — 소형 워커 신뢰성

**아킬레스건**: AgentBench 실측상 소형 오픈 모델의 에이전트 능력 격차가 최대인데 워커 전원이 소형(1.7B dev / 14B prod)이고, vLLM `--enable-lora --max-loras 4` + 어댑터 레지스트리는 **enabled 0으로 유휴**(1차 AS-IS §4.3).

### 4.1 증거의 경계선 — 어디까지 소형으로 되는가

- **성립**: 폐쇄 스키마 태스크 — 추출·분류·구조화 풀·함수 호출. LoRA Land(Predibase 2024, [arXiv:2405.00732](https://arxiv.org/abs/2405.00732)): 310개 4-bit LoRA 파인튜닝, 베이스 대비 평균 +34점, **GPT-4 대비 평균 +10점, 224/310이 GPT-4 초과** — 최고 베이스가 7B급이므로 14B는 적용 구간 한복판. Distilling Step-by-Step(ACL 2023): 근거 포함 증류로 **770M이 540B PaLM을 초과**(>700배 격차). 함수 호출: xLAM-7B가 BFCL 88.24%로 GPT-4·Claude-3-Opus 상회, **1B 모델도 78.94%로 GPT-3.5 상회** — 실행 검증된 SFT 데이터가 비결.
- **불성립**: 개방형 다단 추론·새로운 계획·광범위 지식 — Berkeley "False Promise"(2023): 광범위 모방은 **스타일만 배우고 능력 격차를 못 닫음**. 단 좁은 태스크의 밀집 데이터 모방은 그 태스크에서 격차를 닫음.
- NVIDIA SLM 포지션(2025): 오픈소스 에이전트의 LLM 호출 중 **40–70%가 잘 튜닝된 SLM으로 대체 가능** 추정 + LLM→SLM 변환 알고리즘(프로덕션 호출 로깅 → 태스크 클러스터링 → 클러스터당 SLM 튜닝) — 정확히 우리 헤드-트레이스 상황.

### 4.2 Stage 0 — 훈련 없이 이번 주에 가능한 것

1. **2단계 출력** (Tam et al. EMNLP 2024의 처방): 자유 생성 → 경계에서만 Outlines/vLLM 문법 제약으로 포맷 변환. 분류형 풀은 예외적으로 라벨 토큰만 제약(분류엔 제약이 오히려 +11~19점).
2. **결정론 검증기 + 재시도 1회**: 스키마·필드 범위·원문 인용 존재 검사 → 실패 시 검증기 오류를 첨부해 1회 재시도 → 재실패 시 부서장 격상.
3. **합의 기반 격상** (Kolawole et al., *TMLR*, [arXiv:2407.02348](https://arxiv.org/abs/2407.02348)): k=3 샘플(온도 ~0.7) — 만장일치면 수용, 갈리면 격상. 라우터 훈련 불요, 실측 2–25배 비용 절감. 투표가 크기 격차를 실제로 좁힘("More Agents Is All You Need", TMLR 2024: Llama-13B가 충분한 샘플로 70B/GPT-3.5급 — 답이 이산적인 태스크에서).

효과: 워커의 **조용한 오류가 격상으로 전환**된다. 자체 GPU라 3배 추론 비용은 무시 가능.

### 4.3 Stage 1–2 — 헤드 트레이스로 어댑터 만들기

- **데이터**: 격상 건 전수 + 일상 호출 샘플에 대해 프런티어 헤드 모델이 골드 출력 + 짧은 근거(Distilling Step-by-Step)를 생산. 역할별 클러스터링. **LIMA 규율(NeurIPS 2023: 엄선 1,000개 > 잡음 52,000개)**: 역할당 500–2,000개, 중복 제거·다양성 샘플링·전 예제 실행/스키마 검증(APIGen 규율). 우선순위 = 호출량 × 격상률: ① 구조화 데이터 풀 ② 문서 추출 ③ 리서치 브리핑 요약.
- **훈련**: QLoRA/LoRA SFT를 **프로덕션급 14B에**(1.7B 결과로 게이트 금지 — 비예측적, 스모크 테스트 전용). r=8–16 — Biderman et al.(*TMLR* 2024) "LoRA Learns Less and Forgets Less": 저랭크가 저망각 구간이고 베이스 가중 불변이라 나쁜 어댑터가 다른 역할을 못 망침. QLoRA 기준 14B 역할 어댑터는 단일 GPU 당일 작업.
- **게이트**: ① 홀드아웃 역할 eval에서 비적용 14B 대비 사전 설정 마진 초과, ② 망각 회귀 스위트(일반 지시 준수 + 타 역할 eval)가 노이즈 이내, ③ 포맷 유효율 목표 이상 → ④ 최근 1주 트래픽 **섀도 리플레이** → ⑤ 5% **카나리**(7일 프로덕션 베이스라인 대비, 위반 시 자동 롤백) → ⑥ 레지스트리 플립. vLLM 런타임 어댑터 로드라 배포 없이 롤백 — Multi-LoRA 슬롯 4개에 현직+후보 공존.

### 4.4 Stage 3 — 루프 닫기

격상 건과 헤드 모델 스팟 채점(출력의 1–5% 샘플)이 역할별 훈련셋에 계속 축적 → 주기 재훈련(항상 같은 게이트 통과) → **역할별 격상률이 건강 지표**(상승 = 드리프트 알람). 라벨이 쌓이면 격상 트리거를 합의/검증기에서 소형 라우터로 업그레이드(RouteLLM: GPT-4 성능 95% 유지하며 비용 85% 절감; FrugalGPT: 최대 98% 절감).

**시스템 반영 지점**: `worker_model_gateway.py`(2단계 출력·k-샘플), `worker_model_registry.json`(어댑터 게이트 필드), LangGraph 워커 그래프(검증기 노드·격상 엣지), eval_runner(역할 eval — 1차 지도 9위의 pass^k와 같은 하네스).

---

## 5. 방책 D — 전략 생애주기 거버넌스

**아킬레스건**: 공장은 전략을 만들지만 "언제 믿기를 멈출까"를 지배하는 규칙이 없다. 자동 트립 문턱(1차 지도 12위)도 통계적 근거가 필요하다.

### 5.1 은퇴 규칙의 대전제 — 무엇으로 해고하면 안 되는가

- **Goyal & Wahal (*JF* 2008)**: 연기금 3,400개 실증 — 고수익 후 고용, 저수익 후 해고했으나 **해고된 매니저의 이후 성과가 신규 고용과 구별 불가(종종 더 좋음)**. 원시 트레일링 수익률 해고는 가치 파괴.
- **Cornell-Hsu-Nanigian (*JPM* 2017)**: 최근 3년 성과 기반 선택은 **역지표**.
- 따라서 은퇴는 원시 수익률 랭크가 아니라 **프로세스 수준 통계 증거**(5.2)와 **구조적 레짐 무효화**(5.4)로만.

감쇠의 사전 기대(헤어컷 캘리브레이션): McLean-Pontiff — OOS −26%, 발표 후 −58%; Di Mascio-Lines-Naik "Alpha Decay" — 기관 매수 알파는 **~12개월에 걸쳐 전방 하중 감쇠**(펀더멘털 신호 반감기는 연 단위가 아니라 월 단위); Jacobs-Müller (*JFE* 2020, 39개 시장) — **발표 후 감쇠가 신뢰되는 곳은 미국뿐**, 비미국(아시아 포함)은 훨씬 약함 — 한국 엣지는 미국 경험보다 느리게 감쇠할 개연성, 단 2025-03 공매도 재개가 차익 자본을 늘려 가속 요인.

### 5.2 3중 탐지기 — 각자 오탐률이 명시된다

**① CUSUM** (Philips-Yashchin-Stein, *JPM* 2003 — **$500B+ 기관 자산에서 실사용**, Moustakides 1986이 최속 탐지 최적성 증명):

```
1. 월별(→일별 전환 가능) 로그 초과수익 eᵢ = ln((1+rᵢ)/(1+bᵢ))
2. 지수가중 Von Neumann 추정 트래킹 에러: σ̂ᵢ² = γσ̂ᵢ₋₁² + (1−γ)·12·(eᵢ−eᵢ₋₁)²/2, γ=0.9
3. 현재 IR 추정: IR̂ᵢ = 12·eᵢ/σ̂ᵢ₋₁ (시차 σ̂로 비편향)
4. 단측 CUSUM: Lᵢ = max[0, Lᵢ₋₁ + LLR(기준점 = 두 가설 중점)] — H_bad(IR=0) vs H_good(IR=목표/2)
5. L이 문턱 h 초과 시 알람 (h는 원하는 오탐률로 표 조회/시뮬레이션)
```

운영 특성(원 논문): IR 0.5→0 열화를 **평균 ~41개월에 탐지(롤링 t-검정의 ~10배 속도)**, 건강한 전략의 오경보는 ~84개월당 1회. 탐지 시간은 ~1/ΔIR² 스케일 — 우리는 H_good(SR=백테스트/2, 즉 McLean-Pontiff 헤어컷 내장) vs H_bad(SR=0)를 **일별로** 돌리므로 격차 1.5면 죽은 SR-1.5 전략은 연 단위가 아니라 **월 단위**에 알람.

**② BOCPD** (Adams & MacKay 2007, [arXiv:0710.3742](https://arxiv.org/abs/0710.3742)): 런 길이(마지막 변화점 이후 경과) 사후분포의 온라인 정확 갱신 — NIG 공액 우도, 위험률 1/250. **P(런 < 20일) > 0.5**가 5일 지속되면 분포 단절 플래그. CUSUM이 못 보는 **방향 미지정 변화**(분산 급증 등)를 잡는 보완재.

**③ 드로다운 정합성 검정** (Bailey-LdP "Triple Penance", *Journal of Risk*; 공식 원문 그대로):

```
IID 정규 N(μ,σ²), μ>0, Z_α<0 기준:
MaxQL_α = (Z_α σ)²/(4μ)              — 손실 한도 (SR로 쓰면: Z_α²·σ_ann/(4·SR_ann))
TuW_α  = (Z_α σ/μ)² = (Z_α/SR_ann)² 년 — 수면 아래 시간 한도
Triple Penance 정리: 최대 분위 손실에서의 회복은 도달의 3배 시간 (SR 무관)
실현 손실 π̃ₜ<0, 경과 t의 함의 수면시간: ITuW = π̃ₜ²/(μ̂²t) − 2π̃ₜ/μ̂ + t
운영 규칙: ITuW > TuW_α 면 트립 — 손실 캡 도달 전이라도. 얕고 긴 드로다운(죽은 전략의 시그니처)을 잡는다.
```

실계산(α=5%): SR 1.5 → 손실 캡 = 백테스트 파라미터로 역산, TuW = **1.20년** / SR 1.0 → **2.71년** / SR 0.5 → **10.8년**. 함의 둘: **백테스트 SR이 높을수록 한도가 타이트**해지고, **SR 0.5급은 드로다운 규칙으로 통치 불가능한 시간 척도**라 배포 게이트에서 SR ≥ ~1.2를 요구할 이유가 하나 더 생긴다(TuW₅% ≤ 2년 조건). 직렬상관 보정 필수: AR(1) 양의 자기상관 무시는 드로다운 잠재력을 **최대 ~70% 과소평가**(같은 논문의 AR(1) 확장 사용).

### 5.3 트립 문턱을 오탐률로 역산한다

이것이 1차 지도 12위(자동 트립)의 "임의 숫자가 아닌 문턱" 요구의 답이다.

- 배경 이론 (Magdon-Ismail & Atiya, "Maximum Drawdown", *Risk* 2004 — 원문 확인): 브라운 운동의 기대 최대 드로다운은 **건강(μ>0)하면 log 성장, 죽음(μ=0)이면 √T 성장, 유해(μ<0)면 선형 성장** — "정상적 고통"과 "고장"을 가르는 수학적 기반. μ=0일 때 E[MDD] ≈ 1.2533·σ√T.
- **임의 레거시 문턱의 진단**: 손실 캡 후보 D가 주어지면 α(D) = Φ(−2√(μ̂D)/σ̂) — 그 문턱이 얼마나 "임의"였는지 즉시 나온다.
- **부트스트랩 조리법** (표준 관행): 백테스트 일별 수익률의 정상 블록 부트스트랩(블록 ~20일 — 자기상관·변동성 군집 보존) → ≥10,000 경로 → (최악 k일 손실, MDD, 최장 TuW)의 경험적 1%/5% 분위수 = 트립 표. **오탐률이 구성상 알려짐**. 분기마다 + 레짐 셀마다 재산출.
- 일일 하드킬 감각: 3.5σ 일일 손실 = 귀무 하 ~17년당 1회(하드킬 적정), 2.5σ ≈ 5개월당 1회(소프트 알림 적정).

### 5.4 디리스킹은 이진이 아니라 점진 — 그리고 언제 돕는가

- **Kaminski & Lo (*JFM* 2014)**: 랜덤워크 하에서 0/1 손절은 **항상 기대수익을 깎는다**. 양의 직렬상관(모멘텀)이 있을 때만 stopping premium이 양수(실증: 미국 월별 주식에서 스톱아웃 기간 +50–100bp/월). **이진 스톱을 수익 장치로 쓰기 전에 자기 전략 수익률의 ρ̂₁부터 검정**할 것 — 아니면 스톱은 추론 장치(고장 증거)와 꼬리 캡으로만.
- **Grossman & Zhou (1993)**: 이론적으로 근거 있는 점진 스케줄 — 노출 ∝ (Wₜ − α·러닝맥스) 잉여. 절벽형 아님.
- **변동성 타기팅이 최강 증거의 기계 규칙** — Moreira & Muir (*JF* 2017): 1/실현분산 스케일링이 시장·밸류·모멘텀 등에서 **양의 알파·샤프 상승**; Harvey et al. (*JPM* 2018, 60자산 1926~): 주식·크레딧에서 샤프 개선 + **모든 자산에서 좌측 꼬리 축소**(폭락은 고변동 상태에 군집). 우리가 거래하는 자산군이 정확히 수혜 대상.

### 5.5 레짐 계기판 (일별, 시장 수준)

| 계기 | 구성 | 근거 |
|---|---|---|
| 2–3상태 가우스 HMM | KOSPI/KOSDAQ 일별 수익률, 스무딩된 약세 상태 확률 | Hamilton 1989; Ang-Bekaert 2004 (*FAJ*) — 레짐 조건 배분이 정적 배분을 **OOS에서 지배**한 최정갈 증거 |
| 터뷸런스 지수 | KRX 섹터 ~20개 패널의 마할라노비스 거리, 상위 5분위 플래그 | Kritzman-Li 2010 — 상위 십분위에서 위험 자산 수익 체계적 악화, 지속성 있어 당일 디리스킹 신호 가능 |
| 흡수 비율 | 동일 패널 공분산 상위 k 고유벡터 설명 분산 비중(250–500일 창, k≈n/5), 표준화 15일-대-1년 이동 > 1σ 플래그 | Kritzman-Li-Page-Rigobon 2011 — 미국 최악 드로다운 전부가 AR 스파이크 후행 |
| 단순 프록시 | 20일 실현 변동성 백분위, 폭(60일 MA 상회 %), 크레딧 스프레드 | 교차 검증용 |
| **구조 더미** | **공매도 금지 창(2023-11-06 ~ 2025-03-30) + 재개 이후 시대** | KCMI 2025 — 통계 모델이 표본 내에서 못 배우는 날짜 있는 구조 경계는 더미로 인코딩 |

**모든 백테스트 지표를 레짐 셀별로 보고** — 금지 창 안에서 주로 검증된 전략은 현 레짐에서 "미검증" 플래그.

### 5.6 생애주기 프로토콜 요약

```
[출생 증명서] 배포 시: μ̂, σ̂, γ₃, γ₄, ρ̂₁, 레짐 셀별 백테스트 성적, 부트스트랩 트립 표. 모든 모니터링은 이 대비.
   배포 조건: DSR ≥ 0.95 (누적 시도 N 기준) + TuW₅% ≤ 2년 (⇒ SR ≥ ~1.2).

[일일 통계 6종] 로그 초과수익·EWMA TE·IR̂ / CUSUM Lₜ / BOCPD 런 길이 / 드로다운 패널(깊이·TuW·ITuW)
   / 실전-페이퍼-백테스트 슬리피지 롤링 60일 / 롤링 PSR(0)·PSR(백테스트/2)와 T vs MinTRL.

[하드킬 — 자동, 당일] 부트스트랩 3.5σ급 일일 손실(오탐 ~17년당 1회) 또는 1%ile MDD 초과(직렬상관 보정 +~70%).
   노출 0, 재가동은 사람 검토 후. → 1차 지도 12위 자동 트립·2위 킬스위치에 직결.

[소프트 트립 → 보호관찰 — 자동] CUSUM 알람 / ITuW > TuW₅% / 깊이 > MaxQL₅% / BOCPD 5일 지속 / 슬리피지 2σ.
   조치: 노출 절반 → Grossman-Zhou 잉여 비례 스케일(점진, 이진 아님).

[레짐 디리스크 — 자동, 포트폴리오 수준] (목표 변동성/실현 20일 변동성) 캡 1 스케일링 +
   HMM 약세 확률 > 0.7 또는 터뷸런스·흡수 동시 점등 시 추가 승수 < 1.

[보호관찰 해소 — 사람+통계, 60–90 거래일] CUSUM 리셋 + ITuW 재진입 시 복귀.
   독립 탐지기 2개 확인 + 조사 결과 수리 불가 시 은퇴로 격상.

[은퇴 조건] (a) 독립 탐지기 2개 + 원인 조사 무수리 / (b) 엣지가 구조적으로 끝난 레짐 전용(예: 금지 창 전용)
   / (c) T > MinTRL이고 PSR(0) < 0.5 / (d) 슬리피지 감쇠만으로 총 엣지 50%+ 잠식 2분기 연속.
   절대 금지: 트레일링 원시 수익률 랭크 단독 은퇴 (Goyal-Wahal). 은퇴 전략은 이력과 함께 도서관으로 — 레짐 회귀 시 재활용.

[분기 재검증] (μ̂,σ̂,ρ̂₁) 레짐별 갱신, 트립 표 재부트스트랩, 누적 N으로 DSR 재검(공장이 계속 캐므로 문턱은 시간이 갈수록 조여진다).
```

**시스템 반영 지점**: 신규 `lifecycle/` 모듈(일일 통계 배치), `risk.trading_states`/킬스위치와 연동(하드킬), autopilot 사이클에 레짐 계기판 갱신 추가, 판정 어휘에 `PROBATION / RETIRED` 추가.

---

## 6. 통합 — 하나의 파이프라인으로

4개 방책은 독립 처방이 아니라 하나의 전략 수명 사슬로 조립된다:

```
발굴(스카우트)          ── C: 워커 Stage 0–3 (추출·요약의 신뢰성) + A: 익명화·LAP 프로브
  ↓ 리드·제안
Gate 0                 ── A: DSR vs E[max SR](유효 N), 지문에 컷오프 스탬프 (1차 2-1·2-6·2-7 이행)
  ↓ 가설
백테스트·판정            ── 1차 2-4 비용 모델 + D: 출생 증명서(레짐 셀별 성적·트립 표) 생산
  ↓ SUPPORTED
블렌딩 계층 (신설)       ── B: 정규화→잔차화→테마 동일가중→통합 포트폴리오→GP 부분 조정→최소 리스크 모델
  ↓ 합성 포트폴리오
PAPER (재정의)          ── A: 컷오프 백필 + 베팅 e-process + e-LOND + 공장 수준 검정 + 보수적 체결
  ↓ 승격 (소액 → 전액, e-process는 계속)
실전 운용               ── D: 일일 6종 통계 + 3중 탐지기 + 레짐 계기판 + 통계적 트립
  ↓                        (하드킬 = 1차 12위 자동 트립의 문턱 공급, 2위 내구 킬스위치가 집행)
보호관찰 / 은퇴 / 재활용  ── D: 원시 수익률 해고 금지, 독립 2-탐지기 규칙, 도서관 환류
                            (은퇴 사유는 lesson_codes로 공장에 환류 — 기존 환류 루프의 확장)
```

1차 지도와의 결합: 방책 A·D가 1차 1·2·6·7·12위를 **구현 수준으로 구체화**하고, 방책 B가 10위를, 방책 C가 9위(eval 하네스)와 AS-IS §4.3(LoRA 유휴)을 해소한다. 1차 3·5위(QA fail-closed·팬아웃 가드)와 8·9·11위(인프라)는 이 문서 범위 밖 — 1차 처방이 그대로 유효하다.

---

## 7. 실행 순서 제안

의존성과 노력 기준:

| 순서 | 작업 | 노력 | 의존 | 즉시 효과 |
|---:|---|---|---|---|
| 1 | **C-Stage 0**: 2단계 출력 + 검증기·재시도 + k=3 합의 격상 | 하 (훈련 불요) | 없음 | 워커 조용한 오류 → 격상 전환 |
| 2 | **A-0**: 지문 구속 + 컷오프·LAP 스탬프 + 익명화 (1차 2-1 병합) | 하 | 없음 | 이후 모든 증거의 적격성 확보 — **이게 늦을수록 백필 가능 증거가 오염** |
| 3 | **A-2~4**: e-process 라이브러리 + 컷오프 백필 검정 가동 | 중 (수학은 누적 곱) | 2 | 기존 SUPPORTED 가설들의 futility/승격이 수주 내 갈리기 시작 |
| 4 | **B-v0**: z-score→잔차화→테마 동일가중→통합 포트폴리오 + 무거래 밴드 | 중 | 없음 | 공장 수준 e-process(A-6)의 대상 포트폴리오 제공 |
| 5 | **D-v0**: 출생 증명서 + CUSUM·ITuW + 부트스트랩 트립 표 | 중 | 없음 | 1차 12위 자동 트립의 문턱 공급 — 킬스위치(1차 2위)와 동시 작업 권장 |
| 6 | **A-5**: e-LOND 게이트 + 공장 수준 e-process | 하 (3·4 위에 규칙 하나) | 3, 4 | "공장이 알파를 만드는가"가 수개월 내 답 나옴 |
| 7 | **C-Stage 1–2**: 트레이스 수집 → 첫 LoRA 어댑터 → 섀도·카나리 | 중–상 | 1 | 격상률 하락, 헤드 모델 비용 절감 |
| 8 | **D-레짐**: HMM·터뷸런스·흡수비 계기판 + 레짐 셀 보고 | 중 | 5 | 금지 창 전용 엣지 식별, 변동성 타기팅 가동 |
| 9 | **B-업그레이드**: 역변동성 틸트, BL 조립, GP λ 보정 | 상 | 4 + 라이브 이력 | IR 상한 확대 |

원칙: **1–2번이 가장 급하다.** 1번은 이번 주에 되고, 2번은 늦어질수록 컷오프-이후 백필 증거가 "스펙이 데이터를 본 뒤 동결"로 오염되어 장치 ①이 무력화된다.

---

## 부록 — 2차 조사 소스 (영역별 전체)

### 방책 A (17편)
[Bailey-LdP, Sharpe Ratio Efficient Frontier (J. Risk 2012)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1821643) · [PSR/MinTRL 공식 해설 (Portfolio Optimizer)](https://portfoliooptimizer.io/blog/the-probabilistic-sharpe-ratio-bias-adjustment-confidence-intervals-hypothesis-testing-and-minimum-track-record-length/) · Wald 1945 (SPRT) · [Ramdas-Grünwald-Vovk-Shafer, SAVI (Stat. Sci. 2023)](https://arxiv.org/abs/2210.01948) · [Waudby-Smith & Ramdas (JRSS-B 2024)](https://academic.oup.com/jrsssb/article/86/1/39/7303215) · [Casgrain-Larsson-Ziegel (2023)](https://arxiv.org/abs/2204.05680) · Foster-Stine 2008 (α-investing) · Javanmard-Montanari 2018 (LOND/LORD) · [SAFFRON (ICML 2018)](http://proceedings.mlr.press/v80/ramdas18a/ramdas18a.pdf) · [onlineFDR 패키지](https://dsrobertson.github.io/onlineFDR/reference/onlineFDR-package.html) · [e-LOND (AISTATS 2024)](https://proceedings.mlr.press/v238/xu24a.html) · [DSR (JPM 2014)](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) · [LdP-Lewis 2019 (유효 N 클러스터링)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3167017) · [Harvey-Liu Backtesting (JPM 2015)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2345489) · [ChronoBERT/GPT](https://arxiv.org/abs/2502.21206) · [Glasserman-Lin 2023](https://arxiv.org/abs/2309.17322) · [Gao-Jiang-Yan LAP (2025)](https://arxiv.org/abs/2512.23847) · [Perold 1988 (JPM)](https://jpm.pm-research.com/content/14/3/4) · [Suhonen et al. 2017 (JPM)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2757113)

### 방책 B (20편)
[DeMiguel-Garlappi-Uppal (RFS 2009)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=911512) · [Michaud 1989 (FAJ)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2387669) · [Tu-Zhou (JFE 2011)](https://www.sciencedirect.com/science/article/abs/pii/S0304405X10001893) · [Kirby-Ostdiek (JFQA 2012)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1530022) · Grinold 1994 (JPM) · Qian-Hua-Sorensen 2007 (책 + [JPM 논문](http://gyanresearch.wdfiles.com/local--files/alpha/JPM_FA_07_Qian.pdf)) · [MSCI Barra, Converting Scores into Alphas](https://www.msci.com/documents/10199/1645561/PI_Converting_Scores_Into_Alphas.pdf/7adf1f42-10aa-40eb-9e8c-ecc11eeba2d4) · [Ledoit-Wolf 2004](http://www.ledoit.net/Honey_2004.pdf) · [JKP (JF 2023)](https://onlinelibrary.wiley.com/doi/full/10.1111/jofi.13249) · [He-Litterman 1999](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=334304) · [Gârleanu-Pedersen (JF 2013)](https://nbgarleanu.github.io/DynTrad.pdf) · [Fitzgibbons et al., Don't Just Mix, Integrate (JOI 2017)](https://images.aqr.com/-/media/AQR/Documents/Insights/White-Papers/Long-Only-Style-Investing-Dont-Just-Mix-Integrate.pdf) · [Sun et al. 2006 (JPM)](https://people.csail.mit.edu/fan/papers/JPM_Winter2006_opt_rebalancing.pdf) · [Frazzini-Israel-Moskowitz, Trading Costs (2018)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3229719) · [Connor-Korajczyk 서베이](https://efalken.com/pdfs/ConnorKor07.pdf) · Bai-Ng 2002 (Econometrica) · [Jagannathan-Ma (JF 2003)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=424756) · [Claeskens et al., 예측 결합 퍼즐 (IJF 2016)](https://www.sciencedirect.com/science/article/abs/pii/S0169207016000327) · [Wang et al., 예측 결합 50년 리뷰](https://arxiv.org/pdf/2205.04216) · [pypbo 도구](https://github.com/esvhd/pypbo)

### 방책 C (24편)
[Distilling Step-by-Step (ACL 2023)](https://arxiv.org/abs/2305.02301) · [LoRA Land (2024)](https://arxiv.org/abs/2405.00732) · [False Promise (2023)](https://arxiv.org/abs/2305.15717) · [NVIDIA SLM 포지션 (2025)](https://arxiv.org/abs/2506.02153) · [LoRA (2021)](https://arxiv.org/abs/2106.09685) · [QLoRA (NeurIPS 2023)](https://arxiv.org/abs/2305.14314) · [LIMA (NeurIPS 2023)](https://arxiv.org/abs/2305.11206) · [LoRA Learns Less and Forgets Less (TMLR 2024)](https://arxiv.org/abs/2405.09673) · [S-LoRA (2023)](https://arxiv.org/abs/2311.03285) · [Self-Instruct (ACL 2023)](https://arxiv.org/abs/2212.10560) · [FrugalGPT (2023)](https://arxiv.org/abs/2305.05176) · [RouteLLM (ICLR 2025)](https://arxiv.org/abs/2406.18665) · [Hybrid LLM (ICLR 2024)](https://proceedings.iclr.cc/paper_files/paper/2024/hash/b47d93c99fa22ac0b377578af0a1f63a-Abstract-Conference.html) · [Agreement-Based Cascading (TMLR)](https://arxiv.org/abs/2407.02348) · [Self-Consistency (ICLR 2023)](https://arxiv.org/abs/2203.11171) · [More Agents Is All You Need (TMLR 2024)](https://arxiv.org/abs/2402.05120) · [Chain-of-Verification (ACL Findings 2024)](https://arxiv.org/abs/2309.11495) · [Let Me Speak Freely? (EMNLP 2024)](https://arxiv.org/abs/2408.02442) · [Outlines/Guided Generation (2023)](https://arxiv.org/abs/2307.09702) · [Gorilla (NeurIPS 2024)](https://arxiv.org/abs/2305.15334) · [xLAM/APIGen (2024)](https://arxiv.org/abs/2409.03215) · [ToolLLM (ICLR 2024)](https://arxiv.org/abs/2307.16789) · [vLLM LoRA 문서](https://docs.vllm.ai/en/latest/features/lora.html) · LLMOps 섀도·카나리 관행 2편

### 방책 D (23편)
[McLean-Pontiff (JF 2016)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2156623) · [Jacobs-Müller (JFE 2020)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2816490) · [Goyal-Wahal (JF 2008)](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2008.01375.x) · [Cornell-Hsu-Nanigian (JPM 2017)](https://jpm.pm-research.com/content/43/4/33) · [Di Mascio-Lines-Naik, Alpha Decay](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2580551) · [Philips-Yashchin-Stein CUSUM (JPM 2003)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=371121) + [실무 전문](https://www.northinfo.com/Documents/144.pdf) · [Adams-MacKay BOCPD](https://arxiv.org/abs/0710.3742) · [Bailey-LdP Triple Penance](https://www.davidhbailey.com/dhbpapers/stop-out.pdf) · [Magdon-Ismail-Atiya Maximum Drawdown](https://www.cs.rpi.edu/~magdon/ps/journal/drawdown_journal.pdf) · [Kaminski-Lo (JFM 2014)](https://www.smallake.kr/wp-content/uploads/2017/02/When_Do_Stop-Loss_Rules_Stop_Losses.pdf) · [Grossman-Zhou 1993](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-9965.1993.tb00044.x) · [Moreira-Muir (JF 2017)](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12513) · [Harvey et al., Volatility Targeting (JPM 2018)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3175538) · Hamilton 1989 (Econometrica) · [Kritzman-Li 터뷸런스 (FAJ 2010)](https://www.top1000funds.com/wp-content/uploads/2010/11/FAJskulls.pdf) · [흡수 비율 (JPM 2011)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1633027) · [Ang-Bekaert (FAJ 2004)](https://www.nber.org/papers/w10080) · [Kritzman-Page-Turkington (FAJ 2012)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2064801) · [Two Sigma 레짐 모델링 (2021)](https://www.twosigma.com/wp-content/uploads/2021/10/Machine-Learning-Approach-to-Regime-Modeling_.pdf) · [LdP, Quantitative Meta-Strategies (2015)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2547325) · [KCMI 공매도 재개 (2025)](https://www.kcmi.re.kr/en/publications/pub_detail_view?syear=2025&zcd=002001017&zno=1840&cno=6517) · [S&P Global (2025)](https://www.spglobal.com/market-intelligence/en/news-insights/research/2025/04/from-ban-to-boom-how-south-korea-learned-to-love-short-selling) · Moustakides 1986 (CUSUM 최적성)

---

*이 문서는 2026-08-13 시점의 2차 문헌 조사 결과다. 공식이 중요한 3편(Triple Penance, CUSUM, Maximum Drawdown)은 원문 전문 추출로 공식을 검증했고, 나머지 소스는 검색·발행처 페이지 대조로 실재를 확인했다. 1차 지도(LITERATURE_BREAKTHROUGH_MAP.md)의 우선순위 보드와 함께 읽을 것.*
