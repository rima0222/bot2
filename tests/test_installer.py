"""install.sh: syntax, shellcheck-style hygiene and the resumable `fetch` helper (real curl, real dropped connections)."""
import hashlib
import os
import pathlib
import shutil
import subprocess

import pytest

from test_download import PAYLOAD, make_server

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "install.sh"
ENV = {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}


def bash(code: str, **kw):
    return subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=ENV, timeout=120, **kw)


def test_syntax_and_help():
    assert bash(f"bash -n {SCRIPT}").returncode == 0
    out = bash(f"bash {SCRIPT} --help")
    assert out.returncode == 0 and "--public" in out.stdout and "does NOT do" in out.stdout


def test_refuses_without_root_or_with_bad_args():
    bad = bash(f"bash {SCRIPT} --bogus")
    assert bad.returncode != 0 and "unknown option" in bad.stderr
    port = bash(f"bash {SCRIPT} --port 80")
    assert port.returncode != 0 and "1024-65535" in port.stderr


def test_script_never_touches_network_configuration():
    text = SCRIPT.read_text().lower()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    for forbidden in ("iptables", "nft ", "ufw ", "firewall-cmd", "ip route", "ip rule", "sysctl", "resolv.conf",
                      "systemctl restart xray", "systemctl restart nginx", "networkmanager", "netplan"):
        assert forbidden not in code, forbidden


def test_fetch_resumes_partial_download_with_curl(tmp_path):
    srv, state = make_server(drop_after=250_000, drops=2)
    dest = tmp_path / "pkg.whl"
    try:
        r = bash(f'source {SCRIPT}; sleep(){{ :; }}; fetch "http://127.0.0.1:{srv.server_port}/pkg" "{dest}" 10')
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == hashlib.sha256(PAYLOAD).hexdigest()
    ranges = [x for x in state["requests"] if x]
    assert ranges, "curl never sent a Range header, so it restarted from zero"
    assert all(int(x.split("=")[1].rstrip("-")) > 0 for x in ranges)


def test_fetch_restarts_cleanly_if_server_cannot_resume(tmp_path):
    srv, _ = make_server(drop_after=200_000, honour_range=False)
    dest = tmp_path / "pkg.bin"
    try:
        r = bash(f'source {SCRIPT}; sleep(){{ :; }}; fetch "http://127.0.0.1:{srv.server_port}/pkg" "{dest}" 10')
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr
    assert dest.read_bytes() == PAYLOAD


def test_fetch_gives_up_with_nonzero_status(tmp_path):
    r = bash(f'source {SCRIPT}; sleep(){{ :; }}; fetch "http://127.0.0.1:9/none" "{tmp_path}/x" 2')
    assert r.returncode != 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", "-S", "warning", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
