from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.domain import uuid_str


class RoutineDispatchPermit(Base):
    __tablename__ = "routine_dispatch_permits"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    publication_id: Mapped[str] = mapped_column(ForeignKey("pin_publications.id", ondelete="RESTRICT"), index=True, nullable=False)
    dispatch_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="buffer")
    approval_id: Mapped[str] = mapped_column(ForeignKey("pin_approvals.id", ondelete="RESTRICT"), nullable=False)
    pinterest_board_record_id: Mapped[str] = mapped_column(ForeignKey("pinterest_boards.id", ondelete="RESTRICT"), nullable=False)
    publication_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduled_for_snapshot: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quality_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    quality_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    duplicate_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    readiness_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    authorized_by: Mapped[str] = mapped_column(String(255), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(255))
    revoke_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("dispatch_provider = 'buffer'", name="ck_routine_dispatch_permit_provider"),
        CheckConstraint("status IN ('ACTIVE','CONSUMED','REVOKED','EXPIRED')", name="ck_routine_dispatch_permit_status"),
        Index(
            "uq_routine_dispatch_permit_active",
            "publication_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_routine_dispatch_permit_expires_at", "expires_at"),
    )


class RoutinePublishingControl(Base):
    __tablename__ = "routine_publishing_control"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="PAUSED")
    pause_reason: Mapped[str | None] = mapped_column(String(255))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_by: Mapped[str | None] = mapped_column(String(255))
    last_unknown_publication_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("state IN ('PAUSED','DRY_RUN','LIVE')", name="ck_routine_publishing_control_state"),
    )


class RoutinePublishingRun(Base):
    __tablename__ = "routine_publishing_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING")
    scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    eligible: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claimed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    published: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unknown: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED','BLOCKED')", name="ck_routine_publishing_run_status"),
        Index(
            "uq_routine_publishing_run_running",
            "status",
            unique=True,
            sqlite_where=text("status = 'RUNNING'"),
            postgresql_where=text("status = 'RUNNING'"),
        ),
        Index("ix_routine_publishing_run_started_at", "started_at"),
    )


class RoutineAttemptBoundary(Base):
    __tablename__ = "routine_attempt_boundaries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("publication_attempts.id", ondelete="RESTRICT"), unique=True, index=True, nullable=False)
    publication_id: Mapped[str] = mapped_column(ForeignKey("pin_publications.id", ondelete="RESTRICT"), index=True, nullable=False)
    routine_dispatch_permit_id: Mapped[str] = mapped_column(ForeignKey("routine_dispatch_permits.id", ondelete="RESTRICT"), index=True, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_mutation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    safe_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
