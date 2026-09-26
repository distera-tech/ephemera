"""Structured analysis schema returned by the model and validated before display."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

KeyPoint = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=600)
    severity: Severity


class EntityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=60)


class DocumentAnalysis(BaseModel):
    """What the model must return. Bounded sizes so model output cannot break the UI."""

    model_config = ConfigDict(extra="forbid")

    document_type: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=4000)
    key_points: list[KeyPoint] = Field(default_factory=list, max_length=15)
    potential_risks: list[RiskItem] = Field(default_factory=list, max_length=15)
    entities: list[EntityItem] = Field(default_factory=list, max_length=30)
    requires_human_review: bool


def analysis_json_schema() -> dict[str, object]:
    return DocumentAnalysis.model_json_schema()
