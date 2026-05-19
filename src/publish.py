# SPDX-License-Identifier: AGPL-3.0-only
"""
publish.py — Upload bulletins to VPS via SSH/rsync with pre-flight checks.

Gate: refuses to publish if output/.review_flag is absent or stale.
Backup: renames existing remote files to *.previous.json before overwrite.
Verify: fetches public URL post-upload and validates JSON round-trip.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import paramiko
import yaml

from .schema.ecosystem import BulletinEcosystem
from .schema.patterns import BulletinPatterns
from .schema.skills import BulletinSkills


def _check_review_flag(review_flag_path: Path, max_age_minutes: int) -> None:
    """Abort if the review flag is missing or older than max_age_minutes."""
    if not review_flag_path.exists():
        print(
            "Error: review flag not found. "
            "Run `updater review` before publishing."
        )
        sys.exit(1)

    with open(review_flag_path, "r", encoding="utf-8") as fh:
        flag_ts_str = fh.read().strip()

    try:
        flag_ts = datetime.fromisoformat(flag_ts_str)
    except ValueError:
        print("Error: review flag has unrecognised timestamp. Re-run `updater review`.")
        sys.exit(1)

    age = datetime.now(timezone.utc) - flag_ts
    if age > timedelta(minutes=max_age_minutes):
        print(
            f"Error: review flag is {age.seconds // 60}m old "
            f"(max allowed: {max_age_minutes}m). Re-run `updater review`."
        )
        sys.exit(1)


def _sftp_backup_and_upload(
    ssh: paramiko.SSHClient,
    local_path: Path,
    remote_dir: str,
    previous_suffix: str,
) -> None:
    """
    Via SFTP: backup existing remote file to *.previous.json, then upload.
    """
    sftp = ssh.open_sftp()
    remote_path = f"{remote_dir}/{local_path.name}"
    prev_stem = local_path.stem
    prev_name = prev_stem + previous_suffix
    remote_prev = f"{remote_dir}/{prev_name}"

    # Backup existing
    try:
        sftp.stat(remote_path)
        sftp.rename(remote_path, remote_prev)
        print(f"  Backed up remote {remote_path} → {remote_prev}")
    except FileNotFoundError:
        pass  # No existing file; no backup needed

    sftp.put(str(local_path), remote_path)
    print(f"  Uploaded {local_path.name} → {remote_path}")
    sftp.close()


def _verify_remote(url: str, schema_class: type) -> None:
    """Fetch the public URL and validate JSON round-trip against the pydantic schema."""
    try:
        resp = httpx.get(url, timeout=15.0)
        resp.raise_for_status()
        schema_class.model_validate_json(resp.text)
        print(f"  Verified: {url} → HTTP {resp.status_code}, schema OK")
    except httpx.HTTPError as exc:
        print(f"  Warning: HTTP verification failed for {url}: {exc}")
    except Exception as exc:
        print(f"  Warning: schema round-trip failed for {url}: {exc}")


def run(config_path: str = "config.yaml") -> None:
    """Execute the publish step."""
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    eco_path = Path(cfg["output"]["ecosystem_path"])
    pat_path = Path(cfg["output"]["patterns_path"])
    skl_path = Path(cfg["output"].get("skills_path", "output/bulletin_skills.json"))
    review_flag_path = Path(cfg["output"]["review_flag_path"])
    previous_suffix = cfg["output"]["previous_suffix"]
    max_age = cfg["threshold_policy"]["review_flag_max_age_minutes"]
    env_vars = cfg.get("env_vars", {})

    # Gate: review flag check
    _check_review_flag(review_flag_path, max_age)

    # Gate: core bulletin files exist (skills bulletin may be absent if no sources configured)
    for p in (eco_path, pat_path):
        if not p.exists():
            print(f"Error: bulletin file not found: {p}. Run `updater build` first.")
            sys.exit(1)

    # Read SSH env vars
    vps_host = os.environ.get(env_vars.get("vps_host", "VPS_HOST"), "")
    vps_user = os.environ.get(env_vars.get("vps_user", "VPS_USER"), "")
    vps_path = os.environ.get(env_vars.get("vps_path", "VPS_PATH"), "")
    vps_key_path = os.environ.get(env_vars.get("vps_key_path", "VPS_KEY_PATH"), "")

    for var_name, val in [
        ("VPS_HOST", vps_host),
        ("VPS_USER", vps_user),
        ("VPS_PATH", vps_path),
    ]:
        if not val:
            print(f"Error: {var_name} env var not set.")
            sys.exit(1)

    print(f"Connecting to {vps_user}@{vps_host}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {
        "hostname": vps_host,
        "username": vps_user,
        "timeout": 30,
    }
    if vps_key_path:
        connect_kwargs["key_filename"] = vps_key_path

    ssh.connect(**connect_kwargs)

    # Upload the two core bulletins
    upload_targets = [
        (eco_path, BulletinEcosystem),
        (pat_path, BulletinPatterns),
    ]
    # Upload skills bulletin only if the file was produced by the build step
    if skl_path.exists():
        upload_targets.append((skl_path, BulletinSkills))
    else:
        print(
            f"Note: {skl_path} not found — skipping bulletin_skills.json upload. "
            "Configure skill_sources in config.yaml and re-run `updater build` to enable."
        )

    for local_path, _schema_class in upload_targets:
        _sftp_backup_and_upload(ssh, local_path, vps_path, previous_suffix)

    ssh.close()

    # Post-upload verification (requires VPS_URL env var, optional)
    vps_base_url = os.environ.get("VPS_BULLETIN_URL", "")
    if vps_base_url:
        _verify_remote(
            f"{vps_base_url.rstrip('/')}/bulletin_ecosystem.json",
            BulletinEcosystem,
        )
        _verify_remote(
            f"{vps_base_url.rstrip('/')}/bulletin_patterns.json",
            BulletinPatterns,
        )
        if skl_path.exists():
            _verify_remote(
                f"{vps_base_url.rstrip('/')}/bulletin_skills.json",
                BulletinSkills,
            )
    else:
        print(
            "Note: VPS_BULLETIN_URL not set — skipping post-upload HTTP verification. "
            "Set it to enable automatic round-trip check."
        )

    print("\nPublish complete.")
