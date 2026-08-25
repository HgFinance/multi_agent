"""최종 카드 -> 근거 등급이 붙은 사용자 답변. 파이프라인 마지막 단계.

호스트에서 돌린다(외부 조회 없음). 투자성향 프로필을 주면 적합성까지 판정한다.
"""
from __future__ import annotations

import json
import os
import sys

# answer_builder 정본은 리서치 소유다 - 배치(여기)와 대화(research-mcp)가
# 같은 조립기를 써야 같은 문장·같은 등급이 나간다.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE,
              os.path.normpath(os.path.join(
                  _HERE, "..", "..", "..", "01-research", "evidence")),
              "/app/departments/01-research/evidence"):
    if os.path.isfile(os.path.join(_cand, "answer_builder.py")) and _cand not in sys.path:
        sys.path.insert(0, _cand)

from answer_builder import build_answer, render

IN = os.environ.get("CARDS_FINAL", "/tmp/recommendation/cards_final.json")
OUT = os.environ.get("ANSWERS_OUT", "/tmp/recommendation/answers.json")
PROFILE = os.environ.get("INVESTOR_PROFILE_JSON", "")


def main() -> int:
    cards = json.load(open(IN, encoding="utf-8"))
    profile = json.loads(PROFILE) if PROFILE else None
    if profile is None:
        print("투자성향 프로필 없음 - 적합성은 UNKNOWN 으로 남는다"
              " (INVESTOR_PROFILE_JSON 으로 주입 가능)\n")
    as_of = os.environ.get("AS_OF", "")
    def _day(value: str) -> str:
        return str(value or "")[:10]

    answers = [build_answer(c, as_of=_day(as_of or c.get("as_of", "")),
                            profile=profile)
               for c in cards]
    missing = [a["symbol"] for a in answers if not a["as_of"]]
    if missing:
        # 기준일 없는 답변은 내보내면 안 된다 - 언제 시세인지 모른다.
        raise SystemExit(f"as_of 가 비었다: {missing}")
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(answers, fh, ensure_ascii=False, indent=1)
    for a in answers:
        print(render(a)); print("=" * 74)
    kinds = {}
    for a in answers:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    print(f"{len(answers)}건 -> {OUT}   구분: {kinds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
