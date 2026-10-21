"""Raw ASGI request helper for middleware tests.

httpx always sends a Host header and normalises the URL; these tests need
full control (no Host, duplicate headers, percent-encoded or doubled
slashes), so build the scope the way uvicorn does: ``path`` is the
percent-decoded ``raw_path``.
"""

from urllib.parse import unquote

import anyio


async def asgi_request(app, method: str, raw_path: str, headers=(), body: bytes = b""):
    """Send one HTTP request straight into an ASGI app.

    ``headers`` is a sequence of (name, value) pairs; Host is only sent when
    listed. Returns (status, headers-dict, body-bytes).
    """
    path, _, query = raw_path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(path),
        "raw_path": path.encode("latin-1"),
        "root_path": "",
        "query_string": query.encode("latin-1"),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8080),
    }
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    done = anyio.Event()
    sent = []

    async def receive():
        if pending:
            return pending.pop(0)
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            done.set()

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    resp_headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start.get("headers", [])}
    resp_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], resp_headers, resp_body
