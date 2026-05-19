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

from src.scan import (
    _decode_readme,
    _get_with_retry,
    _github_headers,
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

def test_run_creates_raw_file(tmp_path, monkeypatch):
    """run() should write a timestamped JSON file to output/raw/."""
    # Write a minimal config.yaml in tmp_path
    config = {
        "seeds": [{"owner": "test-org", "name": "test-repo", "follow_forks": False}],
        "threshold_policy": {
            "active_window_days": 90,
            "warn_repos_changed_pct": 30,
            "warn_new_patterns_count": 5,
            "review_flag_max_age_minutes": 120,
        },
        "env_vars": {"github_token": "GITHUB_TOKEN"},
        "output": {
            "raw_dir": str(tmp_path / "raw"),
            "ecosystem_path": str(tmp_path / "bulletin_ecosystem.json"),
            "patterns_path": str(tmp_path / "bulletin_patterns.json"),
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
    assert len(data["repos"]) == 1
    assert data["repos"][0]["owner"] == "test-org"
