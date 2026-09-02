/**
 * 인증된 사용자의 PAPER 조건주문 목록 — FastAPI BFF
 * `/ui/conditional-rules`.
 *
 * 조건주문은 실시간 브로커 주문 사건과 별개의 대기 지시다. 이 클라이언트는
 * 백엔드가 내려주는 canonical AST를 보존하고, 대시보드가 읽을 수 있는 짧은
 * 한국어 문구로 바꾸는 표시 함수도 함께 제공한다.
 */

import { BFF, bffFetch } from "./bffClient";

export type ConditionalExpression = {
  type: string;
  value?: string | number | boolean | null;
  unit?: string | null;
  field?: string | null;
  name?: string | null;
  output?: string | null;
  source?: string | null;
  provider?: string | null;
  timeframe?: string | null;
  parameters?: Record<string, string | number> | null;
  operator?: string | null;
  left?: ConditionalExpression | null;
  right?: ConditionalExpression | null;
  operand?: ConditionalExpression | null;
  children?: ConditionalExpression[] | null;
};

export type ConditionalRuleAction = {
  side: "BUY" | "SELL" | string;
  sizing: {
    type: "FIXED_SHARES" | "POSITION_PERCENT" | "ALL" | string;
    value?: string | number | null;
  };
  order_type: "MARKET" | "LIMIT" | string;
  limit_price?: string | number | null;
  time_in_force?: "DAY" | string;
};

export type ConditionalRuleSpec = {
  symbol: string;
  condition: ConditionalExpression;
  action: ConditionalRuleAction;
  expires_at: string;
  execution_mode?: string;
  repeat_policy?: string;
};

export type ConditionalRuleView = {
  rule_id: string;
  fund_id: string;
  book_id: string;
  raw_instruction: string;
  state: string;
  rule_version: number;
  spec_sha256: string;
  confirmed_at: string | null;
  created_at: string;
  updated_at: string;
  spec: ConditionalRuleSpec;
  last_execution_state: string | null;
  last_guard_code: string | null;
  last_error_code: string | null;
  directive_id: string | null;
  status_message: string | null;
};

export class ConditionalRuleError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function explain(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `조건주문 조회 실패 (HTTP ${status})`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isConditionalRule(value: unknown): value is ConditionalRuleView {
  if (
    !isRecord(value) ||
    typeof value.rule_id !== "string" ||
    typeof value.fund_id !== "string" ||
    typeof value.book_id !== "string" ||
    typeof value.raw_instruction !== "string" ||
    value.raw_instruction.trim().length === 0 ||
    typeof value.state !== "string"
  ) {
    return false;
  }
  const spec = value.spec;
  const action = isRecord(spec) ? spec.action : null;
  const sizing = isRecord(action) ? action.sizing : null;
  return (
    isRecord(spec) &&
    typeof spec.symbol === "string" &&
    typeof spec.expires_at === "string" &&
    isRecord(spec.condition) &&
    typeof spec.condition.type === "string" &&
    isRecord(action) &&
    typeof action.side === "string" &&
    isRecord(sizing) &&
    typeof sizing.type === "string"
  );
}

export async function fetchConditionalRules(): Promise<ConditionalRuleView[]> {
  let response: Response;
  try {
    response = await bffFetch("/ui/conditional-rules", {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new ConditionalRuleError(`BFF(${BFF})에 연결하지 못했습니다.`, 0);
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new ConditionalRuleError(explain(body, response.status), response.status);
  if (!Array.isArray(body) || !body.every(isConditionalRule)) {
    throw new ConditionalRuleError("조건주문 응답 계약이 올바르지 않습니다.", response.status);
  }
  return body;
}

async function transitionConditionalRule(
  ruleId: string,
  method: "POST" | "DELETE",
  suffix = "",
): Promise<ConditionalRuleView> {
  let response: Response;
  try {
    response = await bffFetch(
      `/ui/conditional-rules/${encodeURIComponent(ruleId)}${suffix}`,
      {
        method,
        cache: "no-store",
        headers: { Accept: "application/json" },
      },
    );
  } catch {
    throw new ConditionalRuleError(`BFF(${BFF})에 연결하지 못했습니다.`, 0);
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ConditionalRuleError(explain(body, response.status), response.status);
  }
  if (!isConditionalRule(body)) {
    throw new ConditionalRuleError("조건주문 응답 계약이 올바르지 않습니다.", response.status);
  }
  return body;
}

export function pauseConditionalRule(ruleId: string): Promise<ConditionalRuleView> {
  return transitionConditionalRule(ruleId, "POST", "/pause");
}

export function resumeConditionalRule(ruleId: string): Promise<ConditionalRuleView> {
  return transitionConditionalRule(ruleId, "POST", "/resume");
}

/** 감사 레코드는 보존하고 실행 가능한 규칙 상태만 CANCELLED로 전환한다. */
export function cancelConditionalRule(ruleId: string): Promise<ConditionalRuleView> {
  return transitionConditionalRule(ruleId, "DELETE");
}

const FIELD_LABELS: Record<string, string> = {
  LAST_PRICE: "현재가",
  OPEN: "시가",
  HIGH: "고가",
  LOW: "저가",
  CLOSE: "종가",
  VOLUME: "거래량",
  AVG_ENTRY_PRICE: "평균 매입가",
  POSITION_QUANTITY: "보유 수량",
  SELLABLE_QUANTITY: "매도 가능 수량",
  OBSERVED_AT_EPOCH_SECONDS: "시각",
};

const INDICATOR_LABELS: Record<string, string> = {
  ENVELOPE: "엔벨로프",
  BOLLINGER: "볼린저",
  SMA: "이동평균",
  RSI: "RSI",
  MACD: "MACD",
};

const OPERATOR_LABELS: Record<string, string> = {
  GTE: "이상",
  GT: "초과",
  LTE: "이하",
  LT: "미만",
  EQ: "일치",
  NE: "불일치",
  ABOVE: "상단 돌파",
  BELOW: "하단 이탈",
};

function formatScalar(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "참" : "거짓";
  const text = String(value);
  if (!/^-?\d+(?:\.\d+)?$/.test(text)) return text;
  const [whole, fraction] = text.split(".");
  return `${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction ? `.${fraction}` : ""}`;
}

function formatLiteral(node: ConditionalExpression): string {
  const value = formatScalar(node.value);
  switch (node.unit) {
    case "PRICE":
    case "KRW":
      return `${value}원`;
    case "RATIO":
      return `${value}%`;
    case "SHARES":
    case "VOLUME":
      return `${value}주`;
    default:
      return value;
  }
}

function formatIndicator(node: ConditionalExpression): string {
  const name = INDICATOR_LABELS[node.name ?? ""] ?? node.name ?? "지표";
  const parameters = node.parameters ?? {};
  const orderedKeys = ["PERIOD", "PERCENT", "STDDEV"];
  const keys = [
    ...orderedKeys.filter((key) => key in parameters),
    ...Object.keys(parameters).filter((key) => !orderedKeys.includes(key)),
  ];
  const parameterText = keys.length > 0 ? `(${keys.map((key) => formatScalar(parameters[key])).join(", ")})` : "";
  const output =
    node.output === "UPPER" ? " 상단" : node.output === "LOWER" ? " 하단" : node.output === "MIDDLE" ? " 중간" : "";
  return `${name}${parameterText}${output}`;
}

function formatTimeLiteral(node: ConditionalExpression): string | null {
  if (node.type !== "LITERAL" || node.unit !== "NUMBER") return null;
  const epoch = Number(node.value);
  if (!Number.isFinite(epoch)) return null;
  const date = new Date(epoch * 1000);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleTimeString("ko-KR", {
    timeZone: "Asia/Seoul",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Canonical 조건 AST를 대시보드에 표시할 짧은 문구로 변환한다. */
export function formatConditionalRuleCondition(node: ConditionalExpression): string {
  switch (node.type) {
    case "LITERAL":
      return formatLiteral(node);
    case "TIME":
      return FIELD_LABELS[node.field ?? ""] ?? node.field ?? "시각";
    case "MARKET":
    case "PORTFOLIO":
      return FIELD_LABELS[node.field ?? ""] ?? node.field ?? "시장 값";
    case "INDICATOR":
      return formatIndicator(node);
    case "ARITHMETIC": {
      const operator = node.operator === "ADD" ? "+" : node.operator === "SUBTRACT" ? "−" : node.operator ?? "";
      return `${formatConditionalRuleCondition(node.left ?? { type: "UNKNOWN" })} ${operator} ${formatConditionalRuleCondition(node.right ?? { type: "UNKNOWN" })}`;
    }
    case "COMPARISON": {
      const left = node.left ?? { type: "UNKNOWN" };
      const right = node.right ?? { type: "UNKNOWN" };
      const time = left.type === "TIME" ? formatTimeLiteral(right) : null;
      if (time) return `${time} 도달`;
      return `${formatConditionalRuleCondition(left)} ${OPERATOR_LABELS[node.operator ?? ""] ?? node.operator ?? "비교"} ${formatConditionalRuleCondition(right)}`;
    }
    case "CROSS": {
      const rightNode = node.right ?? { type: "UNKNOWN" };
      const right = formatConditionalRuleCondition(rightNode);
      const operator = OPERATOR_LABELS[node.operator ?? ""] ?? "돌파";
      const repeatsOutput = rightNode.type === "INDICATOR" && ((rightNode.output === "UPPER" && node.operator === "ABOVE") || (rightNode.output === "LOWER" && node.operator === "BELOW"));
      return `${right} ${repeatsOutput ? operator.split(" ").pop() : operator}`;
    }
    case "LOGICAL":
      return (node.children ?? []).map(formatConditionalRuleCondition).join(node.operator === "OR" ? " 또는 " : " 그리고 ");
    case "NOT":
      return `아님: ${formatConditionalRuleCondition(node.operand ?? { type: "UNKNOWN" })}`;
    default:
      return "조건 확인 필요";
  }
}

export function formatConditionalRuleAction(action: ConditionalRuleAction): string {
  const sizing =
    action.sizing.type === "ALL"
      ? "전량"
      : action.sizing.type === "POSITION_PERCENT"
        ? `${formatScalar(action.sizing.value)}%`
        : `${formatScalar(action.sizing.value)}주`;
  const orderType = action.order_type === "LIMIT" ? `지정가 ${formatScalar(action.limit_price)}원` : "시장가";
  return `${sizing} ${orderType} ${action.side === "BUY" ? "매수" : action.side === "SELL" ? "매도" : action.side}`;
}

export function formatConditionalRuleExpiry(value: string): string {
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "만료 시각 확인 필요";
  return `만료 ${new Date(timestamp).toLocaleString("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}
