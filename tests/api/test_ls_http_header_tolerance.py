"""LS의 NUL 패딩 응답 헤더를 httpx 경로가 견디는지 확인한다.

2026-08-26 회계 부서 카드가 통째로 "저장된 기록"으로 떨어졌다. 원인은 브로커
장애가 아니라 파서다 - LS가 연속조회 키를 고정폭으로 돌려주며 남는 자리를
NUL로 채우는데(`tr_cont_key: 202608210000002 \x003`), h11이 그 헤더를 가진
정상 200 응답을 `RemoteProtocolError: illegal header line`으로 버렸다.

실제 소켓을 세워 그 바이트를 그대로 흘려 보낸다. MockTransport로는 h11을
지나가지 않아 이 결함이 재현되지 않는다 - 회귀를 잡으려면 소켓이어야 한다.
"""
from __future__ import annotations

import asyncio
import gzip
import socket
import sys
import threading
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "departments" / "03-risk" / "integrations"))

from ls_http import (  # noqa: E402
    LSHeaderTolerantAsyncTransport,
    LSHeaderTolerantTransport,
    _AsyncSanitizedBackend,
    _SanitizedBackend,
    ls_async_client,
    ls_client,
)


# 실측 바이트열. 값 안의 \x00 이 h11 이 거부하는 지점이다.
MALFORMED_HEADER = b"tr_cont_key: 202608210000002 \x003\r\n"
BODY = b'{"rsp_cd":"00000","rsp_msg":"ok"}'


def _response(body: bytes, *, encoding: bytes | None = None) -> bytes:
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\ntr_cont: N\r\n"
    head += MALFORMED_HEADER
    if encoding:
        head += b"Content-Encoding: " + encoding + b"\r\n"
    head += b"Content-Length: " + str(len(body)).encode() + b"\r\n"
    head += b"Connection: close\r\n\r\n"
    return head + body


class _RawServer:
    """요청을 무시하고 정해진 바이트를 그대로 내보내는 1회용 서버."""

    def __init__(self, payload: bytes, *, chunk: int | None = None) -> None:
        self._payload = payload
        self._chunk = chunk
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        with conn:
            conn.settimeout(5)
            self._drain_request(conn)
            if self._chunk is None:
                conn.sendall(self._payload)
            else:
                for start in range(0, len(self._payload), self._chunk):
                    conn.sendall(self._payload[start : start + self._chunk])
            # 보낸 것을 클라이언트가 다 읽기 전에 닫으면 RST 로 끊긴다.
            try:
                conn.shutdown(socket.SHUT_WR)
                while conn.recv(65536):
                    pass
            except OSError:  # pragma: no cover - 클라이언트가 먼저 닫은 경우
                pass
        self._sock.close()

    @staticmethod
    def _drain_request(conn: socket.socket) -> None:
        """요청을 본문까지 다 읽는다 - 남겨 두고 닫으면 응답이 RST 에 묻힌다."""
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buffer += chunk
        head, _, body = buffer.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                return
            body += chunk

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/stock/accno"


def test_plain_httpx_still_rejects_the_header() -> None:
    """전제 확인: 고치지 않은 클라이언트는 여전히 이 응답에서 죽는다.

    이게 통과를 멈추면 h11이 스스로 느슨해진 것이고, 그때는 우리 우회를 걷어낼
    수 있다는 신호다.
    """
    server = _RawServer(_response(BODY))
    with httpx.Client(timeout=5) as client:
        try:
            client.post(server.url, json={})
        except httpx.RemoteProtocolError as exc:
            assert "illegal header line" in str(exc)
        else:  # pragma: no cover - 회귀 신호
            raise AssertionError("h11이 NUL 헤더를 통과시켰다 - 우회를 재검토할 것")


def test_ls_client_reads_the_body() -> None:
    server = _RawServer(_response(BODY))
    with ls_client(timeout=5) as client:
        response = client.post(server.url, json={})
    assert response.status_code == 200
    assert response.json()["rsp_cd"] == "00000"


def test_header_terminator_split_across_reads() -> None:
    """헤더 끝(\\r\\n\\r\\n)이 read 경계에 걸려도 본문 시작을 놓치지 않는다."""
    server = _RawServer(_response(BODY), chunk=7)
    with ls_client(timeout=5) as client:
        response = client.post(server.url, json={})
    assert response.json()["rsp_cd"] == "00000"


def test_compressed_body_is_not_rewritten() -> None:
    """치환은 헤더 구간에서만 일어난다 - 압축 본문에 손대면 복원이 깨진다."""
    raw = BODY * 40
    server = _RawServer(_response(gzip.compress(raw), encoding=b"gzip"), chunk=13)
    with ls_client(timeout=5) as client:
        response = client.post(server.url, json={})
    assert response.content == raw


def test_async_client_reads_the_body() -> None:
    server = _RawServer(_response(BODY))

    async def call() -> httpx.Response:
        async with ls_async_client(timeout=5) as client:
            return await client.post(server.url, json={})

    response = asyncio.run(call())
    assert response.json()["rsp_cd"] == "00000"


def test_transport_actually_wraps_the_httpcore_backend() -> None:
    """httpx/httpcore 업그레이드가 훅 지점을 옮기면 여기서 먼저 깨진다.

    조용히 원래 백엔드로 되돌아가면 장애가 그대로 돌아온다.
    """
    assert isinstance(
        LSHeaderTolerantTransport()._pool._network_backend, _SanitizedBackend
    )
    assert isinstance(
        LSHeaderTolerantAsyncTransport()._pool._network_backend,
        _AsyncSanitizedBackend,
    )


def test_ls_callers_use_the_tolerant_client() -> None:
    """실제 LS 호출 경로가 맨 httpx로 되돌아가지 않았는지 본다."""
    for path in (
        ROOT / "apps" / "api" / "ls_account_stream.py",
        ROOT / "departments" / "03-risk" / "integrations" / "ls_openapi.py",
        ROOT / "departments" / "02-trading" / "broker" / "ls_paper_broker.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "httpx.AsyncClient(" not in source, path
        assert "httpx.Client(timeout=" not in source, path


if __name__ == "__main__":
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
            print("ok", name)
