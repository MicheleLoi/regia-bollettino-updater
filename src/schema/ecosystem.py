# SPDX-License-Identifier: AGPL-3.0-only
"""
Pydantic models for bulletin_ecosystem.json.

This module is the schema source of truth for the BeccarIA ecosystem-scout skill consumer.
Field names here take precedence over any consumer-side expectations documented in Brief A.
Schema changes that are breaking require coordination with the skill consumer (BeccarIA).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"


class EcosystemRepo(BaseModel):
    """Metadata for a single repository in the legal-AI open-source ecosystem."""

    name: str = Field(description="Repository name (without owner prefix)")
    owner: str = Field(description="GitHub owner (user or org)")
    url: str = Field(description="Full HTTPS URL, e.g. https://github.com/owner/name")
    description: str = Field(description="Repository description (from GitHub or README first line)")
    license: str = Field(
        description="SPDX license identifier, e.g. 'AGPL-3.0-only', 'Apache-2.0', or 'Unknown'"
    )
    inferred_jurisdiction: str = Field(
        description=(
            "Inferred primary jurisdiction: 'IT', 'EU', 'US', 'CH', etc., or 'Unknown'. "
            "Derived from README language, keywords, and explicit mentions of legal codes."
        )
    )
    inferred_capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Inferred list of legal-AI capability tags, e.g. "
            "['contract_review', 'clause_extraction', 'pseudonymization', 'case_summarization']"
        ),
    )
    last_activity: datetime = Field(description="ISO 8601 timestamp of last commit or push activity")
    stars: int = Field(description="GitHub star count at time of scan")
    fork_count: int = Field(description="Number of forks at time of scan")
    is_active: bool = Field(
        description=(
            "True if last_activity is within the configured active_window_days threshold. "
            "Computed by build.py; not stored in raw scan output."
        )
    )
    notes: Optional[str] = Field(
        default=None,
        description=(
            "Optional free-text annotations added by the founder post-build. "
            "Not populated automatically; edit the JSON file directly."
        ),
    )


class BulletinEcosystem(BaseModel):
    """
    Top-level envelope for bulletin_ecosystem.json.

    Consumed by the BeccarIA ecosystem-scout skill.
    Schema version: 1.0.0
    """

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Semantic version of this schema (e.g. '1.0.0')",
    )
    generated_at: datetime = Field(description="ISO 8601 timestamp when this bulletin was generated")
    source_count: int = Field(description="Number of repos included in this bulletin (len(repos))")
    repos: list[EcosystemRepo] = Field(default_factory=list)
