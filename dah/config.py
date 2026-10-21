"""Runtime configuration, read from ``DAH_*`` environment variables (or ``.env``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DAH_", env_file=".env", extra="ignore")

    # Root of all persisted state: catalog db (sqlite), blobs, parquet zones.
    data_dir: Path = Path("./data")
    # Optional external catalog, e.g. postgresql+psycopg://user:pw@db/dah
    database_url: str | None = None

    # Bearer token for the admin API, UI and MCP server.
    admin_token: str = "change-me"
    # Signs SSO session tokens and derives the settings-encryption key.
    secret_key: str = "dev-secret-change-me"
    # Externally reachable base URL, used for SSO redirect / ACS URLs.
    public_base_url: str = "http://localhost:8000"

    # Ingestion
    ocr_languages: str = "eng"
    ocr_min_page_chars: int = 25  # pages with less extracted text get OCR'd
    ingest_workers: int = 2
    max_upload_mb: int = 512
    soffice_bin: str = "soffice"

    # Pipelines
    pipeline_timeout_s: int = 300
    pipeline_memory_mb: int = 2048

    # SSO
    session_ttl_s: int = 8 * 3600

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def lake_dir(self) -> Path:
        return self.data_dir / "lake"

    @property
    def catalog_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'catalog.db').resolve()}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.blob_dir, self.lake_dir, self.lake_dir / "bronze", self.lake_dir / "tables", self.data_dir / "tmp"):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
