# Feature 07 — Open-source license acknowledgement

Status: COMPLETE

## Tasks
- [x] Runtime inventory of installed Python distributions + licenses
- [x] Static inventory of system / frontend components (Tesseract, LibreOffice, Monaco, ...)
- [x] `/api/licenses` + acknowledgement page in UI
- [x] `THIRD_PARTY_LICENSES.md`
- [x] Test that every direct dependency resolves to an OSI license

## Log
- 2026-10-21: `dah/licenses.py` walks the real installed dependency closure of `dah[saml,postgres]`
  (evaluating environment markers and extras) instead of listing everything in the venv, so dev
  tools aren't included and new transitive deps show up automatically. Licenses are normalised
  to SPDX (License-Expression → classifiers → hand-verified overrides for packages with free-text
  metadata) and checked against an open-source allowlist.
- 2026-10-21: System and frontend components listed explicitly: CPython, Tesseract + eng
  traineddata, Leptonica, LibreOffice, xmlsec1, libxml2, tini, Monaco, PostgreSQL, psycopg,
  Debian base image.
- 2026-10-21: Public (no auth) `/licenses` HTML page, `/api/licenses` JSON and
  `/api/licenses/{package}/text` (full license files shipped in each wheel); "Licenses" view in
  the UI; `dah licenses [--markdown]` CLI; generated `THIRD_PARTY_LICENSES.md` (68 packages, all
  open source). The Dockerfile copies Monaco's LICENSE alongside the vendored editor.
- 2026-10-21: 4 tests pass (49 total): every package is open source, every direct dependency in
  pyproject is listed, the page and license texts are served, and THIRD_PARTY_LICENSES.md is current.
- 2026-10-21: Fixed two bugs found by the tests: `PackagePath.read_text()` doesn't accept
  `errors=` (now uses `locate()`), and the "direct" flag depended on traversal order.
