// ============================================================
//  나의 AI 회사 설정 — 여기 한 파일만 고치면 됩니다
// ============================================================
//  회사 이름, 부서 이름, 직원 이름·성격·머리색까지 전부 여기 있어요.
//  다른 파일은 건드리지 않아도 됩니다.
//
//  ⚠️ 딱 2가지 규칙
//   1. 부서 id(research, brand, ...)는 절대 바꾸지 마세요. 시뮬레이션 엔진이
//      이 id로 움직입니다. 바꾸면 캐릭터가 길을 잃어요.
//      → 바꿔도 되는 건 name(부서 이름) · icon · short 입니다.
//   2. 부서는 12개를 유지하세요. 사무실 배치가 4열 3행 = 12칸 고정입니다.
//      안 쓰는 부서는 지우지 말고 이름만 바꿔서 쓰세요.
//
//  직원 수는 자유롭게 늘리고 줄여도 됩니다. 한 팀에 팀장(lead) 1명은 두세요.
// ============================================================

/** 회사 기본 정보 */
export const COMPANY = {
  /** 좌측 상단 헤더에 뜨는 회사 이름 */
  name: "MY AI COMPANY",
  /** 헤더 로고 배지에 들어갈 글자 1개 (이모지도 됩니다) */
  logoLetter: "M",
  /** 화면 상단 큰 제목 (앞부분) */
  titlePrefix: "나의",
  /** 화면 상단 큰 제목 (강조되는 뒷부분) */
  titleAccent: "AI Office",
  /** 브라우저 탭 제목 */
  pageTitle: "My AI Company — 나의 AI 사무실",
  /** 검색·공유될 때 뜨는 설명 */
  description: "12개 AI 팀이 조사·기획·제작·보고까지 돌아가는 1인 크리에이터용 AI 오피스",
  /** 창 하단 파일명 느낌의 라벨 */
  windowLabel: "my_ai_company.exe — 대표실",
  /** 일일 브리핑 제목에 들어갈 이름 */
  reportName: "AI Office",
} as const;

/** 대표(나) — 사무실 대표실에 앉아 있는 캐릭터 */
export const CEO_PROFILE = {
  name: "김대표",
  callsign: "대표님",
  role: "대표 · 최종 의사결정",
  hair: "#42283a",
  shirt: "#ff8fc0",
  accent: "#fff3b0",
  skin: "#ffdcc4",
  thoughts: [
    "AI는 비서, 최종 결정은 내가 해요.",
    "오늘 결정할 건 딱 1개만 남기자.",
    "저장될 만한 콘텐츠인지부터 본다.",
  ],
};

/**
 * 부서 12개.
 * id = 고정(엔진용) / name·short·icon = 자유롭게 변경
 * task = 오늘 하는 일 / report = 팀장 한줄보고
 */
export const DEPARTMENTS = [
  {
    id: "research",
    name: "시장조사팀",
    short: "trend.lab",
    icon: "🔎",
    task: "업계 뉴스·트렌드 수집",
    report: "출처를 검증하고 오늘의 후보를 정리해요.",
  },
  {
    id: "brand",
    name: "브랜드 분석팀",
    short: "brand.room",
    icon: "🧬",
    task: "채널 흐름·정체성 점검",
    report: "지표 연동이 되면 수치까지 붙습니다.",
  },
  {
    id: "strategy1",
    name: "기획 1팀",
    short: "idea.studio",
    icon: "💡",
    task: "오늘의 아이디어 10개",
    report: "점수 기준으로 TOP 3까지 좁혀요.",
  },
  {
    id: "qa",
    name: "품질 검수팀",
    short: "qa.check",
    icon: "🛡️",
    task: "근거·중복·톤 검사",
    report: "기준에서 벗어난 안은 되돌려보내요.",
  },
  {
    id: "strategy2",
    name: "기획 2팀",
    short: "script.team",
    icon: "✍️",
    task: "승인된 안 원고 작성",
    report: "대표가 고른 아이디어만 글로 옮겨요.",
  },
  {
    id: "reels",
    name: "영상 제작팀",
    short: "video.edit",
    icon: "🎬",
    task: "영상 원본 접수·초안 편집",
    report: "원본은 보존하고 편집본만 새로 만들어요.",
  },
  {
    id: "carousel",
    name: "이미지 제작팀",
    short: "design.studio",
    icon: "🖼️",
    task: "카드·썸네일 디자인",
    report: "필요한 장수만 만들고 CTA로 닫아요.",
  },
  {
    id: "partner",
    name: "제휴 커뮤니케이션팀",
    short: "partner.mail",
    icon: "💌",
    task: "협업 문의 검토·답장 초안",
    report: "초안까지만 씁니다. 발송은 대표가 해요.",
  },
  {
    id: "finance",
    name: "재무·정산팀",
    short: "finance.xls",
    icon: "🧾",
    task: "수익·입금 현황 정리",
    report: "현황 파일이 오면 바로 정리합니다.",
  },
  {
    id: "review",
    name: "성과리뷰팀",
    short: "review.data",
    icon: "📈",
    task: "성과·학습점 기록",
    report: "잘된 이유를 패턴으로 남겨요.",
  },
  {
    id: "ops",
    name: "자동화 운영팀",
    short: "automation.ops",
    icon: "⚙️",
    task: "연동·실패·재시도 관리",
    report: "실패하면 재시도하고 로그를 남겨요.",
  },
  {
    id: "secretary",
    name: "비서실",
    short: "secretary.hq",
    icon: "📋",
    task: "전사 한줄보고·최종 브리핑",
    report: "모든 팀 상태를 모아 결정할 것만 남겨드려요.",
  },
] as const;

/**
 * 직원 명단.
 * dept = 위 부서 id / rank: "lead"(팀장) 또는 "member"(팀원)
 * colors = [머리색, 옷색, 포인트색]
 * thoughts = 자리를 비웠을 때 머리 위에 뜨는 혼잣말
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
  // ① 시장조사팀
  { dept: "research", rank: "lead", name: "김서연", role: "시장조사 팀장", callsign: "김리서",
    colors: ["#6b3d34", "#fff3b0", "#ff8fc0"],
    thoughts: ["이 기사, 공식 출처가 있나 확인해야 해.", "발표일이 7일 넘었으면 후보에서 빼자.", "원문부터 다시 본다."] },
  { dept: "research", rank: "member", name: "오태윤", role: "뉴스 리서처",
    colors: ["#2f2a3d", "#c9b8ff", "#b8f0dd"],
    thoughts: ["신규 업로드인데 반응 0이면 인기 아님.", "우리나라에서 되는 기능인지 체크."] },
  { dept: "research", rank: "member", name: "하은채", role: "동향 조사",
    colors: ["#8a4a3c", "#b8f0dd", "#ff8fc0"],
    thoughts: ["이번 주 사람들이 뭘 저장했지?", "재포장 기사는 원문으로 안 쳐요."] },

  // ② 브랜드 분석팀
  { dept: "brand", rank: "lead", name: "박보라", role: "브랜드 분석 팀장", callsign: "박브리",
    colors: ["#372b4a", "#c9b8ff", "#c9b8ff"],
    thoughts: ["지표 연동 전엔 수치를 지어내지 않아요.", "우리 색깔에서 벗어난 각도인지 본다."] },
  { dept: "brand", rank: "member", name: "신재원", role: "채널 지표 분석",
    colors: ["#3c3a4f", "#ffe6f2", "#c9b8ff"],
    thoughts: ["저장률이 도달보다 중요해요.", "30일 흐름부터 그려보자."] },
  { dept: "brand", rank: "member", name: "임다혜", role: "정체성 검증",
    colors: ["#5a3450", "#fff3b0", "#ff8fc0"],
    thoughts: ["우리가 안 쓰기로 한 프레임이에요.", "타겟이 흐려지면 다시 잡아요."] },

  // ③ 기획 1팀
  { dept: "strategy1", rank: "lead", name: "최아름", role: "기획 1팀장", callsign: "최아이",
    colors: ["#c26e4b", "#ff8fc0", "#fff3b0"],
    thoughts: ["오늘도 정확히 10개, 예외 없어요.", "기준 점수부터 채우고 시작.", "각도가 겹치면 프레임을 바꾼다."] },
  { dept: "strategy1", rank: "member", name: "정유진", role: "아이디어 발굴",
    colors: ["#7b4a2f", "#b8f0dd", "#ff8fc0"],
    thoughts: ["제목을 좀 더 구체적으로 바꿔볼까.", "오늘 행동 1개가 빠졌다."] },
  { dept: "strategy1", rank: "member", name: "배시현", role: "후킹 카피",
    colors: ["#2c2638", "#fff3b0", "#c9b8ff"],
    thoughts: ["훅 3초 안에 안 걸리면 다시 써요.", "단정형으로 닫자, 권유형 금지."] },

  // ④ 품질 검수팀
  { dept: "qa", rank: "lead", name: "윤규아", role: "품질 검수 팀장", callsign: "윤큐아",
    colors: ["#2d4b46", "#b8f0dd", "#b8f0dd"],
    thoughts: ["금칙어 스캔 돌립니다.", "근거 링크 없는 안은 반려예요."] },
  { dept: "qa", rank: "member", name: "강태오", role: "중복·근거 검사",
    colors: ["#463227", "#ffe6f2", "#b8f0dd"],
    thoughts: ["최근 7일 안에 40% 겹쳤네.", "바로 써먹을 실물이 있는지 확인."] },
  { dept: "qa", rank: "member", name: "문세라", role: "톤 검수",
    colors: ["#6c3a55", "#c9b8ff", "#fff3b0"],
    thoughts: ["과장된 표현은 바로 빼요.", "우리 톤 유지하는지 본다."] },

  // ⑤ 기획 2팀
  { dept: "strategy2", rank: "lead", name: "한도빈", role: "원고 팀장", callsign: "한대본",
    colors: ["#8b534a", "#fff3b0", "#ff8fc0"],
    thoughts: ["승인된 안만 원고로 씁니다.", "결론은 하나로 닫아야 해요."] },
  { dept: "strategy2", rank: "member", name: "조민서", role: "영상 대본",
    colors: ["#33304a", "#ff8fc0", "#b8f0dd"],
    thoughts: ["구조부터 잡고 들어간다.", "30초 안에 끝나야 해요."] },
  { dept: "strategy2", rank: "member", name: "백가온", role: "카드 원고",
    colors: ["#5d3a2c", "#b8f0dd", "#c9b8ff"],
    thoughts: ["3장에서 원인을 다시 정의합니다.", "마지막 장은 댓글 유도로."] },

  // ⑥ 영상 제작팀
  { dept: "reels", rank: "lead", name: "송리원", role: "영상 제작 팀장", callsign: "송릴스",
    colors: ["#2c2638", "#ff8fc0", "#ff8fc0"],
    thoughts: ["원본은 절대 안 건드려요.", "무음 컷부터 치고 시작."] },
  { dept: "reels", rank: "member", name: "권지호", role: "편집",
    colors: ["#4a3a2a", "#fff3b0", "#b8f0dd"],
    thoughts: ["컷 템포가 늘어지면 이탈이에요.", "도입부는 대표가 직접 넣어요."] },
  { dept: "reels", rank: "member", name: "유세아", role: "자막·썸네일",
    colors: ["#7a3f58", "#c9b8ff", "#ff8fc0"],
    thoughts: ["썸네일 5종 뽑아둘게요.", "워터마크는 안 넣습니다."] },

  // ⑦ 이미지 제작팀
  { dept: "carousel", rank: "lead", name: "이가림", role: "이미지 제작 팀장", callsign: "이캐리",
    colors: ["#d88d68", "#c9b8ff", "#c9b8ff"],
    thoughts: ["원본 템플릿은 복제만, 수정 금지.", "필요한 장수만 뽑아요."] },
  { dept: "carousel", rank: "member", name: "남주하", role: "레이아웃",
    colors: ["#3a2f4d", "#ffe6f2", "#ff8fc0"],
    thoughts: ["글자 밀도 맞추는 중.", "표지 3안부터 만들자."] },
  { dept: "carousel", rank: "member", name: "표하늘", role: "텍스트 교체",
    colors: ["#274a44", "#fff3b0", "#b8f0dd"],
    thoughts: ["마지막 장 CTA 빠지면 반려예요.", "복제본에만 손댑니다."] },

  // ⑧ 제휴 커뮤니케이션팀
  { dept: "partner", rank: "lead", name: "정파랑", role: "제휴 팀장", callsign: "정파트",
    colors: ["#563a32", "#b8f0dd", "#b8f0dd"],
    thoughts: ["메일 연동 전이라 아직 못 읽어요.", "실제 발송은 대표 손으로."] },
  { dept: "partner", rank: "member", name: "구예성", role: "협업 검토",
    colors: ["#452d3f", "#c9b8ff", "#fff3b0"],
    thoughts: ["결이 맞는 제안만 받습니다.", "답장 초안까지만 준비해둘게요."] },

  // ⑨ 재무·정산팀
  { dept: "finance", rank: "lead", name: "오재민", role: "재무 팀장", callsign: "오재무",
    colors: ["#313b56", "#fff3b0", "#fff3b0"],
    thoughts: ["현황 파일이 오면 바로 정리합니다.", "입금 대기 건부터 확인해요."] },
  { dept: "finance", rank: "member", name: "심우진", role: "정산 관리",
    colors: ["#4b3b2c", "#b8f0dd", "#c9b8ff"],
    thoughts: ["지연된 건은 따로 표시해둡니다.", "결제는 자동으로 안 해요."] },

  // ⑩ 성과리뷰팀
  { dept: "review", rank: "lead", name: "강성아", role: "성과리뷰 팀장", callsign: "강성과",
    colors: ["#9c5c72", "#ff8fc0", "#ff8fc0"],
    thoughts: ["잘된 이유를 패턴으로 남겨야 해요.", "저장·댓글이 진짜 지표입니다."] },
  { dept: "review", rank: "member", name: "마지훈", role: "지표 수집",
    colors: ["#2e3a4a", "#ffe6f2", "#b8f0dd"],
    thoughts: ["도달·저장·공유 다시 긁어옵니다.", "연동되면 자동화돼요."] },
  { dept: "review", rank: "member", name: "여름", role: "학습점 정리",
    colors: ["#6b4a2f", "#c9b8ff", "#fff3b0"],
    thoughts: ["반복할 패턴 1개, 중단할 패턴 1개.", "다음 기획팀에 넘길 학습점 정리 중."] },

  // ⑪ 자동화 운영팀
  { dept: "ops", rank: "lead", name: "안도현", role: "자동화 운영 팀장", callsign: "안오토",
    colors: ["#3b3b49", "#b8f0dd", "#b8f0dd"],
    thoughts: ["오전 스케줄 정상입니다.", "실패하면 재시도하고 로그 남겨요."] },
  { dept: "ops", rank: "member", name: "천유나", role: "연동 모니터링",
    colors: ["#573049", "#fff3b0", "#ff8fc0"],
    thoughts: ["연결 안 된 서비스를 성공으로 안 씁니다.", "연동 대기 중이에요."] },

  // ⑫ 비서실
  { dept: "secretary", rank: "lead", name: "김세리", role: "비서실장", callsign: "김비서",
    colors: ["#7a453c", "#c9b8ff", "#c9b8ff"],
    thoughts: ["대표가 결정할 것만 추립니다.", "중복 설명은 다 지워요."] },
  { dept: "secretary", rank: "member", name: "홍보람", role: "브리핑 정리",
    colors: ["#334a3a", "#ffe6f2", "#fff3b0"],
    thoughts: ["상태별로 묶어서 올릴게요.", "막힌 건 먼저 보고해요."] },
];

/**
 * 외부 연동을 아직 안 붙인 팀 → 화면에 "연동 대기"로 표시됩니다.
 * 연동을 다 붙였거나, 그냥 전부 초록불로 보고 싶으면 빈 배열 []로 두세요.
 */
export const PENDING_INTEGRATIONS: Record<string, string> = {
  brand: "채널 지표 연동",
  partner: "메일 연동",
  finance: "재무 현황 파일",
};

/**
 * 결과 보관함 링크 (Notion 등). 비워두면 화면에서 링크 버튼이 숨겨집니다.
 * 예: "https://www.notion.so/내페이지주소"
 */
export const STORAGE_LINK = "";
