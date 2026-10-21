# Feature 02 — Ingestion, metadata, OCR / transcription

Status: NOT STARTED

## Tasks
- [ ] File type detection (magic bytes + extension)
- [ ] Generic metadata (size, hash, mime, counts, timestamps)
- [ ] Text extractors: txt/md/csv/json/html/eml
- [ ] PDF text extraction with per-page OCR fallback for scanned pages
- [ ] DOCX / PPTX / XLSX extraction
- [ ] Legacy office formats (doc/xls/ppt/rtf/odt) via LibreOffice conversion
- [ ] Image OCR (png/jpg/tiff/...) with EXIF metadata
- [ ] Bronze zone: pages + tabular records written as Parquet, exposed as `pages` / `records` views
- [ ] Async ingestion worker + status tracking, reprocess endpoint
- [ ] Upload / list / get / download API
- [ ] Tests

## Log
