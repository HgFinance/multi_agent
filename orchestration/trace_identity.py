#!/usr/bin/env python3
"""프로세스를 넘나드는 Trace 신원 1벌. 표준 라이브러리만 쓴다.

## 왜 이 모듈이 따로 있나 (2026-08-27)

부서장 e2e 계측의 걸림돌은 계측 코드가 없다는 게 아니라 **같은 요청 하나가
프로세스 세 개에 흩어져 각자 자기 이벤트를 쏜다**는 것이었다:

    BFF(apps/api/hermes_boundary.ask)
      -> Kanban dispatcher(scripts/qa_hermes_worker.py)
        -> Hermes 자식 프로세스(부서장 턴)

셋이 같은 `trace_id` 를 못 만들면 Langfuse 에서 트리로 뭉치지 않는다. 그런데 셋이
import 할 수 있는 것이 서로 다르다 - dispatcher 쪽 wrapper 는 `/app/repo/scripts`
에서 뜨고 무거운 의존성이 없는 상태를 유지해야 한다(scripts/hermes_worker_observability.py
머리말: "Hermes 이미지는 그 SDK 를 담지 않는다"). 그래서 이 모듈은 **표준 라이브러리
밖으로 나가지 않는다.** yaml·pydantic·langfuse 어느 것도 import 하지 않는다.

## 왜 langfuse SDK 를 안 부르고 직접 계산하나

langfuse 4.14.5 소스 직접 확인(2026-08-27):

    create_trace_id(seed=s)  ==  sha256(s.encode()).digest()[:16].hex()   # 32 hex
    (span id 파생)           ==  sha256(s.encode()).digest()[:8].hex()    # 16 hex

즉 SDK 가 하는 일이 sha256 자르기다. 이걸 복제하면 **SDK 가 설치된 프로세스(BFF)와
설치되지 않은 프로세스(Hermes 이미지)가 같은 id 를 만든다.** 반대로 SDK 를 이미지에
넣는 선택은 에이전트 런타임 표면을 넓히는 쪽이라 하지 않는다.

SDK 를 쓰는 쪽에서 `Langfuse.create_trace_id(seed=trace_seed(root_id))` 를 불러도
같은 값이 나온다 - seed 문자열만 이 모듈에서 가져가면 된다.

## 결정론 id 의 두 번째 이득: 재관측이 중복을 안 만든다

span id 를 (root, 부서, task, run, attempt, 종류, 순번)에서 파생하면 같은 실행을
두 번 관측해도 같은 span 을 덮어쓴다. 카드 종료가 실시간 이벤트와 내구성 복구
양쪽으로 도달하는 경로가 이미 있고(ceo_supervisor 의 reconciliation), LangSmith 는
그걸 409 로 돌려주는 걸 성공으로 접어 왔다(llm_observability.close_root_trace).
여기서는 애초에 같은 id 라 그 예외 처리가 필요 없다.

자체 점검: python orchestration/trace_identity.py
"""

from __future__ import annotations

import os
from hashlib import sha256
from typing import Mapping

# ─────────────────────────────────────────────────────────────────────────────
# 전파 봉투. 값이 아니라 **이름**을 여기 하나로 모은다 - 이름이 갈리면 자식
# 프로세스가 부모를 못 찾고, 그때 생기는 고아 trace 는 "계측이 됐는데 안 보이는"
# 가장 찾기 어려운 형태로 나타난다.
# ─────────────────────────────────────────────────────────────────────────────
TRACE_ID_ENV = "HGFINANCE_TRACE_ID"
PARENT_SPAN_ID_ENV = "HGFINANCE_PARENT_SPAN_ID"
SESSION_ID_ENV = "HGFINANCE_TRACE_SESSION"

# seed 이름공간. 접두어 없이 root_id 만 넣으면 다른 용도로 같은 문자열을 seed 로
# 쓴 누군가와 id 가 겹친다. 이 값은 **바꾸면 과거 trace 와의 연결이 끊긴다** -
# 바꿔야 할 이유가 생기면 v2 를 새로 만들고 둘 다 유지한다.
_TRACE_SEED_PREFIX = "hgfinance:trace:v1:"
_SPAN_SEED_PREFIX = "hgfinance:span:v1:"

_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16


def trace_seed(root_id: str) -> str:
    """`Langfuse.create_trace_id(seed=...)` 에 그대로 넘길 수 있는 seed 문자열."""

    return f"{_TRACE_SEED_PREFIX}{str(root_id or '').strip()}"


def trace_id_for(root_id: str) -> str:
    """워크플로 루트 1개 -> Langfuse trace id(32 hex).

    입력은 보통 Kanban 루트 카드 id(`t_...`)다. 시나리오에 따라 조인 키가 다르다 -
    실험은 experiment_id, 자동 전략은 signal/intent id, 사용자 PAPER 지시는
    directive id 다. **무엇을 루트로 볼지는 호출자가 정하고, 여기서는 지어내지
    않는다** - 루트를 잘못 고르면 조용히 두 트리로 갈라진다.
    """

    root = str(root_id or "").strip()
    if not root:
        # 빈 루트로 만든 id 는 모든 호출자가 같은 trace 에 쏟아붓는다는 뜻이라
        # 계측이 아니라 오염이다. 계측을 포기하는 쪽이 맞다.
        return ""
    digest = sha256(trace_seed(root).encode("utf-8")).digest()[:16].hex()
    # W3C 는 all-zero 를 무효로 본다. 확률은 무시할 만하지만 그때 조용히 버려지는
    # 쪽이 더 나쁘므로 한 번 더 돌린다(SDK 는 이 가드가 없다).
    if digest == _ZERO_TRACE_ID:
        digest = sha256(f"{trace_seed(root)}:rehash".encode("utf-8")).digest()[:16].hex()
    return digest


def span_id_for(*parts: object) -> str:
    """관측 1개 -> span id(16 hex). 같은 부품이면 같은 id(재관측이 덮어쓴다).

    부품 순서가 신원이다. `(root, department, task, run, attempt, kind, index)`
    처럼 **호출자가 항상 같은 순서로** 넣어야 한다.
    """

    seed = _SPAN_SEED_PREFIX + "|".join(str(part or "").strip() for part in parts)
    digest = sha256(seed.encode("utf-8")).digest()[:8].hex()
    if digest == _ZERO_SPAN_ID:
        digest = sha256(f"{seed}:rehash".encode("utf-8")).digest()[:8].hex()
    return digest


def is_trace_id(value: object) -> bool:
    text = str(value or "").strip()
    return (
        len(text) == 32
        and text != _ZERO_TRACE_ID
        and all(char in "0123456789abcdef" for char in text)
    )


def is_span_id(value: object) -> bool:
    text = str(value or "").strip()
    return (
        len(text) == 16
        and text != _ZERO_SPAN_ID
        and all(char in "0123456789abcdef" for char in text)
    )


def propagation_env(
    *,
    trace_id: str,
    parent_span_id: str = "",
    session_id: str = "",
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """자식 프로세스에 실어 보낼 환경변수. 무효한 값은 **싣지 않는다**.

    깨진 id 를 실어 보내면 자식이 그걸로 span 을 만들고, 그 span 은 어디에도
    안 붙은 채 성공한 것처럼 보인다. 없는 편이 낫다 - 없으면 자식이 자기 루트를
    만들고, 그건 최소한 "부모를 못 찾았다"로 읽힌다.
    """

    env = dict(base) if base is not None else {}
    if is_trace_id(trace_id):
        env[TRACE_ID_ENV] = str(trace_id).strip()
        if is_span_id(parent_span_id):
            env[PARENT_SPAN_ID_ENV] = str(parent_span_id).strip()
        session = str(session_id or "").strip()[:200]
        if session:
            env[SESSION_ID_ENV] = session
    return env


def inherited_context(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, str]:
    """부모가 넘긴 (trace_id, parent_span_id, session_id). 없으면 빈 문자열."""

    source = env if env is not None else os.environ
    trace_id = str(source.get(TRACE_ID_ENV, "") or "").strip()
    if not is_trace_id(trace_id):
        return "", "", ""
    parent = str(source.get(PARENT_SPAN_ID_ENV, "") or "").strip()
    return (
        trace_id,
        parent if is_span_id(parent) else "",
        str(source.get(SESSION_ID_ENV, "") or "").strip(),
    )


if __name__ == "__main__":
    # 1. 결정론: 같은 루트는 언제나 같은 trace id.
    assert trace_id_for("t_abc") == trace_id_for("t_abc")
    assert trace_id_for("t_abc") != trace_id_for("t_abd")
    assert is_trace_id(trace_id_for("t_abc"))

    # 2. langfuse SDK 의 create_trace_id(seed=) 와 **같은 값**이어야 한다.
    #    (langfuse 4.14.5 _client/client.py: sha256(seed)[:16].hex())
    expected = sha256(trace_seed("t_abc").encode("utf-8")).digest()[:16].hex()
    assert trace_id_for("t_abc") == expected, "SDK 파생식과 어긋남"

    # 3. 빈 루트는 계측을 포기한다 - 공용 trace 에 쏟아붓지 않는다.
    assert trace_id_for("") == ""
    assert trace_id_for("   ") == ""

    # 4. span 은 부품 순서가 신원이다. 재관측은 같은 id 라 덮어쓴다.
    a = span_id_for("t_abc", "research", "t_x", "1", 1, "head", 0)
    assert a == span_id_for("t_abc", "research", "t_x", "1", 1, "head", 0)
    assert a != span_id_for("t_abc", "research", "t_x", "1", 2, "head", 0)
    assert is_span_id(a)

    # 5. 전파: 유효한 값만 실린다.
    env = propagation_env(
        trace_id=trace_id_for("t_abc"), parent_span_id=a, session_id="t_abc"
    )
    assert env[TRACE_ID_ENV] == trace_id_for("t_abc")
    assert env[PARENT_SPAN_ID_ENV] == a
    assert env[SESSION_ID_ENV] == "t_abc"
    assert inherited_context(env) == (trace_id_for("t_abc"), a, "t_abc")

    # 6. 깨진 값은 아예 안 실린다(자식이 고아 span 을 만들지 않게).
    broken = propagation_env(trace_id="not-a-trace-id", parent_span_id=a)
    assert TRACE_ID_ENV not in broken and PARENT_SPAN_ID_ENV not in broken
    partial = propagation_env(trace_id=trace_id_for("t_abc"), parent_span_id="zz")
    assert TRACE_ID_ENV in partial and PARENT_SPAN_ID_ENV not in partial
    assert inherited_context({}) == ("", "", "")

    # 7. 기존 환경을 덮지 않고 얹는다(자식 env 조립에 그대로 쓰인다).
    merged = propagation_env(trace_id=trace_id_for("t_abc"), base={"PATH": "/x"})
    assert merged["PATH"] == "/x" and TRACE_ID_ENV in merged

    print("ok - trace 신원 계약 점검 통과")
