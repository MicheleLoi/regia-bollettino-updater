# SPDX-License-Identifier: AGPL-3.0-only
"""
Pydantic models for bulletin_patterns.json.

This module is the schema source of truth for the BeccarIA schemi-di-ragionamento skill consumer.
Field names here take precedence over any consumer-side expectations documented in Brief A.
Schema changes that are breaking require coordination with the skill consumer (BeccarIA).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"

ConfidenceLevel = Literal["low", "medium", "high"]


class Pattern(BaseModel):
    """
    A single extracted legal-AI prompt pattern from a source repository.

    AGPL attribution note: prompt_template may contain text derived from
    AGPL-licensed source code or README. The source_* fields carry the
    attribution chain required for downstream AGPL compliance.
    """

    task_name: str = Field(
        description="Canonical task identifier, e.g. 'contract_clause_extraction', 'case_summarization'"
    )
    description: str = Field(description="Human-readable description of what this pattern does")
    prompt_template: str = Field(
        description=(
            "The prompt text that Claude applies in conversation. "
            "Verbatim when clearly delimited in source README (e.g. code-fenced with label); "
            "paraphrased/reconstructed when confidence is medium/low."
        )
    )
    example_input: Optional[str] = Field(
        default=None, description="Optional example input document or query"
    )
    example_output: Optional[str] = Field(
        default=None, description="Optional example expected output"
    )
    source_repo: str = Field(description="'owner/name' slug of the source repository")
    source_owner: str = Field(description="GitHub owner of the source repository")
    source_url: str = Field(description="Full HTTPS URL of the source repository")
    source_commit: Optional[str] = Field(
        default=None,
        description="Git commit hash from which this pattern was extracted, if available",
    )
    source_license: str = Field(
        description="SPDX identifier of the source repository license, e.g. 'AGPL-3.0-only'"
    )
    extraction_confidence: ConfidenceLevel = Field(
        description=(
            "'high' = prompt_template is clearly delimited in source (code-fence + label); "
            "'medium' = plausible extraction requiring minor interpretation; "
            "'low' = heuristically reconstructed."
        )
    )


class BulletinPatterns(BaseModel):
    """
    Top-level envelope for bulletin_patterns.json.

    Consumed by the BeccarIA schemi-di-ragionamento skill.
    Schema version: 1.0.0
    """

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Semantic version of this schema (e.g. '1.0.0')",
    )
    generated_at: datetime = Field(description="ISO 8601 timestamp when this bulletin was generated")
    source_count: int = Field(
        description="Number of source repos from which patterns were extracted"
    )
    patterns: list[Pattern] = Field(default_factory=list)
