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

▶ 중복은 접되, 해석은 덮지 않는다
  24시간 상주에서 같은 논문을 매일 새 리드로 만들면 `independent_mentions` 가
  의미를 잃고 예산 계산이 망가진다. 같은 url+title·같은 AST 계약은
  `lead_id_for()` 로 접는다. 다만 데이터면 확장 뒤 같은 문헌에서 새 메커니즘
  변형이 나오면 원 리드를 바꾸지 않고 결정론적 revision ID 로 별도 보존한다.

자체 점검: python departments/01-research/factory/lead_intake.py
"""

from __future__ import annotations

import hashlib
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
    ScoutLens,
    SourceType,
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

MODULE_VERSION = "research-lead-intake-v6"

# 스카우트가 내는 블록의 필드. 앞의 셋이 없으면 리드가 아니다.
REQUIRED = ("TITLE", "URL", "MECHANISM", "READINESS")
OPTIONAL = ("COUNTERPARTY", "TESTABLE_WITH", "REPORTED_EFFECT", "EXCERPT",
            "MARKET_CONTEXT", "FAILURE_MODE", "OBSERVABLES",
            "CANDIDATE_SIGNAL_EXPR", "MISSING_DATA", "MAPPING_LOSS",
            "RESEARCH_LANE", "SEMANTIC_PLAN",
            "FEATURE_WINDOW_CONTRACT_VERSION",
            "DERIVATION_MODE", "SOURCE_BASELINE_EXPR",
            "DERIVATION_TRANSFORMS", "NOVELTY_RATIONALE",
            "PARENT_SIGNAL_EXPR", "EVOLUTION_OPERATORS",
            "EXPECTED_INCREMENT", "ABLATIONS", "FORMULA_THESIS",
            # These labels are part of the live Scout output vocabulary.  Some
            # are provenance-only, but they still must terminate the preceding
            # field instead of being concatenated into a JSON value.
            "PUBLISHED", "PUBLICATION_DATE", "ACCESSED", "ACCESS_TIME",
            "CLAIMED_EDGE", "TESTABILITY", "LESSONS_ADDRESSED")
_FIELD_RE = re.compile(
    r"^(" + "|".join(re.escape(k) for k in REQUIRED + OPTIONAL) +
    r")\s*:\s*(.*)$")
_ANY_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_]*\s*:\s*")
_JSON_FIELDS = frozenset({
    "OBSERVABLES", "CANDIDATE_SIGNAL_EXPR", "SEMANTIC_PLAN",
    "SOURCE_BASELINE_EXPR", "DERIVATION_TRANSFORMS", "PARENT_SIGNAL_EXPR",
    "EVOLUTION_OPERATORS", "ABLATIONS", "FORMULA_THESIS",
})

AST_READY = "AST_READY"
DATA_BLOCKED = "DATA_BLOCKED"
SEMANTIC_MISMATCH = "SEMANTIC_MISMATCH"
READINESS_VALUES = frozenset({AST_READY, DATA_BLOCKED, SEMANTIC_MISMATCH})


def _alpha_ast():
    """Load the container-neutral grammar shared with quant execution."""
    import alpha_ast_surface  # noqa: PLC0415
    return alpha_ast_surface


def _intraday_ast():
    """Load the exact event-time grammar used by the quant worker."""
    import intraday_ast_contract  # noqa: PLC0415
    return intraday_ast_contract


def _literature_derivation():
    """Load the deterministic public-baseline novelty policy."""
    import literature_derivation  # noqa: PLC0415
    return literature_derivation


def _alpha_evolution():
    """Load the deterministic parent/child novelty policy."""
    import alpha_evolution  # noqa: PLC0415
    return alpha_evolution


def _formula_discovery():
    """Load the typed financial-mathematics contract for LLM formulas."""
    import formula_discovery  # noqa: PLC0415
    return formula_discovery


def _as_text(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value or "").strip()


def _strip_json_fence(value: str) -> str:
    """Remove an optional Markdown JSON fence without accepting trailing prose."""
    text = str(value or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text, count=1)
    return text.strip()


def _json_document_complete(value: str) -> bool:
    """True only when *value* is one complete JSON document."""
    text = _strip_json_fence(value)
    if not text:
        return False
    try:
        _, end = json.JSONDecoder().raw_decode(text)
    except (TypeError, ValueError):
        return False
    return not text[end:].strip()


def _readiness_metadata(block: dict, mechanism: str) -> dict:
    """Validate whether a sourced idea can enter the current AST/data search space."""
    readiness = _as_text(block.get("READINESS")).upper()
    if readiness not in READINESS_VALUES:
        raise ValueError("READINESS must be AST_READY, DATA_BLOCKED, or SEMANTIC_MISMATCH")

    raw_observables = block.get("OBSERVABLES")
    if isinstance(raw_observables, (list, tuple, set)):
        observable_items = raw_observables
    elif _as_text(raw_observables).startswith("["):
        try:
            observable_items = json.loads(_strip_json_fence(_as_text(raw_observables)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid OBSERVABLES: {exc}") from exc
        if not isinstance(observable_items, list):
            raise ValueError("OBSERVABLES JSON must be an array")
    else:
        observable_items = _as_text(raw_observables).split(",")
    observables = sorted({_as_text(x) for x in observable_items if _as_text(x)})
    missing_data = _as_text(block.get("MISSING_DATA"))
    mapping_loss = _as_text(block.get("MAPPING_LOSS"))
    raw_expr = block.get("CANDIDATE_SIGNAL_EXPR")
    lane = _as_text(block.get("RESEARCH_LANE") or "DAILY_CROSS_SECTIONAL").upper()
    if lane not in {"DAILY_CROSS_SECTIONAL", "INTRADAY_EVENT"}:
        raise ValueError("RESEARCH_LANE must be DAILY_CROSS_SECTIONAL or INTRADAY_EVENT")
    raw_plan = block.get("SEMANTIC_PLAN")
    semantic_plan = {}
    if raw_plan not in (None, ""):
        try:
            semantic_plan = (raw_plan if isinstance(raw_plan, dict)
                             else json.loads(_strip_json_fence(_as_text(raw_plan))))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid SEMANTIC_PLAN: {exc}") from exc
    candidate = None
    feature_window_contract_version = ""

    if readiness == AST_READY:
        if not observables or not _as_text(raw_expr):
            raise ValueError("AST_READY requires OBSERVABLES and CANDIDATE_SIGNAL_EXPR")
        try:
            candidate = (raw_expr if isinstance(raw_expr, dict)
                         else json.loads(_strip_json_fence(_as_text(raw_expr))))
            ast = _intraday_ast() if lane == "INTRADAY_EVENT" else _alpha_ast()
            candidate = ast.parse(candidate)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid CANDIDATE_SIGNAL_EXPR: {exc}") from exc
        fields = sorted(ast.fields_of(candidate))
        if observables != fields:
            raise ValueError(f"OBSERVABLES {observables} do not match AST fields {fields}")
        if lane == "INTRADAY_EVENT":
            declared_window_contract = _as_text(
                block.get("FEATURE_WINDOW_CONTRACT_VERSION"))
            if not declared_window_contract:
                declared_window_contract = (
                    ast.EXPLICIT_FEATURE_WINDOW_CONTRACT
                    if any(seconds is not None for _field, seconds in
                           ast.field_window_bindings_of(candidate))
                    else ast.LEGACY_FEATURE_WINDOW_CONTRACT)
            candidate = ast.validate_feature_window_contract(
                candidate, contract_version=declared_window_contract)
            feature_window_contract_version = declared_window_contract
            if not semantic_plan:
                raise ValueError("INTRADAY_EVENT AST_READY requires SEMANTIC_PLAN")
            from alpha_semantics import check_observables, validate  # noqa: PLC0415
            plan = validate(semantic_plan)
            alignment = check_observables(
                plan, fields, operators=ast.operators_of(candidate),
                conditional_fields=ast.conditional_fields_of(candidate))
            if not alignment["ok"]:
                raise ValueError(
                    "SEMANTIC_MISMATCH: " + "; ".join(alignment["missing"]))
            semantic_plan = plan
        else:
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
        # The source identity (lead_id) and formula identity are different things.
        # Persist both so independent papers supporting the same executable formula
        # can be consolidated instead of masquerading as novel experiments.
        ast_fingerprint = ast.fingerprint(candidate)
        ast_shape_fingerprint = ast.shape_fingerprint(candidate)
        raw_baseline = block.get("SOURCE_BASELINE_EXPR")
        if raw_baseline not in (None, "") and not isinstance(raw_baseline, dict):
            try:
                raw_baseline = json.loads(_strip_json_fence(_as_text(raw_baseline)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid SOURCE_BASELINE_EXPR: {exc}") from exc
        raw_transforms = block.get("DERIVATION_TRANSFORMS") or ()
        if isinstance(raw_transforms, str) and raw_transforms.strip().startswith("["):
            try:
                raw_transforms = json.loads(_strip_json_fence(raw_transforms))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid DERIVATION_TRANSFORMS: {exc}") from exc
        derivation = _literature_derivation().assess(
            candidate=candidate,
            mode=block.get("DERIVATION_MODE"),
            source_baseline=raw_baseline,
            transforms=raw_transforms,
            novelty_rationale=block.get("NOVELTY_RATIONALE") or "",
            ast_module=ast,
        )
        raw_parent = block.get("PARENT_SIGNAL_EXPR")
        if raw_parent not in (None, "") and not isinstance(raw_parent, dict):
            try:
                raw_parent = json.loads(_strip_json_fence(_as_text(raw_parent)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid PARENT_SIGNAL_EXPR: {exc}") from exc
        raw_evolution_ops = block.get("EVOLUTION_OPERATORS") or ()
        if (isinstance(raw_evolution_ops, str)
                and raw_evolution_ops.strip().startswith("[")):
            try:
                raw_evolution_ops = json.loads(_strip_json_fence(raw_evolution_ops))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid EVOLUTION_OPERATORS: {exc}") from exc
        raw_ablations = block.get("ABLATIONS") or ()
        if isinstance(raw_ablations, str) and raw_ablations.strip().startswith("["):
            try:
                raw_ablations = json.loads(_strip_json_fence(raw_ablations))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid ABLATIONS: {exc}") from exc
        evolution = _alpha_evolution().assess_lineage(
            candidate=candidate,
            parent=raw_parent,
            operators=raw_evolution_ops,
            expected_increment=block.get("EXPECTED_INCREMENT") or "",
            ablations=raw_ablations,
            grammar=ast,
        )
        if lane == "INTRADAY_EVENT":
            raw_thesis = block.get("FORMULA_THESIS")
            if raw_thesis not in (None, "") and not isinstance(raw_thesis, dict):
                try:
                    raw_thesis = json.loads(_strip_json_fence(_as_text(raw_thesis)))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid FORMULA_THESIS: {exc}") from exc
            formula_discovery = _formula_discovery().assess(
                raw_thesis, candidate=candidate, semantic_plan=semantic_plan,
                grammar=ast)
        else:
            formula_discovery = {
                "formula_discovery_version": "",
                "formula_contract_complete": False,
                "formula_thesis": None,
                "formula_math_profile": {},
            }
    else:
        ast_fingerprint = ""
        ast_shape_fingerprint = ""
        derivation = {
            "novelty_policy_version": "",
            "derivation_mode": "",
            "derivation_transforms": [],
            "novelty_rationale": "",
            "source_baseline_expr": None,
            "source_baseline_fingerprint": "",
            "source_baseline_shape_fingerprint": "",
            "candidate_vs_source_similarity": None,
            "alpha_candidate_eligible": False,
            "novelty_classification": "NON_EXECUTABLE_LEAD",
        }
        evolution = {
            "evolution_policy_version": "",
            "evolution_role": "NON_EXECUTABLE",
            "parent_signal_expr": None,
            "parent_ast_fingerprint": "",
            "parent_ast_shape_fingerprint": "",
            "child_vs_parent_similarity": None,
            "evolution_operators": [],
            "expected_increment": "",
            "ablations": [],
        }
        formula_discovery = {
            "formula_discovery_version": "",
            "formula_contract_complete": False,
            "formula_thesis": None,
            "formula_math_profile": {},
        }
    if readiness == DATA_BLOCKED and not missing_data:
        raise ValueError("DATA_BLOCKED requires MISSING_DATA")
    elif readiness == SEMANTIC_MISMATCH and not mapping_loss:
        raise ValueError("SEMANTIC_MISMATCH requires MAPPING_LOSS")

    return {"ast_readiness": readiness, "observables": observables,
            "candidate_signal_expr": candidate, "missing_data": missing_data,
            "mapping_loss": mapping_loss,
            "lessons_addressed": _as_text(block.get("LESSONS_ADDRESSED")),
            "research_lane": lane, "semantic_plan": semantic_plan,
            "feature_window_contract_version":
                feature_window_contract_version,
            "ast_fingerprint": ast_fingerprint,
            "ast_shape_fingerprint": ast_shape_fingerprint,
            "primary_data_plane": ("MICROSTRUCTURE" if readiness == AST_READY
                                   else "UNRESOLVED"),
            "daily_data_role": "EXECUTION_BENCHMARK_REGIME_AUXILIARY",
            **derivation, **evolution, **formula_discovery}

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
    스카우트 어휘다. 모르는 대문자 `KEY:` 줄은 메타데이터 경계로 보고 버린다.
    앞의 구조화 JSON 필드에 붙여 수식을 손상시키지 않기 위해서다.
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
            if key in cur:
                # Preserve repeated fields instead of silently keeping only the
                # last one. This matters for structured list/map fields such as
                # LESSONS_ADDRESSED: an agent may emit one line per lesson.
                # Scalar/JSON repetition now fails closed downstream instead of
                # quietly changing the registered hypothesis to the last value.
                cur[key] = ", ".join(x for x in (cur[key], val) if x)
            else:
                cur[key] = val
        elif _ANY_LABEL_RE.match(raw.strip()):
            # Unknown uppercase labels are metadata boundaries, not prose
            # continuations. Ignoring one is safer than corrupting the prior
            # typed JSON field (the live failure was FORMULA_THESIS followed by
            # an unrecognised LESSONS_ADDRESSED label).
            key = ""
        elif key and raw.strip() and cur:
            # 이어지는 줄. 값을 자르면 메커니즘 문장이 잘려 뜻이 바뀐다.
            # A complete JSON document is immutable: later prose or a closing
            # Markdown fence must not be concatenated into it.
            if key not in _JSON_FIELDS or not _json_document_complete(cur[key]):
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
    lens_value = str(getattr(lens, "value", lens)).strip().upper().replace("-", "_")
    if lens_value == "CROSSDOMAIN":
        lens_value = "CROSS_DOMAIN"
    try:
        lens_value = ScoutLens(lens_value).value
    except ValueError as exc:
        allowed = [item.value for item in ScoutLens]
        raise ValueError(f"lens must be one of {allowed}") from exc
    source_value = str(getattr(source_type, "value", source_type)).strip().upper()
    try:
        source_value = SourceType(source_value).value
    except ValueError as exc:
        allowed = [item.value for item in SourceType]
        raise ValueError(
            f"source_type={source_type!r} is invalid; choose the source medium "
            f"from {allowed}, not the Scout lens") from exc
    # MECHANISM 대체분은 길이 규율이 없다 - 계약 상한으로 자른다(clip_excerpt 참고).
    excerpt = clip_excerpt(block.get("EXCERPT") or block.get("MECHANISM") or "")
    mech = (block.get("MECHANISM") or "").strip()
    reported = (block.get("REPORTED_EFFECT") or "").strip()
    testable = (block.get("TESTABLE_WITH") or "").strip()
    counterparty = (block.get("COUNTERPARTY") or "").strip()

    # 우리 데이터로 어떻게 재현하는지 못 적었으면 규칙으로 못 옮긴다.
    readiness = _readiness_metadata(block, mech)
    if (readiness.get("derivation_mode") == "CROSS_DOMAIN_TRANSFER"
            and lens_value != "CROSS_DOMAIN"):
        raise ValueError(
            "CROSS_DOMAIN_TRANSFER is only valid for the isolated CROSS_DOMAIN scout lens")
    ref = {"url": url, "title": title, "accessed_at": now.isoformat(),
           "excerpt": excerpt}
    source_published = _as_text(
        block.get("PUBLISHED") or block.get("PUBLICATION_DATE"))
    declared_access = _as_text(block.get("ACCESSED") or block.get("ACCESS_TIME"))
    if source_published:
        ref["source_published"] = source_published
    if declared_access:
        ref["declared_accessed"] = declared_access

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
        "scout_lens": lens_value,
        "source_type": source_value,
        "as_known_at": now,
        "refs": [ref],
        "ast_contract": readiness,
        "claimed_edge": _as_text(block.get("CLAIMED_EDGE")) or title,
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
  -- 최초 수집의 PIT 의미와 그 리드를 이미 인용한 proposal 계보는 불변이다.
  -- 뒤의 Scout가 같은 문헌을 다른 렌즈로 해석했다고 ast_contract/as_known_at을
  -- 덮으면 과거 실험의 입력 의미까지 소급해 바뀐다. persist가 다른 계약은
  -- revision ID로 먼저 분기하므로 여기 도달한 행은 같은 해석의 재언급이다.
  independent_mentions = research.methodology_leads.independent_mentions + 1
returning (xmax = 0) as inserted
"""

_SQL_LOCK_SOURCE = "select pg_advisory_xact_lock(hashtextextended(%s, 0))"
_SQL_EXISTING_CONTRACT = """
select ast_contract
  from research.methodology_leads
 where lead_id = %s
"""


def _canonical_contract(contract: dict | str | None) -> str:
    if isinstance(contract, str):
        try:
            contract = json.loads(contract)
        except ValueError:
            contract = {"_invalid_legacy_contract": contract}
    return json.dumps(contract or {}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def revision_lead_id(source_lead_id: str, ast_contract: dict) -> str:
    """같은 출처의 다른 해석을 불변 별도 리드로 식별한다."""
    digest = hashlib.sha256(
        _canonical_contract(ast_contract).encode("utf-8")).hexdigest()[:12]
    return f"{source_lead_id}_r{digest}"


def routed_lead_id(source_lead_id: str, existing_contract: dict | str | None,
                   candidate_contract: dict) -> str:
    """최초/동일 해석은 source ID, 다른 해석만 revision ID 로 보낸다."""
    if existing_contract is None:
        return source_lead_id
    if _canonical_contract(existing_contract) == _canonical_contract(candidate_contract):
        return source_lead_id
    return revision_lead_id(source_lead_id, candidate_contract)


def persist(conn, leads: list[dict], *, return_ids: bool = False
            ) -> tuple[int, int] | tuple[int, int, list[str]]:
    """리드를 한 트랜잭션으로 적재한다.

    기본 반환값 ``(신규, 중복접힘)`` 은 기존 CLI/호출자와 호환된다. 쓰기 API처럼
    이어서 proposal을 만들어야 하는 호출자는 ``return_ids=True`` 로 요청해
    ``(신규, 중복접힘, 실제_리드_ID들)`` 을 받는다. 같은 출처에서 AST 해석이
    갈려 revision ID로 라우팅된 경우에도 원래 ID가 아니라 실제 저장 ID를 준다.
    """
    cur = conn.cursor()
    new = dup = 0
    persisted_ids: list[str] = []
    for lead in leads:
        payload = dict(lead)
        source_lead_id = str(payload["lead_id"])
        # 없는 행까지 SELECT FOR UPDATE 로 잠글 수 없으므로 출처 해시 advisory
        # lock을 잡는다. 두 Scout가 동시에 처음 본 같은 문헌을 서로 다른 AST로
        # 해석해도 한쪽 계약이 단순 upsert에 먹혀 사라지지 않는다.
        cur.execute(_SQL_LOCK_SOURCE, (source_lead_id,))
        cur.fetchone()
        cur.execute(_SQL_EXISTING_CONTRACT, (source_lead_id,))
        existing = cur.fetchone()
        payload["lead_id"] = routed_lead_id(
            source_lead_id, existing[0] if existing else None,
            lead["ast_contract"])
        if payload["lead_id"] not in persisted_ids:
            persisted_ids.append(payload["lead_id"])
        payload["refs"] = json.dumps(lead["refs"], ensure_ascii=False)
        payload["ast_contract"] = json.dumps(lead["ast_contract"], ensure_ascii=False)
        cur.execute(_SQL_UPSERT, payload)
        row = cur.fetchone()
        if row and row[0]:
            new += 1
        else:
            dup += 1
    conn.commit()
    if return_ids:
        return new, dup, persisted_ids
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
DERIVATION_MODE: MECHANISM_MUTATION
SOURCE_BASELINE_EXPR: {"op":"ts_mean","field":"order_flow_imbalance","n":5}
DERIVATION_TRANSFORMS: FAILURE_MODE_INVERSION
NOVELTY_RATIONALE: Test the reversal implied by liquidity absorption, not the published pressure direction.

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
    upsert_sql = " ".join(_SQL_UPSERT.lower().split())
    check("중복은 PIT·AST 계보 불변",
          "ast_contract = excluded" not in upsert_sql
          and "as_known_at = excluded" not in upsert_sql)
    base = "lead_0123456789abcdef"
    old = {"ast_readiness": "DATA_BLOCKED", "missing_data": "queue events"}
    new = {"ast_readiness": "AST_READY", "candidate_signal_expr": {"field": "ofi"}}
    check("같은 해석은 원 리드로 접힘",
          routed_lead_id(base, old, dict(reversed(list(old.items())))) == base)
    revised = routed_lead_id(base, old, new)
    check("새 해석은 불변 revision",
          revised.startswith(base + "_r")
          and revised == revision_lead_id(base, new))

    # JSON 입력도 받는다
    j = json.dumps([{"TITLE": "J", "URL": "http://x/2",
                     "MECHANISM": "order flow imbalance predicts reversal",
                     "TESTABLE_WITH": "lagged order_flow_imbalance",
                     "READINESS": "AST_READY", "OBSERVABLES": ["order_flow_imbalance"],
                     "DERIVATION_MODE": "MECHANISM_MUTATION",
                     "SOURCE_BASELINE_EXPR": {
                         "op": "ts_mean", "field": "order_flow_imbalance", "n": 5},
                     "DERIVATION_TRANSFORMS": ["FAILURE_MODE_INVERSION"],
                     "NOVELTY_RATIONALE": "Test reversal rather than pressure continuation.",
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
    total = 23
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
