"""스카우트 산출을 방법론 리드로 적재한다 - 에이전트는 제안, 코드가 판정.

담당: 재일 (리서치본부 RES)
계약: departments/01-research/contracts/factory_contracts.py (MethodologyLeadV1)
스킬: skills/methodology-scout/SKILL.md

▶ 이게 없어서 공장 입구가 비어 있었다
  스카우트는 스킬도 있고 web 도구도 있어서 **실제로 논문을 찾아온다**(2026-08-10
  실측: NBER w17653, SSRN 157835). 그런데 그 산출을 `research.methodology_leads`
  로 옮기는 경로가 코드에 없어서, DB 에 있던 2건은 전부 손으로 넣은 데모였다
  (`model_version=''` 이 그 증거였다).

▶ **에이전트의 말을 그대로 믿지 않는다**
  스카우트가 URL 을 지어내는 것은 흔한 실패다. 여기서 실제로 접속해 본다.
  다만 403·429 를 "가짜" 로 읽지 않는다 - SSRN 처럼 봇을 막는 사이트가 많고,
  접속 거부는 부재의 증거가 아니다. 죽은 링크(404·DNS 실패)만 버린다.

▶ **출처를 반드시 남긴다**
  `model_version`/`prompt_version` 이 비면 그 리드가 사람이 넣은 것인지 에이전트가
  발굴한 것인지 나중에 구분할 수 없다. 비어 있으면 적재를 거부한다 - 계보를 잃은
  리드는 재현할 수 없고, 재현할 수 없는 입력으로 만든 전략은 검증된 것이 아니다.

▶ 중복은 접는다
  24시간 상주에서 같은 논문을 매일 새 리드로 만들면 `independent_mentions` 가
  의미를 잃고 예산 계산이 망가진다. `lead_id_for()` 가 url+title 로 접는다.

자체 점검: python departments/01-research/factory/lead_intake.py
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_RESEARCH / "contracts"))

from factory_contracts import (  # noqa: E402
    MAX_EXCERPT_CHARS,
    lead_id_for,
)

# 잘렸다는 사실을 발췌 안에 남긴다. 조용히 자르면 읽는 쪽이 원문 전체로 안다.
_TRUNC_MARK = "…(발췌 상한 초과로 잘림)"


def clip_excerpt(text: str, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    """발췌를 계약 상한 안으로 줄인다. **상한은 계약에서 읽는다.**

    ▶ 왜 필요한가 (2026-08-12 실측)
      `EXCERPT` 가 없으면 `MECHANISM` 으로 대체하는데(아래 to_lead), 기제 서술에는
      길이 규율이 없어 500자를 넘긴다. 그러면 `MethodologyLeadV1` 검증이 터지고
      **수확 전체가 죽는다** - 한 리드가 길어서 그 배치의 나머지가 다 버려졌다.
      게다가 성공해야만 처리 표식이 남으므로 같은 카드를 매 주기 다시 수확하다
      똑같이 실패했다(20260812T00 이 계속 반복됐다).

      상한을 여기 다시 적지 않는다 - 계약과 갈리면 오늘 퀀트에서 겪은 것과 같은
      "표가 둘" 사고가 된다.
    """
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: max(0, limit - len(_TRUNC_MARK))].rstrip() + _TRUNC_MARK

MODULE_VERSION = "research-lead-intake-v2"

# 스카우트가 내는 블록의 필드. 앞의 셋이 없으면 리드가 아니다.
REQUIRED = ("TITLE", "URL", "MECHANISM", "READINESS")
OPTIONAL = ("COUNTERPARTY", "TESTABLE_WITH", "REPORTED_EFFECT", "EXCERPT",
            "MARKET_CONTEXT", "FAILURE_MODE", "OBSERVABLES",
            "CANDIDATE_SIGNAL_EXPR", "MISSING_DATA", "MAPPING_LOSS")
_FIELD_RE = re.compile(
    r"^(TITLE|URL|MECHANISM|READINESS|COUNTERPARTY|TESTABLE_WITH|REPORTED_EFFECT|"
    r"EXCERPT|MARKET_CONTEXT|FAILURE_MODE|OBSERVABLES|CANDIDATE_SIGNAL_EXPR|"
    r"MISSING_DATA|MAPPING_LOSS)\s*:\s*(.*)$")

AST_READY = "AST_READY"
DATA_BLOCKED = "DATA_BLOCKED"
SEMANTIC_MISMATCH = "SEMANTIC_MISMATCH"
READINESS_VALUES = frozenset({AST_READY, DATA_BLOCKED, SEMANTIC_MISMATCH})


def _alpha_ast():
    """Load the container-neutral grammar shared with quant execution."""
    import alpha_ast_surface  # noqa: PLC0415
    return alpha_ast_surface


def _as_text(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value or "").strip()


def _readiness_metadata(block: dict, mechanism: str) -> dict:
    """Validate whether a sourced idea can enter the current AST/data search space."""
    readiness = _as_text(block.get("READINESS")).upper()
    if readiness not in READINESS_VALUES:
        raise ValueError("READINESS must be AST_READY, DATA_BLOCKED, or SEMANTIC_MISMATCH")

    raw_observables = block.get("OBSERVABLES")
    observable_items = (raw_observables if isinstance(raw_observables, (list, tuple, set))
                        else _as_text(raw_observables).split(","))
    observables = sorted({_as_text(x) for x in observable_items if _as_text(x)})
    missing_data = _as_text(block.get("MISSING_DATA"))
    mapping_loss = _as_text(block.get("MAPPING_LOSS"))
    raw_expr = block.get("CANDIDATE_SIGNAL_EXPR")
    candidate = None

    if readiness == AST_READY:
        if not observables or not _as_text(raw_expr):
            raise ValueError("AST_READY requires OBSERVABLES and CANDIDATE_SIGNAL_EXPR")
        try:
            candidate = raw_expr if isinstance(raw_expr, dict) else json.loads(_as_text(raw_expr))
            ast = _alpha_ast()
            candidate = ast.parse(candidate)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid CANDIDATE_SIGNAL_EXPR: {exc}") from exc
        fields = sorted(ast.fields_of(candidate))
        if observables != fields:
            raise ValueError(f"OBSERVABLES {observables} do not match AST fields {fields}")
        micro_fields = sorted(set(fields) & set(ast.MICRO_FIELDS))
        if not micro_fields:
            raise ValueError(
                "MICROSTRUCTURE_PRIMARY_REQUIRED: AST_READY must use at least one "
                "quote/trade microstructure field; daily close/notional/returns are "
                "auxiliary execution, benchmark, and regime inputs only")
        alignment = ast.check_alignment(
            candidate, " ".join((mechanism, _as_text(block.get("TESTABLE_WITH")))))
        if not alignment["ok"]:
            raise ValueError(f"SEMANTIC_MISMATCH: {alignment['note']}")
    elif readiness == DATA_BLOCKED and not missing_data:
        raise ValueError("DATA_BLOCKED requires MISSING_DATA")
    elif readiness == SEMANTIC_MISMATCH and not mapping_loss:
        raise ValueError("SEMANTIC_MISMATCH requires MAPPING_LOSS")

    return {"ast_readiness": readiness, "observables": observables,
            "candidate_signal_expr": candidate, "missing_data": missing_data,
            "mapping_loss": mapping_loss,
            "primary_data_plane": ("MICROSTRUCTURE" if readiness == AST_READY
                                   else "UNRESOLVED"),
            "daily_data_role": "EXECUTION_BENCHMARK_REGIME_AUXILIARY"}

# 링크 판정. 접속 거부는 부재의 증거가 아니다.
LINK_OK = "OK"
LINK_UNVERIFIED = "UNVERIFIED"     # 403·429 등 봇 차단 - 리드는 살린다
LINK_BROKEN = "BROKEN"             # 404·DNS 실패 - 버린다
_BOT_BLOCKED = frozenset({401, 403, 405, 406, 429, 503})


@dataclass
class Rejected:
    title: str
    reason: str


@dataclass
class Intake:
    leads: list = field(default_factory=list)        # 적재 가능한 리드 dict
    rejected: list = field(default_factory=list)     # Rejected
    link_notes: dict = field(default_factory=dict)   # url -> 판정

    @property
    def ok(self) -> bool:
        return bool(self.leads)


# ── 파싱 ───────────────────────────────────────────────────────────────────
def parse_blocks(text: str, keys: tuple[str, ...] | None = None) -> list[dict]:
    """에이전트 산출을 블록 리스트로 자른다.

    JSON 배열이면 그대로 읽는다. 아니면 `KEY: value` 줄 형식으로 읽되, 다음
    TITLE 이 나오면 새 블록이다. 여러 줄에 걸친 값은 이어 붙인다.

    `keys` 로 어휘를 바꿔 기획안·회의론자 산출에도 쓴다. 어휘를 넘기지 않으면
    스카우트 어휘다. **모르는 키는 값의 일부로 흘려보낸다** - 새 키로 잘못 잡으면
    앞 필드가 그 줄에서 끊겨 뜻이 바뀐다.
    """
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            return [dict(d) for d in json.loads(stripped)]
        except (ValueError, TypeError):
            pass          # JSON 인 척하다 실패하면 줄 파싱으로 내려간다

    field_re = _FIELD_RE if keys is None else re.compile(
        r"^(" + "|".join(re.escape(k) for k in keys) + r")\s*:\s*(.*)$")
    blocks: list[dict] = []
    cur: dict = {}
    key = ""
    for raw in text.splitlines():
        m = field_re.match(raw.strip())
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key == "TITLE" and cur:
                blocks.append(cur)
                cur = {}
            cur[key] = val
        elif key and raw.strip() and cur:
            # 이어지는 줄. 값을 자르면 메커니즘 문장이 잘려 뜻이 바뀐다.
            cur[key] = (cur[key] + " " + raw.strip()).strip()
    if cur:
        blocks.append(cur)
    return blocks


# ── 링크 확인 ──────────────────────────────────────────────────────────────
def check_link(url: str, *, opener=None, timeout: int = 30) -> str:
    """실제로 접속해 본다. 판정 셋만 돌려준다."""
    if opener is None:                      # pragma: no cover - 주입해서 시험한다
        import urllib.error
        import urllib.request

        def opener(u):
            req = urllib.request.Request(
                u, headers={"User-Agent": "Mozilla/5.0 (research-scout-intake)"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return int(r.status)
            except urllib.error.HTTPError as e:
                return int(e.code)
            except Exception:
                return 0                    # DNS·연결 실패
    code = opener(url)
    if 200 <= code < 400:
        return LINK_OK
    if code in _BOT_BLOCKED:
        return LINK_UNVERIFIED
    return LINK_BROKEN


# ── 리드로 변환 ────────────────────────────────────────────────────────────
def to_lead(block: dict, *, lens: str, source_type: str, case_id: str,
            model_version: str, prompt_version: str,
            as_known_at: datetime | None = None) -> dict:
    """블록 하나를 적재 가능한 리드 dict 로. 검증은 호출부가 이미 했다고 보지 않는다."""
    if not model_version.strip() or not prompt_version.strip():
        # 계보 없는 리드는 재현할 수 없다. 여기서 막지 않으면 손으로 넣은 것과
        # 에이전트가 찾은 것이 DB 에서 구분되지 않는다.
        raise ValueError("model_version·prompt_version 이 비었다 - 계보 없는 리드는 안 받는다")

    now = as_known_at or datetime.now(timezone.utc)
    url, title = block["URL"].strip(), block["TITLE"].strip()
    # MECHANISM 대체분은 길이 규율이 없다 - 계약 상한으로 자른다(clip_excerpt 참고).
    excerpt = clip_excerpt(block.get("EXCERPT") or block.get("MECHANISM") or "")
    mech = (block.get("MECHANISM") or "").strip()
    reported = (block.get("REPORTED_EFFECT") or "").strip()
    testable = (block.get("TESTABLE_WITH") or "").strip()
    counterparty = (block.get("COUNTERPARTY") or "").strip()

    # 우리 데이터로 어떻게 재현하는지 못 적었으면 규칙으로 못 옮긴다.
    readiness = _readiness_metadata(block, mech)
    ref = {"url": url, "title": title, "accessed_at": now.isoformat(),
           "excerpt": excerpt}

    # v1 columns remain compatible; the source-specific verdict lives in refs JSON.
    readiness_value = readiness["ast_readiness"]
    testability = {AST_READY: "RULE_EXPRESSIBLE", DATA_BLOCKED: "VAGUE",
                   SEMANTIC_MISMATCH: "UNUSABLE"}[readiness_value]
    status = {AST_READY: "COMPLETE", DATA_BLOCKED: "BLOCKED",
              SEMANTIC_MISMATCH: "UNUSABLE"}[readiness_value]

    context = (block.get("MARKET_CONTEXT") or "").strip()
    if not context and reported and reported.upper() != "NONE":
        context = reported          # 보고 수치에 표본 시장·기간이 들어 있다

    return {
        "lead_id": lead_id_for([ref]),
        "case_id": case_id,
        "scout_lens": lens,
        "source_type": source_type,
        "as_known_at": now,
        "refs": [ref],
        "ast_contract": readiness,
        "claimed_edge": title,
        "stated_mechanism": mech,
        # 반대편을 소스가 밝히지 않았으면 스카우트의 추론이다 - 표시해 둔다.
        "inferred": not counterparty,
        "market_context": context,
        "stated_failure_mode": (block.get("FAILURE_MODE") or "").strip(),
        "independent_mentions": 1,
        "testability": testability,
        "status": status,
        "model_version": model_version.strip(),
        "prompt_version": prompt_version.strip(),
    }


def intake(text: str, *, lens: str, source_type: str, case_id: str,
           model_version: str, prompt_version: str,
           opener=None, as_known_at: datetime | None = None) -> Intake:
    """스카우트 산출 전체를 받아 적재 가능한 것만 통과시킨다."""
    out = Intake()
    for block in parse_blocks(text):
        title = (block.get("TITLE") or "(제목 없음)").strip()
        missing = [k for k in REQUIRED if not (block.get(k) or "").strip()]
        if missing:
            # 메커니즘 없는 리드는 성과 서술일 뿐이다 - 발행 게이트가 어차피 막는다.
            out.rejected.append(Rejected(title, f"필수 항목 없음: {','.join(missing)}"))
            continue
        url = block["URL"].strip()
        verdict = check_link(url, opener=opener)
        out.link_notes[url] = verdict
        if verdict == LINK_BROKEN:
            out.rejected.append(Rejected(title, f"끊어진 링크({url})"))
            continue
        try:
            out.leads.append(to_lead(
                block, lens=lens, source_type=source_type, case_id=case_id,
                model_version=model_version, prompt_version=prompt_version,
                as_known_at=as_known_at))
        except (KeyError, ValueError) as e:
            out.rejected.append(Rejected(title, str(e)))
    return out


# ── 적재 ───────────────────────────────────────────────────────────────────
_SQL_UPSERT = """
insert into research.methodology_leads
  (lead_id, case_id, scout_lens, source_type, as_known_at, refs, ast_contract, claimed_edge,
   stated_mechanism, inferred, market_context, stated_failure_mode,
   independent_mentions, testability, status, model_version, prompt_version)
values (%(lead_id)s, %(case_id)s, %(scout_lens)s, %(source_type)s,
        %(as_known_at)s, %(refs)s, %(ast_contract)s, %(claimed_edge)s, %(stated_mechanism)s,
        %(inferred)s, %(market_context)s, %(stated_failure_mode)s,
        %(independent_mentions)s, %(testability)s, %(status)s,
        %(model_version)s, %(prompt_version)s)
on conflict (lead_id) do update set
  -- 같은 소스를 다시 주웠다. 새 리드가 아니라 **언급이 하나 는 것**이다.
  independent_mentions = research.methodology_leads.independent_mentions + 1,
  ast_contract = excluded.ast_contract,
  as_known_at = excluded.as_known_at
returning (xmax = 0) as inserted
"""


def persist(conn, leads: list[dict]) -> tuple[int, int]:
    """(신규, 중복접힘). 커밋은 한 번에 - 반쯤 적재된 스카우트 회차를 남기지 않는다."""
    cur = conn.cursor()
    new = dup = 0
    for lead in leads:
        payload = dict(lead)
        payload["refs"] = json.dumps(lead["refs"], ensure_ascii=False)
        payload["ast_contract"] = json.dumps(lead["ast_contract"], ensure_ascii=False)
        cur.execute(_SQL_UPSERT, payload)
        row = cur.fetchone()
        if row and row[0]:
            new += 1
        else:
            dup += 1
    conn.commit()
    return new, dup


# ── 자체 점검 ──────────────────────────────────────────────────────────────
_SAMPLE = """TITLE: Short-Term Reversal as Returns to Liquidity Provision
URL: https://www.nber.org/system/files/working_papers/w17653/w17653.pdf
MECHANISM: Order flow imbalance reveals urgent liquidity demand that liquidity providers absorb.
COUNTERPARTY: Urgent liquidity demanders.
TESTABLE_WITH: Rank the negative five-day mean of order_flow_imbalance.
READINESS: AST_READY
OBSERVABLES: order_flow_imbalance
CANDIDATE_SIGNAL_EXPR: {"op":"neg","arg":{"op":"ts_mean","field":"order_flow_imbalance","n":5}}

TITLE: Missing mechanism
URL: https://example.com/backtest
READINESS: SEMANTIC_MISMATCH
MAPPING_LOSS: no mechanism

TITLE: Broken link
URL: https://example.com/gone
MECHANISM: Some mechanism
TESTABLE_WITH: measurable
READINESS: DATA_BLOCKED
MISSING_DATA: unavailable series
"""


def _selfcheck() -> int:
    fails = []

    def check(label, cond):
        if not cond:
            fails.append(label)

    def fake_opener(url):
        return 404 if url.endswith("/gone") else (403 if "example.com" in url else 200)

    r = intake(_SAMPLE, lens="ACADEMIC", source_type="PAPER", case_id="c-1",
               model_version="gpt-5.6-luna", prompt_version="scout-v1",
               opener=fake_opener)

    check("정상 리드 1건", len(r.leads) == 1)
    check("메커니즘 없으면 반려",
          any("MECHANISM" in x.reason for x in r.rejected))
    check("끊어진 링크 반려", any("끊어진" in x.reason for x in r.rejected))
    # 필수 항목이 없는 블록은 링크를 확인하기 전에 반려한다 - 이미 버릴 것에
    # 네트워크를 쓰지 않는다. 그래서 그 URL 은 link_notes 에 없는 게 맞다.
    check("반려 블록은 링크 조회 안 함",
          "https://example.com/backtest" not in r.link_notes)
    # 403 은 봇 차단이지 부재가 아니다 - SSRN 이 실제로 이렇게 응답한다.
    check("403 은 살린다", check_link("https://ssrn.test/x",
                                      opener=lambda u: 403) == LINK_UNVERIFIED)
    check("404 는 버린다", check_link("https://x.test/y",
                                      opener=lambda u: 404) == LINK_BROKEN)
    check("DNS 실패는 버린다", check_link("https://nope.test",
                                          opener=lambda u: 0) == LINK_BROKEN)

    lead = r.leads[0]
    check("lead_id 결정론", lead["lead_id"] == lead_id_for(lead["refs"]))
    check("계보 기록", lead["model_version"] == "gpt-5.6-luna")
    check("반대편 있으면 inferred 아님", lead["inferred"] is False)
    check("재현법 있으면 RULE_EXPRESSIBLE",
          lead["testability"] == "RULE_EXPRESSIBLE")
    check("COMPLETE", lead["status"] == "COMPLETE")
    check("mechanism retained", "liquidity providers" in lead["stated_mechanism"])

    # 계보 없이는 못 만든다
    try:
        to_lead({"TITLE": "t", "URL": "u", "MECHANISM": "m"}, lens="ACADEMIC",
                source_type="PAPER", case_id="c", model_version="",
                prompt_version="p")
        check("계보 없는 리드 차단", False)
    except ValueError:
        check("계보 없는 리드 차단", True)

    # 같은 소스는 같은 ID (중복 접기)
    a = to_lead({"TITLE": "T", "URL": "http://x/1", "MECHANISM": "m",
                 "READINESS": "DATA_BLOCKED", "MISSING_DATA": "x"},
                lens="ACADEMIC", source_type="PAPER", case_id="c1",
                model_version="m1", prompt_version="p1")
    b = to_lead({"TITLE": "T", "URL": "http://x/1", "MECHANISM": "m2",
                 "READINESS": "DATA_BLOCKED", "MISSING_DATA": "x"},
                lens="COMMUNITY", source_type="BLOG", case_id="c2",
                model_version="m1", prompt_version="p1")
    check("같은 소스 = 같은 ID", a["lead_id"] == b["lead_id"])

    # JSON 입력도 받는다
    j = json.dumps([{"TITLE": "J", "URL": "http://x/2",
                     "MECHANISM": "order flow imbalance predicts reversal",
                     "TESTABLE_WITH": "lagged order_flow_imbalance",
                     "READINESS": "AST_READY", "OBSERVABLES": ["order_flow_imbalance"],
                     "CANDIDATE_SIGNAL_EXPR": {"op": "neg", "arg": {
                         "op": "ts_mean", "field": "order_flow_imbalance", "n": 5}}}],
                   ensure_ascii=False)
    rj = intake(j, lens="ACADEMIC", source_type="PAPER", case_id="c",
                model_version="m", prompt_version="p",
                opener=lambda u: 200)
    check("JSON 입력", len(rj.leads) == 1)

    blocked = to_lead(
        {"TITLE": "Needs borrow data", "URL": "http://x/3",
         "MECHANISM": "borrow pressure predicts returns", "READINESS": "DATA_BLOCKED",
         "MISSING_DATA": "point-in-time borrow fee"},
        lens="ACADEMIC", source_type="PAPER", case_id="c",
        model_version="m", prompt_version="p")
    check("data-blocked is preserved", blocked["status"] == "BLOCKED")
    check("readiness metadata is auditable",
          blocked["ast_contract"]["ast_readiness"] == "DATA_BLOCKED")

    try:
        to_lead({"TITLE": "Bad fields", "URL": "http://x/4",
                 "MECHANISM": "returns reversal", "READINESS": "AST_READY",
                 "OBSERVABLES": "close", "CANDIDATE_SIGNAL_EXPR": {
                     "op": "ts_mean", "field": "returns", "n": 5}},
                lens="ACADEMIC", source_type="PAPER", case_id="c",
                model_version="m", prompt_version="p")
        check("observable/AST mismatch rejected", False)
    except ValueError:
        check("observable/AST mismatch rejected", True)

    try:
        to_lead({"TITLE": "Proxy substitution", "URL": "http://x/5",
                 "MECHANISM": "news sentiment predicts returns", "READINESS": "AST_READY",
                 "OBSERVABLES": "spread_bps", "CANDIDATE_SIGNAL_EXPR": {
                     "op": "ts_mean", "field": "spread_bps", "n": 5}},
                lens="ACADEMIC", source_type="PAPER", case_id="c",
                model_version="m", prompt_version="p")
        check("semantic mismatch rejected", False)
    except ValueError:
        check("semantic mismatch rejected", True)

    for f in fails:
        print(f"  FAIL {f}")
    total = 20
    print(f"lead_intake 자체 점검: {total - len(fails)}/{total} 통과")
    return 1 if fails else 0


def _cli(argv: list[str]) -> int:
    """스카우트 산출 파일을 받아 검증하고 적재한다.

    python lead_intake.py --persist out.txt --lens ACADEMIC \
        --model gpt-5.6-luna --prompt scout-v1 [--dry-run]

    --dry-run 이 기본이 아니다. 적재를 기본으로 두면 시험 삼아 돌린 것이
    원장에 남는다 - 반대로 두면 진짜 적재를 잊는다. 명시적으로 고르게 한다.
    """
    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    path = opt("--persist")
    if not path:
        return _selfcheck()

    lens = opt("--lens", "ACADEMIC")
    stype = opt("--source-type", "PAPER")
    model = opt("--model", "")
    prompt = opt("--prompt", "")
    # 회차 id 는 렌즈+날짜. 같은 날 같은 렌즈를 두 번 돌리면 같은 회차다.
    case_id = opt("--case", f"scout-{lens.lower()}-"
                            f"{datetime.now(timezone.utc):%Y%m%d}")

    text = Path(path).read_text(encoding="utf-8")
    r = intake(text, lens=lens, source_type=stype, case_id=case_id,
               model_version=model, prompt_version=prompt)

    print(f"{MODULE_VERSION}: 통과 {len(r.leads)} / 반려 {len(r.rejected)}")
    for lead in r.leads:
        note = r.link_notes.get(lead["refs"][0]["url"], "?")
        print(f"  + {lead['lead_id']}  [{note}] {lead['claimed_edge'][:52]}")
        print(f"      {lead['status']} / {lead['testability']}"
              f" / inferred={lead['inferred']}")
    for x in r.rejected:
        print(f"  - {x.title[:44]}: {x.reason}")

    if "--dry-run" in argv or not r.leads:
        print("  (dry-run - 적재하지 않았다)")
        return 0

    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collectors"))
    from source_registry import load_project_env

    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        new, dup = persist(conn, r.leads)
        print(f"  적재: 신규 {new} / 중복접힘 {dup}")
    finally:
        conn.close()
    return 0


def _check_excerpt_fits_contract():
    """**한 리드가 길어서 배치 전체가 죽지 않는다.** 상한은 계약에서 읽는다."""
    long_mech = "가" * (MAX_EXCERPT_CHARS + 400)
    got = clip_excerpt(long_mech)
    assert len(got) <= MAX_EXCERPT_CHARS, len(got)
    assert got.endswith(_TRUNC_MARK), got[-30:]
    # 짧은 것은 손대지 않는다 - 멀쩡한 발췌에 표식이 붙으면 원문을 의심하게 된다
    assert clip_excerpt("짧은 발췌") == "짧은 발췌"
    assert clip_excerpt("") == ""


def _check_truncated_lead_passes_validation():
    """계약 검증을 실제로 통과하는지 본다 - 길이만 맞추고 끝내지 않는다."""
    from factory_contracts import MethodologyLeadV1  # noqa: PLC0415

    block = {"URL": "https://example.org/p", "TITLE": "t",
             "MECHANISM": "나" * (MAX_EXCERPT_CHARS + 900),
             "READINESS": "DATA_BLOCKED",
             "MISSING_DATA": "point-in-time quote/trade feature"}
    lead = to_lead(block, lens="ACADEMIC", source_type="PAPER", case_id="c1",
                   model_version="m1", prompt_version="p1")
    MethodologyLeadV1.model_validate(lead)      # 여기서 터지면 수확이 또 죽는다


if __name__ == "__main__":
    if "--check" in sys.argv:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print("lead-intake 자체 점검 (DB 없음)")
        _check_excerpt_fits_contract();          print("  발췌 상한·표식        OK")
        _check_truncated_lead_passes_validation(); print("  잘린 리드가 계약 통과  OK")
        print("리드 접수 2개 영역 통과.")
        raise SystemExit(0)
    raise SystemExit(_cli(sys.argv[1:]))
