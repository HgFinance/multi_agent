#!/usr/bin/env python3
"""뉴스 스토리 군집(Near-Duplicate/Story Cluster) v1 - 결정론 모듈 (LLM 없음).

소유: 재일 (리서치본부)
근거: 뉴스가 두 소스(NAVER·LS)로 들어오는데 정규화 제목의 교집합은 8~12% 실측에
      그친다 - 같은 사건을 표현만 바꾼 기사(근접중복)는 문자열 비교로 안 잡힌다.
      그 결과 감성 분석·Packet 이 한 사건을 여러 기사로 **중복 가중**한다.
      해법: 제목 임베딩(bge-m3, 1024차원) 코사인 유사도로 근접중복을 연결해
      스토리(연결 성분) 단위로 묶는다. 사건당 1 스토리 = 사건당 1 가중.

▶ 설계 결정
  - **전부 결정론이다.** 임베딩 호출 외에는 순수 함수이고, 임베딩도 주입
    가능(embed 인자)해서 자체 점검은 네트워크 없이 가짜 벡터로 돈다.
  - **연결 성분(Union-Find) 군집이다.** A~B, B~C 가 임계 이상이면 A~C 유사도가
    낮아도 셋은 한 스토리다 - 속보가 이어지며 표현이 점진 변형되는 뉴스 특성상
    의도한 동작이고, 임계를 올리면 사슬이 짧아진다(운영 손잡이는 threshold 뿐).
  - **numpy 없이 표준 라이브러리만.** 24h 창의 종목별 기사는 수백 건 규모라
    O(n^2) 쌍 비교를 허용한다. n>1500 은 ValueError 로 거부해 폭주를 막는다.
  - 대표 제목은 **최초 게시(가장 이른 published_at) 기사의 제목**이다 - 사건을
    처음 보도한 표현이 후속 재가공보다 사건 자체에 가깝다. published_at 이
    없는 기사는 최후순위, 동률이면 입력 순서가 빠른 기사.

사용
  python evidence/story_cluster.py     # 자체 점검 (네트워크 없음, 가짜 임베딩)
"""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

STORY_CLUSTER_VERSION = "story-cluster-v1"

# 임베딩 모델 - agents/rag_librarian.py 인덱서와 같은 상수다. 컨테이너 이미지에
# agents/ 가 없어(Dockerfile COPY 목록) import 대신 복제한다 - 모델 교체 시 함께.
EMBED_MODEL = "bge-m3"

# O(n^2) 쌍 비교 폭주 가드 - 24h 종목 창(수백 건)의 여유분. 이 위는 설계
# 전제(소규모 창) 밖이므로 조용히 느려지는 대신 명시적으로 거부한다.
MAX_ITEMS = 1500

# 내적 - 여전히 표준 라이브러리만이다. math.sumprod(3.12+, C 구현)가 있으면
# 우선한다: 실측(2026-08-01, 000660 48h 기사 996건) 컴프리헨션 ~14초가 1초
# 미만으로 준다. 구버전 폴백은 리스트 컴프리헨션.
try:
    from math import sumprod as _dot
except ImportError:  # Python < 3.12
    def _dot(a, b):
        return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# 군집 - 순수 함수 (임베딩 무관, 벡터 차원 무관)
# ---------------------------------------------------------------------------

def cluster_titles(items: Sequence, embeddings: Sequence[Sequence[float]],
                   *, threshold: float = 0.85) -> list[list[int]]:
    """코사인 유사도 >= threshold 쌍을 연결 성분(Union-Find)으로 묶는다.

    입력은 인덱스 기반: items[i] 의 벡터가 embeddings[i] 다(items 는 길이
    검증에만 쓴다). 출력은 인덱스 군집 목록 - 크기 내림차순(동률이면 첫 원소
    인덱스 오름차순), 각 군집 내부는 입력 순서를 유지한다. 결정론이다.

    ⚠ 연결 성분 특성: A~B, B~C 만 임계 이상이어도 A·C 는 한 군집이다.
    """
    n = len(items)
    if len(embeddings) != n:
        raise ValueError(f"items {n}개 != embeddings {len(embeddings)}개")
    if n > MAX_ITEMS:
        raise ValueError(f"n={n} > {MAX_ITEMS} - O(n^2) 폭주 방지, 창을 좁혀라")
    if not -1.0 <= threshold <= 1.0:
        raise ValueError(f"threshold {threshold} 는 코사인 범위(-1~1) 밖이다")
    if n == 0:
        return []

    # 사전 정규화 - 쌍마다 내적만 남긴다. 영벡터는 무엇과도 안 묶인다(norm 0
    # 유사도를 지어내지 않는다).
    unit: list[Optional[list[float]]] = []
    for vec in embeddings:
        norm = math.sqrt(_dot(vec, vec))
        unit.append([x / norm for x in vec] if norm > 0.0 else None)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # 경로 압축
            x = parent[x]
        return x

    for i in range(n):
        ui = unit[i]
        if ui is None:
            continue
        for j in range(i + 1, n):
            uj = unit[j]
            if uj is None or find(i) == find(j):
                # 이미 같은 성분이면 내적(1024차원)을 건너뛴다 - 결과 동일
                continue
            if _dot(ui, uj) >= threshold:
                parent[find(j)] = find(i)

    groups: dict[int, list[int]] = {}
    for i in range(n):                      # 입력 순서 순회 = 군집 내부 순서 유지
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: (-len(g), g[0]))


# ---------------------------------------------------------------------------
# 스토리 조립
# ---------------------------------------------------------------------------

def _published_key(article: dict) -> tuple:
    """정렬 키 (결측 여부, UTC ISO 문자열). 결측·파싱 불가는 최후순위."""
    v = article.get("published_at")
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc)
        return (0, v.isoformat())
    if v:
        s = str(v)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return (0, dt.astimezone(timezone.utc).isoformat())
            return (0, dt.isoformat())
        except ValueError:
            return (1, s)                   # 형식 모르는 문자열끼리는 사전순
    return (2, "")


def _embed_titles_ollama(titles: list[str], *,
                         base: Optional[str] = None) -> list[list[float]]:
    """기본 임베딩 - Ollama /api/embed 배치 (rag_librarian.embed_texts 와 동형)."""
    base = (base or os.environ.get("OLLAMA_BASE_URL",
                                   "http://127.0.0.1:11434")).rstrip("/")
    req = urllib.request.Request(
        base + "/api/embed", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"model": EMBED_MODEL, "input": titles}).encode())
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["embeddings"]


def build_stories(articles: list[dict], *, embed: Optional[Callable] = None,
                  threshold: float = 0.85) -> dict:
    """기사 목록 -> 스토리 군집 요약 (제목 임베딩 -> cluster_titles).

    articles: dict 목록 - title 로 임베딩하고 source·published_at·document_id 는
    있으면 쓴다(필드 유연). embed(list[str])->list[list[float]] 주입은 자체
    점검용(가짜 벡터), None 이면 Ollama bge-m3 배치.

    반환:
      stories: 크기 2 이상 군집만, 크기 내림차순.
        size / sources(중복 제거, 군집 내 첫 등장 순) /
        first_published·last_published(파싱된 published_at 의 최소·최대,
        전부 결측이면 None) /
        representative_title(**최초 게시** 기사 제목 - 가장 이른 published_at,
        결측은 최후순위, 동률이면 입력 순서가 빠른 기사) / member_ids
      singletons: 크기 1 군집(짝 없는 기사) 수
      clustered_ratio: 스토리로 묶인 기사 비율 = (전체-singletons)/전체
    """
    total = len(articles)
    out = {"version": STORY_CLUSTER_VERSION, "threshold": threshold,
           "total": total, "stories": [], "singletons": 0,
           "clustered_ratio": 0.0}
    if total == 0:
        return out                          # 빈 입력에 임베딩을 호출하지 않는다

    titles = [(a.get("title") or "").strip() for a in articles]
    embeddings = (embed or _embed_titles_ollama)(titles)
    clusters = cluster_titles(articles, embeddings, threshold=threshold)

    stories = []
    singletons = 0
    for members in clusters:
        if len(members) < 2:
            singletons += 1
            continue
        rep = min(members, key=lambda i: (_published_key(articles[i]), i))
        known = sorted(_published_key(articles[i])[1]
                       for i in members if _published_key(articles[i])[0] == 0)
        sources: list[str] = []
        for i in members:
            s = articles[i].get("source")
            if s and s not in sources:
                sources.append(s)
        stories.append({
            "size": len(members),
            "sources": sources,
            "first_published": known[0] if known else None,
            "last_published": known[-1] if known else None,
            "representative_title": articles[rep].get("title"),
            "member_ids": [articles[i].get("document_id", i) for i in members],
        })
    out["stories"] = stories
    out["singletons"] = singletons
    out["clustered_ratio"] = round((total - singletons) / total, 4)
    return out


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음 (가짜 임베딩)
# ---------------------------------------------------------------------------

def _rot(cos_v: float) -> list[float]:
    """단위원 위 2D 벡터 - [1,0] 과의 코사인이 정확히 cos_v 가 되게 만든다."""
    return [cos_v, math.sqrt(1.0 - cos_v * cos_v)]


def _check_identical_and_boundary():
    # 동일 벡터는 무조건 한 군집 (sim=1.0)
    same = cluster_titles(["a", "b", "c"], [[1.0, 0.0]] * 3)
    assert same == [[0, 1, 2]], same
    # 임계 경계: 0.849 < 0.85 는 분리, 0.851 >= 0.85 는 결합
    apart = cluster_titles(["a", "b"], [[1.0, 0.0], _rot(0.849)])
    assert apart == [[0], [1]], apart
    joined = cluster_titles(["a", "b"], [[1.0, 0.0], _rot(0.851)])
    assert joined == [[0, 1]], joined
    # 영벡터는 무엇과도 안 묶인다 - 유사도를 지어내지 않는다
    zero = cluster_titles(["a", "b"], [[1.0, 0.0], [0.0, 0.0]])
    assert zero == [[0], [1]], zero
    print("  동일 벡터·임계 경계      OK")


def _check_chain_component():
    # 연쇄 연결: A~B=0.86, B~C=0.86 이 임계 이상이면 A~C=0.4792(<0.85)여도
    # 셋은 한 군집이다 - 연결 성분 특성(모듈 docstring 의 의도된 동작)
    a, b, c = [1.0, 0.0], _rot(0.86), _rot(2 * 0.86 * 0.86 - 1)
    direct_ac = sum(x * y for x, y in zip(a, c))
    assert direct_ac < 0.85, f"전제 깨짐: A~C={direct_ac}"
    chain = cluster_titles(["A", "B", "C"], [a, b, c])
    assert chain == [[0, 1, 2]], chain
    # 크기 내림차순 + 군집 내부 입력 순서 유지 - _rot(0.6)=[0.6,0.8] 은
    # [1,0]과 0.6, [0,1]과 0.8 로 어느 쪽 임계에도 못 미친다
    mixed = cluster_titles(list("abcd"), [_rot(0.6), a, [0.0, 1.0], a])
    assert mixed == [[1, 3], [0], [2]], mixed
    print("  연쇄 연결(성분)·정렬     OK")


def _check_build_stories_representative():
    vec = {"하이닉스 상한가 마감": [1.0, 0.0],
           "SK하이닉스 가격제한폭까지 급등": _rot(0.93),
           "[속보] 하이닉스 30% 상승": _rot(0.90),
           "코스피 기관 순매수 전환": [0.0, 1.0],
           "환율 1,380원대 하락": [-1.0, 0.0]}
    calls = []

    def fake_embed(titles):
        calls.append(list(titles))
        return [vec[t] for t in titles]

    arts = [  # 입력은 최신순(API 와 동일) - 대표는 그래도 최초 게시여야 한다
        {"title": "[속보] 하이닉스 30% 상승", "source": "ls_news",
         "published_at": "2026-07-31T06:40:00+00:00", "document_id": "d3"},
        {"title": "코스피 기관 순매수 전환", "source": "naver_apihub",
         "published_at": "2026-07-31T06:20:00+00:00", "document_id": "d4"},
        {"title": "SK하이닉스 가격제한폭까지 급등", "source": "naver_apihub",
         "published_at": "2026-07-31T06:10:00+00:00", "document_id": "d2"},
        {"title": "하이닉스 상한가 마감", "source": "naver_apihub",
         # KST 표기 - UTC 로 정규화해 비교해야 최초(05:50Z)가 맞다
         "published_at": "2026-07-31T14:50:00+09:00", "document_id": "d1"},
        {"title": "환율 1,380원대 하락", "source": "ls_news",
         "published_at": "2026-07-31T05:00:00+00:00", "document_id": "d5"},
    ]
    out = build_stories(arts, embed=fake_embed)
    assert calls == [[a["title"] for a in arts]], "제목 배치 1회 임베딩이 아니다"
    assert out["total"] == 5 and len(out["stories"]) == 1, out
    st = out["stories"][0]
    assert st["size"] == 3 and st["member_ids"] == ["d3", "d2", "d1"], st
    assert st["sources"] == ["ls_news", "naver_apihub"], st  # 중복 제거·등장 순
    assert st["representative_title"] == "하이닉스 상한가 마감", \
        f"대표 제목이 최초 게시(05:50Z)가 아니다: {st['representative_title']}"
    assert st["first_published"] == "2026-07-31T05:50:00+00:00", st
    assert st["last_published"] == "2026-07-31T06:40:00+00:00", st
    assert out["singletons"] == 2 and out["clustered_ratio"] == 0.6, out
    # published_at 전부 결측이면 입력 순서가 빠른 기사가 대표다
    nodate = build_stories([{"title": "하이닉스 상한가 마감", "source": "x"},
                            {"title": "SK하이닉스 가격제한폭까지 급등", "source": "y"}],
                           embed=lambda ts: [vec[t] for t in ts])
    st2 = nodate["stories"][0]
    assert st2["representative_title"] == "하이닉스 상한가 마감", st2
    assert st2["first_published"] is None and st2["last_published"] is None, st2
    print("  대표=최초 게시·요약      OK")


def _check_guards_and_empty():
    # n 폭주 거부 - 임베딩 개수 불일치도 거부
    try:
        cluster_titles(list(range(MAX_ITEMS + 1)),
                       [[1.0, 0.0]] * (MAX_ITEMS + 1))
        raise AssertionError("n>1500 이 통과했다 - O(n^2) 폭주 가드 깨짐")
    except ValueError:
        pass
    try:
        cluster_titles(["a", "b"], [[1.0, 0.0]])
        raise AssertionError("길이 불일치가 통과했다")
    except ValueError:
        pass
    # 빈 입력 - 군집도 스토리도 없고, 임베딩을 호출하지 않는다
    assert cluster_titles([], []) == []

    def must_not_call(_):
        raise AssertionError("빈 입력에 임베딩을 호출했다")

    empty = build_stories([], embed=must_not_call)
    assert empty["stories"] == [] and empty["singletons"] == 0, empty
    assert empty["clustered_ratio"] == 0.0 and empty["total"] == 0, empty
    print("  폭주 거부·빈 입력        OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{STORY_CLUSTER_VERSION} 자체 점검 (네트워크 없음, 가짜 임베딩)")
    _check_identical_and_boundary()
    _check_chain_component()
    _check_build_stories_representative()
    _check_guards_and_empty()
    print("스토리 군집 4개 영역 통과. 실측은 research-api /evidence/stories")
