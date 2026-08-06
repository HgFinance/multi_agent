// ============================================================
// HgFinance AI Office projection 설정
// ============================================================
// 이 파일은 ai-office 데모에 표시할 조직·직원 정보를 정의한다.
// 실제 Hermes/LangGraph 런타임의 Source of Truth는 departments/<n>/hermes/
// 및 각 부서 Worker Registry이며, 이 화면은 외부 운영 상태를 대신하지 않는다.
//
// 시뮬레이션 엔진 호환성 때문에 아래 부서 id는 고정한다.
// - research: Research
// - strategy1: Quant/Backtest
// - strategy2: Trading
// - ops: Risk
// - finance: Accounting/Portfolio
// - qa: AI QA/Audit
// - review: Agent Workforce/HR
// - secretary: CEO Office shared-support projection (독립 Hermes 부서 아님)
// ============================================================

export const COMPANY = {
  name: "HgFinance",
  logoLetter: "F",
  titlePrefix: "개인형 헤지펀드",
  titleAccent: "AI Office",
  pageTitle: "HgFinance - AI 헤지펀드 오피스",
  description:
    "CEO와 7개 Hermes 부서(Research·Trading·Risk·Quant·Accounting·QA·HR)를 직원별 독립 LangGraph Worker로 투영하는 AI Office 데모",
  windowLabel: "HgFinance.exe — 대표실",
  reportName: "HgFinance AI Office",
} as const;

/** CEO Profile은 별도 Hermes Profile(ceo-agent)의 Projection이다. */
export const CEO_PROFILE = {
  name: "홍진표",
  callsign: "대표님",
  role: "CEO Agent · Mandate 해석과 본부 조율",
  hair: "#42283a",
  shirt: "#ff8fc0",
  accent: "#fff3b0",
  skin: "#ffdcc4",
  thoughts: [
    "주문 제출·Risk 승인·원장 수정·NAV 확정은 제 권한이 아니에요.",
    "7개 부서의 typed context와 게이트 결과를 최종 요약으로 묶어요.",
    "최종 판단은 설명 가능한 paper decision으로 남겨야 해요.",
  ],
} as const;

/**
 * 시뮬레이션 엔진이 사용하는 8개 공간.
 * 실제 조직 기준으로는 CEO + 7개 부서이며 secretary는 CEO 지원용 shared service다.
 */
export const DEPARTMENTS = [
  {
    id: "research",
    name: "리서치본부",
    short: "research",
    icon: "🔎",
    task: "Research Packet: 시장·데이터·촉매·무효화 조건",
    report: "출처·시점·근거가 확인된 사실만 Trading으로 넘겨요.",
  },
  {
    id: "strategy1",
    name: "퀀트·백테스트본부",
    short: "quant.lab",
    icon: "📊",
    task: "PIT 데이터·전략 가설·Backtest·Robustness 검증",
    report: "과적합·누수·비용 문제가 없는 후보만 QA로 올려요.",
  },
  {
    id: "strategy2",
    name: "트레이딩본부",
    short: "trading.desk",
    icon: "📈",
    task: "Research context를 OrderIntent로 구조화",
    report: "Risk/Compliance Gate 전에는 주문을 제출하지 않아요.",
  },
  {
    id: "ops",
    name: "리스크관리본부",
    short: "risk.control",
    icon: "🛡️",
    task: "Market·Pre-trade·Policy·Derivatives Risk context",
    report: "바인딩 승인·한도·상태 전이는 결정론적 Risk Engine이 맡아요.",
  },
  {
    id: "finance",
    name: "회계·포트폴리오본부",
    short: "ledger.close",
    icon: "🧾",
    task: "Position·Ledger·Reconciliation·NAV context",
    report: "공식 수치는 결정론적 원장과 대사 결과만 사용해요.",
  },
  {
    id: "qa",
    name: "AI QA·감사본부",
    short: "qa.audit",
    icon: "🕵️",
    task: "Evidence·Hallucination·Model Risk·Ops·Incident 검증",
    report: "Evidence QA Gate가 PASS가 아니면 CEO와 다음 단계에 에스컬레이션해요.",
  },
  {
    id: "review",
    name: "Agent Workforce 인사팀",
    short: "workforce.hr",
    icon: "🧑‍💼",
    task: "Worker Profile·역할·SLA·Lifecycle 관리",
    report: "인사팀은 투자 판단을 하지 않고 역할과 권한을 관리해요.",
  },
  {
    id: "secretary",
    name: "CEO Office 지원",
    short: "ceo.staff",
    icon: "🗂️",
    task: "부서 결과 요약·회의록·Action Item Projection",
    report: "CEO가 결정할 context만 정리하며 주문 권한은 없어요.",
  },
] as const;

export type StaffEntry = {
  dept: string;
  rank: "lead" | "member";
  name: string;
  role: string;
  colors: [string, string, string];
  thoughts: string[];
  callsign?: string;
};

const COLORS: [string, string, string][] = [
  ["#2f2a3d", "#c9b8ff", "#b8f0dd"],
  ["#8a4a3c", "#b8f0dd", "#ff8fc0"],
  ["#372b4a", "#c9b8ff", "#fff3b0"],
  ["#463227", "#ffe6f2", "#b8f0dd"],
  ["#6c3a55", "#fff3b0", "#ff8fc0"],
  ["#3b3b49", "#b8f0dd", "#c9b8ff"],
  ["#274a44", "#fff3b0", "#b8f0dd"],
  ["#573049", "#ff8fc0", "#fff3b0"],
];

const workerThoughts = [
  ["근거와 시점을 먼저 확인해요.", "권한 밖의 결론은 내리지 않아요."],
  ["입력 스냅샷과 출력 계약을 함께 남겨요.", "불확실하면 보수적인 상태로 넘겨요."],
  ["실패를 성공으로 포장하지 않아요.", "재현 가능한 context만 보고해요."],
  ["결정론적 게이트를 자연어로 덮어쓰지 않아요.", "도구 결과와 추론을 분리해요."],
  ["중복 신호는 하나의 근거로 합쳐요.", "다음 부서가 검증할 수 있게 기록해요."],
  ["PIT와 입력 버전을 고정해요.", "오류 원인과 fallback을 숨기지 않아요."],
  ["금지된 권한은 호출하지 않아요.", "필요하면 HOLD 또는 ESCALATE로 넘겨요."],
  ["로그·리플레이·리뷰를 남겨요.", "결과보다 경계 준수를 우선해요."],
];

function staff(
  dept: string,
  rank: StaffEntry["rank"],
  name: string,
  role: string,
  index: number,
  callsign?: string,
  thoughts?: string[],
): StaffEntry {
  return {
    dept,
    rank,
    name,
    role,
    callsign,
    colors: COLORS[index % COLORS.length],
    thoughts: thoughts ?? workerThoughts[index % workerThoughts.length],
  };
}

/**
 * 부서장(lead)은 화면상 Hermes Head, member는 독립 LangGraph Worker다.
 * 실제 Worker 수는 각 부서 config.yaml의 worker_count와 동일하다.
 */
export const STAFF_LIST: StaffEntry[] = [
  staff("research", "lead", "조재일", "Hermes Head · Research", 0, "조리서"),
  staff("research", "member", "워런 버핏", "research-data-worker", 1),
  staff("research", "member", "찰리 멍거", "microstructure-worker", 2),
  staff("research", "member", "피터 린치", "technical-signal-worker", 3),
  staff("research", "member", "벤저민 그레이엄", "fundamental-valuation-worker", 4),
  staff("research", "member", "하워드 막스", "news-macro-worker", 5),
  staff("research", "member", "한소이", "evidence-rag-worker", 6),

  staff("strategy1", "lead", "김재일", "Hermes Head · Quant/Backtest", 1, "김퀀트"),
  staff("strategy1", "member", "박민성", "strategy-hypothesis-worker", 2),
  staff("strategy1", "member", "신라온", "dataset-feature-worker", 3),
  staff("strategy1", "member", "방시혁", "backtest-optimization-worker", 4),
  staff("strategy1", "member", "진하율", "strategy-release-worker", 5),
  staff("strategy1", "member", "최윤슬", "ml-quant-worker", 6),
  staff("strategy1", "member", "오세훈", "execution-cost-worker", 7),
  staff("strategy1", "member", "장도현", "regime-robustness-worker", 0),

  staff("strategy2", "lead", "윤도현", "Hermes Head · Trading", 2, "윤트레", [
    "Bull/Bear 토론 없이 바로 주문 제안 안 나가요.",
    "여러 종목이면 trade_case_id 하나로 묶어요.",
  ]),
  // 이현서·장정훈은 config.yaml agent.personalities의 TRD-01/TRD-02 페르소나다.
  // runtime worker가 아니라 worker_count(6)에 포함되지 않는다.
  // app/game/sim.ts의 debate()가 이 두 role 문자열로 둘을 찾으므로 문구를 바꾸지 않는다.
  staff("strategy2", "member", "이현서", "Bull 리서처", 3, undefined, [
    "정훈이가 또 딴지 걸겠지. 근거부터 챙기자.",
    "리서치본부가 준 근거만 씁니다. 그래야 싸울 때 안 밀려요.",
    "오늘은 커피 얻어먹는다.",
  ]),
  staff("strategy2", "member", "장정훈", "Bear 리서처", 4, undefined, [
    "현서 논리 약점부터 찾습니다. 미워서가 아니라 그게 제 일이라서요.",
    "무효화 조건 없는 상승 논리는 그냥 기대예요.",
    "이번엔 현서가 맞았으면 좋겠는데.",
  ]),
  // 2026-08-06 tool 강등. 조건부 직원 5명(윤서준·임채린·정하람·서지호·박서연)과
  // 이미 분리돼 사라진 market-thesis-worker 자리를 잡무 담당 하나로 합쳤다.
  // 위 Bull/Bear 의 고정 멘트는 **사무실 화면용 연출**이다 — 실제 토론은
  // departments/02-trading/scripts.py 가 직원 모델로 2라운드를 돌리고 부서장이 사회를 본다.
  staff("strategy2", "member", "한지우", "desk-runner", 3, undefined, [
    "저는 판단 안 합니다. 숫자만 가져와요.",
    "브로커 초당 한도 넘는 분할은 제 선에서 반려됩니다. 규칙이 그래서요.",
    "인증 서명 없으면 없다고 적습니다. 있다고 못 씁니다.",
  ]),

  staff("ops", "lead", "이예주", "Hermes Head · Risk", 3, "이리스"),
  staff("ops", "member", "류하진", "compliance-policy-worker", 4),
  staff("ops", "member", "문가온", "risk-runner", 5),

  staff("finance", "lead", "김승리", "Hermes Head · Accounting/Portfolio", 4, "김회계"),
  staff("finance", "member", "지수아", "portfolio-control-worker", 5),
  staff("finance", "member", "백승희", "ledger-reconciliation-worker", 6),
  staff("finance", "member", "하지민", "nav-close-worker", 7),
  staff("finance", "member", "오세인", "treasury-liquidity-worker", 0),
  staff("finance", "member", "곽나은", "pnl-attribution-worker", 1),
  staff("finance", "member", "성지우", "investor-reporting-worker", 2),
  staff("finance", "member", "편하늘", "valuation-corporate-actions-worker", 3),
  staff("finance", "member", "김서현", "fee-accrual-tax-worker", 4),

  staff("qa", "lead", "김동규", "Hermes Head · AI QA/Audit", 5, "김QA"),
  staff("qa", "member", "문세라", "hallucination-critic-worker", 7),
  staff("qa", "member", "이수빈", "incident-postmortem-worker", 2),
  staff("qa", "member", "강태오", "qa-runner", 3),

  staff("review", "lead", "류영주", "Hermes Head · Agent Workforce", 6, "류인사"),
  staff("review", "member", "임도훈", "workforce-planning-worker", 7),
  staff("review", "member", "최여은", "profile-architecture-worker", 0),
  staff("review", "member", "조민규", "selection-performance-worker", 1),
  staff("review", "member", "이하윤", "lifecycle-coordination-worker", 2),
  staff("review", "member", "박지현", "workforce-governance-worker", 3),

  // secretary는 독립 Hermes 부서가 아닌 CEO 지원 Projection이다.
  staff("secretary", "lead", "박유안", "CEO Office shared support", 7, "박비서"),
  staff("secretary", "member", "박지현", "executive-briefing-worker", 0),
];

/** 실제 외부 연동이 없는 화면은 시뮬레이션 상태로 표시한다. */
export const PENDING_INTEGRATIONS: Record<string, string> = {};

/** Notion 연동은 별도 API adapter와 credential이 있을 때만 활성화한다. */
export const STORAGE_LINK = "";
