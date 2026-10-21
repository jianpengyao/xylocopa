"""POST /api/cert/regenerate argument safety.

subprocess.run / Popen are faked throughout: the route writes the live
certs/selfsigned.* and reloads the pm2 frontend when it really runs.
"""

import os
import subprocess

import pytest

from routers import system


@pytest.fixture()
def fake_proc(monkeypatch):
    """Record every subprocess call; openssl "fails" so the idempotency
    shortcut never kicks in, mkcert "succeeds"."""
    calls = []

    def run(args, *a, **kw):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1 if args[0] == "openssl" else 0, "", "")

    def popen(args, *a, **kw):
        calls.append(list(args))

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls


def _mkcert_calls(calls):
    return [c for c in calls if c[0] == "mkcert"]


@pytest.mark.anyio
@pytest.mark.parametrize("dns", [
    ["-cert-file=/tmp/pwned"],
    ["-install"],
    ["-"],
    ["xylocopa", "--help"],
    ["host-"],
    [".host"],
    ["a b"],
    ["xylocopa\n"],
    ["*.example.com"],
    [""],
    [7],
])
async def test_rejects_bad_dns_names(client, fake_proc, dns):
    resp = await client.post("/api/cert/regenerate", json={"ips": ["100.64.0.10"], "dns": dns})
    assert resp.status_code == 400
    assert "invalid DNS name" in resp.json()["detail"]
    assert fake_proc == []


@pytest.mark.anyio
@pytest.mark.parametrize("ip", ["-1.2.3.4", "1.2.3", "1.2.3.4.5", "999.1.1.1", "01.2.3.4",
                                "1.2.3.4\n", "-cert-file=/tmp/x", "", 16843009, None,
                                # IPv6 zone IDs: ipaddress accepts any text after "%"
                                "fe80::1%eth0", "fe80::1%a b", "fe80::1%\n", "fe80::1%x\x00"])
async def test_rejects_bad_ips(client, fake_proc, ip):
    resp = await client.post("/api/cert/regenerate", json={"ips": [ip], "dns": ["xylocopa"]})
    assert resp.status_code == 400
    assert "invalid IP" in resp.json()["detail"]
    assert fake_proc == []


@pytest.mark.anyio
async def test_rejects_non_object_body(client, fake_proc):
    resp = await client.post("/api/cert/regenerate", json=["1.2.3.4"])
    assert resp.status_code == 400
    assert fake_proc == []


@pytest.mark.anyio
async def test_argv_separates_flags_from_names(client, fake_proc):
    """The CertGuidePage request builds the same SAN list as before, with "--"
    between mkcert's flags and the positional names."""
    resp = await client.post("/api/cert/regenerate",
                             json={"ips": ["100.64.0.10"], "dns": ["xylocopa", "localhost"]})
    assert resp.status_code == 200 and resp.json() == {"ok": True}

    (argv,) = _mkcert_calls(fake_proc)
    certs_dir = os.path.normpath(
        os.path.join(os.path.dirname(system.__file__), "..", "..", "certs"))
    assert argv == [
        "mkcert",
        "-cert-file", os.path.join(certs_dir, "selfsigned.crt"),
        "-key-file", os.path.join(certs_dir, "selfsigned.key"),
        "--",
        "xylocopa", "localhost", "127.0.0.1", "100.64.0.10",
    ]
    assert ["pm2", "reload", "xylocopa-frontend"] in fake_proc


@pytest.mark.anyio
async def test_accepts_ipv6_and_dotted_names(client, fake_proc):
    resp = await client.post("/api/cert/regenerate",
                             json={"ips": ["fd7a:115c:a1e0::1", "10.0.0.2"],
                                   "dns": ["box.tail1234.ts.net", "my_host", "x"]})
    assert resp.status_code == 200
    (argv,) = _mkcert_calls(fake_proc)
    names = argv[argv.index("--") + 1:]
    assert names == ["box.tail1234.ts.net", "my_host", "x", "127.0.0.1", "fd7a:115c:a1e0::1", "10.0.0.2"]
