# SPDX-License-Identifier: AGPL-3.0-only
"""
test_review.py — Tests for src/review.py diff logic and threshold gate.

All I/O interactions (stdin, file system) are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.review import _diff_ecosystems, _diff_patterns, _diff_skills


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture(name: str) -> dict:
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / name) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# _diff_ecosystems
# ---------------------------------------------------------------------------

def test_diff_ecosystems_no_previous():
    curr = _load_fixture("bulletin_ecosystem_current.json")
    diff = _diff_ecosystems(None, curr)
    assert len(diff["added"]) == 2
    assert diff["removed"] == []
    assert diff["changed"] == []


def test_diff_ecosystems_with_previous():
    prev = _load_fixture("bulletin_ecosystem_previous.json")
    curr = _load_fixture("bulletin_ecosystem_current.json")
    diff = _diff_ecosystems(prev, curr)

    # test-org/test-repo changed (description, capabilities, stars)
    changed_keys = [c["repo"] for c in diff["changed"]]
    assert "test-org/test-repo" in changed_keys

    # italian-legal-lab/codice-civile-parser is new
    added_keys = [f"{r['owner']}/{r['name']}" for r in diff["added"]]
    assert "italian-legal-lab/codice-civile-parser" in added_keys


def test_diff_ecosystems_no_changes():
    curr = _load_fixture("bulletin_ecosystem_current.json")
    diff = _diff_ecosystems(curr, curr)  # same as previous
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["changed"] == []


# ---------------------------------------------------------------------------
# _diff_patterns
# ---------------------------------------------------------------------------

def test_diff_patterns_no_previous():
    curr = _load_fixture("bulletin_patterns_current.json")
    diff = _diff_patterns(None, curr)
    assert len(diff["added"]) == 1
    assert diff["removed"] == []


def test_diff_patterns_no_changes():
    curr = _load_fixture("bulletin_patterns_current.json")
    diff = _diff_patterns(curr, curr)
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["changed"] == []


def test_diff_patterns_detects_removed():
    prev = _load_fixture("bulletin_patterns_current.json")
    # Current has no patterns
    curr = {
        "schema_version": "1.0.0",
        "generated_at": "2026-04-20T00:00:00+00:00",
        "source_count": 0,
        "patterns": [],
    }
    diff = _diff_patterns(prev, curr)
    assert len(diff["removed"]) == 1
    assert diff["removed"][0]["task_name"] == "contract_review"


# ---------------------------------------------------------------------------
# Threshold: warning triggers
# ---------------------------------------------------------------------------

def test_threshold_pct_calculation():
    """
    2 repos changed out of 2 total = 100% → above 30% threshold.
    Verify that the diff structure drives the threshold logic correctly.
    """
    prev = _load_fixture("bulletin_ecosystem_previous.json")
    curr = _load_fixture("bulletin_ecosystem_current.json")
    diff = _diff_ecosystems(prev, curr)

    total = len(curr["repos"])
    changed_count = len(diff["added"]) + len(diff["removed"]) + len(diff["changed"])
    changed_pct = (changed_count / total * 100) if total > 0 else 0

    # 1 added + 0 removed + 1 changed = 2 out of 2 = 100% > 30%
    assert changed_pct > 30


def test_threshold_new_patterns_count():
    """More than warn_new_patterns_count new patterns should flag."""
    warn_count = 5
    curr = _load_fixture("bulletin_patterns_current.json")
    diff = _diff_patterns(None, curr)  # all patterns are "new"
    # Only 1 new pattern in fixture — below threshold of 5
    assert len(diff["added"]) < warn_count


# ---------------------------------------------------------------------------
# _diff_skills
# ---------------------------------------------------------------------------

def test_diff_skills_no_previous():
    curr = _load_fixture("bulletin_skills_current.json")
    diff = _diff_skills(None, curr)
    assert len(diff["added"]) == 2  # 2 skills in current fixture
    assert diff["removed"] == []
    assert diff["changed"] == []


def test_diff_skills_with_previous_detects_new_entry():
    prev = _load_fixture("bulletin_skills_previous.json")
    curr = _load_fixture("bulletin_skills_current.json")
    diff = _diff_skills(prev, curr)

    # 1 new skill (terminalskills-contract-review not in previous)
    added_ids = [s["id"] for s in diff["added"]]
    assert "terminalskills-contract-review" in added_ids

    # existing skill unchanged (same tier, jurisdiction, status)
    assert diff["removed"] == []


def test_diff_skills_no_changes():
    curr = _load_fixture("bulletin_skills_current.json")
    diff = _diff_skills(curr, curr)
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["changed"] == []


def test_diff_skills_detects_removed():
    prev = _load_fixture("bulletin_skills_current.json")
    curr = {
        "schema_version": "1.0.0",
        "generated_at": "2026-05-20T00:00:00+00:00",
        "source_count": 0,
        "skills": [],
    }
    diff = _diff_skills(prev, curr)
    assert len(diff["removed"]) == 2


def test_diff_skills_detects_tier_change():
    prev = _load_fixture("bulletin_skills_current.json")
    import copy
    curr = copy.deepcopy(prev)
    # Change tier for first skill
    curr["skills"][0]["tier"] = 2
    diff = _diff_skills(prev, curr)
    changed_ids = [c["skill"] for c in diff["changed"]]
    assert curr["skills"][0]["id"] in changed_ids


def test_diff_skills_threshold_warning_count():
    """3 or more skill changes should be above the warn_skills_changed_count=3 threshold."""
    warn_count = 3
    curr = _load_fixture("bulletin_skills_current.json")
    diff = _diff_skills(None, curr)  # all 2 skills are "new"
    # 2 new additions — not above threshold of 3
    total_changes = len(diff["added"]) + len(diff["removed"]) + len(diff["changed"])
    assert total_changes < warn_count + 1  # 2 < 4


# ---------------------------------------------------------------------------
# _split_eco_by_source_type + _diff_eco_human_picks (human_picks support)
# ---------------------------------------------------------------------------

from src.review import _split_eco_by_source_type, _diff_eco_human_picks


def test_split_eco_by_source_type_separates_correctly():
    """Mixed bulletin splits cleanly into github_scanned and human_picked sub-bulletins."""
    curr = _load_fixture("bulletin_ecosystem_with_human_picks_current.json")
    gh, hp = _split_eco_by_source_type(curr)
    # Fixture has 1 github_scanned + 2 human_picked
    assert len(gh["repos"]) == 1
    assert len(hp["repos"]) == 2
    assert all(r["source_type"] == "github_scanned" for r in gh["repos"])
    assert all(r["source_type"] == "human_picked" for r in hp["repos"])


def test_split_eco_by_source_type_legacy_previous_no_source_type():
    """Forward-compat: previous bulletin entries without source_type default to github_scanned.

    Without this default, the first diff after the v1.0.0 → v1.1.0 upgrade would
    misclassify every previous entry as human-picked (because v1.0.0 bulletins
    never wrote the field).
    """
    legacy_bulletin = {
        "schema_version": "1.0.0",
        "generated_at": "2026-04-01T00:00:00+00:00",
        "source_count": 2,
        "repos": [
            {"name": "a", "owner": "x", "url": "https://github.com/x/a"},  # no source_type
            {"name": "b", "owner": "y", "url": "https://github.com/y/b"},  # no source_type
        ],
    }
    gh, hp = _split_eco_by_source_type(legacy_bulletin)
    assert len(gh["repos"]) == 2
    assert len(hp["repos"]) == 0


def test_split_eco_by_source_type_none_input():
    """When the previous bulletin is None (first run), split returns (None, None)."""
    gh, hp = _split_eco_by_source_type(None)
    assert gh is None
    assert hp is None


def test_diff_eco_human_picks_detects_added():
    """A human pick present in current but not in previous shows up under 'added'."""
    prev = _load_fixture("bulletin_ecosystem_with_human_picks_previous.json")
    curr = _load_fixture("bulletin_ecosystem_with_human_picks_current.json")

    _, prev_hp = _split_eco_by_source_type(prev)
    _, curr_hp = _split_eco_by_source_type(curr)

    diff = _diff_eco_human_picks(prev_hp, curr_hp)
    added_owners = [r["owner"] for r in diff["added"]]
    assert "another-curated.com" in added_owners


def test_diff_eco_human_picks_detects_changed_curatorial_fields():
    """Changes to topic/tags/notes_curatorial/description are detected."""
    prev = _load_fixture("bulletin_ecosystem_with_human_picks_previous.json")
    curr = _load_fixture("bulletin_ecosystem_with_human_picks_current.json")

    _, prev_hp = _split_eco_by_source_type(prev)
    _, curr_hp = _split_eco_by_source_type(curr)

    diff = _diff_eco_human_picks(prev_hp, curr_hp)
    # example-curated.test/example-curated-test should be in changed (topic, tags, description, notes_curatorial all changed)
    changed_keys = [c["repo"] for c in diff["changed"]]
    assert "example-curated.test/example-curated-test" in changed_keys
    # Verify the watched fields are surfaced
    changed_entry = next(c for c in diff["changed"] if c["repo"] == "example-curated.test/example-curated-test")
    assert "topic" in changed_entry["diffs"]
    assert "tags" in changed_entry["diffs"]
    assert "notes_curatorial" in changed_entry["diffs"]
    assert "description" in changed_entry["diffs"]


def test_diff_eco_human_picks_no_previous():
    """When prev is None, all current human picks are 'added'."""
    curr = _load_fixture("bulletin_ecosystem_with_human_picks_current.json")
    _, curr_hp = _split_eco_by_source_type(curr)
    diff = _diff_eco_human_picks(None, curr_hp)
    assert len(diff["added"]) == 2  # both human-picked entries in fixture


def test_diff_ecosystems_does_not_include_human_picks_with_split():
    """When we pre-split, _diff_ecosystems on the github_scanned subset should not see human picks."""
    prev = _load_fixture("bulletin_ecosystem_with_human_picks_previous.json")
    curr = _load_fixture("bulletin_ecosystem_with_human_picks_current.json")

    prev_gh, _ = _split_eco_by_source_type(prev)
    curr_gh, _ = _split_eco_by_source_type(curr)

    diff = _diff_ecosystems(prev_gh, curr_gh)
    # Only test-org/test-repo (the one github_scanned repo) should appear in diff
    all_keys = (
        [f"{r['owner']}/{r['name']}" for r in diff["added"]]
        + [f"{r['owner']}/{r['name']}" for r in diff["removed"]]
        + [c["repo"] for c in diff["changed"]]
    )
    for key in all_keys:
        assert "curated.test" not in key
        assert "curated.com" not in key


def test_eco_repo_v1_0_0_previous_bulletin_validates_against_v1_1_0_schema():
    """A v1.0.0 bulletin (without source_type) must still validate as v1.1.0 (additive change)."""
    from src.schema.ecosystem import BulletinEcosystem
    legacy_repo = {
        "name": "legacy",
        "owner": "legacy-owner",
        "url": "https://github.com/legacy-owner/legacy",
        "description": "Old entry from v1.0.0 bulletin",
        "license": "AGPL-3.0",
        "inferred_jurisdiction": "Unknown",
        "inferred_capabilities": [],
        "last_activity": "2025-01-01T00:00:00+00:00",
        "stars": 10,
        "fork_count": 2,
        "is_active": False,
        # NB: no source_type field → must default to "github_scanned"
    }
    legacy_bulletin = {
        "schema_version": "1.0.0",
        "generated_at": "2026-04-01T00:00:00+00:00",
        "source_count": 1,
        "repos": [legacy_repo],
    }
    validated = BulletinEcosystem.model_validate(legacy_bulletin)
    assert validated.repos[0].source_type == "github_scanned"
    assert validated.repos[0].curator is None
    assert validated.repos[0].topic is None
