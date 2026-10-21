from __future__ import annotations

import json

import pytest

from dah.ingest.extractors import ocr_available

from .fixtures import make_docx, make_pdf, make_png, make_xlsx


def upload(client, name, data, **form):
    form = {k: (json.dumps(v) if isinstance(v, dict) else str(v)) for k, v in form.items()}
    form.setdefault("sync", "true")
    r = client.post("/api/documents", files=[("files", (name, data))], data=form)
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


def q(client, sql):
    r = client.post("/api/query", json={"sql": sql})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def test_txt_and_metadata(client):
    item = upload(client, "notes.txt", "hello lakehouse\nsecond line".encode(), path="team/notes.txt", metadata={"owner": "ops"})
    assert item["status"] == "ready" and item["version"] == 1 and item["mime"] == "text/plain"
    rows = q(client, "SELECT path, text, metadata->>'word_count' AS wc, metadata->'user'->>'owner' AS owner FROM documents")
    assert rows == [{"path": "team/notes.txt", "text": "hello lakehouse\nsecond line", "wc": "4", "owner": "ops"}]


def test_pdf_text_layer_and_pages(client):
    item = upload(client, "report.pdf", make_pdf(["Quarterly revenue grew", "Second page text here"]))
    assert item["mime"] == "application/pdf" and item["status"] == "ready"
    rows = q(client, "SELECT page, text FROM pages ORDER BY page")
    assert [r["page"] for r in rows] == [1, 2]
    assert "Quarterly revenue grew" in rows[0]["text"]
    doc = client.get(f"/api/documents/{item['document_id']}").json()
    assert doc["current"]["metadata"]["page_count"] == 2
    hits = client.get("/api/documents/search", params={"q": "revenue"}).json()["rows"]
    assert hits and hits[0]["page"] == 1


def test_docx_with_table_records(client):
    data = make_docx(["Contract A", "Payment terms net 30"], table=[["item", "qty"], ["bolts", "10"], ["nuts", "5"]])
    item = upload(client, "contract.docx", data)
    assert item["mime"].endswith("wordprocessingml.document")
    doc = client.get(f"/api/documents/{item['document_id']}", params={"text": True}).json()
    assert "Payment terms" in doc["current"]["text"]
    assert doc["current"]["metadata"]["author"] == "Ada"
    rows = q(client, "SELECT sheet, data->>'item' AS item, data->>'qty' AS qty FROM records ORDER BY row_number")
    assert rows == [{"sheet": "table1", "item": "bolts", "qty": "10"}, {"sheet": "table1", "item": "nuts", "qty": "5"}]


def test_xlsx_and_csv_records(client):
    upload(client, "sales.xlsx", make_xlsx([["region", "amount"], ["eu", 10], ["us", 20]]))
    upload(client, "people.csv", b"name;age\nann;31\nbob;42\n")
    rows = q(client, "SELECT sheet, sum(CAST(data->>'amount' AS INT)) s FROM records WHERE sheet='Sales' GROUP BY sheet")
    assert rows == [{"sheet": "Sales", "s": 30}]
    rows = q(client, "SELECT data->>'name' n FROM records WHERE sheet='csv' ORDER BY 1")
    assert [r["n"] for r in rows] == ["ann", "bob"]


def test_html_email_json(client):
    upload(client, "page.html", b"<html><head><title>T1</title><script>x()</script></head><body><p>Body words</p></body></html>")
    eml = b"From: a@x.org\r\nTo: b@y.org\r\nSubject: Invoice 7\r\nContent-Type: text/plain\r\n\r\nPlease pay.\r\n"
    upload(client, "mail.eml", eml)
    upload(client, "items.json", json.dumps([{"sku": "a1"}, {"sku": "b2"}]).encode())
    rows = {r["name"]: r for r in q(client, "SELECT name, text, metadata->>'title' t FROM documents")}
    assert rows["page.html"]["t"] == "T1" and "x()" not in rows["page.html"]["text"]
    assert rows["mail.eml"]["t"] == "Invoice 7" and "Please pay." in rows["mail.eml"]["text"]
    assert q(client, "SELECT count(*) n FROM records WHERE sheet='json'") == [{"n": 2}]


def test_image_metadata_and_ocr(client):
    item = upload(client, "scan.png", make_png("HELLO OCR"))
    assert item["mime"] == "image/png" and item["status"] == "ready"
    md = client.get(f"/api/documents/{item['document_id']}").json()["current"]["metadata"]
    assert md["width"] == 900 and md["height"] == 200
    if ocr_available():
        assert "HELLO" in q(client, "SELECT text FROM documents")[0]["text"].upper()
    else:
        assert any("OCR" in w for w in md["warnings"])


@pytest.mark.skipif(not ocr_available(), reason="tesseract not installed")
def test_scanned_pdf_page_gets_ocr(client):
    from PIL import Image
    import io

    png = Image.open(io.BytesIO(make_png("SCANNED PAGE")))
    buf = io.BytesIO()
    png.save(buf, format="PDF")
    item = upload(client, "scan.pdf", buf.getvalue())
    rows = q(client, "SELECT ocr, text FROM pages")
    assert rows[0]["ocr"] is True and "SCANNED" in rows[0]["text"].upper()
    assert item["status"] == "ready"


def test_unknown_binary_and_dedupe(client):
    blob = bytes(range(256)) * 10
    a = upload(client, "blob.bin", blob)
    assert a["status"] == "ready" and a["mime"] == "application/octet-stream"
    b = upload(client, "blob.bin", blob)
    assert b["unchanged"] is True and b["version"] == 1


def test_async_upload_and_download(client):
    import time

    r = client.post("/api/documents", files=[("files", ("a.txt", b"async body"))])
    doc_id = r.json()["items"][0]["document_id"]
    for _ in range(100):
        d = client.get(f"/api/documents/{doc_id}").json()
        if d["current"] and d["current"]["status"] == "ready":
            break
        time.sleep(0.05)
    assert d["current"]["status"] == "ready"
    assert client.get(f"/api/documents/{doc_id}/download").content == b"async body"


def test_legacy_doc_without_converter_fails_cleanly(client, monkeypatch):
    import shutil as _sh

    real = _sh.which
    monkeypatch.setattr("dah.ingest.extractors.shutil.which", lambda n: None if n in ("soffice", "libreoffice", "antiword") else real(n))
    ole = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504
    item = upload(client, "old.doc", ole)
    assert item["status"] == "failed" and item["mime"] == "application/msword"
    d = client.get(f"/api/documents/{item['document_id']}").json()
    assert "LibreOffice" in d["current"]["error"]


def test_detect_sniffing(tmp_path):
    from dah.ingest.detect import detect_mime

    cases = {"a.toml": b"[build]\nx = 1\n", "noext": b"[section]\nk=v\n", "arr": b' [ {"a": 1} ]', "obj": b'{"a": 1}', "page": b"<!DOCTYPE html><p>x"}
    want = {"a.toml": "application/toml", "noext": "text/plain", "arr": "application/json", "obj": "application/json", "page": "text/html"}
    for name, data in cases.items():
        f = tmp_path / name
        f.write_bytes(data)
        assert detect_mime(f, name) == want[name], name
