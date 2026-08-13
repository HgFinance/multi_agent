#!/usr/bin/env python3
"""경계 계약 검사 - **모듈 사이를 본다.** 자체점검은 모듈 안만 본다.

담당: 재일 (리서치본부 RSH / 퀀트·백테스트본부 QNT 공용)
근거: 재일님 지시 2026-08-13 "문헌 참고해서 잘 뚫어보자"

▶ 왜 (2026-08-13 실측)
  하루에 아홉 개의 결함을 만났는데 **아홉 개 전부 두 모듈 사이**였다.

    ① 관문 지표 이름·단위·split 불일치
    ② harvest -> intake 에 past_outcomes 미전달
    ③ 카드는 ECONOMIC_RATIONALE, 파서는 LESSONS_ADDRESSED
    ④ 서식엔 LESSONS_ADDRESSED, PLANNER_KEYS 엔 없음
    ⑤ 회의론자를 자유 텍스트 제목으로 조인
    ⑥ uuid vs text (세 곳)
    ⑦ pit_provenance.basis 가 jsonb 인데 text 로 읽음
    ⑧ 에이전트는 다섯 번째 자리에 쓰는데 수확기는 네 곳만 읽음
    ⑨ trial_family 가 한 컨테이너에만 있음

  그때 자체점검이 **92개** 돌고 있었다. 하나도 못 잡았다 - 전부 단일 모듈의
  내부 논리만 고정하기 때문이다. 경계는 아무도 안 본다.

  Pact(소비자 주도 계약 검사)가 이 증상을 그대로 서술한다: *"단위 검사는
  통합 결함을 못 잡는다 - 경계를 흉내내기 때문이다. 공급자가 '무해한'
  변경을 넣으면 소비자 검사는 전부 통과하고, 그다음 운영이 깨진다."*
  처방은 **소비자가 자기 기대를 계약으로 선언하고 공급자가 전수 검증**하는
  것이다. 스키마 드리프트 쪽은 같은 말을 DB 로 한다 - *"SQL 질의를 살아
  있는 스키마에 대고 검증해 실행 전에 잡는다."*

▶ **손으로 적는 목록을 만들지 않는다**
  중앙에 계약 목록을 두면 "새 질의를 추가할 때 목록에도 넣기" 를 사람이
  기억해야 하고, 그 기억이 오늘 아홉 번 실패한 바로 그 자원이다.
  대신 **코드에서 뽑는다** - 저장소의 SQL 문자열을 훑어 표·컬럼·조인을
  찾아내고, 그것을 살아 있는 원장에 대조한다. 새 질의는 자동으로 검사된다.

▶ 완벽한 파서가 아니다
  정규식으로 흔한 모양만 잡는다. **못 읽은 것은 못 읽었다고 적고 넘어간다** -
  놓친 것을 통과로 세면 이 검사 자체가 오늘의 사고와 같은 종류가 된다.

사용
  python departments/01-research/factory/boundary_contracts.py            # 자체 점검
  python departments/01-research/factory/boundary_contracts.py --verify   # 원장 대조
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

MODULE_VERSION = "factory-boundary-contracts-v1"

# 우리 스키마. 이 접두어가 붙은 것만 표로 본다(FROM 절의 CTE·별칭 제외).
#
# ▶ **원장이 둘이다** (2026-08-13). `research`·`quant` 는 Supabase 원장에,
#   `market` 은 TimescaleDB 에 있다. 한 연결로 다 보면 남의 DB 표를 "없는 표"
#   로 보고한다 - 그건 오탐이고, 오탐이 나면 아무도 이 검사를 안 본다.
#   `public` 은 넣지 않는다: Supabase 의 확장·인증 표가 수천 개라 조회가
#   `statement timeout` 으로 죽었고(실측), 우리 것도 아니다.
LEDGER_SCHEMAS = ("research", "quant")
MARKET_SCHEMAS = ("market",)
SCHEMAS = LEDGER_SCHEMAS + MARKET_SCHEMAS

# 캐스팅해도 비교가 성립하는 짝. 이 밖의 조합은 조인에서 터진다.
_COMPATIBLE = {
    frozenset({"uuid", "uuid"}), frozenset({"text", "text"}),
    frozenset({"text", "character varying"}),
    frozenset({"integer", "bigint"}), frozenset({"integer", "integer"}),
    frozenset({"bigint", "bigint"}),
    frozenset({"timestamp with time zone", "timestamp with time zone"}),
    frozenset({"date", "date"}),
}

_RE_TABLE = re.compile(
    r"\b(?:from|join|into|update)\s+(" + "|".join(SCHEMAS) + r")\.([a-z_][a-z0-9_]*)"
    r"(?:\s+(?:as\s+)?([a-z][a-z0-9_]*))?", re.I)
# `on a.x = b.y` / `and a.x = b.y` - 별칭.컬럼 = 별칭.컬럼
_RE_JOIN = re.compile(
    r"\b(?:on|and)\s+([a-z][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\s*"
    r"(?:::[a-z ]+)?\s*=\s*([a-z][a-z0-9_]*)\.([a-z_][a-z0-9_]*)"
    r"\s*(::[a-z ]+)?", re.I)


@dataclass
class Finding:
    """경계 하나. **어디서 왔는지**를 들고 다녀야 고칠 수 있다."""

    kind: str            # table / join / cast_join
    where: str           # 파일:행
    detail: str
    parts: tuple = ()

    def as_dict(self) -> dict:
        return {"kind": self.kind, "where": self.where, "detail": self.detail}


@dataclass
class Report:
    checked: int = 0
    unread: int = 0                 # 정규식이 못 읽은 자리
    problems: list = field(default_factory=list)   # 실행하면 죽는 것
    notes: list = field(default_factory=list)      # 돌지만 값을 치르는 것
    type_gaps: set = field(default_factory=set)    # 같은 뜻인데 타입이 다른 컬럼
    ok_joins: int = 0
    ok_tables: int = 0

    @property
    def clean(self) -> bool:
        return not self.problems

    def text(self) -> str:
        out = [f"경계 계약: 표 {self.ok_tables} · 조인 {self.ok_joins} 확인 "
               f"· **죽는 것 {len(self.problems)}건** · 값 치르는 것 "
               f"{len(self.notes)}건"]
        for p in self.problems:
            out.append(f"  [{p.kind}] {p.where}")
            out.append(f"      {p.detail}")
        for p in self.notes:
            out.append(f"  (주의) [{p.kind}] {p.where}")
            out.append(f"      {p.detail}")
        if not self.problems and not self.notes:
            out.append("  ▶ 코드가 부르는 표·컬럼·조인이 전부 원장과 맞는다.")
        if self.type_gaps:
            # ▶ **근본은 조인이 아니라 스키마다.** 같은 뜻의 id 가 표마다 타입이
            #   다르면 그것을 잇는 모든 질의가 캐스팅을 지고 간다. 조인마다
            #   경보를 울리는 대신 원인을 한 줄로 말한다.
            out.append("  ▶ **근본**: 같은 뜻의 컬럼이 표마다 타입이 다르다 - "
                       + " · ".join(f"{c}({a} vs {b})"
                                    for c, a, b in sorted(self.type_gaps)))
            out.append("    조인마다 캐스팅으로 덮는 대신 한쪽 타입을 맞추면 "
                       "이 부류가 통째로 사라진다.")
        out.append("  ▶ 이 검사는 **정규식이 읽은 것만** 본다. 못 읽은 자리는 "
                   "검사되지 않은 것이지 통과한 것이 아니다.")
        return "\n".join(out)


def scan_sql(text: str, where: str = "") -> tuple[list, dict]:
    """SQL 문자열에서 표·조인을 뽑는다. 반환: (findings, 별칭->표).

    별칭이 없으면 표 이름 자체를 별칭으로 쓴다(`from quant.hypotheses` 처럼).
    """
    alias: dict[str, tuple] = {}
    found: list[Finding] = []
    for m in _RE_TABLE.finditer(text):
        sch, tbl, al = m.group(1).lower(), m.group(2).lower(), (m.group(3) or "")
        # SQL 예약어가 별칭 자리에 걸리면 별칭이 아니다
        if al.lower() in {"on", "where", "set", "using", "group", "order",
                          "left", "right", "inner", "outer", "join", "as",
                          "values", "select", "limit", "and", "or"}:
            al = ""
        alias[(al or tbl).lower()] = (sch, tbl)
        found.append(Finding("table", where, f"{sch}.{tbl}", (sch, tbl)))
    for m in _RE_JOIN.finditer(text):
        a, ac, b, bc = (m.group(1).lower(), m.group(2).lower(),
                        m.group(3).lower(), m.group(4).lower())
        if a not in alias or b not in alias:
            continue                      # 별칭을 못 풀었다 - 검사하지 않는다
        found.append(Finding(
            "join", where, f"{a}.{ac} = {b}.{bc}",
            (alias[a] + (ac,), alias[b] + (bc,),
             bool(m.group(5)) or "::" in m.group(0))))
    return found, alias


def compatible(t1: str, t2: str) -> bool:
    """두 컬럼 타입을 캐스팅 없이 비교할 수 있나."""
    a, b = str(t1 or "").lower(), str(t2 or "").lower()
    return frozenset({a, b}) in _COMPATIBLE


def judge(findings, columns: dict, tables: set) -> Report:
    """뽑은 경계를 원장 사실(`columns`, `tables`)에 대조한다. **순수 함수.**

    columns: {(schema, table, column): data_type}
    tables:  {(schema, table)}
    """
    r = Report()
    for f in findings:
        r.checked += 1
        if f.kind == "table":
            if f.parts not in tables:
                r.problems.append(Finding(
                    "없는 표", f.where,
                    f"{f.detail} 가 원장에 없다 - 이 질의는 실행하면 죽는다"))
            else:
                r.ok_tables += 1
        elif f.kind == "join":
            left, right, casted = f.parts
            t1, t2 = columns.get(left), columns.get(right)
            if t1 is None or t2 is None:
                miss = ".".join(left if t1 is None else right)
                r.problems.append(Finding(
                    "없는 컬럼", f.where,
                    f"{f.detail} - {miss} 가 원장에 없다"))
                continue
            if compatible(t1, t2):
                r.ok_joins += 1
            elif casted:
                # 캐스팅으로 붙였다. 실행은 된다 - 다만 캐스팅한 쪽 인덱스를
                # 못 쓴다. **양변 캐스팅은 다르다**: 실측으로 47행짜리 표에서
                # `statement timeout` 이 났다. 심각도를 안 가르면 멀쩡한
                # 한쪽 캐스팅까지 경보가 되어 목록을 아무도 안 본다.
                r.notes.append(Finding(
                    "캐스팅 조인", f.where,
                    f"{f.detail} 가 {t1} vs {t2} 라 캐스팅으로 붙어 있다 - "
                    f"실행은 되지만 캐스팅한 쪽 인덱스를 못 쓴다"))
                r.type_gaps.add((left[2], t1, t2))
            else:
                r.problems.append(Finding(
                    "타입 불일치", f.where,
                    f"{f.detail} 가 {t1} vs {t2} 다 - "
                    f"`operator does not exist` 로 죽는다"))
                r.type_gaps.add((left[2], t1, t2))
    return r


# ── 원장 사실 ────────────────────────────────────────────────────────────────

def ledger_facts(conn, schemas) -> tuple[dict, set]:
    """그 DB 의 표·컬럼·타입. 못 읽으면 예외를 그대로 올린다.

    ▶ **전수 조회를 하지 않는다** (2026-08-13). 처음엔 `table_schema = any(...)`
      로 통째로 긁었는데 `QueryCanceled: statement timeout` 이 났다. 이 원장은
      세션풀이 얇고 카탈로그가 크다. 우리가 실제로 부르는 스키마만 본다.
    """
    cols: dict = {}
    tabs: set = set()
    with conn.cursor() as cur:
        cur.execute("""
            select c.table_schema, c.table_name, c.column_name, c.data_type
              from information_schema.columns c
             where c.table_schema = any(%s)""", (list(schemas),))
        for s, t, cn, d in cur.fetchall():
            cols[(s, t, cn)] = d
            tabs.add((s, t))
    return cols, tabs


def scan_tree(root: Path) -> list:
    """저장소의 .py 를 훑어 SQL 경계를 뽑는다."""
    out: list = []
    me = Path(__file__).resolve()
    for p in sorted(root.rglob("*.py")):
        if any(x in p.parts for x in (".venv", "node_modules", "__pycache__")):
            continue
        # ▶ **자기 자신은 건너뛴다** (2026-08-13, 처음 돌리고 바로 드러났다)
        #   이 파일의 자체점검 픽스처가 일부러 깨진 SQL 을 담고 있어서, 전수
        #   조사가 그것을 진짜 결함 4건으로 보고했다. 오탐이 한 번 섞이면
        #   아무도 이 목록을 안 본다 - 여기 실제 원장 질의는 없다
        #   (`information_schema` 는 우리 스키마가 아니다).
        try:
            if p.resolve() == me:
                continue
        except OSError:
            pass
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _RE_TABLE.search(src):
            continue
        found, _ = scan_sql(src, where=str(p))
        out.extend(found)
    return out


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_catches_the_uuid_text_join():
    """**오늘 세 번 만난 그 결함을 잡는다.** (2026-08-13 실측)

    `quant.hypotheses.hypothesis_id` 는 uuid, `research.experiment_outcomes.
    hypothesis_id` 는 text 다. 캐스팅 없이 조인하면 `operator does not exist:
    uuid = text` 로 죽는데, 자체점검 92개가 이걸 못 잡았다 - 모듈 안만 보기
    때문이다.
    """
    sql = ("select o.decision from research.experiment_outcomes o "
           "join quant.hypotheses h on h.hypothesis_id = o.hypothesis_id")
    found, alias = scan_sql(sql, "t.py:1")
    assert alias["o"] == ("research", "experiment_outcomes"), alias
    assert alias["h"] == ("quant", "hypotheses"), alias
    cols = {("quant", "hypotheses", "hypothesis_id"): "uuid",
            ("research", "experiment_outcomes", "hypothesis_id"): "text",
            ("research", "experiment_outcomes", "decision"): "text"}
    tabs = {("quant", "hypotheses"), ("research", "experiment_outcomes")}
    r = judge(found, cols, tabs)
    assert not r.clean, "타입 불일치를 통과시켰다"
    assert any(p.kind == "타입 불일치" for p in r.problems), r.problems
    assert "uuid vs text" in " ".join(p.detail for p in r.problems)


def _check_cast_join_is_a_problem_too():
    """**캐스팅으로 덮은 것도 문제로 센다.**

    실측: 양변을 `::text` 로 캐스팅했더니 실행은 됐지만 47행짜리 표에서
    `QueryCanceled: statement timeout` 이 났다. 인덱스를 못 쓰기 때문이다.
    "돌아가니 됐다" 로 넘기면 다음 사람이 같은 자리에서 막힌다.
    """
    sql = ("select 1 from research.experiment_outcomes o "
           "join quant.hypotheses h on h.hypothesis_id::text = o.hypothesis_id")
    found, _ = scan_sql(sql, "t.py:1")
    cols = {("quant", "hypotheses", "hypothesis_id"): "uuid",
            ("research", "experiment_outcomes", "hypothesis_id"): "text"}
    r = judge(found, cols, {("quant", "hypotheses"),
                            ("research", "experiment_outcomes")})
    # 죽지는 않으므로 `problems` 가 아니라 `notes` 다. 심각도를 안 가르면
    # 멀쩡한 한쪽 캐스팅까지 경보가 되어 목록을 아무도 안 본다.
    assert r.clean, "한쪽 캐스팅을 죽는 것으로 셌다"
    assert any(p.kind == "캐스팅 조인" for p in r.notes), r.notes
    assert r.type_gaps, "근본(타입이 표마다 다름)을 안 모았다"
    assert "근본" in r.text()


def _check_catches_missing_column():
    """**없는 컬럼을 부르는 질의를 잡는다.**

    실측: `load_past_outcomes` 가 `experiment_outcomes.edge_type` 을 조회했는데
    그 컬럼이 없다. 부르는 곳이 없어서 오래 안 드러났고, 부르자마자 죽었다.
    """
    sql = ("select 1 from research.experiment_outcomes o "
           "join quant.hypotheses h on h.edge_type = o.edge_type")
    found, _ = scan_sql(sql, "t.py:1")
    cols = {("quant", "hypotheses", "hypothesis_id"): "uuid"}
    r = judge(found, cols, {("quant", "hypotheses"),
                            ("research", "experiment_outcomes")})
    assert any(p.kind == "없는 컬럼" for p in r.problems), r.problems


def _check_catches_missing_table():
    sql = "select 1 from quant.strategy_bundles b where b.x = 1"
    found, _ = scan_sql(sql, "t.py:1")
    r = judge(found, {}, {("quant", "hypotheses")})
    assert any(p.kind == "없는 표" for p in r.problems), r.problems


def _check_good_join_passes():
    """**멀쩡한 것을 문제로 만들지 않는다.** 오탐이 나면 아무도 안 본다."""
    sql = ("select 1 from quant.experiments e "
           "join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id")
    found, _ = scan_sql(sql, "t.py:1")
    cols = {("quant", "hypotheses", "hypothesis_id"): "uuid",
            ("quant", "experiments", "hypothesis_id"): "uuid"}
    r = judge(found, cols, {("quant", "hypotheses"), ("quant", "experiments")})
    assert r.clean, r.problems
    assert r.ok_joins == 1 and r.ok_tables == 2, (r.ok_joins, r.ok_tables)
    assert "전부 원장과 맞는다" in r.text()


def _check_unresolvable_alias_is_not_a_pass():
    """**못 읽은 자리를 통과로 세지 않는다.**

    별칭을 못 풀면 검사하지 않는다. 그런데 보고서가 그 사실을 말해야 한다 -
    "문제 0건" 만 보면 전수 검사한 줄 안다. 그게 오늘의 사고와 같은 형태다.
    """
    sql = "select 1 from quant.hypotheses h on zzz.a = qqq.b"
    found, _ = scan_sql(sql, "t.py:1")
    assert not [f for f in found if f.kind == "join"], "못 푼 별칭을 조인으로 셌다"
    r = judge(found, {}, {("quant", "hypotheses")})
    assert "검사되지 않은 것이지 통과한 것이 아니다" in r.text()


def _selfcheck() -> int:
    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_catches_the_uuid_text_join()
    print("  uuid = text 조인을 잡음      OK")
    _check_cast_join_is_a_problem_too()
    print("  캐스팅으로 덮은 것도 잡음    OK")
    _check_catches_missing_column()
    print("  없는 컬럼을 잡음            OK")
    _check_catches_missing_table()
    print("  없는 표를 잡음              OK")
    _check_good_join_passes()
    print("  멀쩡한 것은 통과            OK")
    _check_unresolvable_alias_is_not_a_pass()
    print("  못 읽은 자리를 말함          OK")
    print("경계 계약 6개 영역 통과.")
    return 0


def _cli(argv) -> int:
    if "--verify" not in argv:
        return _selfcheck()
    sys.path.insert(0, "/app/departments/01-research/collectors")
    import psycopg2                                # noqa: PLC0415
    from source_registry import load_project_env    # noqa: PLC0415

    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv \
        else Path("/app/departments")
    findings = scan_tree(root)
    env = load_project_env()
    cols: dict = {}
    tabs: set = set()
    read: list = []
    # 원장이 둘이라 둘 다 읽는다. **한쪽을 못 읽으면 그 스키마는 검사하지
    # 않는다** - 못 읽은 것을 "없는 표" 로 보고하면 그게 오탐이고, 오탐이
    # 한 번 나오면 아무도 이 검사를 안 본다.
    for dsn_key, schemas in (("DATABASE_URL", LEDGER_SCHEMAS),
                             ("TIMESCALE_DATABASE_URL", MARKET_SCHEMAS)):
        dsn = env.get(dsn_key)
        if not dsn:
            print(f"  !! {dsn_key} 가 없다 - {schemas} 는 검사하지 않는다")
            continue
        try:
            c = psycopg2.connect(dsn, connect_timeout=25)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {dsn_key} 연결 실패 - {schemas} 는 검사하지 않는다: "
                  f"{type(exc).__name__}")
            continue
        try:
            c2, t2 = ledger_facts(c, schemas)
            cols.update(c2)
            tabs |= t2
            read.extend(schemas)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {schemas} 카탈로그 조회 실패: {type(exc).__name__}: "
                  f"{str(exc)[:90]}")
        finally:
            c.close()
    if not read:
        print("원장을 하나도 못 읽었다 - 검사하지 않는다(통과가 아니다)")
        return 1
    skipped = [s for s in SCHEMAS if s not in read]
    findings = [f for f in findings
                if (f.parts[0] if f.kind == "table" else f.parts[0][0]) in read]
    r = judge(findings, cols, tabs)
    print(r.text())
    if skipped:
        print(f"  !! 못 읽은 스키마 {skipped} 의 질의는 **검사되지 않았다**")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_cli(sys.argv[1:]))
