# SPDX-License-Identifier: AGPL-3.0-only
"""
test_publish.py — Mock-based tests for src/publish.py.

No real SSH connections or HTTP requests are made.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.publish import _check_review_flag


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path) -> Path:
    cfg = {
        "seeds": [],
        "threshold_policy": {
            "active_window_days": 90,
            "warn_repos_changed_pct": 30,
            "warn_new_patterns_count": 5,
            "review_flag_max_age_minutes": 120,
        },
        "env_vars": {
            "github_token": "GITHUB_TOKEN",
            "vps_host": "VPS_HOST",
            "vps_user": "VPS_USER",
            "vps_path": "VPS_PATH",
            "vps_key_path": "VPS_KEY_PATH",
        },
        "output": {
            "raw_dir": str(tmp_path / "raw"),
            "ecosystem_path": str(tmp_path / "bulletin_ecosystem.json"),
            "patterns_path": str(tmp_path / "bulletin_patterns.json"),
            "previous_suffix": ".previous.json",
            "review_flag_path": str(tmp_path / ".review_flag"),
        },
    }
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as fh:
        yaml.dump(cfg, fh)
    return config_path


# ---------------------------------------------------------------------------
# _check_review_flag
# ---------------------------------------------------------------------------

def test_check_review_flag_missing_aborts(tmp_path):
    flag_path = tmp_path / ".review_flag"
    with pytest.raises(SystemExit):
        _check_review_flag(flag_path, max_age_minutes=120)


def test_check_review_flag_stale_aborts(tmp_path):
    flag_path = tmp_path / ".review_flag"
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    flag_path.write_text(stale_ts)
    with pytest.raises(SystemExit):
        _check_review_flag(flag_path, max_age_minutes=120)


def test_check_review_flag_fresh_passes(tmp_path):
    flag_path = tmp_path / ".review_flag"
    fresh_ts = datetime.now(timezone.utc).isoformat()
    flag_path.write_text(fresh_ts)
    # Should not raise
    _check_review_flag(flag_path, max_age_minutes=120)


# ---------------------------------------------------------------------------
# run() — full flow mock
# ---------------------------------------------------------------------------

def _make_bulletin_fixture(tmp_path: Path) -> None:
    """Write minimal valid bulletin files for testing."""
    eco = {
        "schema_version": "1.0.0",
        "generated_at": "2026-04-15T10:00:00+00:00",
        "source_count": 1,
        "repos": [
            {
                "name": "test-repo",
                "owner": "test-org",
                "url": "https://github.com/test-org/test-repo",
                "description": "Test",
                "license": "AGPL-3.0",
                "inferred_jurisdiction": "Unknown",
                "inferred_capabilities": [],
                "last_activity": "2026-04-15T10:00:00+00:00",
                "stars": 1,
                "fork_count": 0,
                "is_active": True,
                "notes": None,
            }
        ],
    }
    pat = {
        "schema_version": "1.0.0",
        "generated_at": "2026-04-15T10:00:00+00:00",
        "source_count": 0,
        "patterns": [],
    }
    with open(tmp_path / "bulletin_ecosystem.json", "w") as fh:
        json.dump(eco, fh)
    with open(tmp_path / "bulletin_patterns.json", "w") as fh:
        json.dump(pat, fh)


def test_publish_run_calls_ssh(tmp_path, monkeypatch):
    """publish.run() should connect via SSH and upload both bulletin files."""
    config_path = _write_config(tmp_path)
    _make_bulletin_fixture(tmp_path)

    # Set review flag to now
    flag_path = tmp_path / ".review_flag"
    flag_path.write_text(datetime.now(timezone.utc).isoformat())

    # Mock env vars
    monkeypatch.setenv("VPS_HOST", "vps.example.com")
    monkeypatch.setenv("VPS_USER", "ubuntu")
    monkeypatch.setenv("VPS_PATH", "/var/www/bulletins")
    monkeypatch.setenv("VPS_KEY_PATH", "")

    mock_sftp = MagicMock()
    mock_sftp.stat.side_effect = FileNotFoundError  # no existing remote file

    mock_ssh_instance = MagicMock()
    mock_ssh_instance.open_sftp.return_value = mock_sftp

    with patch("src.publish.paramiko.SSHClient", return_value=mock_ssh_instance), \
         patch("src.publish.paramiko.AutoAddPolicy"):
        from src.publish import run
        run(config_path=str(config_path))

    mock_ssh_instance.connect.assert_called_once()
    # sftp.put should have been called twice (eco + patterns)
    assert mock_sftp.put.call_count == 2


def test_publish_run_aborts_without_review(tmp_path, monkeypatch):
    """publish.run() should abort (SystemExit) if review flag is absent."""
    config_path = _write_config(tmp_path)
    _make_bulletin_fixture(tmp_path)
    # No review flag written

    monkeypatch.setenv("VPS_HOST", "vps.example.com")
    monkeypatch.setenv("VPS_USER", "ubuntu")
    monkeypatch.setenv("VPS_PATH", "/var/www/bulletins")

    with pytest.raises(SystemExit):
        from src.publish import run
        run(config_path=str(config_path))
