#!/usr/bin/env python3
"""RES-08 RAG 사서 - 공시 원문을 청크·임베딩해 검색 가능한 Evidence 로 굳힌다.

소유: 재일 (리서치본부, RES-08 rag-librarian-evidence-curator)
근거: supabase/migrations/20260729000300 (research.evidence_chunks - 1024 차원은
      20260801001200 에서 변경), collectors/opendart_document_collector.py
      (원문 ZIP Archive), source_registry UseScope.EMBEDDING (Open DART 허용),
      TEAM_JAEIL Sprint J4 모델 결정(임베딩 = bge-m3, 로컬 무료)

▶ 설계 결정
  - **인덱싱은 전부 결정론이다.** 청크 경계·해시·임베딩 호출·적재에 LLM 이
    관여하지 않는다. RES-08 페르소나의 "선별·큐레이션" 판단 층은 후속이고,
    이 파일은 그 전제가 되는 사서의 결정론 절반이다.
  - **라이선스 게이트가 먼저다.** 임베딩은 UseScope.EMBEDDING 이 허용된
    소스(현재 opendart)만. 게이트 통과가 코드 경로에 박혀 있어 뉴스 스니펫
    등을 실수로 임베딩할 수 없다.
  - 임베딩 모델 bge-m3(1024, 다국어) - Ollama /api/embed. 모델·차원을 행마다
    기록해(embedding_model/version) 모델 교체 시 재임베딩 대상을 식별한다.
  - DART 원문은 ZIP 안 XML 이다. 태그 제거 + 공백 정리로 본문을 얻는다 -
    표 구조 보존 같은 정밀 추출은 후속(문서에 남김). token_count 는 세지
    않았으므로 None 이다(추정치를 채우지 않는다).
  - 멱등: 이미 청크가 있는 document_version 은 건너뛴다. 같은 버전을 다시
    돌려도 (version, chunk_index) unique 가 이중 적재를 막는다.

사용
  python agents/rag_librarian.py                       # 자체 점검 (네트워크 없음)
  python agents/rag_librarian.py --index --limit 50    # 원문 -> 청크·임베딩 적재
  python agents/rag_librarian.py --search "유상증자 결정"  # 코사인 검색
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collectors"))

LIBRARIAN_VERSION = "research-rag-librarian-v1"
EMBED_MODEL = "bge-m3"
EMBED_DIMS = 1024
EMBED_VERSION = f"{EMBED_MODEL}@ollama/1024"
BUCKET = "research-documents-private"
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
EMBED_BATCH = 16


# ---------------------------------------------------------------------------
# 본문 추출·청크 - 순수 함수
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t　]+")
# style/script 는 태그만 지우면 내부 텍스트(CSS/JS)가 본문으로 남는다 -
# 실측 2026-08-01: 발췌 머리가 ".xforms { font-family: 돋움체 }" 로 오염됐다
_BLOCK_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.S | re.I)
_CSS_LINE_RE = re.compile(r"^[.#@\w\-,:*\[\]]+\s*\{|^\s*[\w\-]+\s*:\s*[^;]+;\s*}?$")


def extract_text_from_zip(data: bytes) -> str:
    """DART 원문 ZIP -> 본문 텍스트. style/script 블록·태그 제거·엔티티 복원."""
    out: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in sorted(z.namelist()):
            if not name.lower().endswith((".xml", ".html", ".htm")):
                continue
            raw = z.read(name)
            for enc in ("utf-8", "cp949", "euc-kr"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    text = ""
            if not text:
                continue
            text = _BLOCK_RE.sub(" ", text)
            text = unescape(_TAG_RE.sub(" ", text))
            text = _WS_RE.sub(" ", text)
            lines = [ln.strip() for ln in text.splitlines()]
            out.append("\n".join(ln for ln in lines
                                 if ln and not _CSS_LINE_RE.match(ln)))
    return "\n".join(out).strip()


def chunk_text(text: str, *, max_chars: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """문단 우선 청크. 경계: max_chars 이하, 인접 청크는 overlap 만큼 겹친다.

    겹침은 검색 시 경계 걸친 문맥이 잘리는 것을 줄이기 위한 표준 장치다.
    """
    if not text:
        return []
    if max_chars <= overlap:
        raise ValueError("max_chars 는 overlap 보다 커야 한다")
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            # 문단·문장 경계로 당겨 자른다 - 없으면 그대로 하드 컷
            cut = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if cut > start + max_chars // 2:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def vector_literal(vec: list[float]) -> str:
    """pgvector 입력 형식 '[v1,v2,...]' - 차원을 여기서 강제한다."""
    if len(vec) != EMBED_DIMS:
        raise ValueError(f"임베딩 차원 {len(vec)} != {EMBED_DIMS} - 모델 확인")
    return "[" + ",".join(f"{v:.7g}" for v in vec) + "]"


# ---------------------------------------------------------------------------
# 임베딩 (Ollama)
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str], *, base: str | None = None) -> list[list[float]]:
    import os

    base = (base or os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
    req = urllib.request.Request(
        base + "/api/embed", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode())
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["embeddings"]


# ---------------------------------------------------------------------------
# 인덱싱 파이프라인 (결정론 - Storage 읽기 + 청크 + 임베딩 + 적재)
# ---------------------------------------------------------------------------

def index_documents(limit: int = 50) -> int:
    import psycopg2

    from source_registry import SourceRegistry, UseScope, load_project_env

    env = load_project_env()
    # 라이선스 게이트 - EMBEDDING 이 허용 안 된 소스는 여기서 예외로 끊긴다
    SourceRegistry(env=env).check_use("opendart", UseScope.EMBEDDING)

    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    base, key = env["SUPABASE_URL"].rstrip("/"), env["SUPABASE_SERVICE_ROLE_KEY"]
    done = skipped = failed = 0
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select v.document_version_id, v.object_path, v.license_scope,
                       d.published_at, d.title
                from research.document_versions v
                join research.documents d using (document_id)
                join reference.data_sources s using (source_id)
                where s.source_code = 'opendart'
                  and not exists (select 1 from research.evidence_chunks c
                                  where c.document_version_id = v.document_version_id)
                order by v.observed_at desc limit %s
            """, (limit,))
            targets = cur.fetchall()
        print(f"{LIBRARIAN_VERSION}: 대상 {len(targets)}건 (미청크 원문, 최신 우선)",
              flush=True)

        for i, (vid, path, scope, published_at, title) in enumerate(targets, 1):
            try:
                req = urllib.request.Request(
                    f"{base}/storage/v1/object/{BUCKET}/{path}",
                    headers={"Authorization": f"Bearer {key}", "apikey": key})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                text = extract_text_from_zip(data)
                # 문서 내 동일 내용 반복(공시 상용구)은 한 번만 - (version,
                # content_hash) unique 가 있고, 같은 문장을 두 번 검색에 내줄
                # 이유도 없다 (실측: 40건 중 1건이 이 충돌로 실패했었다)
                seen: set = set()
                chunks = []
                for idx, piece in enumerate(chunk_text(text)):
                    h = content_sha(piece)
                    if h in seen:
                        continue
                    seen.add(h)
                    chunks.append((idx, piece, h))
                if not chunks:
                    skipped += 1
                    continue
                observed = datetime.now(timezone.utc)
                with conn.cursor() as cur:
                    for bi in range(0, len(chunks), EMBED_BATCH):
                        batch = chunks[bi:bi + EMBED_BATCH]
                        vecs = embed_texts([p for _, p, _ in batch])
                        for (cidx, piece, chash), vec in zip(batch, vecs):
                            cur.execute("""
                                insert into research.evidence_chunks
                                  (document_version_id, chunk_index, content,
                                   content_hash, embedding, embedding_model,
                                   embedding_version, license_scope,
                                   published_at, observed_at, metadata)
                                values (%s, %s, %s, %s, %s::extensions.vector,
                                        %s, %s, %s, %s, %s, %s::jsonb)
                                on conflict (document_version_id, chunk_index)
                                do nothing
                            """, (vid, cidx, piece, chash,
                                  vector_literal(vec), EMBED_MODEL, EMBED_VERSION,
                                  scope or "EMBEDDING", published_at, observed,
                                  json.dumps({"title": (title or "")[:120],
                                              "chunker": f"{CHUNK_CHARS}/{CHUNK_OVERLAP}"},
                                             ensure_ascii=False)))
                conn.commit()
                done += 1
                if i % 10 == 0 or i == len(targets):
                    print(f"  [{i}/{len(targets)}] 적재 {done} / 본문없음 {skipped} / "
                          f"실패 {failed}", flush=True)
            except Exception as e:
                conn.rollback()
                failed += 1
                print(f"  ⚠ {path}: {type(e).__name__} {str(e)[:80]}", flush=True)
        print(f"완료: 문서 {done} 청크·임베딩 / 본문없음 {skipped} / 실패 {failed}",
              flush=True)
        return 0 if failed == 0 else 1
    finally:
        conn.close()


def recent_excerpts_for_symbol(symbol: str, k: int = 3) -> list[dict]:
    """종목의 최근 공시 머리 청크(k건) - Packet 의 공시 근거 발췌용.

    시맨틱 검색이 아니라 결정론 조회다: 발행사가 확인된 문서의
    chunk_index=0(문서 머리 - 제목·핵심 결정 사항)을 게재일 역순으로.
    임베딩·LLM 무관이라 재현 가능하고, 다른 회사 공시가 섞일 수 없다.

    연결 경로는 documents.issuer_id -> instruments.issuer_id 다 - 공시는
    발행사 단위로 들어오고 document_instruments 는 뉴스 전용이다(실측
    2026-08-01: 공시 3,380건 전부 issuer_id 보유, instrument 링크 0).
    """
    import psycopg2

    from source_registry import load_project_env

    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select d.title, c.published_at, c.content
                from research.evidence_chunks c
                join research.document_versions v using (document_version_id)
                join research.documents d using (document_id)
                join reference.instruments i on i.issuer_id = d.issuer_id
                join reference.instrument_symbols sy
                     on sy.instrument_id = i.instrument_id and sy.is_primary
                where sy.symbol = %s and c.chunk_index = 0
                order by c.published_at desc nulls last limit %s
            """, (symbol, k))
            return [{"title": t, "published_at": None if p is None else str(p)[:10],
                     "excerpt": c[:400]} for t, p, c in cur.fetchall()]
    finally:
        conn.close()


def search(query: str, k: int = 5) -> list[dict]:
    import psycopg2

    from source_registry import load_project_env

    env = load_project_env()
    qvec = vector_literal(embed_texts([query])[0])
    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select c.content, c.published_at, c.metadata->>'title',
                       1 - (c.embedding <=> %s::extensions.vector) as cosine_sim
                from research.evidence_chunks c
                where c.embedding is not null
                order by c.embedding <=> %s::extensions.vector
                limit %s
            """, (qvec, qvec, k))
            return [{"title": t, "published_at": str(p), "sim": round(s, 4),
                     "content": c[:240]} for c, p, t, s in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_chunker():
    text = "가나다. " * 500                     # 4,000자
    chunks = chunk_text(text)
    assert all(len(c) <= CHUNK_CHARS for c in chunks)
    assert len(chunks) >= 3
    # 겹침: 다음 청크의 머리가 앞 청크의 꼬리와 겹친다
    assert chunks[1][:30] in chunks[0][-(CHUNK_OVERLAP + 60):]
    assert chunk_text("") == []
    assert chunk_text("짧은 문서") == ["짧은 문서"]
    try:
        chunk_text("x", max_chars=100, overlap=100)
        raise AssertionError("overlap >= max_chars 가 통과했다")
    except ValueError:
        pass
    print("  청크 경계·겹침           OK")


def _check_zip_extraction():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.xml",
                   "<style>.xforms * { font-family: 돋움체;}</style>"
                   "<script>var x=1;</script>"
                   "<BODY><P>유상증자 &amp; 결정</P><TABLE>금액 100</TABLE></BODY>")
        z.writestr("skip.bin", b"\x00\x01")
    text = extract_text_from_zip(buf.getvalue())
    assert "유상증자 & 결정" in text and "금액 100" in text
    assert "<P>" not in text and "\x00" not in text
    # 실측 오염 재발 방지 - CSS/JS 는 본문이 아니다
    assert "font-family" not in text and "돋움체" not in text and "var x" not in text
    print("  ZIP/XML 본문 추출        OK")


def _check_vector_literal():
    lit = vector_literal([0.1] * EMBED_DIMS)
    assert lit.startswith("[0.1,") and lit.endswith("]")
    assert lit.count(",") == EMBED_DIMS - 1
    try:
        vector_literal([0.1] * 768)
        raise AssertionError("차원 불일치가 통과했다")
    except ValueError:
        pass
    print("  pgvector 리터럴·차원     OK")


def _check_license_gate():
    from source_registry import SourceRegistry, SourceUseNotAllowed, UseScope

    r = SourceRegistry(env={"OPEN_DART_API_KEY": "k" * 40,
                            "NAVER_CLIENT_ID": "x", "NAVER_CLIENT_SECRET": "y"})
    r.check_use("opendart", UseScope.EMBEDDING)          # 허용
    for src in ("naver_apihub", "ls_news"):
        try:
            r.check_use(src, UseScope.EMBEDDING)
            raise AssertionError(f"{src} 임베딩이 허용됐다 - 라이선스 위반 경로")
        except SourceUseNotAllowed:
            pass
    print("  라이선스 게이트          OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--index" in sys.argv:
        a = sys.argv
        lim = int(a[a.index("--limit") + 1]) if "--limit" in a else 50
        raise SystemExit(index_documents(lim))
    if "--search" in sys.argv:
        q = sys.argv[sys.argv.index("--search") + 1]
        for hit in search(q):
            print(f"[{hit['sim']}] {hit['title']} ({hit['published_at'][:10]})")
            print(f"   {hit['content']}")
        raise SystemExit(0)

    print(f"{LIBRARIAN_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_chunker()
    _check_zip_extraction()
    _check_vector_literal()
    _check_license_gate()
    print("RAG 사서 4개 영역 통과. 인덱싱은 --index, 검색은 --search")
