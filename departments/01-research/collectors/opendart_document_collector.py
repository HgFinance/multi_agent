#!/usr/bin/env python3
"""공시 원문 Archive - DART 2019003 원본 ZIP 을 Private Storage 로, 지문은 DB 로.

담당: 재일 (리서치/퀀트)
근거: TEAM_JAEIL 가이드 Sprint J2 "공시 원본 Archive, Version, 정정 관계"(부분)
      + J0 "Private Storage Bucket"(미착수) - 두 항목을 함께 해소한다.
      DART 는 전문 저장·임베딩이 허용된 Source 다(Registry allowed_uses).

▶ 구조
  research.documents 중 원문 없는 것(document_versions 부재)
    -> DART document.xml(2019003) 로 원본 ZIP 수신 (수정 없이 그대로)
    -> Supabase Storage 'research-documents-private' 업로드 (버킷 실측 2026-07-31)
    -> research.document_versions 에 sha256 지문·경로·크기 기록 (version=1)

▶ 원칙
  - 원본은 바이트 그대로 둔다. 파싱·본문 추출은 후속(parser_name 이 그때 채워짐).
  - ZIP 이 아닌 응답(DART 오류 JSON)은 적재하지 않고 사유별로 센다 - 오류
    본문을 원문인 척 저장하면 Archive 가 오염된다.
  - 재실행 안전: 후보 질의가 version 있는 문서를 제외하므로 멱등이다.

사용
  python collectors/opendart_document_collector.py            # 자체 점검
  python collectors/opendart_document_collector.py --collect --limit 50
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

from source_registry import load_project_env  # noqa: E402

COLLECTOR_VERSION = "research-opendart-document-v1"
BUCKET = "research-documents-private"
DART_URL = "https://opendart.fss.or.kr/api/document.xml"
RATE_PER_SEC = 1.0          # 보수적으로 - 공시검색과 별개 Endpoint 지만 같은 키다
LICENSE_SCOPE = "PRIVATE_ARCHIVE"


@dataclass
class ArchiveStats:
    archived: int = 0
    skipped_not_zip: int = 0
    failed: int = 0
    reasons: dict = field(default_factory=dict)

    def note(self, key: str) -> None:
        self.reasons[key] = self.reasons.get(key, 0) + 1

    def summary(self) -> str:
        s = f"적재 {self.archived} / 비ZIP 제외 {self.skipped_not_zip} / 실패 {self.failed}"
        if self.reasons:
            s += " (" + ", ".join(f"{k}:{v}" for k, v in sorted(self.reasons.items())) + ")"
        return s


def looks_like_zip(data: bytes) -> bool:
    return data[:2] == b"PK"


def classify_non_zip(data: bytes) -> str:
    """DART 가 ZIP 대신 준 것의 정체. 오류는 JSON 또는 XML 로 온다(실측: 원문
    미준비가 XML <status>014</status> '파일이 존재하지 않습니다')."""
    text = data.decode("utf-8", "replace")
    try:
        return "DART_" + str(json.loads(text).get("status", "UNKNOWN"))
    except ValueError:
        pass
    import re
    m = re.search(r"<status>(\w+)</status>", text)
    if m:
        return "DART_" + m.group(1)
    return "NOT_ZIP_NOT_JSON"


def object_path_for(rcept_no: str) -> str:
    # rcept_no 앞 8자리가 접수일 - 날짜별 폴더로 나눠 목록 조회를 견딜 크기로 유지
    return f"dart/{rcept_no[:8]}/{rcept_no}.zip"


def fetch_original(api_key: str, rcept_no: str, *, timeout: float = 30.0) -> bytes:
    qs = urllib.parse.urlencode({"crtfc_key": api_key, "rcept_no": rcept_no})
    with urllib.request.urlopen(f"{DART_URL}?{qs}", timeout=timeout) as r:
        return r.read()


def upload(env: dict, path: str, data: bytes) -> None:
    base, key = env["SUPABASE_URL"].rstrip("/"), env["SUPABASE_SERVICE_ROLE_KEY"]
    req = urllib.request.Request(
        f"{base}/storage/v1/object/{BUCKET}/{path}", method="POST", data=data,
        headers={"Authorization": f"Bearer {key}", "apikey": key,
                 "Content-Type": "application/zip", "x-upsert": "true"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status not in (200, 201):
            raise RuntimeError(f"Storage 업로드 실패: {r.status}")


def _collect(limit: int) -> int:
    from reference_repository import SupabaseReferenceRepository

    env = load_project_env()
    api_key = (env.get("OPEN_DART_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPEN_DART_API_KEY 가 없다")

    ref = SupabaseReferenceRepository()
    stats = ArchiveStats()
    try:
        with ref._conn.cursor() as cur:
            # issuer 연결·최신 우선. version 이 이미 있는 문서는 후보에서 빠진다(멱등)
            cur.execute("""
                select d.document_id, d.external_id, d.published_at
                from research.documents d
                join reference.data_sources s using (source_id)
                where s.source_code = 'opendart'
                  and not exists (select 1 from research.document_versions v
                                  where v.document_id = d.document_id)
                  -- 갓 나온 공시는 원문 파일 생성 전이라 014 가 난다(실측) -
                  -- 2시간 유예. 미준비분은 version 이 안 생기므로 다음 실행이
                  -- 자연히 재시도한다.
                  and d.observed_at < now() - interval '2 hours'
                order by (d.issuer_id is not null) desc, d.observed_at desc
                limit %s
            """, (limit,))
            cands = cur.fetchall()
        print(f"  후보 {len(cands)}건 (원문 미보유·최신 우선)")

        for i, (doc_id, rcept_no, published_at) in enumerate(cands, 1):
            time.sleep(1.0 / RATE_PER_SEC)
            try:
                data = fetch_original(api_key, rcept_no)
            except Exception as e:
                stats.failed += 1
                stats.note(type(e).__name__)
                continue
            if not looks_like_zip(data):
                stats.skipped_not_zip += 1
                stats.note(classify_non_zip(data))
                continue

            digest = hashlib.sha256(data).hexdigest()
            path = object_path_for(rcept_no)
            try:
                upload(env, path, data)
            except Exception as e:
                stats.failed += 1
                stats.note("UPLOAD_" + type(e).__name__)
                continue
            with ref._conn.cursor() as cur:
                # on conflict: 스케줄러 재기동 재실행과 수동 만회 실행이 겹치면
                # 같은 후보를 둘 다 처리한다 - 2026-07-31 실측에서 UniqueViolation
                # 으로 잡 전체가 죽었다. 멱등 계약(겹쳐도 안전)을 여기서 지킨다.
                cur.execute("""
                    insert into research.document_versions
                      (document_id, version, content_hash, object_path, media_type,
                       byte_size, license_scope, published_at, observed_at)
                    values (%s, 1, %s, %s, 'application/zip', %s, %s, %s, %s)
                    on conflict (document_id, version) do nothing
                """, (doc_id, digest, path, len(data), LICENSE_SCOPE,
                      published_at, datetime.now(timezone.utc)))
                inserted = cur.rowcount
            ref._conn.commit()
            if inserted:
                stats.archived += 1
            else:
                stats.note("VERSION_EXISTS")  # 동시 실행이 먼저 적재 - 멱등 통과
            if i % 20 == 0 or i == len(cands):
                print(f"    [{i}/{len(cands)}] {stats.summary()}")
    finally:
        ref.close()
    print(f"  완료: {stats.summary()}")
    return 0 if stats.archived or not cands else 1


# ---------------------------------------------------------------------------
# 자체 점검 - 호출·DB 없이
# ---------------------------------------------------------------------------

def _check_zip_gate():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.xml", "<원문/>")
    assert looks_like_zip(buf.getvalue())
    assert not looks_like_zip(b'{"status":"013","message":"no data"}')
    assert classify_non_zip(b'{"status":"013"}') == "DART_013"
    assert classify_non_zip(
        b'<?xml version="1.0"?><result><status>014</status>'
        b'<message>\xed\x8c\x8c\xec\x9d\xbc\xec\x9d\xb4 \xec\x97\x86\xec\x9d\x8c</message></result>'
    ) == "DART_014", "XML 오류 분류 실패 (실측 014 회귀)"
    assert classify_non_zip(b"<html>err</html>") == "NOT_ZIP_NOT_JSON"
    print("  ZIP 판별/오류 분류       OK")


def _check_object_path():
    p = object_path_for("20260731000123")
    assert p == "dart/20260731/20260731000123.zip", p
    print("  경로 규칙                OK")


def _check_hash_stable():
    a = hashlib.sha256(b"PK\x03\x04same").hexdigest()
    b = hashlib.sha256(b"PK\x03\x04same").hexdigest()
    assert a == b
    print("  지문 안정성              OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        a = sys.argv
        lim = int(a[a.index("--limit") + 1]) if "--limit" in a else 50
        print(f"{COLLECTOR_VERSION} 원문 Archive (limit {lim})")
        raise SystemExit(_collect(lim))

    print(f"{COLLECTOR_VERSION} 자체 점검 (호출 없음)")
    _check_zip_gate()
    _check_object_path()
    _check_hash_stable()
    print("원문 Archive 3개 영역 통과. 수집은 --collect --limit N")
