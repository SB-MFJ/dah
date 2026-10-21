"""Public license acknowledgement page + API."""

from __future__ import annotations

from html import escape as e

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import __version__, licenses

router = APIRouter(tags=["licenses"])


@router.get("/api/licenses")
def list_licenses():
    return {"project": {"name": "dah", "version": __version__, "license": "Apache-2.0"}, **licenses.inventory()}


@router.get("/api/licenses/{package}/text", response_class=PlainTextResponse)
def license_text(package: str):
    text = licenses.license_text(package)
    if text is None:
        raise HTTPException(404, "no license text found for this package")
    return text


def _link(text: str, url: str | None) -> str:
    return f'<a href="{e(url)}">{e(text)}</a>' if url else e(text)


def _package_row(p: dict) -> str:
    note = f" <small>{e(p['note'])}</small>" if p["note"] else ""
    text = _link("license text", f"/api/licenses/{p['name']}/text") if p["has_license_text"] else ""
    return f"<tr><td>{_link(p['name'], p['url'])}</td><td>{e(p['version'])}</td><td>{e(p['license'])}{note}</td><td>{text}</td></tr>"


def _component_row(c: dict) -> str:
    return f"<tr><td>{_link(c['name'], c['url'])}</td><td>{e(c['license'])}</td><td>{e(c['used_for'])}</td></tr>"


STYLE = """body{font:14px system-ui,sans-serif;max-width:1000px;margin:32px auto;padding:0 16px;color:#1d2330;background:#fff}
table{border-collapse:collapse;width:100%;margin-bottom:28px}td,th{text-align:left;padding:5px 8px;border-bottom:1px solid #e3e6eb;vertical-align:top}
th{color:#667085;font-weight:500}small{color:#667085}a{color:#2f6fed}
@media (prefers-color-scheme:dark){body{background:#12151b;color:#e6e8ec}td,th{border-color:#2a303b}a{color:#5b8cff}}"""


@router.get("/licenses", response_class=HTMLResponse, include_in_schema=False)
def licenses_page():
    inv = licenses.inventory()
    pkgs = "".join(_package_row(p) for p in inv["python_packages"])
    comps = "".join(_component_row(c) for c in inv["system_components"])
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Open-source licenses — dah</title><style>{STYLE}</style>
<h1>Open-source acknowledgements</h1>
<p>dah {e(__version__)} is licensed under Apache-2.0 and is built on the open-source projects below.
We are grateful to their authors and maintainers.</p>
<h2>System and frontend components</h2>
<table><tr><th>Component</th><th>License</th><th>Used for</th></tr>{comps}</table>
<h2>Python packages ({len(inv['python_packages'])})</h2>
<table><tr><th>Package</th><th>Version</th><th>License</th><th></th></tr>{pkgs}</table>"""
