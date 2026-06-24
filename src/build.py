# SPDX-License-Identifier: AGPL-3.0-only
"""
build.py — Build bollettini JSON from raw scan output.

Reads the most recent raw/*.json file, infers jurisdiction and capabilities
from README text, computes is_active, validates against pydantic schema,
and writes bulletin_ecosystem.json and bulletin_skills.json.

bulletin_patterns.json is NOT written by build.py: it is produced exclusively
by `updater generate-legal-patterns` (legal_patterns.py), which generates
curated proprietary patterns via the Haiku batch pipeline. build.py will
create an empty-envelope bulletin_patterns.json only if the file does not
yet exist (first-run bootstrap), and leaves any existing file untouched.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .schema.ecosystem import BulletinEcosystem, EcosystemRepo, SCHEMA_VERSION as ECO_VERSION
from .schema.patterns import BulletinPatterns, Pattern, SCHEMA_VERSION as PAT_VERSION, ConfidenceLevel
from .schema.skills import BulletinSkills, SkillEntry, SCHEMA_VERSION as SKL_VERSION


# ---------------------------------------------------------------------------
# Jurisdiction inference helpers
# ---------------------------------------------------------------------------

_JURISDICTION_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(codice civile|codice penale|tribunale|avvocato|italiano)\b", re.I), "IT"),
    (re.compile(r"\b(european union|eu law|gdpr|regulation \(eu\))\b", re.I), "EU"),
    (re.compile(r"\b(united states code|u\.s\.c\.|federal register|american law)\b", re.I), "US"),
    (re.compile(r"\b(schweizer recht|loi suisse|diritto svizzero)\b", re.I), "CH"),
]


def _infer_jurisdiction(readme: str, owner: str = "", name: str = "") -> str:
    for pattern, jurisdiction in _JURISDICTION_RULES:
        if pattern.search(readme):
            return jurisdiction
    return "Unknown"


# ---------------------------------------------------------------------------
# Capability inference helpers
# ---------------------------------------------------------------------------

_CAPABILITY_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcontract[\s_-]?review\b|\breview[\s_-]?contract\b", re.I), "contract_review"),
    (re.compile(r"\bredlin\w*|\bmarkup\b", re.I), "redlining"),
    (re.compile(r"\bclause[\s_-]?extract|\bextract[\s_-]?clause", re.I), "clause_extraction"),
    (re.compile(r"\bpseudonym\w*|\banonymiz\w*|\bde-?identif\w*", re.I), "pseudonymization"),
    (re.compile(r"\bcase[\s_-]?summar|\bsummar[\s_-]?case|\bcase[\s_-]?brief", re.I), "case_summarization"),
    (re.compile(r"\bdeposition\b", re.I), "deposition_analysis"),
    (re.compile(r"\bdue[\s_-]?diligence\b", re.I), "due_diligence"),
    (re.compile(r"\blegal[\s_-]?research|\bresearch[\s_-]?legal|\bgiurisprudenz", re.I), "legal_research"),
    (re.compile(r"\btemplate\b|\bboilerplate\b|\bdraft\b", re.I), "document_drafting"),
]


def _infer_capabilities(readme: str) -> list[str]:
    found: list[str] = []
    for pattern, cap in _CAPABILITY_RULES:
        if pattern.search(readme) and cap not in found:
            found.append(cap)
    return found


# ---------------------------------------------------------------------------
# Pattern extraction helpers
# ---------------------------------------------------------------------------

# Matches fenced code blocks with an optional label line above them
_CODE_FENCE_PATTERN = re.compile(
    r"(?:^#+\s*(?P<label>[^\n]+)\n)?```[a-z]*\n(?P<body>.*?)```",
    re.DOTALL | re.MULTILINE,
)

# Sections in README that typically contain prompt examples
_PROMPT_SECTION_PATTERN = re.compile(
    r"^#{1,3}\s*(examples?|usage|prompts?|templates?|how to use)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_patterns(
    readme: str,
    owner: str,
    name: str,
    url: str,
    license_spdx: str,
) -> list[Pattern]:
    """
    Best-effort extraction of prompt patterns from a README.

    Heuristic strategy:
    1. Look for sections labelled Examples/Usage/Prompts/Templates.
    2. Within those sections, extract code-fenced blocks.
    3. Assign extraction_confidence based on explicitness of delimiters.

    This is an interpretive step — extraction_confidence reflects certainty.
    """
    patterns: list[Pattern] = []

    # Find all code-fenced blocks
    for match in _CODE_FENCE_PATTERN.finditer(readme):
        body = match.group("body").strip()
        label = (match.group("label") or "").strip()

        if len(body) < 20:
            continue

        # Determine confidence
        section_match = _PROMPT_SECTION_PATTERN.search(
            readme[: match.start()][-500:]  # look back 500 chars for a section heading
        )
        if label and section_match:
            confidence: ConfidenceLevel = "high"
            task_name = re.sub(r"\W+", "_", label.lower()).strip("_") or "unknown_task"
        elif section_match:
            confidence = "medium"
            task_name = "extracted_task"
        else:
            confidence = "low"
            task_name = "heuristic_task"

        patterns.append(
            Pattern(
                task_name=task_name,
                description=label or f"Pattern extracted from {owner}/{name} README",
                prompt_template=body,
                source_repo=f"{owner}/{name}",
                source_owner=owner,
                source_url=url,
                source_license=license_spdx,
                extraction_confidence=confidence,
            )
        )

    return patterns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _latest_raw_file(raw_dir: Path) -> Path | None:
    files = sorted(raw_dir.glob("*.json"), reverse=True)
    return files[0] if files else None


def _spdx_from_license_data(license_data: dict[str, Any] | None) -> str:
    if license_data is None:
        return "Unknown"
    lic = license_data.get("license") or {}
    spdx = lic.get("spdx_id") or ""
    if spdx and spdx.upper() != "NOASSERTION":
        return spdx
    return lic.get("name") or "Unknown"


def _build_skill_entry_from_raw(raw: dict[str, Any], scan_date: str) -> SkillEntry:
    """
    Convert a raw skill_sources_raw entry (from scan output) into a SkillEntry.

    Uses resolved_tier, resolved_jurisdiction, resolved_license from the scan step.
    Falls back gracefully for any missing field.
    """
    meta = raw.get("meta") or {}
    owner = raw.get("owner") or meta.get("owner", {}).get("login", "unknown")
    name = raw.get("name") or meta.get("name", "unknown")
    repo_url = meta.get("html_url") or f"https://github.com/{owner}/{name}"
    description = meta.get("description") or ""

    slug = f"{owner}-{name}".lower().replace("/", "-").replace("_", "-")

    tier_raw = raw.get("resolved_tier", 2)
    try:
        tier = int(tier_raw)
        if tier not in (1, 2):
            tier = 2
    except (TypeError, ValueError):
        tier = 2

    jurisdiction_raw = raw.get("resolved_jurisdiction", "[?]")
    valid_jurisdictions = {"IT", "EU", "US", "other", "none", "[?]"}
    jurisdiction = jurisdiction_raw if jurisdiction_raw in valid_jurisdictions else "[?]"

    license_val = raw.get("resolved_license", "Unknown")

    return SkillEntry(
        id=slug,
        name=f"{owner}/{name}",
        description_it=description,
        repo_url=repo_url,
        source_repo=f"{owner}/{name}",
        jurisdiction=jurisdiction,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        last_seen=scan_date,
        italian_adaptation_status="pending",
        reputation=None,
        publisher=None,
        notes=None,
    )


def _build_human_pick_eco_entry(raw: dict[str, Any], now: datetime) -> EcosystemRepo:
    """
    Convert a raw human_picks_raw entry (from scan output) into an EcosystemRepo.

    Design choices specific to human picks:
    - GitHub-only fields (last_activity, stars, fork_count, is_active) are left None
    - inferred_capabilities is always empty: the curator's `tags` carry the
      semantic intent. Running the github_scanned regex inference on a short
      og:description string would produce noisy false matches.
    - source_type is stamped "human_picked"; curator defaults to "Michele Loi"
      (the founder operating the updater).
    - description follows a fallback chain (og:description → meta[description]
      → og:title → <title> → '[no description available]') so the field is never
      empty (the schema requires it).
    - name is derived from the URL host (and a path slug when the URL has a
      non-trivial path), so two picks from the same domain do not collide.
    """
    config_entry = raw.get("config_entry") or {}
    meta = raw.get("meta") or {}
    url = raw.get("url") or config_entry.get("url", "")

    # Description fallback chain.
    description = (
        meta.get("og:description")
        or meta.get("description")
        or meta.get("og:title")
        or meta.get("title")
        or "[no description available]"
    )

    # Derive name + owner from URL host (and path, when non-root).
    parsed = urlparse(url)
    host = parsed.netloc or url
    path = parsed.path.strip("/")
    host_slug = host.lower().replace(".", "-").replace(":", "-")
    if path:
        # Slugify path: lowercase + non-alphanumeric → '-', clamp 30 chars.
        path_slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")[:30]
        name = f"{host_slug}_{path_slug}" if path_slug else host_slug
    else:
        name = host_slug
    owner = host

    # added_date may arrive as ISO string (from YAML) or as a date (already parsed).
    added_date_raw = config_entry.get("added_date")
    added_date_val = None
    if isinstance(added_date_raw, date):
        added_date_val = added_date_raw
    elif isinstance(added_date_raw, str):
        try:
            added_date_val = datetime.fromisoformat(added_date_raw).date()
        except ValueError:
            added_date_val = None

    return EcosystemRepo(
        name=name,
        owner=owner,
        url=url,
        description=description,
        license=config_entry.get("license") or "Unknown",
        inferred_jurisdiction=config_entry.get("inferred_jurisdiction") or "Unknown",
        inferred_capabilities=[],
        last_activity=None,
        stars=None,
        fork_count=None,
        is_active=None,
        notes=None,
        source_type="human_picked",
        topic=config_entry.get("topic"),
        notes_curatorial=config_entry.get("notes"),
        added_date=added_date_val,
        tags=list(config_entry.get("tags") or []),
        curator=config_entry.get("curator") or "Michele Loi",
    )


def run(config_path: str = "config.yaml") -> tuple[Path, Path, Path]:
    """
    Execute the build step. Returns (ecosystem_path, patterns_path, skills_path).
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    raw_dir = Path(cfg["output"]["raw_dir"])
    eco_path = Path(cfg["output"]["ecosystem_path"])
    pat_path = Path(cfg["output"]["patterns_path"])
    skl_path = Path(cfg["output"].get("skills_path", "output/bulletin_skills.json"))
    active_days = cfg["threshold_policy"]["active_window_days"]

    raw_file = _latest_raw_file(raw_dir)
    if raw_file is None:
        raise FileNotFoundError(
            f"No raw scan file found in {raw_dir}. Run `updater scan` first."
        )

    print(f"Building from {raw_file}...")

    with open(raw_file, "r", encoding="utf-8") as fh:
        raw_data = json.load(fh)

    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(days=active_days)

    eco_repos: list[EcosystemRepo] = []

    for entry in raw_data.get("repos", []):
        meta = entry.get("meta") or {}
        readme = entry.get("readme") or ""
        license_data = entry.get("license")

        owner = entry.get("owner") or meta.get("owner", {}).get("login", "")
        name = entry.get("name") or meta.get("name", "")
        if not owner or not name:
            continue

        url = meta.get("html_url") or f"https://github.com/{owner}/{name}"
        description = meta.get("description") or ""
        license_spdx = _spdx_from_license_data(license_data)

        pushed_at_str = meta.get("pushed_at") or meta.get("updated_at") or ""
        try:
            last_activity = datetime.fromisoformat(pushed_at_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            last_activity = datetime(2000, 1, 1, tzinfo=timezone.utc)

        is_active = last_activity >= active_cutoff

        jurisdiction = _infer_jurisdiction(readme, owner, name)
        capabilities = _infer_capabilities(readme)

        eco_repos.append(
            EcosystemRepo(
                name=name,
                owner=owner,
                url=url,
                description=description,
                license=license_spdx,
                inferred_jurisdiction=jurisdiction,
                inferred_capabilities=capabilities,
                last_activity=last_activity,
                stars=meta.get("stargazers_count", 0),
                fork_count=meta.get("forks_count", 0),
                is_active=is_active,
                source_type="github_scanned",
            )
        )

    # --- Human picks: append to eco_repos with source_type='human_picked' ---
    # Curated entries from config.yaml. The scan step produced these in
    # raw_data['human_picks_raw']; build.py's only job is to map them to
    # EcosystemRepo with the right provenance + curator metadata.
    human_pick_count = 0
    for raw_pick in raw_data.get("human_picks_raw", []):
        try:
            eco_repos.append(_build_human_pick_eco_entry(raw_pick, now))
            human_pick_count += 1
        except Exception as exc:
            url_dbg = raw_pick.get("url", "?")
            print(f"  Warning: skipping human pick {url_dbg}: {exc}")
    if human_pick_count > 0:
        print(f"  Added {human_pick_count} human-picked entr{'y' if human_pick_count == 1 else 'ies'} to ecosystem bulletin.")

    # bulletin_patterns.json is NOT generated from README scraping.
    # It is produced exclusively by `updater generate-legal-patterns` (legal_patterns.py),
    # which runs the Haiku batch to create curated, proprietary patterns.
    # build.py writes an empty envelope here to avoid leaving stale scrape output
    # at this path if the file does not yet exist; it never overwrites a non-empty file.
    # Do NOT re-introduce README pattern extraction here -- see decision_log.
    all_patterns: list[Pattern] = []

    # --- Build bulletin_skills.json from skill_sources_raw ---
    scan_date_str = now.strftime("%Y-%m-%d")
    skill_entries: list[SkillEntry] = []
    for raw_skill in raw_data.get("skill_sources_raw", []):
        try:
            entry = _build_skill_entry_from_raw(raw_skill, scan_date_str)
            skill_entries.append(entry)
        except Exception as exc:
            owner_s = raw_skill.get("owner", "?")
            name_s = raw_skill.get("name", "?")
            print(f"  Warning: skipping skill source {owner_s}/{name_s}: {exc}")

    bulletin_eco = BulletinEcosystem(
        schema_version=ECO_VERSION,
        generated_at=now,
        source_count=len(eco_repos),
        repos=eco_repos,
    )
    bulletin_pat = BulletinPatterns(
        schema_version=PAT_VERSION,
        generated_at=now,
        source_count=0,
        patterns=all_patterns,
    )
    bulletin_skl = BulletinSkills(
        schema_version=SKL_VERSION,
        generated_at=now,
        source_count=len(skill_entries),
        skills=skill_entries,
    )

    eco_path.parent.mkdir(parents=True, exist_ok=True)
    with open(eco_path, "w", encoding="utf-8") as fh:
        fh.write(bulletin_eco.model_dump_json(indent=2))

    # bulletin_patterns.json is owned by `updater generate-legal-patterns`.
    # build.py writes an empty envelope ONLY if the file does not yet exist,
    # so that new environments have a valid (empty) file at the expected path.
    # If the file already exists (curated content from generate-legal-patterns),
    # build.py leaves it completely untouched.
    if not pat_path.exists():
        with open(pat_path, "w", encoding="utf-8") as fh:
            fh.write(bulletin_pat.model_dump_json(indent=2))

    skl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(skl_path, "w", encoding="utf-8") as fh:
        fh.write(bulletin_skl.model_dump_json(indent=2))

    pat_status = "preserved (curated)" if pat_path.exists() else "created (empty envelope)"
    print(
        f"Build complete. {len(eco_repos)} repos, {len(skill_entries)} skill(s). "
        f"bulletin_patterns.json: {pat_status}. "
        f"Written to {eco_path} and {skl_path}."
    )
    return eco_path, pat_path, skl_path
