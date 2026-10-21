# Feature 02 — Ingestion, metadata, OCR / transcription

Status: COMPLETE

## Tasks
- [x] File type detection (magic bytes + extension)
- [x] Generic metadata (size, hash, mime, counts, timestamps)
- [x] Text extractors: txt/md/csv/json/html/eml
- [x] PDF text extraction with per-page OCR fallback for scanned pages
- [x] DOCX / PPTX / XLSX extraction
- [x] Legacy office formats (doc/xls/ppt/rtf/odt) via LibreOffice conversion
- [x] Image OCR (png/jpg/tiff/...) with EXIF metadata
- [x] Bronze zone: pages + tabular records written as Parquet, exposed as `pages` / `records` views
- [x] Async ingestion worker + status tracking, reprocess endpoint
- [x] Upload / list / get / download API
- [x] Tests

## Log
- 2026-10-21: `ingest/detect.py` — filetype magic, extension overrides for container formats
  (zip/OLE), text sniffing fallback.
- 2026-10-21: `ingest/extractors.py` — extractors for text, markdown, CSV/TSV (sniffed dialect →
  records), JSON/NDJSON (→ records), HTML (script/style stripped), EML, PDF (pypdf text layer;
  pages under `DAH_OCR_MIN_PAGE_CHARS` rendered with pypdfium2 at 300dpi and OCR'd), DOCX
  (paragraphs, headers/footers, tables → records), PPTX (slides + notes), XLSX (sheets → records),
  legacy formats converted by headless LibreOffice (antiword fallback for .doc), images (EXIF,
  dpi, multi-frame TIFF OCR with mean confidence).
- 2026-10-21: `ingest/service.py` — upload → blob → new version (byte-identical re-upload to the
  same path is a no-op) → background extraction → bronze Parquet → version becomes current
  (never regresses to an older version) → post-ingest hooks. Pending work re-queued on startup.
- 2026-10-21: `/api/documents` upload (multi-file, logical path/prefix, user metadata JSON,
  sync flag), list, get, text, pages, download, search, reprocess, delete.
- 2026-10-21: 14 tests pass. The OCR tests skip on this machine because tesseract is not installed
  locally; the Docker image installs `tesseract-ocr`. The LibreOffice path is tested only for its
  failure message (soffice is not installed locally).
