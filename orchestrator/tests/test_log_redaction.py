"""Log redaction (log_config.RedactSecretsFilter): URL-borne credentials in
uvicorn's access and WebSocket lines become <redacted>.

Records are built exactly as uvicorn 0.34 emits them (h11/httptools access
line, websockets handshake lines) with its own path+query helper.
"""

import logging

import pytest
from uvicorn.protocols.utils import get_path_with_query_string

import log_config
from log_config import RedactSecretsFilter, install_redaction, redact_secrets

ACCESS_FMT = '%s - "%s %s HTTP/%s" %d'
WS_ACCEPTED_FMT = '%s - "WebSocket %s" [accepted]'
WS_DENIED_FMT = '%s - "WebSocket %s" 403'


def _path(path: str, query: str = "") -> str:
    return get_path_with_query_string({"path": path, "root_path": "",
                                       "query_string": query.encode()})


def _record(name: str, msg: str, args) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 0, msg, args, None)


def _filtered(name: str, msg: str, args) -> str:
    rec = _record(name, msg, args)
    assert RedactSecretsFilter().filter(rec) is True
    return rec.getMessage()


def test_access_line_media_token_redacted():
    line = _filtered("uvicorn.access", ACCESS_FMT,
                     ("100.64.0.10:5123", "GET", _path("/api/files/p/a.png", "download=1&token=abc.def"),
                      "1.1", 200))
    assert line == ('100.64.0.10:5123 - "GET /api/files/p/a.png?download=1&token=<redacted> '
                    'HTTP/1.1" 200')


def test_access_line_token_first_and_middle_params():
    line = _filtered("uvicorn.access", ACCESS_FMT,
                     ("127.0.0.1:1", "GET", _path("/api/thumbs/x.jpg", "token=SECRET&_v=3&tile=1"),
                      "1.1", 200))
    assert "SECRET" not in line
    assert "?token=<redacted>&_v=3&tile=1" in line


@pytest.mark.parametrize("fmt", [WS_ACCEPTED_FMT, WS_DENIED_FMT])
def test_websocket_handshake_ticket_redacted(fmt):
    line = _filtered("uvicorn.error", fmt, ("127.0.0.1:2", _path("/ws/status", "ticket=xyt_SECRET")))
    assert "xyt_SECRET" not in line
    assert '"WebSocket /ws/status?ticket=<redacted>"' in line


def test_terminal_ticket_redacted():
    line = _filtered("uvicorn.error", WS_ACCEPTED_FMT,
                     ("127.0.0.1:2", _path("/ws/terminal/abcd1234", "ticket=xyt_SECRET")))
    assert line.endswith('"WebSocket /ws/terminal/abcd1234?ticket=<redacted>" [accepted]')


def test_preview_and_probe_path_capabilities_redacted():
    tok = "eyJzY29wZSI6InByZXZpZXcifQ.c2lnbmF0dXJl"
    line = _filtered("uvicorn.access", ACCESS_FMT,
                     ("127.0.0.1:3", "GET", _path(f"/api/preview/t/{tok}/vproj/index.html"), "1.1", 200))
    assert tok not in line
    assert "/api/preview/t/<redacted>/vproj/index.html" in line

    line = _filtered("uvicorn.access", ACCESS_FMT,
                     ("127.0.0.1:3", "GET", _path("/api/preview/p/0123456789abcdef0123456789abcdef/static/app.js",
                                                  "v=1"), "1.1", 200))
    assert "/api/preview/p/<redacted>/static/app.js?v=1" in line

    line = _filtered("uvicorn.error", WS_ACCEPTED_FMT,
                     ("127.0.0.1:3", _path("/api/preview/p/0123456789abcdef0123456789abcdef/ws")))
    assert "0123456789abcdef" not in line

    line = _filtered("uvicorn.access", ACCESS_FMT,
                     ("127.0.0.1:3", "POST", _path("/api/probe-trigger/pt_SECRET"), "1.1", 200))
    assert line.endswith('"POST /api/probe-trigger/<redacted> HTTP/1.1" 200')


def test_dropbox_callback_code_and_state_redacted_only_there():
    line = _filtered("uvicorn.access", ACCESS_FMT,
                     ("127.0.0.1:4", "GET", _path("/api/dropbox/callback", "code=AUTHCODE&state=NONCE"),
                      "1.1", 307))
    assert "AUTHCODE" not in line and "NONCE" not in line
    assert "/api/dropbox/callback?code=<redacted>&state=<redacted>" in line
    # code= / state= elsewhere are ordinary parameters.
    assert redact_secrets("/api/git/log?code=1&state=open") == "/api/git/log?code=1&state=open"


def test_ordinary_lines_untouched():
    args = ("127.0.0.1:5", "GET", _path("/api/agents", "limit=5&project=vproj"), "1.1", 200)
    assert _filtered("uvicorn.access", ACCESS_FMT, args) == \
        '127.0.0.1:5 - "GET /api/agents?limit=5&project=vproj HTTP/1.1" 200'
    # "tokens" / "token_count" are not the token parameter.
    assert redact_secrets("/api/x?tokens=5&token_count=2") == "/api/x?tokens=5&token_count=2"


def test_message_without_args_redacted_and_non_str_args_kept():
    rec = _record("uvicorn.error", "GET /api/files/a?token=SECRET", None)
    assert RedactSecretsFilter().filter(rec) is True
    assert rec.getMessage() == "GET /api/files/a?token=<redacted>"
    rec = _record("uvicorn.access", "%s %d %s", ("a?ticket=T", 200, None))
    assert RedactSecretsFilter().filter(rec) is True
    assert rec.args == ("a?ticket=<redacted>", 200, None)


def test_filter_attached_to_uvicorn_loggers_after_setup():
    import main  # noqa: F401  (import runs setup_logging())

    for name in ("uvicorn.access", "uvicorn.error"):
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, RedactSecretsFilter)]
        assert len(filters) == 1, name
    install_redaction()  # idempotent
    for name in ("uvicorn.access", "uvicorn.error"):
        assert sum(isinstance(f, RedactSecretsFilter)
                   for f in logging.getLogger(name).filters) == 1
    assert "install_redaction()" in open(log_config.__file__).read().split("def setup_logging")[1]


def test_emitted_through_logger_is_redacted(caplog):
    install_redaction()
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    access = logging.getLogger("uvicorn.access")
    propagate = access.propagate
    access.propagate = True  # uvicorn's dictConfig turns it off; caplog sits on root
    try:
        access.info(ACCESS_FMT, "100.64.0.10:9", "GET",
                    _path("/api/uploads/u/v.mp4", "token=LEAKME"), "1.1", 206)
    finally:
        access.propagate = propagate
    assert "LEAKME" not in caplog.text
    assert "token=<redacted>" in caplog.text
