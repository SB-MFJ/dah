"""Open-source license inventory: the installed Python dependency closure plus bundled system components."""

from __future__ import annotations

import re
from functools import lru_cache
from importlib.metadata import Distribution, PackageNotFoundError, distribution

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = "dah"
ROOT_EXTRAS = ("saml", "postgres")

CLASSIFIER_SPDX = {
    "Apache Software License": "Apache-2.0",
    "MIT License": "MIT",
    "MIT No Attribution License (MIT-0)": "MIT-0",
    "BSD License": "BSD-3-Clause",
    "ISC License (ISCL)": "ISC",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "zlib/libpng License": "Zlib",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.1-or-later",
    "Historical Permission Notice and Disclaimer (HPND)": "HPND",
}

# Free/open-source SPDX identifiers we accept (OSI-approved, plus public-domain dedications).
OPEN_SOURCE = {
    "MIT", "MIT-0", "MIT-CMU", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Apache-2.0", "MPL-2.0", "PSF-2.0", "Python-2.0",
    "ISC", "Zlib", "CC0-1.0", "Unlicense", "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later", "HPND", "PostgreSQL", "BSL-1.0",
}

# Packages whose metadata is free text; verified by hand against the upstream project.
OVERRIDES = {
    "pypdfium2": ("Apache-2.0 OR BSD-3-Clause", "Bundles PDFium (BSD-3-Clause / Apache-2.0) and its dependency licenses"),
    "charset-normalizer": ("MIT", None),
    "lxml": ("BSD-3-Clause", "Statically links libxml2 / libxslt (MIT)"),
    "python3-saml": ("MIT", None),
    "python-dotenv": ("BSD-3-Clause", None),
}

SYSTEM_COMPONENTS = [
    {"name": "CPython", "license": "PSF-2.0", "url": "https://www.python.org/", "used_for": "runtime"},
    {"name": "Tesseract OCR", "license": "Apache-2.0", "url": "https://github.com/tesseract-ocr/tesseract", "used_for": "OCR of images and scanned PDF pages (Docker image)"},
    {"name": "tesseract-ocr-eng traineddata", "license": "Apache-2.0", "url": "https://github.com/tesseract-ocr/tessdata", "used_for": "English OCR model (Docker image)"},
    {"name": "Leptonica", "license": "BSD-2-Clause", "url": "http://www.leptonica.org/", "used_for": "image processing for Tesseract (Docker image)"},
    {"name": "LibreOffice", "license": "MPL-2.0", "url": "https://www.libreoffice.org/", "used_for": "converting doc/xls/ppt/rtf/odt (Docker image, optional)"},
    {"name": "xmlsec1", "license": "MIT", "url": "https://www.aleksey.com/xmlsec/", "used_for": "SAML XML signatures (Docker image)"},
    {"name": "libxml2", "license": "MIT", "url": "https://gitlab.gnome.org/GNOME/libxml2", "used_for": "XML parsing (Docker image)"},
    {"name": "tini", "license": "MIT", "url": "https://github.com/krallin/tini", "used_for": "container init (Docker image)"},
    {"name": "Monaco Editor", "license": "MIT", "url": "https://github.com/microsoft/monaco-editor", "used_for": "pipeline / SQL editor in the web UI (vendored in the Docker image)"},
    {"name": "PostgreSQL", "license": "PostgreSQL", "url": "https://www.postgresql.org/", "used_for": "optional external catalog (compose profile)"},
    {"name": "psycopg", "license": "LGPL-3.0-only", "url": "https://www.psycopg.org/", "used_for": "PostgreSQL driver (optional 'postgres' extra)"},
    {"name": "Debian (python:3.12-slim base image)", "license": "DFSG (various)", "url": "https://www.debian.org/legal/licenses/", "used_for": "container base OS", "open_source": True},
]


def _spdx_from_dist(d: Distribution) -> tuple[str, str | None]:
    name = canonicalize_name(d.metadata["Name"])
    if name in OVERRIDES:
        return OVERRIDES[name]
    expr = d.metadata.get("License-Expression")
    if expr:
        return expr.strip(), None
    classifiers = [c.split(" :: ")[-1] for c in d.metadata.get_all("Classifier") or [] if c.startswith("License ::")]
    mapped = [CLASSIFIER_SPDX.get(c, c) for c in classifiers if c != "OSI Approved"]
    if mapped:
        return " OR ".join(dict.fromkeys(mapped)), None
    raw = (d.metadata.get("License") or "").strip()
    first = raw.splitlines()[0][:80] if raw else "UNKNOWN"
    return first, None


def is_open_source(expression: str) -> bool:
    tokens = [t for t in re.split(r"[\s(),]+", expression) if t and t.upper() not in ("AND", "OR", "WITH")]
    return bool(tokens) and all(t in OPEN_SOURCE for t in tokens)


def _homepage(d: Distribution) -> str | None:
    for entry in d.metadata.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("homepage", "home", "source", "repository", "source code", "github"):
            return url.strip()
    return d.metadata.get("Home-page")


def dependency_closure(root: str = ROOT, extras: tuple[str, ...] = ROOT_EXTRAS) -> dict[str, tuple[Distribution, bool]]:
    """Installed distributions reachable from ``root`` (with ``extras``): name -> (dist, is_direct)."""
    found: dict[str, Distribution] = {}

    def requirements(d: Distribution, req_extras: tuple[str, ...]) -> list[Requirement]:
        out = []
        for r in d.requires or []:
            req = Requirement(r)
            envs = [{"extra": e} for e in (req_extras or ("",))]
            if req.marker and not any(req.marker.evaluate(env) for env in envs):
                continue
            out.append(req)
        return out

    def walk(name: str, req_extras: tuple[str, ...]) -> None:
        key = canonicalize_name(name)
        if key in found:
            return
        try:
            d = distribution(name)
        except PackageNotFoundError:
            return
        found[key] = d
        for req in requirements(d, req_extras):
            walk(req.name, tuple(req.extras))

    walk(root, extras)
    root_dist = found.pop(canonicalize_name(root), None)
    direct = {canonicalize_name(r.name) for r in requirements(root_dist, extras)} if root_dist else set()
    return {k: (d, k in direct) for k, d in found.items()}


@lru_cache
def inventory() -> dict:
    pkgs = []
    for key, (d, direct) in sorted(dependency_closure().items()):
        lic, note = _spdx_from_dist(d)
        pkgs.append({
            "name": d.metadata["Name"],
            "version": d.version,
            "license": lic,
            "open_source": is_open_source(lic),
            "direct": direct,
            "url": _homepage(d),
            "note": note,
            "has_license_text": bool(_license_files(d)),
        })
    system = [{**c, "open_source": c.get("open_source", is_open_source(c["license"]))} for c in SYSTEM_COMPONENTS]
    return {"python_packages": pkgs, "system_components": system}


def _license_files(d: Distribution) -> list:
    files = d.files or []
    return [f for f in files if re.search(r"(LICEN[CS]E|COPYING|NOTICE)", f.name, re.I)]


def license_text(name: str, max_bytes: int = 512_000) -> str | None:
    closure = dependency_closure()
    entry = closure.get(canonicalize_name(name))
    if entry is None:
        return None
    d = entry[0]
    parts = []
    for f in _license_files(d):
        try:
            parts.append(f"===== {f} =====\n{f.locate().read_text(encoding='utf-8', errors='replace')}")
        except OSError:
            continue
    text = "\n\n".join(parts) or (d.metadata.get("License") or "")
    return text[:max_bytes] or None


def markdown() -> str:
    inv = inventory()
    out = [
        "# Third-party licenses",
        "",
        "dah is Apache-2.0 licensed and is built entirely from open-source software. This file is generated by",
        "`dah licenses --markdown` from the installed dependency closure; the running server shows the",
        "authoritative list for its own environment at `/licenses`.",
        "",
        "## Python packages",
        "",
        "| Package | Version | License | Direct |",
        "|---|---|---|---|",
    ]
    for p in inv["python_packages"]:
        name = f"[{p['name']}]({p['url']})" if p["url"] else p["name"]
        out.append(f"| {name} | {p['version']} | {p['license']}{' — ' + p['note'] if p['note'] else ''} | {'yes' if p['direct'] else ''} |")
    out += ["", "## System and frontend components", "", "| Component | License | Used for |", "|---|---|---|"]
    for c in inv["system_components"]:
        out.append(f"| [{c['name']}]({c['url']}) | {c['license']} | {c['used_for']} |")
    out += ["", "Full license texts ship with each package (see `/api/licenses/<package>/text` on a running server).", ""]
    return "\n".join(out)
