# SPDX-License-Identifier: AGPL-3.0-only
"""
Pydantic models for bulletin_skills.json.

This module is the schema source of truth for the BeccarIA catalogo skill consumer.
Field names here are backward-compatible with the bollettino.json legacy format
used by BeccarIA v3.x (legal-tech-cowork/beccaria/bollettino.json).
Schema changes that are breaking require coordination with the catalogo skill consumer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"

# Tier literals matching bollettino.json legacy semantics
TierValue = Literal[1, 2]
TierLabel = Literal["1", "2", "2-WARN", "REFUSE"]

# Jurisdiction codes matching bollettino.json legacy field_notes
JurisdictionCode = Literal["IT", "EU", "US", "other", "none", "[?]"]

# Italian adaptation status matching bollettino.json legacy field_notes
ItalianAdaptationStatus = Literal["pending", "ready", "stale"]


class PublisherInfo(BaseModel):
    """Publisher metadata for a skill entry (mirrors bollettino.json publisher object)."""

    name: str = Field(description="Publisher display name, e.g. 'Anthropic' or 'TerminalSkills'")
    type: str = Field(
        description=(
            "Publisher type: 'anthropic-official' | 'company' | 'community' | 'individual'"
        )
    )
    italian_localized: bool = Field(
        default=False,
        description="True if the publisher has localized this skill for Italian users",
    )


class ReputationInfo(BaseModel):
    """GitHub reputation metrics for a skill entry (mirrors bollettino.json reputation object)."""

    stars: int = Field(default=0, description="GitHub star count at time of last scan")
    last_commit: Optional[str] = Field(
        default=None, description="Date of last commit (YYYY-MM-DD), if available"
    )
    commit_frequency_30d: int = Field(
        default=0, description="Number of commits in the last 30 days"
    )
    contributors: int = Field(default=0, description="Total number of contributors")
    open_issues: int = Field(default=0, description="Number of open issues")
    license: str = Field(
        default="Unknown",
        description="SPDX license identifier, e.g. 'Apache-2.0', or 'Unknown'",
    )
    computed_quality_stars: Optional[int] = Field(
        default=None,
        description="Computed quality score 1-5 (editorial assessment by bollettino-research)",
    )
    computed_trend: Optional[str] = Field(
        default=None,
        description="Trend label: 'in crescita' | 'stabile' | 'in calo' | 'sconosciuto'",
    )


class SkillEntry(BaseModel):
    """
    Metadata for a single Claude skill in the bulletin_skills.json catalog.

    Field names are backward-compatible with the bollettino.json legacy format
    used before BeccarIA v4.0.0. Consumers should treat 'id' as the canonical key.
    """

    id: str = Field(
        description=(
            "Stable unique identifier slug, e.g. 'anthropics-knowledge-work-legal'. "
            "Derived from publisher + skill name, kebab-case."
        )
    )
    name: str = Field(
        description="Human-readable skill name, e.g. 'knowledge-work-plugins / legal'"
    )
    description_it: str = Field(
        description="Italian-language description of what this skill does"
    )

    # Source location
    repo_url: str = Field(
        description="GitHub repository URL, e.g. 'https://github.com/owner/name'"
    )
    skill_path: Optional[str] = Field(
        default=None,
        description=(
            "Relative path to the SKILL.md or plugin manifest within the repository, "
            "e.g. 'legal/.claude-plugin/plugin.json' or 'skills/contract-review/SKILL.md'"
        ),
    )
    source_repo: Optional[str] = Field(
        default=None,
        description="'owner/name' slug derived from repo_url, for machine-readable key access",
    )
    source_url: Optional[str] = Field(
        default=None,
        description="Direct URL to SKILL.md raw content, if available via scan",
    )

    # Classification
    area: Optional[str] = Field(
        default=None,
        description="Practice area label, e.g. 'commerciale', 'penale', 'lavoro', 'generale'",
    )
    jurisdiction: JurisdictionCode = Field(
        description=(
            "Primary jurisdiction: IT|EU|US|other|none|[?]. "
            "IT and EU skip the adaptation prompt at install; "
            "other|none|[?] trigger the Italian-adaptation prompt."
        )
    )
    tier: TierValue = Field(
        description=(
            "Trust tier: 1 = Anthropic-official (publisher anthropics/*); "
            "2 = community vetted (publisher terzo, passa threshold policy). "
            "REFUSE entries are filtered upstream and never reach this file."
        )
    )

    # Publisher
    publisher: Optional[PublisherInfo] = Field(
        default=None,
        description="Publisher metadata block",
    )

    # Reputation
    reputation: Optional[ReputationInfo] = Field(
        default=None,
        description="GitHub reputation metrics at last scan",
    )

    # Editorial
    founder_disclaimer: Optional[str] = Field(
        default=None,
        description="Founder editorial note on usage caveats or limitations",
    )
    recommended_for: Optional[str] = Field(
        default=None,
        description="Short description of the ideal use-case scenario for this skill",
    )

    # Status
    added_to_bollettino: Optional[str] = Field(
        default=None,
        description="Date when this entry was first added (YYYY-MM-DD)",
    )
    last_seen: Optional[str] = Field(
        default=None,
        description=(
            "Date of last scan that confirmed this skill still exists (YYYY-MM-DD). "
            "Populated by updater scan step."
        ),
    )
    italian_adaptation_status: ItalianAdaptationStatus = Field(
        default="pending",
        description=(
            "pending = never adapted; "
            "ready = adaptation template generated and validated at least once; "
            "stale = upstream skill changed after adaptation."
        ),
    )

    # Alerts
    critical_alert: bool = Field(
        default=False,
        description="True if there is a critical security or compatibility issue",
    )
    critical_alert_message: Optional[str] = Field(
        default=None,
        description="Message displayed to the user when critical_alert is True",
    )
    critical_alert_severity: Optional[str] = Field(
        default=None,
        description="Severity label: 'low' | 'medium' | 'high' | 'critical'",
    )

    # Free-form notes
    notes: Optional[str] = Field(
        default=None,
        description=(
            "Optional free-text annotations. Not populated automatically; "
            "edit the JSON file directly or via bollettino-research routine."
        ),
    )


class BulletinSkills(BaseModel):
    """
    Top-level envelope for bulletin_skills.json.

    Consumed by the BeccarIA catalogo skill (autonomous fetch from
    bulletins.micheleloi.pro/bulletin_skills.json).
    Schema version: 1.0.0

    Backward-compatible with bollettino.json legacy format (BeccarIA <= v3.x).
    """

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Semantic version of this schema (e.g. '1.0.0')",
    )
    generated_at: datetime = Field(
        description="ISO 8601 timestamp when this bulletin was generated"
    )
    source_count: int = Field(
        description="Number of skill entries included in this bulletin (len(skills))"
    )
    skills: list[SkillEntry] = Field(default_factory=list)
