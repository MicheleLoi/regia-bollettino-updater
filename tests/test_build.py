# SPDX-License-Identifier: AGPL-3.0-only
"""
test_build.py — Tests for src/build.py.

Uses a raw scan fixture to exercise the build pipeline without any
GitHub API calls.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from src.build import (
    _infer_jurisdiction,
    _infer_capabilities,
    _spdx_from_license_data,
    _build_skill_entry_from_raw,
    run as build_run,
)
from src.schema.ecosystem import BulletinEcosystem
from src.schema.patterns import BulletinPatterns
from src.schema.skills import BulletinSkills, SkillEntry


# ---------------------------------------------------------------------------
# Unit: _infer_jurisdiction
# ---------------------------------------------------------------------------

def test_infer_jurisdiction_italian():
    readme = "Tool per avvocato italiano. Supporto al Codice Civile."
    assert _infer_jurisdiction(readme) == "IT"


def test_infer_jurisdiction_eu():
    readme = "GDPR compliance tool under European Union regulation."
    assert _infer_jurisdiction(readme) == "EU"


def test_infer_jurisdiction_us():
    readme = "Legal research under the United States Code."
    assert _infer_jurisdiction(readme) == "US"


def test_infer_jurisdiction_unknown():
    readme = "A generic legal AI tool with no jurisdiction keywords."
    assert _infer_jurisdiction(readme) == "Unknown"


# ---------------------------------------------------------------------------
# Unit: _infer_capabilities
# ---------------------------------------------------------------------------

def test_infer_capabilities_contract_review():
    readme = "This tool supports contract review and clause extraction."
    caps = _infer_capabilities(readme)
    assert "contract_review" in caps
    assert "clause_extraction" in caps


def test_infer_capabilities_pseudonymization():
    readme = "Pseudonymization and anonymization of legal documents."
    caps = _infer_capabilities(readme)
    assert "pseudonymization" in caps


def test_infer_capabilities_empty():
    readme = "A tool with no recognisable legal-AI capabilities."
    caps = _infer_capabilities(readme)
    assert caps == []


# ---------------------------------------------------------------------------
# Unit: _spdx_from_license_data
# ---------------------------------------------------------------------------

def test_spdx_agpl():
    data = {"license": {"spdx_id": "AGPL-3.0", "name": "AGPL"}}
    assert _spdx_from_license_data(data) == "AGPL-3.0"


def test_spdx_none():
    assert _spdx_from_license_data(None) == "Unknown"


def test_spdx_noassertion_fallback():
    data = {"license": {"spdx_id": "NOASSERTION", "name": "Proprietary License"}}
    assert _spdx_from_license_data(data) == "Proprietary License"


# ---------------------------------------------------------------------------
# Integration: build_run() with fixture raw scan file
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path, raw_dir: Path) -> Path:
    cfg = {
        "seeds": [{"owner": "test-org", "name": "test-repo", "follow_forks": False}],
        "skill_sources": [],
        "threshold_policy": {
            "active_window_days": 90,
            "warn_repos_changed_pct": 30,
            "warn_new_patterns_count": 5,
            "warn_skills_changed_count": 3,
            "review_flag_max_age_minutes": 120,
        },
        "env_vars": {"github_token": "GITHUB_TOKEN"},
        "output": {
            "raw_dir": str(raw_dir),
            "ecosystem_path": str(tmp_path / "bulletin_ecosystem.json"),
            "patterns_path": str(tmp_path / "bulletin_patterns.json"),
            "skills_path": str(tmp_path / "bulletin_skills.json"),
            "previous_suffix": ".previous.json",
            "review_flag_path": str(tmp_path / ".review_flag"),
        },
    }
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as fh:
        yaml.dump(cfg, fh)
    return config_path


def test_build_run_produces_valid_bulletins(tmp_path):
    """build_run() should produce schema-valid JSON from the raw fixture."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    # Copy the fixture raw file into tmp raw_dir
    shutil.copy(fixtures_dir / "raw_scan_fixture.json", raw_dir / "20260415T103000Z.json")

    config_path = _make_config(tmp_path, raw_dir)
    eco_path, pat_path, skl_path = build_run(config_path=str(config_path))

    # Check files exist
    assert eco_path.exists()
    assert pat_path.exists()
    assert skl_path.exists()

    # Validate schema round-trip
    with open(eco_path) as fh:
        eco_data = json.load(fh)
    eco_bulletin = BulletinEcosystem.model_validate(eco_data)
    assert eco_bulletin.source_count == len(eco_bulletin.repos)
    # Schema bumped 1.0.0 → 1.1.0 with the human_picks feature (additive change).
    assert eco_bulletin.schema_version == "1.1.0"

    with open(pat_path) as fh:
        pat_data = json.load(fh)
    pat_bulletin = BulletinPatterns.model_validate(pat_data)
    assert pat_bulletin.schema_version == "1.0.0"

    with open(skl_path) as fh:
        skl_data = json.load(fh)
    skl_bulletin = BulletinSkills.model_validate(skl_data)
    assert skl_bulletin.schema_version == "1.0.0"


def test_build_run_infers_italian_jurisdiction(tmp_path):
    """The 'codice-civile-parser' fixture entry should be inferred as IT."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(fixtures_dir / "raw_scan_fixture.json", raw_dir / "20260415T103000Z.json")

    config_path = _make_config(tmp_path, raw_dir)
    eco_path, _, _ = build_run(config_path=str(config_path))

    with open(eco_path) as fh:
        eco_data = json.load(fh)

    it_repos = [r for r in eco_data["repos"] if r["inferred_jurisdiction"] == "IT"]
    assert len(it_repos) >= 1


def test_build_run_marks_inactive_repo(tmp_path):
    """The old repo (2025-10-01) should be marked is_active: false with 90d window."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(fixtures_dir / "raw_scan_fixture.json", raw_dir / "20260415T103000Z.json")

    config_path = _make_config(tmp_path, raw_dir)
    eco_path, _, _ = build_run(config_path=str(config_path))

    with open(eco_path) as fh:
        eco_data = json.load(fh)

    inactive = [r for r in eco_data["repos"] if not r["is_active"]]
    # codice-civile-parser was pushed 2025-10-01, which is >90d before 2026-04-15
    assert any(r["name"] == "codice-civile-parser" for r in inactive)


def test_build_run_no_raw_file_raises(tmp_path):
    """build_run() should raise FileNotFoundError if raw_dir is empty."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    config_path = _make_config(tmp_path, raw_dir)

    with pytest.raises(FileNotFoundError):
        build_run(config_path=str(config_path))


# ---------------------------------------------------------------------------
# Skill bulletin: _build_skill_entry_from_raw
# ---------------------------------------------------------------------------

def test_build_skill_entry_from_raw_basic():
    """_build_skill_entry_from_raw should map raw scan fields to SkillEntry."""
    raw = {
        "owner": "anthropics",
        "name": "knowledge-work-plugins",
        "resolved_tier": 1,
        "resolved_jurisdiction": "[?]",
        "resolved_license": "Apache-2.0",
        "meta": {
            "html_url": "https://github.com/anthropics/knowledge-work-plugins",
            "description": "Official plugin",
            "default_branch": "main",
        },
        "skill_md": "",
        "frontmatter": {},
    }
    entry = _build_skill_entry_from_raw(raw, "2026-05-17")
    assert isinstance(entry, SkillEntry)
    assert entry.tier == 1
    assert entry.jurisdiction == "[?]"
    assert entry.last_seen == "2026-05-17"
    assert entry.italian_adaptation_status == "pending"
    assert entry.source_repo == "anthropics/knowledge-work-plugins"


def test_build_skill_entry_tier_defaults_to_2_on_invalid():
    """If resolved_tier is invalid, should default to 2."""
    raw = {
        "owner": "test-owner",
        "name": "test-skill",
        "resolved_tier": "REFUSE",  # invalid for our schema
        "resolved_jurisdiction": "[?]",
        "resolved_license": "Unknown",
        "meta": {},
        "skill_md": "",
        "frontmatter": {},
    }
    entry = _build_skill_entry_from_raw(raw, "2026-05-17")
    assert entry.tier == 2


def test_build_skill_entry_unknown_jurisdiction_maps_to_placeholder():
    """Unrecognised jurisdiction string should fall back to '[?]'."""
    raw = {
        "owner": "test-owner",
        "name": "test-skill",
        "resolved_tier": 2,
        "resolved_jurisdiction": "MARS",  # not in valid set
        "resolved_license": "MIT",
        "meta": {},
        "skill_md": "",
        "frontmatter": {},
    }
    entry = _build_skill_entry_from_raw(raw, "2026-05-17")
    assert entry.jurisdiction == "[?]"


def test_build_run_produces_skills_bulletin_from_fixture(tmp_path):
    """build_run() should produce a valid BulletinSkills JSON from the fixture."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(fixtures_dir / "raw_scan_fixture.json", raw_dir / "20260415T103000Z.json")

    config_path = _make_config(tmp_path, raw_dir)
    _, _, skl_path = build_run(config_path=str(config_path))

    assert skl_path.exists()
    with open(skl_path) as fh:
        skl_data = json.load(fh)

    skl_bulletin = BulletinSkills.model_validate(skl_data)
    # The fixture has 1 skill_sources_raw entry
    assert skl_bulletin.source_count == 1
    assert len(skl_bulletin.skills) == 1
    assert skl_bulletin.skills[0].tier == 1
    assert skl_bulletin.skills[0].jurisdiction == "[?]"


def test_bulletin_skills_json_schema_export():
    """BulletinSkills.model_json_schema() should return a valid JSON Schema dict."""
    schema = BulletinSkills.model_json_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema
    assert "skills" in schema["properties"]
    assert schema.get("title") == "BulletinSkills"


# ---------------------------------------------------------------------------
# Unit: _build_human_pick_eco_entry
# ---------------------------------------------------------------------------

from datetime import date as _date, datetime as _datetime
from src.build import _build_human_pick_eco_entry


def _human_pick_raw(url: str = "https://example-curated.test/", **overrides):
    """Build a synthetic raw human-pick entry for unit tests."""
    config_entry = {
        "url": url,
        "topic": "Curated source for testing",
        "notes": "Synthetic test fixture entry.",
        "added_date": "2026-04-15",
        "tags": ["open-core", "test-fixture"],
        "inferred_jurisdiction": "UK",
        "license": "(proprietary)",
    }
    config_entry.update(overrides.get("config_entry", {}))
    return {
        "url": url,
        "fetched_at": "2026-04-15T10:30:00+00:00",
        "fetch_status": overrides.get("fetch_status", "ok"),
        "meta": overrides.get("meta", {
            "og:title": "Curated OG Title",
            "og:description": "OG Description of the curated source.",
        }),
        "config_entry": config_entry,
    }


def test_build_human_pick_basic_fields():
    """Human pick entry produces an EcosystemRepo with source_type='human_picked' and curator."""
    raw = _human_pick_raw()
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.source_type == "human_picked"
    assert eco.curator == "Michele Loi"
    assert eco.topic == "Curated source for testing"
    assert eco.notes_curatorial == "Synthetic test fixture entry."
    assert eco.added_date == _date(2026, 4, 15)
    assert eco.tags == ["open-core", "test-fixture"]
    assert eco.inferred_jurisdiction == "UK"
    assert eco.license == "(proprietary)"


def test_build_human_pick_github_fields_are_none():
    """Human picks have no GitHub metadata → all GitHub-only fields are None."""
    raw = _human_pick_raw()
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.last_activity is None
    assert eco.stars is None
    assert eco.fork_count is None
    assert eco.is_active is None


def test_build_human_pick_description_fallback_chain():
    """Description prefers og:description → meta[description] → og:title → title → fallback."""
    # og:description present
    raw = _human_pick_raw(meta={"og:description": "OG Desc"})
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.description == "OG Desc"

    # only og:title
    raw = _human_pick_raw(meta={"og:title": "OG Title Only"})
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.description == "OG Title Only"

    # only <title>
    raw = _human_pick_raw(meta={"title": "Bare Title"})
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.description == "Bare Title"

    # nothing — fallback
    raw = _human_pick_raw(meta={})
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.description == "[no description available]"


def test_build_human_pick_name_owner_from_url():
    """Name + owner derived from URL host, with path slug appended when path is non-trivial."""
    # Root path → host_slug only
    raw = _human_pick_raw(url="https://suzielaw.com/")
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.owner == "suzielaw.com"
    assert eco.name == "suzielaw-com"

    # With path → host_slug_path-slug
    raw = _human_pick_raw(url="https://example.com/blog/article-name")
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.owner == "example.com"
    assert eco.name.startswith("example-com_")
    assert "blog" in eco.name
    assert "article-name" in eco.name


def test_build_human_pick_no_capability_inference():
    """Human pick description containing 'contract review' must NOT acquire capability tag.

    This is the design decision: curator's `tags` carry semantic intent;
    running regex inference on short marketing copy produces noisy false matches.
    """
    raw = _human_pick_raw(meta={"og:description": "Contract review and clause extraction service"})
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.inferred_capabilities == []


def test_build_human_pick_added_date_parses_iso_string():
    """added_date as ISO string in config is parsed to date object."""
    raw = _human_pick_raw()
    raw["config_entry"]["added_date"] = "2026-01-15"
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.added_date == _date(2026, 1, 15)


def test_build_human_pick_added_date_invalid_string_yields_none():
    """Malformed added_date string falls back to None without raising."""
    raw = _human_pick_raw()
    raw["config_entry"]["added_date"] = "not-a-date"
    eco = _build_human_pick_eco_entry(raw, _datetime(2026, 4, 15))
    assert eco.added_date is None


def test_build_run_includes_human_picks_from_fixture(tmp_path):
    """build_run() integrates human_picks_raw entries into bulletin_ecosystem.json."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(fixtures_dir / "raw_scan_fixture.json", raw_dir / "20260415T103000Z.json")

    config_path = _make_config(tmp_path, raw_dir)
    eco_path, _, _ = build_run(config_path=str(config_path))

    with open(eco_path) as fh:
        eco_data = json.load(fh)

    # Validate schema
    eco_bulletin = BulletinEcosystem.model_validate(eco_data)

    # Find the human-picked entry
    human_picked = [r for r in eco_bulletin.repos if r.source_type == "human_picked"]
    assert len(human_picked) == 1
    hp = human_picked[0]
    assert hp.url == "https://example-curated.test/"
    assert hp.curator == "Michele Loi"
    assert hp.topic == "Curated source for testing"
    assert hp.tags == ["open-core", "test-fixture", "legal-ai"]
    assert hp.last_activity is None
    assert hp.stars is None


def test_build_run_existing_repos_get_explicit_source_type(tmp_path):
    """github_scanned repos should be stamped with explicit source_type='github_scanned'."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(fixtures_dir / "raw_scan_fixture.json", raw_dir / "20260415T103000Z.json")

    config_path = _make_config(tmp_path, raw_dir)
    eco_path, _, _ = build_run(config_path=str(config_path))

    with open(eco_path) as fh:
        eco_data = json.load(fh)

    github_scanned = [r for r in eco_data["repos"] if r.get("source_type") == "github_scanned"]
    # raw_scan_fixture has 2 github_scanned repos
    assert len(github_scanned) == 2
    for r in github_scanned:
        # All GitHub fields must be populated for github_scanned entries
        assert r.get("last_activity") is not None
        assert r.get("stars") is not None
        assert r.get("fork_count") is not None
        assert r.get("is_active") is not None
