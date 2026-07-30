// ============================================================
//  나의 AI 회사 설정 — 여기 한 파일만 고치면 됩니다
// ============================================================
//  회사 이름, 부서 이름, 직원 이름·성격·머리색까지 전부 여기 있어요.
//  다른 파일은 건드리지 않아도 됩니다.
//
//  ⚠️ 딱 2가지 규칙
//   1. 부서 id(research, strategy1, ...)는 절대 바꾸지 마세요. 시뮬레이션 엔진이
//      이 id로 움직입니다. 바꾸면 캐릭터가 길을 잃어요.
//      → 바꿔도 되는 건 name(부서 이름) · icon · short 입니다.
//   2. 부서는 8개다. 1층 4개(2열 2행) + 2층 4개(2열 2행)로 배치된다.
//
//  직원 수는 자유롭게 늘리고 줄여도 됩니다. 한 팀에 팀장(lead) 1명은 두세요.
// ============================================================
//
//  HgFinance 헤지펀드 조직으로 맞춘 버전.
//  실제 조직은 CEO Office + 6개 투자본부(리서치/트레이딩/리스크/퀀트·백테스트/
//  회계·포트폴리오/AI QA·감사) + CEO 직속 Agent Workforce 인사팀 = 8개 단위인데
//  DEPARTMENTS는 그중 7개(6개 투자본부 + 인사팀)와 CEO Office 지원팀 = 8개다.
//    - CEO는 DEPARTMENTS 배열이 아니라 CEO_PROFILE(대표실)로 별도 표현된다.
//    - 1층에 리서치·퀀트·트레이딩·리스크, 2층에 회계·QA감사·인사·CEO지원을 둔다.
//  직원 이름·프로필은 각 본부 Hermes Profile(departments/<n>/hermes/config.yaml)의
//  실제 페르소나(agent.personalities)를 그대로 옮긴 것이다. 이 오피스는 여전히
//  브라우저 안에서만 도는 Scripted Simulation이고 실제 Hermes Runtime이나 Risk/
//  OMS/Ledger 데이터에 연결돼 있지 않다 — AI_OFFICE_FRONTEND_PLAN.md의 목표
//  상태(REST Snapshot + WebSocket)는 아직 구현 전이다.
// ============================================================

/** 회사 기본 정보 */
export const COMPANY = {
  /** 좌측 상단 헤더에 뜨는 회사 이름 */
  name: "HgFinance",
  /** 헤더 로고 배지에 들어갈 글자 1개 (이모지도 됩니다) */
  logoLetter: "F",
  /** 화면 상단 큰 제목 (앞부분) */
  titlePrefix: "개인형 헤지펀드",
  /** 화면 상단 큰 제목 (강조되는 뒷부분) */
  titleAccent: "AI Office",
  /** 브라우저 탭 제목 */
  pageTitle: "HgFinance - AI 헤지펀드 오피스",
  /** 검색·공유될 때 뜨는 설명 */
  description: "리서치·트레이딩·리스크·퀀트/백테스트·회계/포트폴리오·AI QA/감사 6개 본부와 Agent Workforce 인사팀이 함께 돌아가는 AI 헤지펀드 오피스",
  /** 창 하단 파일명 느낌의 라벨 */
  windowLabel: "HgFinance.exe — 대표실",
  /** 일일 브리핑 제목에 들어갈 이름 */
  reportName: "HgFinance AI Office",
} as const;

/** 대표 — 사무실 대표실에 앉아 있는 캐릭터 (CEO Agent / executive-orchestrator) */
export const CEO_PROFILE = {
  name: "홍진표",
  callsign: "대표님",
  role: "CEO Agent · Mandate 해석과 본부 조율",
  hair: "#42283a",
  shirt: "#ff8fc0",
  accent: "#fff3b0",
  skin: "#ffdcc4",
  thoughts: [
    "주문 승인·원장 수정·NAV 확정은 제 권한이 아니에요.",
    "6개 본부 결과를 하나의 결정과 설명으로 묶어야 해요.",
    "인사팀 예산은 승인하지만, 새 Agent 권한 검증은 QA 몫이에요.",
  ],
};

/**
 * 부서 8개. 배열 순서가 배치 순서다 — 앞 4개가 1층, 뒤 4개가 2층.
 * id = 고정(엔진용) / name·short·icon = 자유롭게 변경
 * task = 오늘 하는 일 / report = 팀장 한줄보고
 */
export const DEPARTMENTS = [
  {
    id: "research",
    name: "리서치본부",
    short: "",
    icon: "🔎",
    task: "종목별 Research Packet 작성 — 근거·촉매·무효화 조건",
    report: "출처와 시점을 확인한 것만 트레이딩본부로 넘겨요.",
  },
  {
    id: "strategy1",
    name: "퀀트·백테스트본부",
    short: "quant.lab",
    icon: "📊",
    task: "전략 가설 검증, Point-in-Time Backtest, Walk-Forward",
    report: "과적합·데이터 누수 없는 것만 Shadow 후보로 올려요.",
  },
  {
    id: "strategy2",
    name: "트레이딩본부",
    short: "trading.desk",
    icon: "📈",
    task: "Bull/Bear 토론 후 구조화된 Order Intent 작성",
    report: "리스크본부 승인 전에는 주문을 보내지 않아요.",
  },
  {
    id: "ops",
    name: "리스크본부",
    short: "risk.control",
    icon: "🛡️",
    task: "포지션 리스크·컴플라이언스 실시간 감시",
    report: "승인·축소·거부 근거만 만들고, 집행은 결정론적 엔진이 해요.",
  },
  {
    id: "finance",
    name: "회계·포트폴리오본부",
    short: "ledger.close",
    icon: "🧾",
    task: "체결 반영, 이중분개 원장 기록, NAV 산출",
    report: "공식 수치만 씁니다. 어긋나면 숨기지 않고 Break로 올려요.",
  },
  {
    id: "qa",
    name: "AI QA·감사본부",
    short: "qa.audit",
    icon: "🕵️",
    task: "근거·환각 검증, 권한분리와 Audit Finding 추적",
    report: "다른 본부가 급해도 독립 게이트는 그대로 유지해요.",
  },
  {
    id: "review",
    name: "Agent Workforce 인사팀",
    short: "workforce.hr",
    icon: "🧑‍💼",
    task: "Agent 채용·평가·교육·Lifecycle 관리",
    report: "자기 후보는 스스로 최종 승인 못 해요 — QA가 독립 검증해요.",
  },
  {
    id: "secretary",
    name: "CEO Office 지원팀",
    short: "ceo.staff",
    icon: "🗂️",
    task: "본부 결과 통합, 회의록·Action Item 추적",
    report: "대표님이 결정할 것만 추려서 올려요.",
  },
] as const;

/**
 * 직원 명단.
 * dept = 위 부서 id / rank: "lead"(팀장) 또는 "member"(팀원)
 * colors = [머리색, 옷색, 포인트색]
 * thoughts = 자리를 비웠을 때 머리 위에 뜨는 혼잣말
 *
 * 이름·직책은 각 본부 config.yaml의 agent.personalities를 그대로 옮긴 것이다.
 * (예: research-supervisor -> 리서치본부 팀장)
 */
export type StaffEntry = {
  dept: string;
  rank: "lead" | "member";
  name: string;
  role: string;
  colors: [string, string, string];
  thoughts: string[];
  callsign?: string;
};

export const STAFF_LIST: StaffEntry[] = [
  // ── 리서치본부 (research-department) ──────────────────────
  { dept: "research", rank: "lead", name: "조재일", role: "리서치본부 팀장", callsign: "오리서",
    colors: ["#6b3d34", "#fff3b0", "#ff8fc0"],
    thoughts: ["Research Packet엔 근거·촉매·무효화 조건이 다 있어야 넘겨요.", "주문 방향은 저희가 정하는 게 아니에요."] },
  { dept: "research", rank: "member", name: "워런 버핏", role: "Universe Manager",
    colors: ["#2f2a3d", "#c9b8ff", "#b8f0dd"],
    thoughts: ["거래정지·저유동성 종목은 오늘 대상에서 빼요.", "장중에도 계속 갱신해야 해요."] },
  { dept: "research", rank: "member", name: "찰리 멍거", role: "시세 데이터 관리",
    colors: ["#8a4a3c", "#b8f0dd", "#ff8fc0"],
    thoughts: ["같은 틱이 두 번 들어오면 바로 걸러내요.", "심볼 매핑 어긋난 건 내려보내기 전에 잡아야죠."] },
  { dept: "research", rank: "member", name: "피터 린치", role: "Microstructure 분석",
    colors: ["#372b4a", "#c9b8ff", "#c9b8ff"],
    thoughts: ["호가창 불균형부터 봅니다.", "체결 프린트가 이상하면 스프레드부터 확인해요."] },
  { dept: "research", rank: "member", name: "벤저민 그레이엄", role: "Technical 분석",
    colors: ["#3c3a4f", "#ffe6f2", "#c9b8ff"],
    thoughts: ["돌파인지 소음인지 상대거래량으로 걸러요.", "실현변동성부터 보고 갑니다."] },
  { dept: "research", rank: "member", name: "하워드 막스", role: "Fundamental 분석",
    colors: ["#5a3450", "#fff3b0", "#ff8fc0"],
    thoughts: ["공시 기준일부터 적어둬요.", "밸류에이션은 매번 새로 안 돌려도 돼요."] },
  { dept: "research", rank: "member", name: "존 템플턴", role: "News·Sentiment 분석",
    colors: ["#c26e4b", "#ff8fc0", "#fff3b0"],
    thoughts: ["출처·발표시각·관측시각 다 남겨야 PIT 검증이 돼요.", "재포장 기사는 원문부터 찾아요."] },
  { dept: "research", rank: "member", name: "김소원", role: "Sector·Regime 분석",
    colors: ["#7b4a2f", "#b8f0dd", "#ff8fc0"],
    thoughts: ["섹터에서 혼자 튀는 종목은 표시해둬요.", "상관구조 깨지는 신호는 놓치면 안 돼요."] },
  { dept: "research", rank: "member", name: "한소이", role: "근거 큐레이터 (RAG)",
    colors: ["#2c2638", "#fff3b0", "#c9b8ff"],
    thoughts: ["Evidence ID랑 신뢰도 점수만 넘겨요, 원문 통째로는 안 줘요.", "출처 삭제는 QA 승인 거쳐야 해요."] },

  // ── 퀀트·백테스트본부 (quant-backtest-department) ───────────
  { dept: "strategy1", rank: "lead", name: "김나연", role: "퀀트본부 팀장", callsign: "강퀀트",
    colors: ["#2d4b46", "#b8f0dd", "#b8f0dd"],
    thoughts: ["실패한 실험도 Registry에 다 남겨요.", "Production 코드는 제가 직접 안 건드려요."] },
  { dept: "strategy1", rank: "member", name: "박민성", role: "전략 가설 리서치",
    colors: ["#463227", "#ffe6f2", "#b8f0dd"],
    thoughts: ["반증 가능한 가설로 좁혀야 다음 단계로 가요.", "범위가 애매하면 다시 씁니다."] },
  { dept: "strategy1", rank: "member", name: "신라온", role: "Feature·Dataset",
    colors: ["#6c3a55", "#c9b8ff", "#fff3b0"],
    thoughts: ["미래 정보가 한 행이라도 섞이면 전부 다시 만들어요.", "PIT 안전한지부터 체크해요."] },
  { dept: "strategy1", rank: "member", name: "방시혁", role: "Backtest·Optimizer",
    colors: ["#8b534a", "#fff3b0", "#ff8fc0"],
    thoughts: ["비용·슬리피지 안 넣은 백테스트는 안 믿어요.", "과적합 냄새나면 바로 걸러요."] },
  { dept: "strategy1", rank: "member", name: "진하율", role: "전략 릴리스",
    colors: ["#33304a", "#ff8fc0", "#b8f0dd"],
    thoughts: ["Champion 대비 검증된 것만 Shadow 후보로 올려요.", "Production 승격은 제 권한 밖이에요."] },
  { dept: "strategy1", rank: "member", name: "오세훈", role: "최적화·Capacity",
    colors: ["#5d3a2c", "#b8f0dd", "#c9b8ff"],
    thoughts: ["점 하나 말고 안정적인 구간을 찾아요.", "같은 Test Set 두 번 우려먹지 않아요."] },
  { dept: "strategy1", rank: "member", name: "최윤슬", role: "ML 퀀트 리서치",
    colors: ["#4a3a2a", "#fff3b0", "#b8f0dd"],
    thoughts: ["룰베이스보다 나은 게 증명될 때만 씁니다.", "드리프트 감지되면 재학습 전에 일단 멈춰요."] },

  // ── 트레이딩본부 (trading-department) ────────────────────
  { dept: "strategy2", rank: "lead", name: "윤도현", role: "트레이딩본부 팀장", callsign: "정트레",
    colors: ["#7a3f58", "#c9b8ff", "#ff8fc0"],
    thoughts: ["Bull/Bear 토론 없이 바로 주문 제안 안 나가요.", "여러 종목이면 trade_case_id 하나로 묶어요."] },
  { dept: "strategy2", rank: "member", name: "이현서", role: "Bull 리서처",
    colors: ["#4a2e1c", "#c9b8ff", "#c9b8ff"],
    thoughts: [
      "정훈이가 또 딴지 걸겠지. 근거부터 챙기자.",
      "리서치본부가 준 근거만 씁니다. 그래야 싸울 때 안 밀려요.",
      "오늘은 커피 얻어먹는다.",
    ] },
  { dept: "strategy2", rank: "member", name: "장정훈", role: "Bear 리서처",
    colors: ["#3a2f4d", "#efe6da", "#a9714b"],
    thoughts: [
      "현서 논리 약점부터 찾습니다. 미워서가 아니라 그게 제 일이라서요.",
      "무효화 조건 없는 상승 논리는 그냥 기대예요.",
      "이번엔 현서가 맞았으면 좋겠는데.",
    ] },
  { dept: "strategy2", rank: "member", name: "양서준", role: "Trader/PM",
    colors: ["#274a44", "#fff3b0", "#b8f0dd"],
    thoughts: ["OrderIntent까지만 만들고 전송은 안 해요.", "수량·가격은 계약이 검증하게 두고 제가 우기지 않아요."] },
  { dept: "strategy2", rank: "member", name: "권나윤", role: "집행 설계",
    colors: ["#563a32", "#b8f0dd", "#b8f0dd"],
    thoughts: ["슬리피지 예산 넘기면 잔량은 미체결로 남겨요.", "체결 안 된 건 손실이 아니에요."] },
  { dept: "strategy2", rank: "member", name: "서지호", role: "파생상품 트레이더",
    colors: ["#452d3f", "#c9b8ff", "#fff3b0"],
    thoughts: ["승인 없이 멀티레그 안 엮어요.", "옵션체인 stale하면 일단 막아요."] },

  // ── 리스크본부 (risk-management) ─────────────────────────
  { dept: "ops", rank: "lead", name: "이예주", role: "리스크본부 팀장", callsign: "조리스",
    colors: ["#313b56", "#fff3b0", "#fff3b0"],
    thoughts: ["approve/resize/reject 근거는 제가 만들고 집행은 엔진이 해요.", "본부 간 신호 충돌은 CEO·감사로 바로 올려요."] },
  { dept: "ops", rank: "member", name: "문가온", role: "시장·유동성 리스크",
    colors: ["#4b3b2c", "#b8f0dd", "#c9b8ff"],
    thoughts: ["VaR·집중도 한도 근처면 바로 표시해둬요.", "새 API 호출보다 있는 데이터부터 씁니다."] },
  { dept: "ops", rank: "member", name: "안유하", role: "파생·마진 리스크",
    colors: ["#9c5c72", "#ff8fc0", "#ff8fc0"],
    thoughts: ["Greeks랑 마진 사용률 같이 봐야 해요.", "배정 리스크 놓치면 안 돼요."] },
  { dept: "ops", rank: "member", name: "류하진", role: "Compliance Policy",
    colors: ["#2e3a4a", "#ffe6f2", "#b8f0dd"],
    thoughts: ["Mandate·제한목록 근거 없이 통과 안 시켜요.", "정책은 기억이 아니라 검색해서 인용해요."] },
  { dept: "ops", rank: "member", name: "노은우", role: "Pre-Trade 리스크",
    colors: ["#6b4a2f", "#c9b8ff", "#fff3b0"],
    thoughts: ["스냅샷 오래됐으면 일단 거부부터 해요.", "규칙을 자연어 판단으로 덮어쓰지 않아요."] },
  { dept: "ops", rank: "member", name: "마도연", role: "운영·거래상대방 리스크",
    colors: ["#3b3b49", "#b8f0dd", "#b8f0dd"],
    thoughts: ["브로커 상태 불명이면 새 주문보다 확인이 먼저예요.", "현금·주문 안 맞으면 회계본부랑 같이 봐요."] },

  // ── 회계·포트폴리오본부 (accounting-portfolio-department) ──
  { dept: "finance", rank: "lead", name: "김승리", role: "회계본부 팀장", callsign: "임포트",
    colors: ["#573049", "#fff3b0", "#ff8fc0"],
    thoughts: ["대사·평가·Accrual·손익·NAV 순서를 지켜요.", "Break는 숨기지 않고 바로 올려요."] },
  { dept: "finance", rank: "member", name: "지수아", role: "포지션·현금 관리",
    colors: ["#7a453c", "#c9b8ff", "#c9b8ff"],
    thoughts: ["Accounting Engine이 확정한 숫자만 말해요.", "Long/Short은 항상 따로 보고해요."] },
  { dept: "finance", rank: "member", name: "백승희", role: "대사 담당",
    colors: ["#334a3a", "#ffe6f2", "#fff3b0"],
    thoughts: ["Broker Fill ID부터 맞춰보고 Fuzzy는 제가 확정 안 해요.", "브로커에만 있는 체결이 제일 위험해요."] },
  { dept: "finance", rank: "member", name: "하지민", role: "펀드 회계",
    colors: ["#6b3d34", "#fff3b0", "#ff8fc0"],
    thoughts: ["Posted Journal은 절대 안 고치고 반대분개로만 정정해요.", "근거 없는 평가금액은 NAV라고 안 해요."] },
  { dept: "finance", rank: "member", name: "오세인", role: "자금·증거금",
    colors: ["#2f2a3d", "#c9b8ff", "#b8f0dd"],
    thoughts: ["증거금 부족은 문제 되기 전에 미리 알려요.", "차입비용도 0으로 안 놔둬요."] },
  { dept: "finance", rank: "member", name: "곽나은", role: "손익·귀속 분석",
    colors: ["#8a4a3c", "#b8f0dd", "#ff8fc0"],
    thoughts: ["기대 엣지랑 실현손익 차이는 원인별로 나눠요.", "상관관계를 원인이라고 말 안 해요."] },
  { dept: "finance", rank: "member", name: "성지우", role: "투자자 보고",
    colors: ["#372b4a", "#c9b8ff", "#c9b8ff"],
    thoughts: ["공식 수치 ID로만 인용해요, 기억으로 안 써요.", "안 좋은 결과도 빼지 않아요."] },
  { dept: "finance", rank: "member", name: "편하늘", role: "기업행동·평가",
    colors: ["#3c3a4f", "#ffe6f2", "#c9b8ff"],
    thoughts: ["배당·분할은 기준일부터 확인해요.", "불완전한 통지로는 최종 분개 안 올려요."] },

  // ── AI QA·감사본부 (qa-department) ───────────────────────
  { dept: "qa", rank: "lead", name: "김동규", role: "QA·감사본부 팀장", callsign: "남감사",
    colors: ["#5a3450", "#fff3b0", "#ff8fc0"],
    thoughts: ["압박 있어도 게이트는 그대로 유지해요.", "제 Finding은 저 혼자 못 닫아요."] },
  { dept: "qa", rank: "member", name: "강태오", role: "근거(Evidence) 검증",
    colors: ["#463227", "#ffe6f2", "#b8f0dd"],
    thoughts: ["주장 하나마다 출처까지 연결돼야 해요.", "시점 안 맞는 근거는 통과 안 시켜요."] },
  { dept: "qa", rank: "member", name: "문세라", role: "환각(Hallucination) 검증",
    colors: ["#6c3a55", "#c9b8ff", "#fff3b0"],
    thoughts: ["에이전트가 실제로 검색한 근거랑 대조해요.", "외부 호출 기다리다 막히는 것보단 있는 데이터로 판단해요."] },
  { dept: "qa", rank: "member", name: "정하은", role: "Model Risk",
    colors: ["#8b534a", "#fff3b0", "#ff8fc0"],
    thoughts: ["재현 안 되는 백테스트는 Production 근처도 못 가요.", "선언된 입력으로 다시 돌려봐야 믿어요."] },
  { dept: "qa", rank: "member", name: "배준서", role: "내부 감사",
    colors: ["#33304a", "#ff8fc0", "#b8f0dd"],
    thoughts: ["권한 분리 깨진 건 6개 본부 다 훑어요.", "방치된 Finding부터 찾아냅니다."] },
  { dept: "qa", rank: "member", name: "서유나", role: "Agent 운영 모니터링",
    colors: ["#5d3a2c", "#b8f0dd", "#c9b8ff"],
    thoughts: ["에러율·지연·비용은 항상 켜놓고 봐요.", "상태 나빠지면 Incident로 바로 올려요."] },
  { dept: "qa", rank: "member", name: "한지오", role: "권한·보안 검토",
    colors: ["#4a3a2a", "#fff3b0", "#b8f0dd"],
    thoughts: ["Allowlist 밖 Tool 호출은 바로 잡아요.", "제 권한은 제가 안 늘려요."] },
  { dept: "qa", rank: "member", name: "조은채", role: "인시던트 사후분석",
    colors: ["#7a3f58", "#c9b8ff", "#ff8fc0"],
    thoughts: ["관찰한 사실이랑 추론은 나눠서 적어요.", "증거 없이 한 명 탓 안 해요."] },

  // ── Agent Workforce 인사팀 (hr-department) ───────────────
  { dept: "review", rank: "lead", name: "류영주", role: "인사팀 팀장", callsign: "류인사",
    colors: ["#3d2818", "#c9b8ff", "#c9b8ff"],
    thoughts: ["투자 판단은 제 일이 아니에요.", "제 후보는 제가 최종 승인 못 해요."] },
  { dept: "review", rank: "member", name: "임도훈", role: "채용 우선순위 기획",
    colors: ["#3a2f4d", "#ffe6f2", "#ff8fc0"],
    thoughts: ["Queue 밀린 본부부터 봐요.", "SLA 깨질 위험이면 순위 올려요."] },
  { dept: "review", rank: "member", name: "최여은", role: "Job Profile 설계",
    colors: ["#274a44", "#fff3b0", "#b8f0dd"],
    thoughts: ["금지 권한부터 먼저 적어요.", "권한 경계 문장은 제가 손 안 대요."] },
  { dept: "review", rank: "member", name: "조민규", role: "선발·성과 평가",
    colors: ["#563a32", "#b8f0dd", "#b8f0dd"],
    thoughts: ["Golden/Adversarial Eval 통과해야 수습 시작해요.", "개정판도 신규 채용이랑 같은 기준이에요."] },
  { dept: "review", rank: "member", name: "백서아", role: "Lifecycle 코디네이터",
    colors: ["#452d3f", "#c9b8ff", "#fff3b0"],
    thoughts: ["Identity 생성은 Platform/IAM 몫이지 제가 아니에요.", "승인 났는데 정리 안 된 채로 안 놔둬요."] },

  // ── CEO Office 지원팀 (Chief-of-Staff, executive-orchestrator 보조) ──
  { dept: "secretary", rank: "lead", name: "박유안", role: "Chief of Staff 지원", callsign: "박비서",
    colors: ["#313b56", "#fff3b0", "#fff3b0"],
    thoughts: ["6개 본부 결과를 하나의 설명으로 묶어요.", "주문 전송·리스크 승인 권한은 저희한테 없어요."] },
  { dept: "secretary", rank: "member", name: "박지현", role: "회의록·Action Item 추적",
    colors: ["#4b3b2c", "#b8f0dd", "#c9b8ff"],
    thoughts: ["기한 지난 안건은 자동으로 다시 올려요.", "결정된 것만 대표님께 남겨드려요."] },

];

/**
 * 외부 연동을 아직 안 붙인 팀 → 화면에 "연동 대기"로 표시됩니다.
 * 연동을 다 붙였거나, 그냥 전부 초록불로 보고 싶으면 빈 배열 []로 두세요.
 */
export const PENDING_INTEGRATIONS: Record<string, string> = {};

/**
 * 결과 보관함 링크 (Notion 등). 비워두면 화면에서 링크 버튼이 숨겨집니다.
 * 예: "https://www.notion.so/내페이지주소"
 */
export const STORAGE_LINK = "";
