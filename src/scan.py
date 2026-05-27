# SPDX-License-Identifier: AGPL-3.0-only
"""
scan.py — GitHub API scan step.

Fetches metadata, README, and license info for all seed repos (and their forks
when follow_forks is true). Writes a raw timestamped JSON file to output/raw/.

No calls to the real GitHub API are made in tests — all HTTP is mockable via
the httpx transport layer.

## Incremental fork scanning

For seeds with ``follow_forks: true``, subsequent runs are incremental by
default: only *new* forks (IDs not in scan_state.json) trigger full metadata
fetches.  Previously-seen forks are carried forward from the most recent raw
file, so the raw output always contains the *complete* fork set and build.py
needs no changes.

Use ``full=True`` (or ``updater scan --full``) to force a complete re-scan
of all forks (useful for periodic re-verification).

State file: ``output/scan_state.json`` (configurable via config.yaml
``output.scan_state_path``).  Created automatically on first run.
"""

from __future__ import annotations

import base64
import json
import os
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from bs4 import BeautifulSoup

# User-Agent used for arbitrary-URL fetches (human_picks).
# Identifies the updater so site owners can correlate hits; some WordPress
# installs serve 403 to requests with no User-Agent header.
_HUMAN_PICK_USER_AGENT = (
    "regia-bollettino-updater/0.1 "
    "(+https://github.com/MicheleLoi/regia-bollettino-updater)"
)


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

STATE_SCHEMA_VERSION = "1.0.0"


def _load_scan_state(state_path: Path) -> dict[str, Any]:
    """
    Load persisted scan state from *state_path*.

    Returns a fresh empty state dict if the file does not exist.
    Returns a fresh empty state dict (with a warning) if the file exists but
    the schema_version does not match.
    """
    if not state_path.exists():
        return _empty_state()

    with open(state_path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError:
            warnings.warn(
                f"scan_state.json at {state_path} is not valid JSON — treating as first run.",
                stacklevel=2,
            )
            return _empty_state()

    if data.get("schema_version") != STATE_SCHEMA_VERSION:
        warnings.warn(
            f"scan_state.json schema_version mismatch "
            f"(got {data.get('schema_version')!r}, expected {STATE_SCHEMA_VERSION!r}) "
            f"— treating as first run (full scan).",
            stacklevel=2,
        )
        return _empty_state()

    return data


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_full_scan_at": None,
        "seen_forks": {},
    }


def _save_scan_state(state: dict[str, Any], state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Config + auth helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Repo-level fetch helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Human-pick fetch helpers (curated URLs from config.yaml human_picks)
# ---------------------------------------------------------------------------
#
# Robots.txt and crawl politeness are out of scope here: human picks are
# hand-selected URLs from a small list, low volume (< few dozen entries
# expected), no crawling depth. Each scan does one GET per URL.

def _fetch_url_html(
    client: httpx.Client,
    url: str,
    max_retries: int = 3,
) -> str | None:
    """
    GET an arbitrary HTTPS URL and return its body as text.

    Sibling of `_get_with_retry` but for HTML responses (no JSON parse).
    Reuses the same exponential backoff on 429/503. Sends a custom
    User-Agent identifying the updater (some WordPress installs 403 on
    missing UA). Returns None on non-200 (including 404) after retries.
    """
    headers = {"User-Agent": _HUMAN_PICK_USER_AGENT}
    delay = 1.0
    for attempt in range(max_retries):
        try:
            response = client.get(url, headers=headers, follow_redirects=True)
        except httpx.HTTPError:
            time.sleep(delay)
            delay *= 2
            continue
        if response.status_code == 200:
            return response.text
        if response.status_code in (429, 503):
            time.sleep(delay)
            delay *= 2
            continue
        # 404 or other non-retriable
        return None
    return None


def _parse_html_meta(html: str) -> dict[str, str]:
    """
    Extract title + meta + Open Graph tags from an HTML document.

    Defensive: returns an empty dict on any parse failure or missing
    document. Keys returned (when present):
      - "title"            : <title> text
      - "description"      : <meta name="description" content="...">
      - "og:title"         : <meta property="og:title" content="...">
      - "og:description"   : <meta property="og:description" content="...">
      - "og:image"         : <meta property="og:image" content="...">
      - "og:site_name"     : <meta property="og:site_name" content="...">
    """
    if not html:
        return {}
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return {}

    out: dict[str, str] = {}

    try:
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            out["title"] = title_tag.string.strip()
    except Exception:
        pass

    try:
        desc_tag = soup.find("meta", attrs={"name": "description"})
        if desc_tag:
            content = desc_tag.get("content")
            if content:
                out["description"] = content.strip()
    except Exception:
        pass

    for og_key in ("og:title", "og:description", "og:image", "og:site_name"):
        try:
            tag = soup.find("meta", attrs={"property": og_key})
            if tag:
                content = tag.get("content")
                if content:
                    out[og_key] = content.strip()
        except Exception:
            continue

    return out


def _fetch_human_pick(
    client: httpx.Client,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """
    Fetch + parse a single human-pick URL entry.

    Mirrors the dict shape of `_fetch_repo_data` so build.py consumes both
    sources uniformly. On fetch failure, returns the entry with
    `fetch_status="failed"` and empty meta — the entry is preserved so the
    founder sees a "this URL no longer responds" signal at review time
    instead of a silent drop.
    """
    url = entry.get("url", "")
    fetched_at = datetime.now(timezone.utc).isoformat()
    html = _fetch_url_html(client, url)
    if html is None:
        return {
            "url": url,
            "config_entry": entry,
            "fetched_at": fetched_at,
            "meta": {},
            "fetch_status": "failed",
        }
    meta = _parse_html_meta(html)
    return {
        "url": url,
        "config_entry": entry,
        "fetched_at": fetched_at,
        "meta": meta,
        "fetch_status": "ok",
    }


# ---------------------------------------------------------------------------
# Fork helpers
# ---------------------------------------------------------------------------

def _fetch_forks(
    client: httpx.Client,
    headers: dict[str, str],
    owner: str,
    name: str,
) -> list[dict[str, Any]]:
    """Fetch all forks with full pagination (cheap — 1 API call per 100 forks)."""
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


def _load_previous_fork_data(raw_dir: Path, seed_key: str) -> dict[int, dict[str, Any]]:
    """
    Load fork data from the most recent raw file for a given seed_key.

    Returns a dict mapping fork repo ID → repo entry dict.
    Used in incremental mode to carry forward previously-seen fork data without
    re-fetching from the API.
    """
    files = sorted(raw_dir.glob("*.json"), reverse=True)
    for raw_file in files:
        try:
            with open(raw_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        result: dict[int, dict[str, Any]] = {}
        for entry in data.get("repos", []):
            if entry.get("is_fork") and entry.get("forked_from") == seed_key:
                repo_id = entry.get("meta", {}).get("id")
                if repo_id is not None:
                    result[repo_id] = entry
        if result:
            return result
    return {}


# ---------------------------------------------------------------------------
# Migration seed helper
# ---------------------------------------------------------------------------

def seed_state_from_raw(
    raw_path: Path | str,
    state_path: Path | str,
) -> None:
    """
    One-time migration: initialize scan_state.json from an existing raw file.

    Extracts all fork IDs present in *raw_path* and writes them to *state_path*
    so that the next ``updater scan`` treats those forks as already-seen and
    only fetches truly new forks.

    Usage (one-shot, run after the first full scan completes):
        python -c "
        from pathlib import Path
        from src.scan import seed_state_from_raw
        seed_state_from_raw('output/raw/20260519T211600Z.json', 'output/scan_state.json')
        "
    """
    raw_path = Path(raw_path)
    state_path = Path(state_path)

    with open(raw_path, "r", encoding="utf-8") as fh:
        raw_data = json.load(fh)

    state = _empty_state()
    state["last_full_scan_at"] = raw_data.get("scanned_at")

    for entry in raw_data.get("repos", []):
        if not entry.get("is_fork"):
            continue
        forked_from = entry.get("forked_from", "")
        repo_id = entry.get("meta", {}).get("id")
        if forked_from and repo_id is not None:
            state["seen_forks"].setdefault(forked_from, [])
            if repo_id not in state["seen_forks"][forked_from]:
                state["seen_forks"][forked_from].append(repo_id)

    _save_scan_state(state, state_path)
    total = sum(len(v) for v in state["seen_forks"].values())
    print(
        f"State seeded from {raw_path.name}: "
        f"{total} fork ID(s) across {len(state['seen_forks'])} seed(s) -> {state_path}"
    )


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(config_path: str = "config.yaml", full: bool = False) -> Path:
    """
    Execute the scan step. Returns the path of the raw output file written.

    Parameters
    ----------
    config_path:
        Path to config.yaml.
    full:
        When True, ignore scan_state.json and re-scan ALL forks for every
        seed with follow_forks=true.  Updates last_full_scan_at in state.
        When False (default), only new forks (IDs not in state) are fetched;
        previously-seen forks are carried forward from the most recent raw file.
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

    state_path = Path(
        cfg.get("output", {}).get("scan_state_path", str(raw_dir.parent / "scan_state.json"))
    )

    state = _load_scan_state(state_path)

    # If state is empty (first run or schema mismatch), force full scan
    is_first_run = (state["last_full_scan_at"] is None and not state["seen_forks"])
    if is_first_run:
        print("No prior scan state found — performing full scan (first run).")
        full = True

    results: list[dict[str, Any]] = []
    skill_results: list[dict[str, Any]] = []
    human_pick_results: list[dict[str, Any]] = []

    with httpx.Client(timeout=30.0) as client:
        # --- Ecosystem seeds scan ---
        for seed in cfg.get("seeds", []):
            owner = seed["owner"]
            name = seed["name"]
            follow_forks = seed.get("follow_forks", False)
            seed_key = f"{owner}/{name}"

            print(f"Scanning ecosystem seed {seed_key}...")
            repo_data = _fetch_repo_data(client, headers, owner, name)
            repo_data["owner"] = owner
            repo_data["name"] = name
            repo_data["is_fork"] = False
            results.append(repo_data)

            if follow_forks:
                # Always fetch the fork list (cheap: 1 call per 100 forks)
                all_fork_stubs = _fetch_forks(client, headers, owner, name)
                print(f"  Found {len(all_fork_stubs)} fork(s) in fork list.")

                if full:
                    # Full scan: fetch metadata for every fork
                    print(f"  Full scan — fetching metadata for all {len(all_fork_stubs)} fork(s)...")
                    new_fork_count = 0
                    for fork_stub in all_fork_stubs:
                        fork_owner = fork_stub.get("owner", {}).get("login", "")
                        fork_name = fork_stub.get("name", "")
                        fork_id = fork_stub.get("id")
                        if not fork_owner or not fork_name:
                            continue
                        fork_data = _fetch_repo_data(client, headers, fork_owner, fork_name)
                        fork_data["owner"] = fork_owner
                        fork_data["name"] = fork_name
                        fork_data["is_fork"] = True
                        fork_data["forked_from"] = seed_key
                        results.append(fork_data)
                        new_fork_count += 1

                    # Update state
                    seen_ids = [
                        stub.get("id")
                        for stub in all_fork_stubs
                        if stub.get("id") is not None
                    ]
                    state["seen_forks"][seed_key] = seen_ids
                    print(f"  Full scan complete: {new_fork_count} fork(s) fetched.")

                else:
                    # Incremental scan: compute delta
                    already_seen: set[int] = set(state["seen_forks"].get(seed_key, []))
                    new_stubs = [
                        stub for stub in all_fork_stubs
                        if stub.get("id") not in already_seen
                    ]
                    print(
                        f"  Incremental scan — {len(already_seen)} already seen, "
                        f"{len(new_stubs)} new fork(s) to fetch."
                    )

                    # Carry forward previously-seen fork data from most recent raw
                    prior_fork_data = _load_previous_fork_data(raw_dir, seed_key)

                    # Add all previously-seen forks (from prior raw) to results
                    carried = 0
                    for fork_id in already_seen:
                        if fork_id in prior_fork_data:
                            results.append(prior_fork_data[fork_id])
                            carried += 1

                    if carried < len(already_seen):
                        missing = len(already_seen) - carried
                        print(
                            f"  Warning: {missing} previously-seen fork(s) not found in "
                            f"prior raw files — they will be absent from this run's output."
                        )

                    # Fetch new forks
                    new_ids: list[int] = []
                    for fork_stub in new_stubs:
                        fork_owner = fork_stub.get("owner", {}).get("login", "")
                        fork_name = fork_stub.get("name", "")
                        fork_id = fork_stub.get("id")
                        if not fork_owner or not fork_name:
                            continue
                        fork_data = _fetch_repo_data(client, headers, fork_owner, fork_name)
                        fork_data["owner"] = fork_owner
                        fork_data["name"] = fork_name
                        fork_data["is_fork"] = True
                        fork_data["forked_from"] = seed_key
                        results.append(fork_data)
                        if fork_id is not None:
                            new_ids.append(fork_id)

                    # Update state: append new IDs
                    current_ids = list(already_seen) + new_ids
                    state["seen_forks"][seed_key] = current_ids
                    print(
                        f"  Incremental scan complete: {len(new_ids)} new fork(s) fetched, "
                        f"{carried} carried from prior raw."
                    )

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

        # --- Human picks scan (curated URLs from config.yaml) ---
        # Same YAML quirk handling as skill_sources: `or []` covers the case
        # where the section is present in YAML but parses to None.
        for entry in (cfg.get("human_picks") or []):
            url = entry.get("url", "")
            if not url:
                print("  Warning: human_picks entry without 'url' — skipping.")
                continue
            print(f"Fetching human pick: {url}...")
            pick_data = _fetch_human_pick(client, entry)
            if pick_data["fetch_status"] == "failed":
                print(f"  Warning: fetch failed for {url} — entry kept with empty meta.")
            human_pick_results.append(pick_data)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if full:
        state["last_full_scan_at"] = timestamp
    _save_scan_state(state, state_path)

    out_path = raw_dir / f"{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "scanned_at": timestamp,
                "repos": results,
                "skill_sources_raw": skill_results,
                "human_picks_raw": human_pick_results,
            },
            fh,
            indent=2,
            default=str,
        )

    print(
        f"Scan complete. {len(results)} ecosystem repos, "
        f"{len(skill_results)} skill source(s), "
        f"{len(human_pick_results)} human pick(s) written to {out_path}"
    )
    return out_path
