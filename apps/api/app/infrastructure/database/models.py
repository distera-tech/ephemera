"""SQLAlchemy ORM models.

PostgreSQL stores **metadata only**. There is intentionally no column for raw
PDF bytes, extracted text, prompts or raw model output. The validated analysis
(``job_results``) is kept with an expiry so the UI can display it; see
ADR-006.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, default="demo")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    analysis_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Document metadata (never the document itself)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    input_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    document_state: Mapped[str] = mapped_column(String(16), nullable=False)

    # Compute
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    requested_gpu: Mapped[str | None] = mapped_column(String(64))
    actual_gpu: Mapped[str | None] = mapped_column(String(128))
    gpu_vram_mb: Mapped[int | None] = mapped_column(Integer)
    instance_type: Mapped[str | None] = mapped_column(String(128))
    compute_provider: Mapped[str | None] = mapped_column(String(64))
    price_per_hour: Mapped[float | None] = mapped_column(Float)
    instance_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    instance_state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # Control
    force_inference_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Outcome
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    cleanup_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Timeline
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provisioning_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provisioning_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_loading_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inference_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inference_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    destruction_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    destruction_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_jobs_created_at", "created_at"),)


class JobEventRow(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    status: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JobResultRow(Base):
    __tablename__ = "job_results"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    analysis: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class WorkerHeartbeatRow(Base):
    """Worker liveness plus the worker's latest *provider-observed* instance list.

    The API cannot talk to the compute provider (only the worker can), so the
    worker periodically records what the provider actually reports. This is
    what backs the "COMPUTE = 0" claim, not just the database's belief.
    """

    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    compute_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_instances: Mapped[list[str] | None] = mapped_column(JSONB)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_error: Mapped[str | None] = mapped_column(Text)
