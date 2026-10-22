# Feature 12 — Upload virus scanning (ClamAV)

Status: COMPLETE

## Tasks
- [x] clamd INSTREAM client over TCP / Unix socket, no new dependency (`dah/ingest/scan.py`)
- [x] Scan before the blob is stored (`BlobStore.put_stream(check=…)`), covers API and `dah ingest`
- [x] Multi-file uploads: scan everything before storing anything
- [x] Fail closed (503) when clamd can't give a verdict; `DAH_CLAMAV_FAIL_OPEN` opt-out
- [x] Optional `clamav` docker compose profile
- [x] README, .env.example
- [x] Tests

## Log
- 2026-10-22: Off unless `DAH_CLAMAV_URL` is set. Infected upload → 422 with the signature name,
  nothing written to blobs or the catalog. Scanner errors (unreachable, timeout, over
  StreamMaxLength) → 503. `dah ingest` reports and skips infected files and exits 1.
- 2026-10-22: Tests use a fake clamd speaking INSTREAM with a harmless marker string: a test file
  containing the real EICAR string was quarantined by local antivirus. Also checked against a
  real clamd 1.x: EICAR rejected (`Eicar-Test-Signature`), clean file accepted, 120 MB upload
  → 503 "INSTREAM size limit exceeded".
- 2026-10-22: 6 new tests pass. 10 pre-existing failures in the full suite (pipeline/SSO tests)
  are unrelated: the venv's editable install points at an old checkout path; same failures
  without this change.
