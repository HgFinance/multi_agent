"""httpx transports that survive LS증권's NUL-padded response headers.

LS returns the continuation key of a paged accno TR as a fixed-width field and
pads the slack with NUL (0x00).  Measured 2026-08-26 against CDPCQ04700, the
response header line arrives on the wire as::

    tr_cont_key: 202608210000002 \x003

h11 -- the HTTP/1.1 parser behind httpx -- refuses NUL and whitespace inside a
header value on purpose (see ``h11/_abnf.py``: "those are often treated as
meta-characters and letting them through can lead to nasty issues like SSRF").
So a perfectly good ``200`` with a complete JSON body is discarded as
``httpx.RemoteProtocolError: illegal header line``.  This is why the research
collector, which calls the same TRs through ``urllib.request`` (stdlib
``http.client`` parses headers with the lenient ``email`` parser), never sees
the failure while every httpx-based caller does.

h11 offers no per-connection leniency knob: ``header_field_re`` is a module
global, so relaxing it would also relax the **inbound** parser uvicorn uses to
read requests from our own users.  The repair therefore sits between the socket
and h11, and stays as narrow as the defect:

* only the response header block is rewritten -- the body is handed through
  untouched, so a gzip/deflate/br payload can never be corrupted;
* NUL becomes a space, one byte for one byte, so ``Content-Length`` still
  describes the bytes h11 receives;
* writing a request re-arms the header state, so keep-alive connections carrying
  several responses get each header block sanitized and no body ever does.

Use :func:`ls_client` / :func:`ls_async_client` for every httpx call to LS.
"""

from __future__ import annotations

from typing import Any

import httpx

_HEADER_END = b"\r\n\r\n"
_CARRY = len(_HEADER_END) - 1


class _HeaderSanitizer:
    """Replace NUL with space until the response header block has been seen."""

    def __init__(self) -> None:
        self.rearm()

    def rearm(self) -> None:
        self._in_headers = True
        self._carry = b""

    def feed(self, data: bytes) -> bytes:
        if not data or not self._in_headers:
            return data
        carry = self._carry
        index = (carry + data).find(_HEADER_END)
        if index < 0:
            # The terminator may straddle this read; keep just enough of the
            # tail to recognise it on the next one.
            self._carry = (carry + data)[-_CARRY:]
            return data.replace(b"\x00", b" ")
        self._in_headers = False
        self._carry = b""
        split = max(0, index + len(_HEADER_END) - len(carry))
        return data[:split].replace(b"\x00", b" ") + data[split:]


class _SanitizedStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._sanitizer = _HeaderSanitizer()

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._sanitizer.feed(self._stream.read(max_bytes, timeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._sanitizer.rearm()
        self._stream.write(buffer, timeout)

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> "_SanitizedStream":
        return _SanitizedStream(
            self._stream.start_tls(ssl_context, server_hostname, timeout)
        )

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _AsyncSanitizedStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._sanitizer = _HeaderSanitizer()

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._sanitizer.feed(await self._stream.read(max_bytes, timeout))

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._sanitizer.rearm()
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> "_AsyncSanitizedStream":
        return _AsyncSanitizedStream(
            await self._stream.start_tls(ssl_context, server_hostname, timeout)
        )

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _SanitizedBackend:
    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def connect_tcp(self, *args: Any, **kwargs: Any) -> _SanitizedStream:
        return _SanitizedStream(self._backend.connect_tcp(*args, **kwargs))

    def connect_unix_socket(self, *args: Any, **kwargs: Any) -> _SanitizedStream:
        return _SanitizedStream(self._backend.connect_unix_socket(*args, **kwargs))

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _AsyncSanitizedBackend:
    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def connect_tcp(self, *args: Any, **kwargs: Any) -> _AsyncSanitizedStream:
        return _AsyncSanitizedStream(await self._backend.connect_tcp(*args, **kwargs))

    async def connect_unix_socket(
        self, *args: Any, **kwargs: Any
    ) -> _AsyncSanitizedStream:
        return _AsyncSanitizedStream(
            await self._backend.connect_unix_socket(*args, **kwargs)
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _wrap_pool(pool: Any, wrapper: type) -> None:
    """Swap the pool's network backend.

    ``httpx`` builds the ``httpcore`` pool itself and does not forward
    ``network_backend``, but the pool only reads the attribute when it opens a
    connection, so replacing it after construction is enough.  A miss is loud
    rather than silent -- ``test_ls_http_header_tolerance`` asserts the wrapping
    happened, so an httpx/httpcore upgrade that moves the attribute fails there
    instead of quietly reinstating the outage.
    """

    backend = getattr(pool, "_network_backend", None)
    if backend is None:
        raise RuntimeError(
            "httpcore pool exposes no _network_backend; "
            "LS header tolerance cannot be installed"
        )
    if not isinstance(backend, wrapper):
        pool._network_backend = wrapper(backend)


class LSHeaderTolerantTransport(httpx.HTTPTransport):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        _wrap_pool(self._pool, _SanitizedBackend)


class LSHeaderTolerantAsyncTransport(httpx.AsyncHTTPTransport):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        _wrap_pool(self._pool, _AsyncSanitizedBackend)


def ls_client(**kwargs: Any) -> httpx.Client:
    """``httpx.Client`` for LS REST calls."""

    return httpx.Client(transport=LSHeaderTolerantTransport(), **kwargs)


def ls_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` for LS REST calls."""

    return httpx.AsyncClient(transport=LSHeaderTolerantAsyncTransport(), **kwargs)
