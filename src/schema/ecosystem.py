# SPDX-License-Identifier: AGPL-3.0-only
"""
Pydantic models for bulletin_ecosystem.json.

This module is the schema source of truth for the BeccarIA ecosystem-scout skill consumer.
Field names here take precedence over any consumer-side expectations documented in Brief A.
Schema changes that are breaking require coordination with the skill consumer (BeccarIA).

## Schema versions

- 1.0.0: initial schema (GitHub-scanned repos only)
- 1.1.0: additive — adds `source_type` (default "github_scanned"), and human-pick
  specific fields (`topic`, `notes_curatorial`, `added_date`, `tags`, `curator`).
  GitHub-only fields (`last_activity`, `stars`, `fork_count`, `is_active`) become
  Optional to accommodate human-picked entries that lack GitHub metadata. A
  model_validator enforces that github_scanned entries still populate them.
  Backward-compatible: previous v1.0.0 bulletins (and consumer skills that ignore
  unknown fields) deserialize without changes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.1.0"


class EcosystemRepo(BaseModel):
    """Metadata for a single repository in the legal-AI open-source ecosystem.

    Supports two source provenance modes via the `source_type` field:
    - "github_scanned" (default): result of the automatic GitHub API scan. The
      `last_activity`, `stars`, `fork_count`, `is_active` fields MUST be non-null
      for this source_type (enforced by the model_validator).
    - "human_picked": curated entry added manually by the founder via the
      `human_picks:` section of `config.yaml`. GitHub-only fields are typically
      null (the human pick is a website or article, not a GitHub repo). The
      curator's editorial intent lives in `topic`, `notes_curatorial`, `tags`.
    """

    name: str = Field(description="Repository name (without owner prefix) or URL-host slug for human picks")
    owner: str = Field(description="GitHub owner (user or org) or URL host for human picks (e.g. 'suzielaw.com')")
    url: str = Field(description="Full HTTPS URL (e.g. https://github.com/owner/name or https://suzielaw.com/)")
    description: str = Field(
        description=(
            "Repository description. For github_scanned: from GitHub or README first line. "
            "For human_picked: from og:description / meta[description] / <title>, "
            "or '[no description available]' if all are missing."
        )
    )
    license: str = Field(
        description="SPDX license identifier, e.g. 'AGPL-3.0-only', 'Apache-2.0', or 'Unknown'"
    )
    inferred_jurisdiction: str = Field(
        description=(
            "Inferred primary jurisdiction: 'IT', 'EU', 'US', 'CH', etc., or 'Unknown'. "
            "For github_scanned: derived from README language/keywords. "
            "For human_picked: from the optional config.yaml override or 'Unknown'."
        )
    )
    inferred_capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Inferred list of legal-AI capability tags, e.g. "
            "['contract_review', 'clause_extraction', 'pseudonymization', 'case_summarization']. "
            "Always empty for human_picked entries — curator's `tags` carry that semantic instead "
            "(running regex inference on short marketing copy produces noisy false matches)."
        ),
    )

    # GitHub-only fields. Required for source_type='github_scanned'
    # (enforced by validator); typically null for source_type='human_picked'.
    last_activity: Optional[datetime] = Field(
        default=None,
        description="ISO 8601 timestamp of last commit or push activity (required for github_scanned)",
    )
    stars: Optional[int] = Field(
        default=None,
        description="GitHub star count at time of scan (required for github_scanned)",
    )
    fork_count: Optional[int] = Field(
        default=None,
        description="Number of forks at time of scan (required for github_scanned)",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description=(
            "True if last_activity is within the configured active_window_days threshold. "
            "Computed by build.py; not stored in raw scan output. "
            "Null for human_picked entries (no last_activity to evaluate)."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description=(
            "Optional free-text annotations added by the founder POST-build. "
            "Not populated automatically; edit the JSON file directly. "
            "Distinct from `notes_curatorial` (which lives in config.yaml at curation time)."
        ),
    )

    # Source provenance + human-pick specific fields (added in v1.1.0).
    source_type: Literal["github_scanned", "human_picked"] = Field(
        default="github_scanned",
        description=(
            "Provenance of this entry. 'github_scanned' = result of automatic API scan; "
            "'human_picked' = curated manually by the founder via config.yaml human_picks. "
            "Default preserves backward compat with v1.0.0 bulletins."
        ),
    )
    topic: Optional[str] = Field(
        default=None,
        description=(
            "Curator's short headline for a human-picked entry "
            "(e.g. 'Open-core legal AI workspace UK'). "
            "Null for github_scanned entries."
        ),
    )
    notes_curatorial: Optional[str] = Field(
        default=None,
        description=(
            "Curator's rationale at the time of adding this human pick (free text from "
            "config.yaml). Distinct from `notes` (which is post-hoc, set by editing the JSON)."
        ),
    )
    added_date: Optional[date] = Field(
        default=None,
        description="Date the founder added this human pick (ISO 8601 date). Null for github_scanned.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Curator-assigned flat tag list (e.g. ['open-core', 'uk', 'managed-legal-services']). "
            "Orthogonal to `inferred_capabilities`. Empty for github_scanned by default; "
            "may be populated for human_picked entries from config.yaml."
        ),
    )
    curator: Optional[str] = Field(
        default=None,
        description=(
            "Identity of the curator who added a human-picked entry "
            "(e.g. 'Michele Loi'). Null for github_scanned entries — "
            "the source identity for those is implicit (the regia-bollettino-updater)."
        ),
    )

    @model_validator(mode="after")
    def _require_github_fields_for_scanned(self) -> "EcosystemRepo":
        """Enforce: github_scanned entries must populate GitHub-only metadata.

        Preserves the v1.0.0 invariant: an entry claiming to come from an API
        scan must carry the metadata produced by that scan. Human picks are
        exempt because they typically lack a `last_activity` / star count.
        """
        if self.source_type == "github_scanned":
            missing = [
                field
                for field in ("last_activity", "stars", "fork_count", "is_active")
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError(
                    f"source_type='github_scanned' requires non-null values for: "
                    f"{', '.join(missing)}"
                )
        return self


class BulletinEcosystem(BaseModel):
    """
    Top-level envelope for bulletin_ecosystem.json.

    Consumed by the BeccarIA ecosystem-scout skill.
    Schema version: 1.1.0
    """

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Semantic version of this schema (e.g. '1.1.0')",
    )
    generated_at: datetime = Field(description="ISO 8601 timestamp when this bulletin was generated")
    source_count: int = Field(description="Number of repos included in this bulletin (len(repos))")
    repos: list[EcosystemRepo] = Field(default_factory=list)
