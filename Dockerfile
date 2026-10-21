# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG WITH_LIBREOFFICE=1
ARG MONACO_VERSION=0.52.2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DAH_DATA_DIR=/data

# OCR (tesseract), SAML signature validation (xmlsec), legacy office conversion (LibreOffice)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng \
      libxml2-dev libxmlsec1-dev libxmlsec1-openssl libxslt1-dev pkg-config gcc \
      curl ca-certificates tini \
 && if [ "$WITH_LIBREOFFICE" = "1" ]; then \
      apt-get install -y --no-install-recommends libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui; \
    fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md THIRD_PARTY_LICENSES.md ./
COPY dah ./dah

# Vendor the Monaco editor so the UI works fully offline (MIT licensed).
RUN mkdir -p dah/web/static/vendor \
 && curl -fsSL "https://registry.npmjs.org/monaco-editor/-/monaco-editor-${MONACO_VERSION}.tgz" | tar xz -C /tmp \
 && mv /tmp/package/min dah/web/static/vendor/monaco \
 && cp /tmp/package/LICENSE dah/web/static/vendor/monaco/LICENSE \
 && rm -rf /tmp/package

# lxml/xmlsec must be built against the same libxml2 for python3-saml.
RUN pip install --no-binary lxml,xmlsec ".[saml,postgres]"

RUN useradd --system --uid 10001 --home /data dah && mkdir -p /data && chown dah /data
USER dah
VOLUME ["/data"]
EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://localhost:8000/healthz || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["dah", "serve", "--host", "0.0.0.0", "--port", "8000"]
