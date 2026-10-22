from __future__ import annotations

import socket
import socketserver
import struct
import threading

import pytest

from dah import config

# The fake clamd flags this marker. (Not the real EICAR string: local antivirus quarantines files containing it.)
MALWARE = b"DAH-FAKE-MALWARE-MARKER"


class FakeClamd(socketserver.BaseRequestHandler):
    def handle(self):
        f = self.request.makefile("rb")
        assert f.read(10) == b"zINSTREAM\0"
        data = b""
        while (n := struct.unpack("!L", f.read(4))[0]) > 0:
            data += f.read(n)
        self.server.scanned.append(data)
        verdict = b"Win.Test.EICAR_HDB-1 FOUND" if MALWARE in data else b"OK"
        self.request.sendall(b"stream: " + verdict + b"\0")


@pytest.fixture()
def clamd(env, monkeypatch):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeClamd)
    srv.daemon_threads = True
    srv.scanned = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("DAH_CLAMAV_URL", f"tcp://127.0.0.1:{srv.server_address[1]}")
    config.get_settings.cache_clear()
    yield srv
    srv.shutdown()
    srv.server_close()


def _post(client, *files, **form):
    return client.post("/api/documents", files=[("files", f) for f in files], data={"sync": "true", **form})


def _docs(client):
    return client.get("/api/documents").json()["total"]


def test_clean_upload_is_scanned_and_stored(client, clamd):
    r = _post(client, ("a.txt", b"perfectly fine"))
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["status"] == "ready"
    assert clamd.scanned == [b"perfectly fine"]


def test_infected_upload_is_rejected_and_not_stored(client, clamd, env):
    r = _post(client, ("bad.com", MALWARE))
    assert r.status_code == 422
    assert "Win.Test.EICAR_HDB-1" in r.json()["detail"]
    assert _docs(client) == 0
    assert not [p for p in (env / "data" / "blobs").rglob("*") if p.is_file()]


def test_one_infected_file_rejects_whole_upload(client, clamd):
    r = _post(client, ("ok.txt", b"fine"), ("bad.txt", b"hidden " + MALWARE), path="in/")
    assert r.status_code == 422
    assert _docs(client) == 0


def test_scanner_unreachable_fails_closed(client, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again before the upload: nothing listens there
    monkeypatch.setenv("DAH_CLAMAV_URL", f"tcp://127.0.0.1:{port}")
    config.get_settings.cache_clear()
    r = _post(client, ("a.txt", b"fine"))
    assert r.status_code == 503 and "virus scan" in r.json()["detail"]
    assert _docs(client) == 0

    monkeypatch.setenv("DAH_CLAMAV_FAIL_OPEN", "true")
    config.get_settings.cache_clear()
    assert _post(client, ("a.txt", b"fine")).status_code == 200


def test_disabled_by_default(client):
    assert _post(client, ("bad.com", MALWARE)).status_code == 200


def test_cli_ingest_skips_infected(clamd, tmp_path, capsys):
    from dah.cli import main

    (tmp_path / "ok.txt").write_bytes(b"fine")
    (tmp_path / "bad.txt").write_bytes(MALWARE)
    assert main(["ingest", str(tmp_path / "ok.txt"), str(tmp_path / "bad.txt")]) == 1
    out = capsys.readouterr()
    assert "ok.txt: v1 ready" in out.out
    assert "rejected by the virus scanner" in out.err
