"""Catalog database: every document, pipeline, lake table and SSO object, with full version history."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


def dumps(v: Any) -> str:
    return json.dumps(v, default=str, sort_keys=True)


# --------------------------------------------------------------------------- documents


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # Logical path; uploading to an existing path creates a new version.
    path: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(512))
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", order_by="DocumentVersion.version", cascade="all, delete-orphan"
    )

    def version_row(self, version: int | None = None) -> "DocumentVersion | None":
        v = version or self.current_version
        return next((x for x in self.versions if x.version == v), None)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|processing|ready|failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    text: Mapped[str] = mapped_column(Text, default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    bronze_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # pages parquet
    records_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # tabular rows parquet
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), default="admin")
    restored_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[Document] = relationship(back_populates="versions")

    @property
    def meta(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    @meta.setter
    def meta(self, value: dict) -> None:
        self.metadata_json = dumps(value)


# --------------------------------------------------------------------------- pipelines


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    versions: Mapped[list["PipelineVersion"]] = relationship(
        back_populates="pipeline", order_by="PipelineVersion.version", cascade="all, delete-orphan"
    )

    def version_row(self, version: int | None = None) -> "PipelineVersion | None":
        v = version or self.current_version
        return next((x for x in self.versions if x.version == v), None)


class PipelineVersion(Base):
    __tablename__ = "pipeline_versions"
    __table_args__ = (UniqueConstraint("pipeline_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(ForeignKey("pipelines.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # python|sql
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual|on_ingest
    output_table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    code: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), default="admin")
    restored_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pipeline: Mapped[Pipeline] = relationship(back_populates="versions")

    @property
    def params(self) -> dict:
        return json.loads(self.params_json or "{}")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(ForeignKey("pipelines.id", ondelete="CASCADE"), index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|succeeded|failed
    log: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    outputs_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- lake tables


class LakeTable(Base):
    __tablename__ = "lake_tables"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    versions: Mapped[list["LakeTableVersion"]] = relationship(
        back_populates="table", order_by="LakeTableVersion.version", cascade="all, delete-orphan"
    )

    def version_row(self, version: int | None = None) -> "LakeTableVersion | None":
        v = version or self.current_version
        return next((x for x in self.versions if x.version == v), None)


class LakeTableVersion(Base):
    __tablename__ = "lake_table_versions"
    __table_args__ = (UniqueConstraint("table_name", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(ForeignKey("lake_tables.name", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(1024))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    schema_json: Mapped[str] = mapped_column(Text, default="[]")
    pipeline_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restored_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    table: Mapped[LakeTable] = relationship(back_populates="versions")


# --------------------------------------------------------------------------- settings / sso


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IdentityProvider(Base):
    __tablename__ = "identity_providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug, used in URLs
    kind: Mapped[str] = mapped_column(String(8))  # saml|oidc
    display_name: Mapped[str] = mapped_column(String(255))
    config_json: Mapped[str] = mapped_column(Text, default="{}")  # secrets encrypted inside
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DataEndpoint(Base):
    __tablename__ = "data_endpoints"

    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[str] = mapped_column(Text, default="")
    source_table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON policy: providers, allowed_users, allowed_groups, row_filters, columns, column_groups, max_rows
    policy_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def policy(self) -> dict:
        return json.loads(self.policy_json or "{}")


class AccessLog(Base):
    __tablename__ = "access_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(String(128), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(64))
    allowed: Mapped[bool] = mapped_column(Boolean)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- engine

_engine: Engine | None = None
_Session: sessionmaker | None = None


def engine() -> Engine:
    global _engine, _Session
    if _engine is None:
        url = get_settings().catalog_url
        kw: dict[str, Any] = {}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, future=True, **kw)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _pragmas(conn, _rec):  # pragma: no cover - trivial
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        Base.metadata.create_all(_engine)
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine (tests switch data dirs)."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


@contextmanager
def session_scope() -> Iterator[Session]:
    engine()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# --------------------------------------------------------------------------- users & permissions


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # "local" or the identity provider id that provisioned the account
    source: Mapped[str] = mapped_column(String(64), default="local")
    external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    groups_json: Mapped[str] = mapped_column(Text, default="[]")  # admin-assigned groups
    idp_groups_json: Mapped[str] = mapped_column(Text, default="[]")  # from the IdP at last login
    attributes_json: Mapped[str] = mapped_column(Text, default="{}")  # IdP attributes at last login
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    idp_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # via provider admin_groups
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # bumped on password change / disable to invalidate outstanding sessions
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("source", "external_id"),)

    @property
    def local_groups(self) -> list[str]:
        return json.loads(self.groups_json or "[]")

    @property
    def idp_groups(self) -> list[str]:
        return json.loads(self.idp_groups_json or "[]")

    @property
    def groups(self) -> list[str]:
        return sorted(set(self.local_groups) | set(self.idp_groups))

    @property
    def attributes(self) -> dict:
        return json.loads(self.attributes_json or "{}")

    @property
    def admin(self) -> bool:
        return bool(self.is_admin or self.idp_admin)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FolderGrant(Base):
    __tablename__ = "folder_grants"
    __table_args__ = (UniqueConstraint("path", "principal_type", "principal"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(1024), index=True)  # "" = root; applies to all subfolders
    principal_type: Mapped[str] = mapped_column(String(16))  # user | group | authenticated
    principal: Mapped[str] = mapped_column(String(320), default="")
    level: Mapped[str] = mapped_column(String(8))  # read | write | manage
    created_by: Mapped[str] = mapped_column(String(255), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
