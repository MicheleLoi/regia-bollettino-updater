# SPDX-License-Identifier: AGPL-3.0-only
"""
publish.py — Upload bulletins to VPS via Tailscale with pre-flight checks.

Gate: refuses to publish if output/.review_flag is absent or stale.
Backup: copies existing remote files to *.previous.json before overwrite.
Verify: fetches public URL post-upload and validates JSON round-trip.

Required env vars:
  VPS_HOST          Tailscale hostname of the VPS (e.g. vps-easyname)
  VPS_USER          SSH user on the VPS (e.g. loimi)
  VPS_PATH          Absolute path to the bulletins directory on the VPS
                    (e.g. /var/www/html/bollettini)

  VPS_MAGIC_IP      Tailscale magic IP for scp transfers (set in .env; never
                    hardcoded in code — update it if the Tailscale IP changes).

Optional env vars:
  VPS_BULLETIN_URL  Public base URL for post-upload HTTP verification
                    (e.g. https://bollettino.example.com/). Skipped if unset.

No longer needed:
  VPS_KEY_PATH      Removed — Tailscale SSH handles authentication; no key file required.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import yaml

from .schema.ecosystem import BulletinEcosystem
from .schema.patterns import BulletinPatterns
from .schema.skills import BulletinSkills

# Tailscale magic IP for scp is read from VPS_MAGIC_IP (.env) — never hardcoded here.


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


def _run(cmd: list[str], description: str) -> None:
    """Run a subprocess command; exit with an error message on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error during {description}:")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        sys.exit(1)


def _tailscale_backup_and_upload(
    local_path: Path,
    remote_dir: str,
    vps_host: str,
    vps_user: str,
    magic_ip: str,
    previous_suffix: str,
) -> None:
    """
    Via Tailscale: backup existing remote file to *.previous.json, then upload.

    Steps:
      1. Remote backup: sudo cp BULLETINS_DIR/f BULLETINS_DIR/f.previous.json
      2. SCP to /tmp on VPS via magic IP (avoids sudo for the transfer itself)
      3. Remote move from /tmp to BULLETINS_DIR: sudo cp + rm
    """
    fname = local_path.name
    stem = local_path.stem
    prev_name = stem + previous_suffix
    remote_path = f"{remote_dir}/{fname}"
    remote_prev = f"{remote_dir}/{prev_name}"
    tmp_path = f"/tmp/{fname}"
    ssh_target = f"{vps_user}@{vps_host}"
    scp_target = f"{vps_user}@{magic_ip}:{tmp_path}"

    # Step 1: backup existing file (ignore errors if file doesn't exist yet)
    backup_cmd = f"[ -f {remote_path} ] && sudo cp {remote_path} {remote_prev} || true"
    print(f"  Backing up remote {remote_path} → {remote_prev}")
    _run(
        ["tailscale", "ssh", ssh_target, backup_cmd],
        f"backup of {fname}",
    )

    # Step 2: copy local file to /tmp on VPS via scp + magic IP
    print(f"  Uploading {local_path.name} → {scp_target}")
    _run(
        ["scp", str(local_path), scp_target],
        f"scp transfer of {fname}",
    )

    # Step 3: move from /tmp to BULLETINS_DIR (requires sudo for root-owned webroot)
    deploy_cmd = f"sudo cp {tmp_path} {remote_path} && sudo rm {tmp_path}"
    print(f"  Deploying /tmp/{fname} → {remote_path}")
    _run(
        ["tailscale", "ssh", ssh_target, deploy_cmd],
        f"remote deploy of {fname}",
    )

    print(f"  Done: {fname}")


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

    # Read Tailscale env vars
    vps_host = os.environ.get(env_vars.get("vps_host", "VPS_HOST"), "")
    vps_user = os.environ.get(env_vars.get("vps_user", "VPS_USER"), "")
    vps_path = os.environ.get(env_vars.get("vps_path", "VPS_PATH"), "")
    magic_ip = os.environ.get("VPS_MAGIC_IP", "")

    for var_name, val in [
        ("VPS_HOST", vps_host),
        ("VPS_USER", vps_user),
        ("VPS_PATH", vps_path),
        ("VPS_MAGIC_IP", magic_ip),
    ]:
        if not val:
            print(f"Error: {var_name} env var not set.")
            sys.exit(1)

    # Build list of bulletins to upload
    upload_targets = [
        (eco_path, BulletinEcosystem),
        (pat_path, BulletinPatterns),
    ]
    if skl_path.exists():
        upload_targets.append((skl_path, BulletinSkills))
    else:
        print(
            f"Note: {skl_path} not found — skipping bulletin_skills.json upload. "
            "Configure skill_sources in config.yaml and re-run `updater build` to enable."
        )

    print(f"Publishing via Tailscale to {vps_user}@{vps_host} ({vps_path})...")
    for local_path, _schema_class in upload_targets:
        _tailscale_backup_and_upload(
            local_path,
            vps_path,
            vps_host,
            vps_user,
            magic_ip,
            previous_suffix,
        )

    # Post-upload verification (requires VPS_BULLETIN_URL env var, optional)
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
