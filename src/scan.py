# SPDX-License-Identifier: AGPL-3.0-only
"""
scan.py — GitHub API scan step.

Fetches metadata, README, and license info for all seed repos (and their forks
when follow_forks is true). Writes a raw timestamped JSON file to output/raw/.

No calls to the real GitHub API are made in tests — all HTTP is mockable via
the httpx transport layer.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml


def _load_config(config_path: str = "config.yaml") -> dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _github_headers(token: str | None) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_with_retry(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    max_retries: int = 3,
) -> dict[str, Any] | list[Any] | None:
    """
    GET url with exponential backoff on 429/503.
    Returns parsed JSON or None on non-200 (including 404).
    """
    delay = 1.0
    for attempt in range(max_retries):
        response = client.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        if response.status_code in (429, 503):
            reset_header = response.headers.get("X-RateLimit-Reset")
            if reset_header:
                wait = max(0, int(reset_header) - int(time.time())) + 1
            else:
                wait = delay
            time.sleep(wait)
            delay *= 2
            continue
        # 404 or other non-retriable
        return None
    return None


def _decode_readme(readme_data: dict[str, Any] | None) -> str:
    """Decode base64 README content from GitHub API response."""
    if readme_data is None:
        return ""
    content = readme_data.get("content", "")
    encoding = readme_data.get("encoding", "base64")
    if encoding == "base64" and content:
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return content


def _fetch_repo_data(
    client: httpx.Client,
    headers: dict[str, str],
    owner: str,
    name: str,
) -> dict[str, Any]:
    """Fetch all relevant data for a single repo."""
    base = f"https://api.github.com/repos/{owner}/{name}"
    repo_meta = _get_with_retry(client, base, headers) or {}
    readme_raw = _get_with_retry(client, f"{base}/readme", headers)
    readme_text = _decode_readme(readme_raw)
    license_info = _get_with_retry(client, f"{base}/license", headers)
    return {
        "meta": repo_meta,
        "readme": readme_text,
        "license": license_info,
    }


def _fetch_skill_md(
    client: httpx.Client,
    owner: str,
    name: str,
    default_branch: str = "main",
) -> str:
    """
    Fetch raw SKILL.md content from GitHub raw content CDN.
    Falls back to 'master' branch if 'main' returns 404.
    Returns empty string if not found.
    """
    for branch in (default_branch, "master"):
        url = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/SKILL.md"
        try:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
    return ""


def _parse_skill_frontmatter(skill_md: str) -> dict[str, Any]:
    """
    Parse YAML frontmatter from SKILL.md content.

    Frontmatter is delimited by '---' on its own line at the top.
    Returns a dict with any declared keys (tier, jurisdiction, license, etc.),
    or an empty dict if no frontmatter is present or parsing fails.
    """
    if not skill_md.startswith("---"):
        return {}
    lines = skill_md.splitlines()
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}
    frontmatter_text = "\n".join(lines[1:end_idx])
    try:
        parsed = yaml.safe_load(frontmatter_text) or {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _fetch_skill_source_data(
    client: httpx.Client,
    headers: dict[str, str],
    owner: str,
    name: str,
    default_tier: int,
    default_jurisdiction: str,
) -> dict[str, Any]:
    """
    Fetch all relevant data for a single skill source repo.

    Retrieves repo metadata + SKILL.md, parses frontmatter for
    tier/jurisdiction/license declarations. Falls back to defaults from config.
    """
    base = f"https://api.github.com/repos/{owner}/{name}"
    repo_meta = _get_with_retry(client, base, headers) or {}

    # Infer default branch from API response
    default_branch = repo_meta.get("default_branch", "main")

    skill_md = _fetch_skill_md(client, owner, name, default_branch)
    frontmatter = _parse_skill_frontmatter(skill_md)

    # Resolve tier: SKILL.md frontmatter > config default
    raw_tier = frontmatter.get("tier", default_tier)
    try:
        tier = int(raw_tier)
    except (TypeError, ValueError):
        tier = default_tier

    # Resolve jurisdiction: SKILL.md frontmatter > config default
    jurisdiction = str(frontmatter.get("jurisdiction", default_jurisdiction))

    # Resolve license: SKILL.md frontmatter > repo API > Unknown
    license_from_frontmatter = frontmatter.get("license", "")
    if not license_from_frontmatter:
        license_data = _get_with_retry(client, f"{base}/license", headers)
        if license_data:
            lic = (license_data.get("license") or {})
            spdx = lic.get("spdx_id", "")
            if spdx and spdx.upper() != "NOASSERTION":
                license_from_frontmatter = spdx
            else:
                license_from_frontmatter = lic.get("name", "Unknown")
        else:
            license_from_frontmatter = "Unknown"

    return {
        "meta": repo_meta,
        "skill_md": skill_md,
        "frontmatter": frontmatter,
        "owner": owner,
        "name": name,
        "resolved_tier": tier,
        "resolved_jurisdiction": jurisdiction,
        "resolved_license": license_from_frontmatter,
    }


def _fetch_forks(
    client: httpx.Client,
    headers: dict[str, str],
    owner: str,
    name: str,
) -> list[dict[str, Any]]:
    """Fetch all forks with full pagination."""
    forks: list[dict[str, Any]] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{owner}/{name}/forks"
            f"?per_page=100&page={page}"
        )
        data = _get_with_retry(client, url, headers)
        if not data or not isinstance(data, list) or len(data) == 0:
            break
        forks.extend(data)
        if len(data) < 100:
            break
        page += 1
    return forks


def run(config_path: str = "config.yaml") -> Path:
    """
    Execute the scan step. Returns the path of the raw output file written.
    """
    cfg = _load_config(config_path)
    token = os.environ.get(cfg.get("env_vars", {}).get("github_token", "GITHUB_TOKEN"))

    if not token:
        print(
            "Warning: GITHUB_TOKEN not set. Using unauthenticated requests "
            "(rate limit: 60/h). Set GITHUB_TOKEN for 5000/h."
        )

    headers = _github_headers(token)
    raw_dir = Path(cfg["output"]["raw_dir"])
    raw_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    skill_results: list[dict[str, Any]] = []

    with httpx.Client(timeout=30.0) as client:
        # --- Ecosystem seeds scan ---
        for seed in cfg.get("seeds", []):
            owner = seed["owner"]
            name = seed["name"]
            follow_forks = seed.get("follow_forks", False)

            print(f"Scanning ecosystem seed {owner}/{name}...")
            repo_data = _fetch_repo_data(client, headers, owner, name)
            repo_data["owner"] = owner
            repo_data["name"] = name
            repo_data["is_fork"] = False
            results.append(repo_data)

            if follow_forks:
                forks = _fetch_forks(client, headers, owner, name)
                print(f"  Found {len(forks)} fork(s), fetching details...")
                for fork_meta in forks:
                    fork_owner = fork_meta.get("owner", {}).get("login", "")
                    fork_name = fork_meta.get("name", "")
                    if not fork_owner or not fork_name:
                        continue
                    fork_data = _fetch_repo_data(client, headers, fork_owner, fork_name)
                    fork_data["owner"] = fork_owner
                    fork_data["name"] = fork_name
                    fork_data["is_fork"] = True
                    fork_data["forked_from"] = f"{owner}/{name}"
                    results.append(fork_data)

        # --- Skill sources scan ---
        # Handle YAML quirk: when all skill_sources entries are commented out, the
        # parsed value is None (not missing). Fallback to empty list to avoid
        # TypeError on iteration.
        for skill_source in (cfg.get("skill_sources") or []):
            owner = skill_source["owner"]
            name = skill_source["name"]
            default_tier = int(skill_source.get("default_tier", 2))
            default_jurisdiction = str(skill_source.get("default_jurisdiction", "[?]"))

            print(f"Scanning skill source {owner}/{name}...")
            skill_data = _fetch_skill_source_data(
                client, headers, owner, name, default_tier, default_jurisdiction
            )
            skill_results.append(skill_data)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = raw_dir / f"{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "scanned_at": timestamp,
                "repos": results,
                "skill_sources_raw": skill_results,
            },
            fh,
            indent=2,
            default=str,
        )

    print(
        f"Scan complete. {len(results)} ecosystem repos, "
        f"{len(skill_results)} skill source(s) written to {out_path}"
    )
    return out_path
