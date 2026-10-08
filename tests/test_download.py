"""Resumable downloads against a local server that really drops connections mid-transfer."""
import hashlib
import http.server
import os
import threading

import pytest

from download import DownloadError, fetch_resumable

PAYLOAD = os.urandom(900_000)


def make_server(drop_after=None, honour_range=True, drops=1):
    state = {"requests": [], "dropped": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            state["requests"].append(rng)
            start = 0
            if rng and honour_range:
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(PAYLOAD):
                    self.send_response(416)
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
            else:
                self.send_response(200)
            body = PAYLOAD[start:]
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if drop_after is not None and state["dropped"] < drops:
                state["dropped"] += 1
                self.wfile.write(body[:drop_after])
                self.wfile.flush()
                self.connection.close()  # connection dies mid-file
                return
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def test_resumes_from_where_it_stopped(tmp_path):
    srv, state = make_server(drop_after=300_000)
    try:
        dest = fetch_resumable(f"http://127.0.0.1:{srv.server_port}/f", tmp_path / "f.bin",
                               sha256=hashlib.sha256(PAYLOAD).hexdigest(), sleep=lambda s: None)
    finally:
        srv.shutdown()
    assert dest.read_bytes() == PAYLOAD
    assert state["requests"][0] is None and state["requests"][1].startswith("bytes=") and state["requests"][1] != "bytes=0-"
    resumed_at = int(state["requests"][1].split("=")[1].rstrip("-"))
    assert 0 < resumed_at <= 300_000  # continued, did not restart from zero
    assert not (tmp_path / "f.bin.part").exists()


def test_survives_several_drops(tmp_path):
    srv, state = make_server(drop_after=100_000, drops=4)
    try:
        dest = fetch_resumable(f"http://127.0.0.1:{srv.server_port}/f", tmp_path / "f.bin", sleep=lambda s: None)
    finally:
        srv.shutdown()
    assert dest.read_bytes() == PAYLOAD and len(state["requests"]) == 5


def test_server_without_range_support_restarts_cleanly(tmp_path):
    srv, _ = make_server(drop_after=200_000, honour_range=False)
    try:
        dest = fetch_resumable(f"http://127.0.0.1:{srv.server_port}/f", tmp_path / "f.bin", sleep=lambda s: None)
    finally:
        srv.shutdown()
    assert dest.read_bytes() == PAYLOAD


def test_wrong_checksum_is_rejected_and_not_installed(tmp_path):
    srv, _ = make_server()
    try:
        with pytest.raises(DownloadError, match="sha256"):
            fetch_resumable(f"http://127.0.0.1:{srv.server_port}/f", tmp_path / "f.bin", sha256="0" * 64,
                            sleep=lambda s: None)
    finally:
        srv.shutdown()
    assert not (tmp_path / "f.bin").exists()


def test_gives_up_after_retries(tmp_path):
    with pytest.raises(DownloadError, match="giving up"):
        fetch_resumable("http://127.0.0.1:9/nothing", tmp_path / "x", retries=2, timeout=1, sleep=lambda s: None)
