# SPDX-License-Identifier: AGPL-3.0-only
"""
test_scan.py — Mock-based tests for src/scan.py.

No real GitHub API calls are made. All HTTP responses are mocked via
pytest-mock / unittest.mock.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import warnings

from src.scan import (
    _decode_readme,
    _get_with_retry,
    _github_headers,
    _parse_skill_frontmatter,
    _fetch_skill_source_data,
    _load_scan_state,
    _save_scan_state,
    _empty_state,
    STATE_SCHEMA_VERSION,
    seed_state_from_raw,
    _parse_html_meta,
    _fetch_human_pick,
)


# ---------------------------------------------------------------------------
# _github_headers
# ---------------------------------------------------------------------------

def test_github_headers_with_token():
    headers = _github_headers("my-token")
    assert headers["Authorization"] == "Bearer my-token"
    assert "application/vnd.github+json" in headers["Accept"]


def test_github_headers_without_token():
    headers = _github_headers(None)
    assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# _decode_readme
# ---------------------------------------------------------------------------

def test_decode_readme_base64():
    content = base64.b64encode(b"Hello README").decode()
    data = {"content": content + "\n", "encoding": "base64"}
    assert _decode_readme(data) == "Hello README"


def test_decode_readme_none():
    assert _decode_readme(None) == ""


def test_decode_readme_empty_content():
    assert _decode_readme({"content": "", "encoding": "base64"}) == ""


# ---------------------------------------------------------------------------
# _get_with_retry
# ---------------------------------------------------------------------------

def _make_mock_response(status_code: int, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    return resp


def test_get_with_retry_success():
    mock_client = MagicMock()
    mock_client.get.return_value = _make_mock_response(200, {"id": 1})
    result = _get_with_retry(mock_client, "https://api.github.com/test", {})
    assert result == {"id": 1}
    mock_client.get.assert_called_once()


def test_get_with_retry_404_returns_none():
    mock_client = MagicMock()
    mock_client.get.return_value = _make_mock_response(404)
    result = _get_with_retry(mock_client, "https://api.github.com/test", {})
    assert result is None


def test_get_with_retry_on_429_then_success():
    """Should retry after 429 and succeed on second attempt."""
    rate_limit_resp = _make_mock_response(
        429, headers={"X-RateLimit-Reset": "0"}
    )
    success_resp = _make_mock_response(200, {"id": 42})

    mock_client = MagicMock()
    mock_client.get.side_effect = [rate_limit_resp, success_resp]

    with patch("src.scan.time.sleep"):  # don't actually sleep in tests
        result = _get_with_retry(mock_client, "https://api.github.com/test", {}, max_retries=3)

    assert result == {"id": 42}
    assert mock_client.get.call_count == 2


# ---------------------------------------------------------------------------
# Integration-style: run() with full mock
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _parse_skill_frontmatter
# ---------------------------------------------------------------------------

def test_parse_skill_frontmatter_valid():
    skill_md = "---\ntier: 1\njurisdiction: IT\nlicense: Apache-2.0\n---\n# My Skill\nContent here."
    result = _parse_skill_frontmatter(skill_md)
    assert result["tier"] == 1
    assert result["jurisdiction"] == "IT"
    assert result["license"] == "Apache-2.0"


def test_parse_skill_frontmatter_no_frontmatter():
    skill_md = "# My Skill\nNo frontmatter here."
    result = _parse_skill_frontmatter(skill_md)
    assert result == {}


def test_parse_skill_frontmatter_empty_string():
    result = _parse_skill_frontmatter("")
    assert result == {}


def test_parse_skill_frontmatter_unclosed_delimiter():
    skill_md = "---\ntier: 2\njurisdiction: EU\n# No closing delimiter"
    result = _parse_skill_frontmatter(skill_md)
    assert result == {}


def test_parse_skill_frontmatter_empty_frontmatter():
    skill_md = "---\n---\n# My Skill"
    result = _parse_skill_frontmatter(skill_md)
    assert result == {}


# ---------------------------------------------------------------------------
# _fetch_skill_source_data (mock-based)
# ---------------------------------------------------------------------------

def test_fetch_skill_source_data_uses_frontmatter_values(monkeypatch):
    """resolved_tier/jurisdiction should come from SKILL.md frontmatter when present."""
    skill_md = "---\ntier: 1\njurisdiction: IT\nlicense: MIT\n---\n# Skill"
    repo_meta = {
        "html_url": "https://github.com/test-owner/test-skill",
        "description": "A test skill",
        "default_branch": "main",
    }

    def mock_get(url, headers=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = repo_meta
        resp.text = skill_md
        return resp

    mock_client = MagicMock()
    mock_client.get.side_effect = mock_get

    result = _fetch_skill_source_data(
        mock_client, {}, "test-owner", "test-skill", 2, "[?]"
    )
    assert result["resolved_tier"] == 1
    assert result["resolved_jurisdiction"] == "IT"


def test_fetch_skill_source_data_uses_config_defaults_when_no_frontmatter(monkeypatch):
    """When SKILL.md has no frontmatter, config defaults should be used."""
    skill_md_no_fm = "# Skill\nNo frontmatter."
    repo_meta = {
        "html_url": "https://github.com/test-owner/test-skill",
        "description": "A test skill",
        "default_branch": "main",
    }

    call_count = {"n": 0}

    def mock_get(url, headers=None):
        resp = MagicMock()
        resp.status_code = 200
        # First call = repo meta (via _get_with_retry proxy)
        # SKILL.md fetch = separate .get call
        if "raw.githubusercontent.com" in url:
            resp.text = skill_md_no_fm
        elif "/license" in url:
            resp.json.return_value = {"license": {"spdx_id": "Apache-2.0", "name": "Apache 2.0"}}
        else:
            resp.json.return_value = repo_meta
        return resp

    mock_client = MagicMock()
    mock_client.get.side_effect = mock_get

    result = _fetch_skill_source_data(
        mock_client, {}, "test-owner", "test-skill", 2, "EU"
    )
    # Defaults from config since frontmatter is absent
    assert result["resolved_tier"] == 2
    assert result["resolved_jurisdiction"] == "EU"


def test_run_creates_raw_file(tmp_path, monkeypatch):
    """run() should write a timestamped JSON file to output/raw/."""
    # Write a minimal config.yaml in tmp_path
    config = {
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
            "raw_dir": str(tmp_path / "raw"),
            "ecosystem_path": str(tmp_path / "bulletin_ecosystem.json"),
            "patterns_path": str(tmp_path / "bulletin_patterns.json"),
            "skills_path": str(tmp_path / "bulletin_skills.json"),
            "previous_suffix": ".previous.json",
            "review_flag_path": str(tmp_path / ".review_flag"),
        },
    }
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as fh:
        yaml.dump(config, fh)

    # Mock out all HTTP calls
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "github_repo_response.json") as fh:
        repo_data = json.load(fh)

    readme_b64 = base64.b64encode(b"# Test README\ncontract review tool").decode()
    readme_response = {"content": readme_b64, "encoding": "base64"}

    def mock_get(url, headers=None):
        if "/readme" in url:
            return _make_mock_response(200, readme_response)
        elif "/license" in url:
            return _make_mock_response(200, {"license": {"spdx_id": "AGPL-3.0"}})
        else:
            return _make_mock_response(200, repo_data)

    mock_client = MagicMock()
    mock_client.get.side_effect = mock_get
    mock_client.__enter__ = lambda s: mock_client
    mock_client.__exit__ = MagicMock(return_value=False)

    with patch("src.scan.httpx.Client", return_value=mock_client):
        from src.scan import run
        out_path = run(config_path=str(config_path))

    assert out_path.exists()
    with open(out_path) as fh:
        data = json.load(fh)
    assert "repos" in data
    assert "skill_sources_raw" in data
    assert len(data["repos"]) == 1
    assert data["repos"][0]["owner"] == "test-org"
    assert data["skill_sources_raw"] == []  # no skill_sources configured


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

def test_empty_state_schema():
    state = _empty_state()
    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert state["last_full_scan_at"] is None
    assert state["seen_forks"] == {}


def test_load_scan_state_no_file(tmp_path):
    """Returns empty state when file does not exist."""
    state = _load_scan_state(tmp_path / "nonexistent.json")
    assert state == _empty_state()


def test_load_scan_state_valid(tmp_path):
    state_path = tmp_path / "scan_state.json"
    expected = {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_full_scan_at": "20260519T120000Z",
        "seen_forks": {"org/repo": [111, 222, 333]},
    }
    state_path.write_text(json.dumps(expected), encoding="utf-8")
    loaded = _load_scan_state(state_path)
    assert loaded["seen_forks"]["org/repo"] == [111, 222, 333]
    assert loaded["last_full_scan_at"] == "20260519T120000Z"


def test_load_scan_state_schema_mismatch_warns_and_returns_empty(tmp_path):
    state_path = tmp_path / "scan_state.json"
    stale = {
        "schema_version": "0.9.0",  # old/wrong version
        "last_full_scan_at": "20260101T000000Z",
        "seen_forks": {"org/repo": [1, 2, 3]},
    }
    state_path.write_text(json.dumps(stale), encoding="utf-8")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        state = _load_scan_state(state_path)
    assert state == _empty_state()
    assert any("schema_version mismatch" in str(w.message) for w in caught)


def test_load_scan_state_invalid_json_warns_and_returns_empty(tmp_path):
    state_path = tmp_path / "scan_state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        state = _load_scan_state(state_path)
    assert state == _empty_state()
    assert any("not valid JSON" in str(w.message) for w in caught)


def test_save_and_reload_state(tmp_path):
    state_path = tmp_path / "scan_state.json"
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_full_scan_at": "20260519T120000Z",
        "seen_forks": {"owner/repo": [1, 2, 3]},
    }
    _save_scan_state(state, state_path)
    assert state_path.exists()
    reloaded = _load_scan_state(state_path)
    assert reloaded["seen_forks"]["owner/repo"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# seed_state_from_raw migration helper
# ---------------------------------------------------------------------------

def _make_raw_file(tmp_path: Path, forks: list[dict]) -> Path:
    """Write a minimal raw JSON file with given fork entries."""
    data = {
        "scanned_at": "20260519T210000Z",
        "repos": [
            {
                "owner": "seed-org",
                "name": "seed-repo",
                "is_fork": False,
                "meta": {"id": 9999},
            }
        ] + forks,
        "skill_sources_raw": [],
    }
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "20260519T210000Z.json"
    raw_path.write_text(json.dumps(data), encoding="utf-8")
    return raw_path


def test_seed_state_from_raw_extracts_fork_ids(tmp_path):
    forks = [
        {
            "owner": "fork-owner-1",
            "name": "fork-repo",
            "is_fork": True,
            "forked_from": "seed-org/seed-repo",
            "meta": {"id": 101},
        },
        {
            "owner": "fork-owner-2",
            "name": "fork-repo",
            "is_fork": True,
            "forked_from": "seed-org/seed-repo",
            "meta": {"id": 202},
        },
    ]
    raw_path = _make_raw_file(tmp_path, forks)
    state_path = tmp_path / "scan_state.json"
    seed_state_from_raw(raw_path, state_path)

    state = _load_scan_state(state_path)
    assert set(state["seen_forks"]["seed-org/seed-repo"]) == {101, 202}
    assert state["last_full_scan_at"] == "20260519T210000Z"


def test_seed_state_from_raw_no_forks(tmp_path):
    raw_path = _make_raw_file(tmp_path, [])
    state_path = tmp_path / "scan_state.json"
    seed_state_from_raw(raw_path, state_path)
    state = _load_scan_state(state_path)
    assert state["seen_forks"] == {}


# ---------------------------------------------------------------------------
# run() — incremental scan (delta) tests
# ---------------------------------------------------------------------------

def _write_config_with_fork_seed(tmp_path: Path) -> Path:
    """Write a minimal config.yaml with one follow_forks=true seed."""
    config = {
        "seeds": [{"owner": "seed-org", "name": "seed-repo", "follow_forks": True}],
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
            "raw_dir": str(tmp_path / "raw"),
            "scan_state_path": str(tmp_path / "scan_state.json"),
            "ecosystem_path": str(tmp_path / "bulletin_ecosystem.json"),
            "patterns_path": str(tmp_path / "bulletin_patterns.json"),
            "skills_path": str(tmp_path / "bulletin_skills.json"),
            "previous_suffix": ".previous.json",
            "review_flag_path": str(tmp_path / ".review_flag"),
        },
    }
    import yaml
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as fh:
        yaml.dump(config, fh)
    return config_path


def _make_mock_client_for_scan(repo_meta, readme_response, fork_stubs):
    """
    Build a mock httpx.Client that:
    - Returns fork_stubs for /forks pagination URLs
    - Returns repo_meta for all other GET calls
    - Returns readme_response for /readme URLs
    """
    def mock_get(url, headers=None):
        if "/forks" in url:
            return _make_mock_response(200, fork_stubs)
        elif "/readme" in url:
            return _make_mock_response(200, readme_response)
        elif "/license" in url:
            return _make_mock_response(200, {"license": {"spdx_id": "AGPL-3.0"}})
        else:
            return _make_mock_response(200, repo_meta)

    mock_client = MagicMock()
    mock_client.get.side_effect = mock_get
    mock_client.__enter__ = lambda s: mock_client
    mock_client.__exit__ = MagicMock(return_value=False)
    return mock_client


def _make_fork_stubs(ids: list[int]) -> list[dict]:
    return [
        {
            "id": fid,
            "name": f"fork-repo-{fid}",
            "owner": {"login": f"fork-owner-{fid}"},
        }
        for fid in ids
    ]


def test_run_first_run_no_state_does_full_scan(tmp_path):
    """First run (no state file) triggers full scan — all forks fetched."""
    config_path = _write_config_with_fork_seed(tmp_path)
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)

    repo_meta = {"id": 9999, "default_branch": "main"}
    readme_b64 = base64.b64encode(b"# Test").decode()
    readme_response = {"content": readme_b64, "encoding": "base64"}
    fork_stubs = _make_fork_stubs([101, 102, 103])

    mock_client = _make_mock_client_for_scan(repo_meta, readme_response, fork_stubs)

    with patch("src.scan.httpx.Client", return_value=mock_client):
        from src.scan import run
        out_path = run(config_path=str(config_path))

    with open(out_path) as fh:
        data = json.load(fh)
    forks_in_raw = [r for r in data["repos"] if r.get("is_fork")]
    assert len(forks_in_raw) == 3  # all 3 forks fetched

    # State written with all 3 IDs seen
    state_path = tmp_path / "scan_state.json"
    assert state_path.exists()
    state = _load_scan_state(state_path)
    assert set(state["seen_forks"]["seed-org/seed-repo"]) == {101, 102, 103}
    assert state["last_full_scan_at"] is not None


def test_run_incremental_only_fetches_new_forks(tmp_path):
    """
    Incremental scan: forks 101 + 102 already seen, fork 103 is new.
    Verify only 1 new fetch, 2 carried from prior raw.
    The carried forks retain their prior meta (id 101 / 102).
    The new fork 103 is fetched fresh.
    """
    config_path = _write_config_with_fork_seed(tmp_path)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Write a prior raw file with forks 101 + 102 already present
    prior_raw = {
        "scanned_at": "20260518T120000Z",
        "repos": [
            {"owner": "seed-org", "name": "seed-repo", "is_fork": False,
             "meta": {"id": 9999}},
            {"owner": "fork-owner-101", "name": "fork-repo-101", "is_fork": True,
             "forked_from": "seed-org/seed-repo",
             "meta": {"id": 101}, "readme": "# Fork 101", "license": None},
            {"owner": "fork-owner-102", "name": "fork-repo-102", "is_fork": True,
             "forked_from": "seed-org/seed-repo",
             "meta": {"id": 102}, "readme": "# Fork 102", "license": None},
        ],
        "skill_sources_raw": [],
    }
    (raw_dir / "20260518T120000Z.json").write_text(
        json.dumps(prior_raw), encoding="utf-8"
    )

    # Write state: 101 + 102 already seen
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_full_scan_at": "20260518T120000Z",
        "seen_forks": {"seed-org/seed-repo": [101, 102]},
    }
    (tmp_path / "scan_state.json").write_text(json.dumps(state), encoding="utf-8")

    # Fork list now has 101 + 102 + 103 (103 is new)
    fork_stubs = _make_fork_stubs([101, 102, 103])
    # Mock returns meta with id=103 for fork-repo-103, generic for others
    readme_b64 = base64.b64encode(b"# Test").decode()
    readme_response = {"content": readme_b64, "encoding": "base64"}

    def smart_mock_get(url, headers=None):
        if "/forks" in url:
            return _make_mock_response(200, fork_stubs)
        elif "/readme" in url:
            return _make_mock_response(200, readme_response)
        elif "/license" in url:
            return _make_mock_response(200, {"license": {"spdx_id": "AGPL-3.0"}})
        elif "fork-repo-103" in url:
            return _make_mock_response(200, {"id": 103, "default_branch": "main"})
        else:
            return _make_mock_response(200, {"id": 9999, "default_branch": "main"})

    mock_client = MagicMock()
    mock_client.get.side_effect = smart_mock_get
    mock_client.__enter__ = lambda s: mock_client
    mock_client.__exit__ = MagicMock(return_value=False)

    with patch("src.scan.httpx.Client", return_value=mock_client):
        from src.scan import run
        out_path = run(config_path=str(config_path), full=False)

    with open(out_path) as fh:
        data = json.load(fh)

    forks_in_raw = [r for r in data["repos"] if r.get("is_fork")]
    assert len(forks_in_raw) == 3  # all 3 present (2 carried + 1 new)

    # Carried forks have original meta IDs; new fork 103 has id=103
    fork_ids_in_raw = {r["meta"]["id"] for r in forks_in_raw}
    assert fork_ids_in_raw == {101, 102, 103}

    # State updated: all 3 IDs now seen
    reloaded = _load_scan_state(tmp_path / "scan_state.json")
    assert set(reloaded["seen_forks"]["seed-org/seed-repo"]) == {101, 102, 103}

    # API verification: fork-repo-103 should have been fetched, fork-repo-101/102 should NOT
    calls = [str(call) for call in mock_client.get.call_args_list]
    fork_103_meta_calls = [
        c for c in calls
        if "fork-repo-103" in c and "readme" not in c and "license" not in c and "forks" not in c
    ]
    fork_101_meta_calls = [
        c for c in calls
        if "fork-repo-101" in c and "readme" not in c and "license" not in c and "forks" not in c
    ]
    assert len(fork_103_meta_calls) >= 1, "fork 103 should have been fetched"
    assert len(fork_101_meta_calls) == 0, "fork 101 should NOT have been re-fetched"


def test_run_full_flag_ignores_state(tmp_path):
    """--full re-scans all forks even when state has seen IDs."""
    config_path = _write_config_with_fork_seed(tmp_path)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # State already has 101 + 102 seen
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_full_scan_at": "20260518T120000Z",
        "seen_forks": {"seed-org/seed-repo": [101, 102]},
    }
    (tmp_path / "scan_state.json").write_text(json.dumps(state), encoding="utf-8")

    fork_stubs = _make_fork_stubs([101, 102, 103])
    repo_meta = {"id": 9999, "default_branch": "main"}
    readme_b64 = base64.b64encode(b"# Test").decode()
    readme_response = {"content": readme_b64, "encoding": "base64"}

    mock_client = _make_mock_client_for_scan(repo_meta, readme_response, fork_stubs)

    with patch("src.scan.httpx.Client", return_value=mock_client):
        from src.scan import run
        out_path = run(config_path=str(config_path), full=True)

    with open(out_path) as fh:
        data = json.load(fh)

    forks_in_raw = [r for r in data["repos"] if r.get("is_fork")]
    assert len(forks_in_raw) == 3  # all 3 re-fetched

    # State updated with last_full_scan_at refreshed
    reloaded = _load_scan_state(tmp_path / "scan_state.json")
    assert reloaded["last_full_scan_at"] is not None
    assert reloaded["last_full_scan_at"] != "20260518T120000Z"  # refreshed

    # All 3 forks metadata should have been fetched (not just new ones)
    calls = [str(call) for call in mock_client.get.call_args_list]
    for fid in [101, 102, 103]:
        fork_meta_calls = [c for c in calls if f"fork-repo-{fid}" in c and "readme" not in c and "license" not in c and "forks" not in c]
        assert len(fork_meta_calls) >= 1, f"fork {fid} should have been re-fetched in --full mode"


def test_run_schema_mismatch_triggers_full_scan(tmp_path):
    """If scan_state.json has wrong schema_version, run falls back to full scan."""
    config_path = _write_config_with_fork_seed(tmp_path)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    stale_state = {
        "schema_version": "0.0.1",
        "last_full_scan_at": "20260101T000000Z",
        "seen_forks": {"seed-org/seed-repo": [101, 102]},
    }
    (tmp_path / "scan_state.json").write_text(json.dumps(stale_state), encoding="utf-8")

    fork_stubs = _make_fork_stubs([101, 102])
    repo_meta = {"id": 9999, "default_branch": "main"}
    readme_b64 = base64.b64encode(b"# Test").decode()
    readme_response = {"content": readme_b64, "encoding": "base64"}

    mock_client = _make_mock_client_for_scan(repo_meta, readme_response, fork_stubs)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with patch("src.scan.httpx.Client", return_value=mock_client):
            from src.scan import run
            out_path = run(config_path=str(config_path))

    # Full scan should have been performed (all forks fetched)
    with open(out_path) as fh:
        data = json.load(fh)
    forks_in_raw = [r for r in data["repos"] if r.get("is_fork")]
    assert len(forks_in_raw) == 2

    # State should now be correct schema
    reloaded = _load_scan_state(tmp_path / "scan_state.json")
    assert reloaded["schema_version"] == STATE_SCHEMA_VERSION
    # Warning was issued
    assert any("schema_version mismatch" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# _parse_html_meta (human_picks support)
# ---------------------------------------------------------------------------

def _load_html_fixture(name: str) -> str:
    """Load an HTML fixture from tests/fixtures/html_samples/."""
    fixtures_dir = Path(__file__).parent / "fixtures" / "html_samples"
    with open(fixtures_dir / name, encoding="utf-8") as fh:
        return fh.read()


def test_parse_html_meta_extracts_og_tags():
    """A page with full og:* metadata should yield all four og: keys + title + description."""
    html = _load_html_fixture("full_og.html")
    meta = _parse_html_meta(html)
    assert meta.get("og:title") == "OG Title Value"
    assert meta.get("og:description", "").startswith("OG Description")
    assert meta.get("og:image") == "https://example-curated.test/og-image.png"
    assert meta.get("og:site_name") == "Example Site Name"
    assert "Full OG Page" in meta.get("title", "")
    assert "Meta description" in meta.get("description", "")


def test_parse_html_meta_handles_missing_tags():
    """A page with only <title> should yield 'title' and nothing else."""
    html = _load_html_fixture("bare_title_only.html")
    meta = _parse_html_meta(html)
    assert meta.get("title") == "Bare Title Only Sample"
    assert "og:title" not in meta
    assert "description" not in meta


def test_parse_html_meta_malformed_html_does_not_crash():
    """Malformed HTML should yield (at most) a partial dict, never raise."""
    html = _load_html_fixture("malformed.html")
    meta = _parse_html_meta(html)
    # bs4 with html.parser is permissive; the title MAY be extracted partially or not at all.
    # Either way the function must not raise.
    assert isinstance(meta, dict)


def test_parse_html_meta_empty_string():
    """Empty input returns empty dict, no crash."""
    assert _parse_html_meta("") == {}


# ---------------------------------------------------------------------------
# _fetch_human_pick (human_picks support)
# ---------------------------------------------------------------------------

def test_fetch_human_pick_success():
    """A successful fetch returns fetch_status='ok' with parsed og: meta."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = (
        '<html><head>'
        '<title>Test Page</title>'
        '<meta property="og:title" content="OG Title">'
        '<meta property="og:description" content="OG Desc">'
        '</head><body></body></html>'
    )
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    entry = {"url": "https://example-curated.test/", "topic": "Test Curated Source"}
    result = _fetch_human_pick(mock_client, entry)

    assert result["url"] == "https://example-curated.test/"
    assert result["fetch_status"] == "ok"
    assert result["meta"].get("og:title") == "OG Title"
    assert result["meta"].get("og:description") == "OG Desc"
    assert result["config_entry"] == entry
    assert "fetched_at" in result


def test_fetch_human_pick_network_failure():
    """On 503 exhausting retries, returns fetch_status='failed' with empty meta — entry preserved."""
    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.text = ""
    mock_response.headers = {}
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    entry = {"url": "https://unresponsive.test/", "topic": "Unresponsive"}
    with patch("src.scan.time.sleep"):  # skip retry sleeps in test
        result = _fetch_human_pick(mock_client, entry)

    assert result["fetch_status"] == "failed"
    assert result["meta"] == {}
    assert result["url"] == "https://unresponsive.test/"
    assert result["config_entry"] == entry


def test_fetch_human_pick_404_no_retry():
    """A 404 (non-retriable) returns failed immediately with empty meta."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = ""
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    entry = {"url": "https://gone.test/missing-page", "topic": "Gone"}
    with patch("src.scan.time.sleep"):
        result = _fetch_human_pick(mock_client, entry)

    assert result["fetch_status"] == "failed"
    assert result["meta"] == {}
