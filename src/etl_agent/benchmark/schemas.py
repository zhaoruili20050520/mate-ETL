"""Pydantic schemas for benchmark reports."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CaseResult(BaseModel):
    """Result of a single benchmark test case."""

    case_id: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0, description="0.0-1.0 accuracy score")
    message: str = ""
    details: dict[str, object] = Field(default_factory=dict)


class LevelResult(BaseModel):
    """Aggregated results for one benchmark level (L0/L1/L2)."""

    level: str
    description: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    skipped_cases: int = 0
    accuracy_rate: float = Field(ge=0.0, le=1.0)
    interception_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="L1 only: fraction of faults correctly intercepted",
    )
    false_positive_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="L1 only: fraction of valid inputs incorrectly rejected",
    )
    avg_latency_ms: float = 0.0
    case_results: list[CaseResult] = Field(default_factory=list)


class BenchmarkReport(BaseModel):
    """Complete benchmark report across all levels."""

    version: str = "1.0"
    timestamp: str = ""
    overall_accuracy: float = Field(ge=0.0, le=1.0)
    overall_interception: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="L1 interception rate, 0 if L1 not run",
    )
    levels: list[LevelResult] = Field(default_factory=list)
    summary: str = ""


__all__ = [
    "BenchmarkReport",
    "CaseResult",
    "LevelResult",
]
