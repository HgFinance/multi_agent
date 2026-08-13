#!/usr/bin/env python3
"""**에이전트가 자기 추론을 원장에 남긴다.** 다음 주기의 자기가 읽는다.

▶ 왜 필요한가 (2026-08-12 실측)
  에이전트는 카드 하나를 보고 153초 살다 죽는다. 의제도 연속성도 없다.
  그래서 매번 처음부터 생각한다 - **연구 프로그램을 못 만든다.**

  지금 `research.experiment_outcomes.notes` 는 결정론 코드가 쓴다:

      "관문 4/9 통과. 남은 조항: max_drawdown -50.52% (15.52%p 모자람)…"

  사실이지만 **해석이 아니다.** "왜 v2 에서 통하던 것이 v3 에서 무너졌나",
  "다음엔 무엇을 먼저 확인할 것인가" 는 판단이고 에이전트 몫이다.
  그것이 남지 않으면 다음 주기가 같은 자리에서 다시 생각한다.

  `cycle_brief.load_lessons` 가 이미 `notes` 를 읽어 브리핑으로 나른다 -
  **적기만 하면 다음 주기에 도달한다.**

▶ 규율
  덧붙이기만 한다. 결정론 문장을 지우면 사실이 사라진다.
  실험 id 에 묶는다. 어느 결과에 대한 해석인지 없으면 대조가 안 된다.
  빈 말은 거부한다 - "추가 분석 필요" 같은 문장은 다음 사람에게 0비트다.

사용:
    quant-py research_note.py --experiment <id> --note "…"
    quant-py research_note.py --experiment <id> --note-file /tmp/note.md
    quant-py research_note.py --show <id>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

NOTE_VERSION = "quant-research-note-v1"
MARKER = "[연구노트]"
MIN_CHARS = 60

# 이 말만 있으면 아무것도 안 말한 것이다. 다음 사람이 쓸 수 있는 게 없다.
_EMPTY_PHRASES = (
    "추가 분석 필요", "추가 검토 필요", "더 살펴봐야", "확인이 필요하다",
    "특이사항 없음", "결과를 확인했다", "N/A", "없음",
)


def is_substantive(text: str) -> tuple[bool, str]:
    """빈 말인가. **거부 사유를 같이 돌려준다** - 왜 막혔는지 알아야 고친다."""
    t = " ".join(str(text or "").split())
    if len(t) < MIN_CHARS:
        return False, f"{len(t)}자 - 최소 {MIN_CHARS}자. 한 문장으로는 해석이 안 된다"
    # ▶ **금칙어 사냥이 아니다.** 빼고 남는 것이 있으면 통과시킨다.
    #   길이만으로 자르면 구체적인 노트가 "추가 분석 필요" 를 한 번 썼다고
    #   거부된다(자체 점검이 실제로 잡았다). **오탐은 가드 전체를 무력화한다** -
    #   한 번 억울하게 막히면 사람은 가드를 우회할 방법부터 찾는다.
    low = t.lower()
    hits = [p for p in _EMPTY_PHRASES if p.lower() in low]
    if hits:
        rest = low
        for p in hits:
            rest = rest.replace(p.lower(), " ")
        if len(" ".join(rest.split())) < MIN_CHARS:
            return False, (f"빼고 나면 남는 게 없다({', '.join(hits)}) - 무엇을 "
                           f"왜 그렇게 읽었고 다음에 무엇을 확인할지 적어라")
    return True, ""


def compose(existing: str, note: str, *, author: str) -> str:
    """기존 문장 **뒤에 붙인다.** 결정론이 적은 사실을 지우지 않는다."""
    body = " ".join(str(note or "").split())
    tail = f"{MARKER}({author}) {body}"
    base = str(existing or "").strip()
    return f"{base}\n{tail}" if base else tail


def split_notes(text: str) -> tuple[str, list]:
    """`(결정론 문장, 연구노트들)`. **누가 썼는지 섞이면 신뢰가 무너진다.**"""
    facts, notes = [], []
    for line in str(text or "").splitlines():
        (notes if line.lstrip().startswith(MARKER) else facts).append(line)
    return "\n".join(facts).strip(), [n.strip() for n in notes if n.strip()]


_SQL_FIND = """
select outcome_id, coalesce(notes, '') from research.experiment_outcomes
 where experiment_id = %s order by decided_at desc limit 1
"""
_SQL_APPEND = """
update research.experiment_outcomes set notes = %s where outcome_id = %s
"""


def append(conn, experiment_id: str, note: str, *, author: str) -> tuple[bool, str]:
    """원장에 덧붙인다. 반환 `(성공, 사유)`."""
    ok, why = is_substantive(note)
    if not ok:
        return False, why
    with conn.cursor() as cur:
        cur.execute(_SQL_FIND, (str(experiment_id),))
        row = cur.fetchone()
        if not row:
            return False, (f"실험 {str(experiment_id)[:8]} 의 환류가 없다 - "
                           f"판정이 끝난 실험에만 붙일 수 있다")
        oid, existing = row
        _f, prior = split_notes(existing)
        cur.execute(_SQL_APPEND, (compose(existing, note, author=author), oid))
    conn.commit()
    return True, f"연구노트 적재 (이 실험의 {len(prior) + 1}번째)"


def _conn():
    import psycopg2
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "collectors"))
    from source_registry import load_project_env
    return psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_REAL_FACT = ("관문 4/9 통과. 남은 조항: max_drawdown -50.52% "
              "(기준 -35.0%, 15.52% 모자람); pbo 미측정")
_REAL_NOTE = ("v2(626일)에서 Sharpe 1.28 이던 것이 v3(2,599일)에서 -0.36 이다. "
              "2년 표본에는 2018·2022 하락장이 빠져 있었고 그 구간이 모멘텀을 "
              "뒤집는다. 다음엔 표본 구간을 먼저 확인하고 설계한다.")


def _check_facts_survive():
    """**결정론이 적은 사실을 덮지 않는다.** 덮으면 근거가 사라진다."""
    merged = compose(_REAL_FACT, _REAL_NOTE, author="quant-agent")
    assert _REAL_FACT in merged, merged
    facts, notes = split_notes(merged)
    assert facts == _REAL_FACT and len(notes) == 1, (facts, notes)
    assert MARKER in notes[0] and "2018·2022" in notes[0]

    # 두 번 붙여도 앞의 것이 남는다
    again = compose(merged, "두 번째 해석 " + "가" * 60, author="quant-agent")
    _f2, n2 = split_notes(again)
    assert len(n2) == 2 and "2018·2022" in n2[0], n2
    print("  사실이 안 지워짐         OK")


def _check_empty_talk_is_refused():
    """**빈 말을 막는다.** "추가 분석 필요" 는 다음 사람에게 0비트다."""
    for bad in ("추가 분석 필요", "특이사항 없음", "", "짧다"):
        ok, why = is_substantive(bad)
        assert not ok and why, (bad, ok, why)
    ok, why = is_substantive(_REAL_NOTE)
    assert ok, why

    # ▶ **오탐을 검사에 고정한다.** 구체적인 노트가 그 표현을 한 번 썼다고
    #   막히면 안 된다 - 오탐은 가드 전체를 무력화한다(억울하게 막힌 사람은
    #   가드를 우회할 방법부터 찾는다). 첫 판이 실제로 이걸 막았다.
    long_ok = _REAL_NOTE + " 추가 분석 필요한 부분은 유동성 층화다."
    ok2, why2 = is_substantive(long_ok)
    assert ok2, f"구체적인 노트를 금칙어로 막았다: {why2}"

    # 반대로 빈 말을 길게 늘여도 막힌다 - 빼고 나면 남는 게 없다
    padded = "추가 분석 필요. " * 6
    assert not is_substantive(padded)[0], padded
    print("  빈 말 거부(오탐 없이)     OK")


def _check_author_is_recorded():
    """**누가 썼는지 남는다.** 결정론과 에이전트를 못 가르면 신뢰가 무너진다."""
    m = compose("", _REAL_NOTE, author="quant-agent")
    assert m.startswith(f"{MARKER}(quant-agent) "), m[:40]
    _f, notes = split_notes(m)
    assert notes and "quant-agent" in notes[0]
    print("  작성자 기록              OK")


def _check_brief_reads_it():
    """**적으면 다음 주기에 도달하는가.** 안 닿으면 적을 이유가 없다."""
    import inspect
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "factory"))
    import cycle_brief  # noqa: PLC0415

    src = inspect.getsource(cycle_brief.load_lessons)
    assert "notes" in src, "브리핑이 notes 를 안 읽는다 - 적어도 안 닿는다"
    print("  브리핑이 읽어 간다       OK")


def _selfcheck() -> int:
    print(f"{NOTE_VERSION} 자체 점검 (DB 없음)")
    _check_facts_survive()
    _check_empty_talk_is_refused()
    _check_author_is_recorded()
    _check_brief_reads_it()
    print("연구노트 4개 영역 통과.")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="실험 결과에 대한 내 해석을 남긴다")
    ap.add_argument("--experiment")
    ap.add_argument("--note")
    ap.add_argument("--note-file")
    ap.add_argument("--author", default="quant-agent")
    ap.add_argument("--show")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return _selfcheck()

    if a.show:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute(_SQL_FIND, (a.show,))
                row = cur.fetchone()
            if not row:
                print("환류가 없다."); return 1
            facts, notes = split_notes(row[1])
            print("── 결정론이 적은 사실 ──\n" + (facts or "(없음)"))
            print("\n── 연구노트 %d건 ──" % len(notes))
            for n in notes:
                print("  " + n)
        finally:
            conn.close()
        return 0

    note = a.note
    if a.note_file:
        note = Path(a.note_file).read_text(encoding="utf-8")
    if not a.experiment or not note:
        ap.print_help(); return 2
    conn = _conn()
    try:
        ok, why = append(conn, a.experiment, note, author=a.author)
    finally:
        conn.close()
    print(("OK  " if ok else "거부  ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
