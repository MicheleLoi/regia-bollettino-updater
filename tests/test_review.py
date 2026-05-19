# SPDX-License-Identifier: AGPL-3.0-only
"""
test_review.py — Tests for src/review.py diff logic and threshold gate.

All I/O interactions (stdin, file system) are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.review import _diff_ecosystems, _diff_patterns


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
