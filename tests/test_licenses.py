from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent


def test_every_dependency_is_open_source(anon):
    inv = anon.get("/api/licenses").json()  # public, no auth
    assert inv["project"]["license"] == "Apache-2.0"
    bad = [(p["name"], p["license"]) for p in inv["python_packages"] if not p["open_source"]]
    assert bad == [], f"non open-source or unrecognised licenses: {bad}"
    assert all(c["open_source"] for c in inv["system_components"])
    names = {c["name"] for c in inv["system_components"]}
    assert {"Tesseract OCR", "LibreOffice", "Monaco Editor"} <= names


def test_direct_dependencies_are_acknowledged(anon):
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    listed = {canonicalize_name(p["name"]) for p in anon.get("/api/licenses").json()["python_packages"] if p["direct"]}
    missing = {canonicalize_name(Requirement(d).name) for d in deps} - listed
    assert missing == set()


def test_license_page_and_texts(anon):
    page = anon.get("/licenses")
    assert page.status_code == 200 and "Open-source acknowledgements" in page.text and "duckdb" in page.text
    t = anon.get("/api/licenses/duckdb/text")
    assert t.status_code == 200 and "MIT" in t.text.upper() or "Permission is hereby granted" in t.text
    assert anon.get("/api/licenses/not-a-package/text").status_code == 404


def test_third_party_file_up_to_date():
    from dah import licenses

    on_disk = (ROOT / "THIRD_PARTY_LICENSES.md").read_text()
    names = {canonicalize_name(p["name"]) for p in licenses.inventory()["python_packages"] if p["direct"]}
    for n in names:
        assert n in on_disk.lower(), f"{n} missing from THIRD_PARTY_LICENSES.md (run: dah licenses --markdown > THIRD_PARTY_LICENSES.md)"
