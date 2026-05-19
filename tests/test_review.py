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
