from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "dah" / "web" / "static"


def test_static_ui_served(anon):
    r = anon.get("/")
    assert r.status_code == 200 and "dah" in r.text
    for f in ("app.js", "editor.js", "views.js", "app.css"):
        assert anon.get(f"/static/{f}").status_code == 200


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_editor_autocomplete_logic():
    for f in STATIC.glob("*.js"):
        subprocess.run(["node", "--check", str(f)], check=True)
    r = subprocess.run(["node", str(Path(__file__).parent / "js" / "completions.test.js"), str(STATIC)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "completions ok" in r.stdout
