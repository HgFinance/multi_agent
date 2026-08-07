#!/usr/bin/env python3
"""Layered Memory — 마감 기억 (FinMem 계층 구조 적용).

2026-08-07 tool 강등 전에는 nav-close-worker 전용이었다. 지금은 별도 직원이 아니라
`exception-investigation-worker` 의 근거 provider 셋 중 하나다 — 지난 마감에서 뭐가
걸렸는지가 이번 예외 조사의 재료이기 때문이다.

소유: 도현 (회계/포트폴리오본부)
근거: references/references.md (FinMem: Performance-Enhanced LLM Trading Agent with
      Layered Memory) — 계층별 감쇠와 relevance/recency/importance 검색 구조만 차용한다.
      docs/HEDGE_FUND_MASTER_PLAN.md 12.4(NAV Close), CLAUDE.local.md 원칙 5

마감은 매번 처음이 아니다. 지난 마감에서 뭐가 걸렸는지, 같은 계정에서 같은 Break 가
또 났는지가 다음 마감 준비의 절반이다. 그런데 그걸 마감 담당이 기억으로 들고 있으면
사람이 바뀔 때 사라진다. FinMem 의 계층 메모리를 그 자리에 놓는다.

  shallow      (반감기 3일)   직전 며칠의 운영 잔여물. 빨리 잊혀도 된다
  intermediate (반감기 30일)  이번 달 반복 패턴. 월 마감 준비에서 다시 본다
  deep         (반감기 365일) 구조적 원인. 같은 계정·같은 브로커에서 반복되는 것

점수 = relevance x recency_decay x importance. 자주 회상된 기억은 아래 계층으로
승격된다(FinMem 의 강화). 계층·가중·반감기는 accounting_ops.yaml 에 있다.

**기억 계층과 System of Record 를 섞지 않는다.** 이게 이 파일에서 제일 중요하다.

  - 기억은 **문장과 참조만** 담는다. 금액·수량·NAV 를 담는 필드가 아예 없다
    (`MemoryEntry` 에 숫자 필드가 없고, 자체 점검이 그걸 검사한다).
  - 회상 결과는 `authoritative: False` 다. 마감 수치는 ledger/portfolio 에서만 나온다.
  - 기억이 "지난달 NAV 는 X 였다"고 말해도 그건 회계 기록이 아니다. 그래서 애초에
    그런 문장을 저장하지 못하게 `remember()` 가 금액처럼 보이는 본문을 거부한다.
  - **`is_official` 은 어떤 고도화 뒤에도 False 다.** NAV 확정은 기억이 아니라 승인
    절차이고, 이 모듈에는 그 값을 True 로 만드는 경로가 없다.

저장은 JSONL 하나다. 기억은 비공식 보조 자료라 System of Record 스키마
(`accounting.*`)에 테이블을 만들지 않는다 — 만들면 그 순간 둘의 경계가 흐려진다.

자체 점검: python departments/05-accounting-portfolio/nav_close_memory.py
"""
from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_HERE = Path(__file__).resolve().parent

import yaml

OPS_PATH = _HERE / "accounting_ops.yaml"
MEMORY_PATH = _HERE / "reports" / "nav_close_memory.jsonl"

LAYERS = ("shallow", "intermediate", "deep")
# 승격 방향. shallow 에서 시작해 자주 쓰이면 아래로 내려간다.
_NEXT_LAYER = {"shallow": "intermediate", "intermediate": "deep", "deep": "deep"}

# 본문에 금액이 들어오는 것을 막는다. 기억은 서술이지 회계 기록이 아니다.
# 3자리 이상 숫자(1,234 / 1234 / 1234.5)와 통화 표기를 잡는다.
_AMOUNT = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d{3,})(?:\.\d+)?|[₩$]\s*\d")


class CloseMemoryError(Exception):
    """기억을 쓰거나 읽을 수 없는 경우. 조용히 빈 기억으로 넘어가지 않는다."""


@dataclass
class MemoryEntry:
    """마감 기억 한 조각.

    **숫자 필드가 없다.** 있으면 그게 곧 두 번째 회계 기록이 된다. 수치가 필요하면
    `refs` 로 원본(reconciliation_id / break_id / nav_run_id)을 가리키고 소비자가
    System of Record 에서 읽는다.
    """

    memory_id: str
    layer: str
    text: str                       # 무슨 일이 있었나 (서술만)
    tags: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)   # 원본 참조 id
    importance: float = 0.5         # 0~1
    created_at: str = ""
    last_access: str = ""
    accesses: int = 0


@dataclass(frozen=True)
class Recalled:
    entry: MemoryEntry
    score: float
    recency: float


@dataclass(frozen=True)
class Settings:
    half_life_days: Mapping[str, float]
    weights: Mapping[str, float]
    max_recall: int
    min_score: float
    promote_after_accesses: int


def load_settings(path: Path = OPS_PATH) -> Settings:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CloseMemoryError(f"운영 튜닝 파일을 읽을 수 없습니다: {path}") from exc
    block = (doc or {}).get("close_memory")
    if not block:
        raise CloseMemoryError(f"{path} 에 close_memory 블록이 없습니다. 튜닝값을 코드에 두지 않습니다")
    half_lives = {name: float(spec["half_life_days"]) for name, spec in block["layers"].items()}
    missing = [layer for layer in LAYERS if layer not in half_lives]
    if missing:
        raise CloseMemoryError(f"close_memory.layers 에 빠진 계층: {missing}")
    return Settings(
        half_life_days=half_lives,
        weights={k: float(v) for k, v in block["weights"].items()},
        max_recall=int(block["max_recall"]),
        min_score=float(block["min_score"]),
        promote_after_accesses=int(block["promote_after_accesses"]),
    )


# ── 쓰기 ───────────────────────────────────────────────────────────────────
def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _memory_id(text: str, created: datetime) -> str:
    import hashlib

    raw = f"{created.isoformat()}|{text}"
    return f"mem:{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def remember(text: str, *, layer: str = "shallow", tags: Sequence[str] = (),
             refs: Sequence[str] = (), importance: float = 0.5,
             now: datetime | None = None, path: Path = MEMORY_PATH) -> MemoryEntry:
    """마감 기억 한 줄을 남긴다. **수치가 들어간 문장은 거부한다.**"""
    text = " ".join(str(text or "").split())
    if not text:
        raise CloseMemoryError("빈 기억은 저장하지 않습니다")
    if layer not in LAYERS:
        raise CloseMemoryError(f"알 수 없는 계층입니다: {layer!r} (허용: {LAYERS})")
    if not 0.0 <= float(importance) <= 1.0:
        raise CloseMemoryError(f"importance 는 0~1 입니다: {importance}")
    hit = _AMOUNT.search(text)
    if hit:
        raise CloseMemoryError(
            f"기억 본문에 수치가 있습니다({hit.group()!r}). 회계 수치는 원장에서만 나옵니다 — "
            "금액 대신 refs 로 원본 id 를 가리키십시오")

    created = _now(now)
    entry = MemoryEntry(
        memory_id=_memory_id(text, created), layer=layer, text=text,
        tags=[str(t) for t in tags], refs=[str(r) for r in refs],
        importance=float(importance), created_at=created.isoformat(),
        last_access=created.isoformat(), accesses=0,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    return entry


def load_memories(path: Path = MEMORY_PATH) -> list[MemoryEntry]:
    """JSONL 을 읽는다. 같은 memory_id 는 마지막 줄이 이긴다(append 로 갱신하므로)."""
    if not path.exists():
        return []
    latest: dict[str, MemoryEntry] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        latest[row["memory_id"]] = MemoryEntry(**row)
    return list(latest.values())


# ── 읽기 (FinMem 점수) ─────────────────────────────────────────────────────
def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9A-Za-z가-힣]+", (text or "").lower()) if len(t) > 1}


def _recency(entry: MemoryEntry, now: datetime, settings: Settings) -> float:
    """계층별 반감기 지수 감쇠. deep 은 거의 안 줄고 shallow 는 며칠이면 사라진다."""
    created = datetime.fromisoformat(entry.created_at)
    days = max((now - created).total_seconds() / 86400.0, 0.0)
    half_life = settings.half_life_days[entry.layer]
    return math.pow(0.5, days / half_life) if half_life > 0 else 0.0


def _relevance(entry: MemoryEntry, query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    hay = _tokens(entry.text) | {t.lower() for t in entry.tags}
    overlap = query_tokens & hay
    return len(overlap) / len(query_tokens)


def recall(query: str, memories: Iterable[MemoryEntry] | None = None, *,
           now: datetime | None = None, settings: Settings | None = None,
           path: Path = MEMORY_PATH) -> dict[str, Any]:
    """마감 준비에 쓸 기억을 꺼낸다. **비공식 보조 자료다.**"""
    settings = settings or load_settings()
    now = _now(now)
    entries = list(memories) if memories is not None else load_memories(path)
    tokens = _tokens(query)
    w = settings.weights

    scored: list[Recalled] = []
    for entry in entries:
        relevance = _relevance(entry, tokens) ** w.get("relevance", 1.0)
        recency = _recency(entry, now, settings) ** w.get("recency", 1.0)
        importance = float(entry.importance) ** w.get("importance", 1.0)
        score = relevance * recency * importance
        if score >= settings.min_score:
            scored.append(Recalled(entry=entry, score=score, recency=recency))
    scored.sort(key=lambda r: (-r.score, r.entry.memory_id))
    top = scored[: settings.max_recall]
    return {
        "query": query,
        "recalled": [
            {"memory_id": r.entry.memory_id, "layer": r.entry.layer, "text": r.entry.text,
             "tags": list(r.entry.tags), "refs": list(r.entry.refs),
             "score": round(r.score, 6), "recency": round(r.recency, 6),
             "importance": r.entry.importance}
            for r in top
        ],
        "considered": len(entries),
        "by_layer": {layer: sum(1 for r in top if r.entry.layer == layer) for layer in LAYERS},
        # 계약 — 마감 수치는 여기서 나오지 않는다.
        "authoritative": False,
        "source_of_record": "accounting.* (ledger / portfolio_snapshots / nav_runs)",
        "is_official": False,
    }


def reinforce(recalled: Mapping[str, Any], *, now: datetime | None = None,
              settings: Settings | None = None, path: Path = MEMORY_PATH) -> list[MemoryEntry]:
    """회상된 기억의 접근 횟수를 올리고, 임계를 넘으면 아래 계층으로 승격한다.

    FinMem 의 강화에 해당한다. 자주 쓰이는 기억은 오래 남고, 안 쓰이는 기억은
    감쇠로 조용히 사라진다 - 삭제 절차를 따로 두지 않는 것이 요지다.
    """
    settings = settings or load_settings()
    now = _now(now)
    index = {e.memory_id: e for e in load_memories(path)}
    updated: list[MemoryEntry] = []
    for item in recalled.get("recalled", []):
        entry = index.get(item["memory_id"])
        if entry is None:
            continue
        entry.accesses += 1
        entry.last_access = now.isoformat()
        if entry.accesses >= settings.promote_after_accesses:
            entry.layer = _NEXT_LAYER[entry.layer]
            entry.accesses = 0      # 승격하면 다시 센다 - 한 번에 두 계층을 못 뛴다
        updated.append(entry)
    if updated:
        with path.open("a", encoding="utf-8") as handle:
            for entry in updated:
                handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    return updated


def close_memory_context(recalled: Mapping[str, Any]) -> str:
    """exception-investigation-worker 프롬프트에 붙이는 블록. 비공식 보조다."""
    if not recalled.get("recalled"):
        return ("과거 마감 기억: 없음. 기억이 없다는 이유로 마감 상태를 추정하지 마십시오 — "
                "수치와 상태는 원장에서 읽으십시오.")
    lines = [
        "아래는 **비공식 과거 마감 기억**입니다. 참고만 하십시오.",
        "여기서 금액·NAV·수량을 읽지 마십시오 — 수치는 원장(accounting.*)에서만 나옵니다.",
        "기억은 NAV 를 확정하지 못합니다(is_official 은 항상 false).",
        "",
    ]
    lines += [f"- {m['memory_id']} [{m['layer']}] {m['text']}"
              + (f" (원본: {', '.join(m['refs'])})" if m["refs"] else "")
              for m in recalled["recalled"]]
    return "\n".join(lines)


if __name__ == "__main__":
    import tempfile
    from datetime import timedelta

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = load_settings()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

    def raises(fn, why):
        try:
            fn()
        except CloseMemoryError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 튜닝값은 코드가 아니라 accounting_ops.yaml 에서 온다
    assert settings.half_life_days == {"shallow": 3.0, "intermediate": 30.0, "deep": 365.0}
    assert settings.max_recall == 5 and settings.promote_after_accesses == 3
    print("  튜닝값 YAML 적재           OK")

    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "mem.jsonl"

        # 2. **수치가 든 기억은 저장되지 않는다** - 기억이 두 번째 회계 기록이 되는 것을 막는다
        raises(lambda: remember("전월 NAV 는 1,204,000,000 이었다", path=mem), "금액이 든 기억")
        raises(lambda: remember("현금 차이 250000 발생", path=mem), "수량이 든 기억")
        raises(lambda: remember("₩5000 미달", path=mem), "통화 표기")
        raises(lambda: remember("", path=mem), "빈 기억")
        raises(lambda: remember("정상", layer="ghost", path=mem), "없는 계층")
        raises(lambda: remember("정상", importance=1.5, path=mem), "범위 밖 importance")
        print("  회계 수치 저장 차단        OK")

        # 3. 서술 + 원본 참조는 저장된다 (수치가 필요하면 refs 로 가리킨다)
        a = remember("브로커 명세서 지연으로 현금 대사가 마감 직전까지 열려 있었다",
                     layer="shallow", tags=["cash", "recon"],
                     refs=["reconciliation:11111111", "break:22222222"],
                     importance=0.6, now=now - timedelta(days=1), path=mem)
        b = remember("같은 계정에서 수수료 분개 누락이 반복된다", layer="deep",
                     tags=["fee", "journal"], refs=["break:33333333"],
                     importance=0.9, now=now - timedelta(days=120), path=mem)
        c = remember("배당 반영이 하루 늦어 포지션 대사가 어긋났다", layer="intermediate",
                     tags=["corporate_action"], refs=["break:44444444"],
                     importance=0.7, now=now - timedelta(days=20), path=mem)
        assert not hasattr(a, "amount") and "amount" not in asdict(a), asdict(a)
        # MemoryEntry 자체에 숫자 필드가 없다 (importance/accesses 는 회계 수치가 아니다)
        numeric_fields = {k for k, v in asdict(a).items() if isinstance(v, (int, float))}
        assert numeric_fields == {"importance", "accesses"}, numeric_fields
        print("  서술 + 원본 참조 저장     OK")

        # 4. **계층별 감쇠** - 같은 나이라도 deep 이 오래 남는다
        loaded = {e.memory_id: e for e in load_memories(mem)}
        assert set(loaded) == {a.memory_id, b.memory_id, c.memory_id}
        shallow_old = MemoryEntry(memory_id="mem:x", layer="shallow", text="오래된 얕은 기억",
                                  created_at=(now - timedelta(days=120)).isoformat(),
                                  importance=0.9)
        deep_old = MemoryEntry(memory_id="mem:y", layer="deep", text="오래된 깊은 기억",
                               created_at=(now - timedelta(days=120)).isoformat(),
                               importance=0.9)
        assert _recency(shallow_old, now, settings) < _recency(deep_old, now, settings) / 1000
        print("  계층별 감쇠 (FinMem)      OK")

        # 5. 회상 - relevance x recency x importance
        out = recall("수수료 분개 누락", now=now, settings=settings, path=mem)
        assert out["recalled"], out
        assert out["recalled"][0]["memory_id"] == b.memory_id, out["recalled"]
        assert out["recalled"][0]["refs"] == ["break:33333333"]
        # 관련 없는 질의는 아무것도 안 꺼낸다 - 억지 회상이 마감 서술을 흐린다
        assert recall("완전히 무관한 주제 zzz", now=now, settings=settings,
                      path=mem)["recalled"] == []
        print("  회상 점수 (관련성)        OK")

        # 6. **기억은 System of Record 가 아니다** - 계약이 반환값에 박혀 있다
        assert out["authoritative"] is False
        assert out["is_official"] is False, "기억이 NAV 를 공식으로 만들었다"
        assert out["source_of_record"].startswith("accounting.")
        ctx = close_memory_context(out)
        assert "비공식" in ctx and "원장" in ctx and "is_official 은 항상 false" in ctx
        empty_ctx = close_memory_context({"recalled": []})
        assert "추정하지 마십시오" in empty_ctx
        print("  기억 != System of Record  OK")

        # 7. 강화와 승격 - 3회 접근하면 아래 계층으로 내려간다
        target = recall("브로커 명세서 지연", now=now, settings=settings, path=mem)
        assert target["recalled"][0]["memory_id"] == a.memory_id
        for _ in range(2):
            reinforce(target, now=now, settings=settings, path=mem)
        still = {e.memory_id: e for e in load_memories(mem)}[a.memory_id]
        assert still.layer == "shallow" and still.accesses == 2, (still.layer, still.accesses)
        reinforce(target, now=now, settings=settings, path=mem)
        promoted = {e.memory_id: e for e in load_memories(mem)}[a.memory_id]
        assert promoted.layer == "intermediate" and promoted.accesses == 0, promoted
        # 승격 뒤 감쇠가 느려져 같은 질의에서 점수가 올라간다
        after = recall("브로커 명세서 지연", now=now, settings=settings, path=mem)
        assert after["recalled"][0]["score"] > target["recalled"][0]["score"]
        print("  강화 -> 계층 승격          OK")

        # 8. deep 은 더 승격되지 않고, 없는 기억을 강화해도 죽지 않는다
        deep_recall = recall("수수료 분개 누락", now=now, settings=settings, path=mem)
        for _ in range(4):
            reinforce(deep_recall, now=now, settings=settings, path=mem)
        assert {e.memory_id: e for e in load_memories(mem)}[b.memory_id].layer == "deep"
        assert reinforce({"recalled": [{"memory_id": "mem:nonexistent"}]},
                         now=now, settings=settings, path=mem) == []
        assert load_memories(Path(tmp) / "없는파일.jsonl") == []
        print("  deep 상한 + 결측 내성      OK")

    # 9. 마감 파이프라인의 is_official 계약과 같은 값인지 (두 곳이 어긋나면 안 된다)
    close_source = (_HERE / "scripts.py").read_text(encoding="utf-8")
    assert '"is_official": False' in close_source, "마감 파이프라인의 is_official 계약이 바뀌었다"
    assert recall("아무거나", [], now=now, settings=settings)["is_official"] is False
    raises(lambda: load_settings(_HERE / "없는파일.yaml"), "없는 튜닝 파일")
    print("  is_official 계약 일치      OK")

    print("ok - 마감 Layered Memory 9개 영역 점검 통과 (비공식 보조, is_official 은 항상 False)")
