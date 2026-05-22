# SPDX-License-Identifier: AGPL-3.0-only
"""
review.py — Review step with diff display and threshold gate.

Compares current bulletin JSON files with their .previous.json counterparts
(if they exist), prints a human-readable diff, checks configurable thresholds,
requests typed confirmation, and writes output/.review_flag on approval.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .schema.ecosystem import BulletinEcosystem
from .schema.patterns import BulletinPatterns
from .schema.skills import BulletinSkills


def _load_bulletin_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _split_eco_by_source_type(
    bulletin: dict[str, Any] | None,
    default_source_type: str = "github_scanned",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Split an ecosystem bulletin into (github_scanned, human_picked) sub-bulletins.

    Forward-compat invariant: a repo entry without `source_type` defaults to
    `github_scanned` — without this, the first diff after the v1.0.0 → v1.1.0
    upgrade would misclassify every previous entry as human-picked (because
    .previous.json files written by v1.0.0 lack the field entirely).

    Returns (None, None) if the input bulletin is None (no previous file).
    """
    if bulletin is None:
        return None, None
    gh_repos: list[dict[str, Any]] = []
    hp_repos: list[dict[str, Any]] = []
    for r in bulletin.get("repos", []):
        src = r.get("source_type", default_source_type)
        if src == "human_picked":
            hp_repos.append(r)
        else:
            gh_repos.append(r)
    return {"repos": gh_repos}, {"repos": hp_repos}


def _diff_ecosystems(prev: dict[str, Any] | None, curr: dict[str, Any]) -> dict[str, Any]:
    """Compute a structured diff between previous and current ecosystem bulletins.

    Watches GitHub-scanned-style fields (description, license, jurisdiction,
    capabilities, is_active, stars). For a human-picks-focused diff, see
    `_diff_eco_human_picks` which watches different fields (topic, tags,
    notes_curatorial).
    """
    if prev is None:
        return {"added": curr.get("repos", []), "removed": [], "changed": [], "unchanged": []}

    prev_by_key = {f"{r['owner']}/{r['name']}": r for r in prev.get("repos", [])}
    curr_by_key = {f"{r['owner']}/{r['name']}": r for r in curr.get("repos", [])}

    added = [r for k, r in curr_by_key.items() if k not in prev_by_key]
    removed = [r for k, r in prev_by_key.items() if k not in curr_by_key]
    changed = []
    unchanged = []

    for k, curr_r in curr_by_key.items():
        if k in prev_by_key:
            prev_r = prev_by_key[k]
            diffs: dict[str, Any] = {}
            for field in ("description", "license", "inferred_jurisdiction",
                          "inferred_capabilities", "is_active"):
                if curr_r.get(field) != prev_r.get(field):
                    diffs[field] = {"prev": prev_r.get(field), "curr": curr_r.get(field)}
            stars_delta = (curr_r.get("stars") or 0) - (prev_r.get("stars") or 0)
            if abs(stars_delta) >= 10:
                diffs["stars_delta"] = stars_delta
            if diffs:
                changed.append({"repo": k, "diffs": diffs})
            else:
                unchanged.append(k)

    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged}


def _diff_eco_human_picks(prev: dict[str, Any] | None, curr: dict[str, Any]) -> dict[str, Any]:
    """Compute a structured diff for the human-picked subset of the ecosystem bulletin.

    Watches curatorial fields (topic, tags, notes_curatorial) plus `description`
    (which reflects og:description and may change if the source site updates).
    """
    if prev is None:
        return {"added": curr.get("repos", []), "removed": [], "changed": [], "unchanged": []}

    prev_by_key = {f"{r['owner']}/{r['name']}": r for r in prev.get("repos", [])}
    curr_by_key = {f"{r['owner']}/{r['name']}": r for r in curr.get("repos", [])}

    added = [r for k, r in curr_by_key.items() if k not in prev_by_key]
    removed = [r for k, r in prev_by_key.items() if k not in curr_by_key]
    changed = []
    unchanged = []

    for k, curr_r in curr_by_key.items():
        if k in prev_by_key:
            prev_r = prev_by_key[k]
            diffs: dict[str, Any] = {}
            for field in ("topic", "tags", "notes_curatorial", "description"):
                if curr_r.get(field) != prev_r.get(field):
                    diffs[field] = {"prev": prev_r.get(field), "curr": curr_r.get(field)}
            if diffs:
                changed.append({"repo": k, "diffs": diffs})
            else:
                unchanged.append(k)

    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged}


def _diff_patterns(prev: dict[str, Any] | None, curr: dict[str, Any]) -> dict[str, Any]:
    if prev is None:
        return {"added": curr.get("patterns", []), "removed": [], "changed": []}

    prev_by_task = {p["task_name"]: p for p in prev.get("patterns", [])}
    curr_by_task = {p["task_name"]: p for p in curr.get("patterns", [])}

    added = [p for k, p in curr_by_task.items() if k not in prev_by_task]
    removed = [p for k, p in prev_by_task.items() if k not in curr_by_task]
    changed = []
    for k, curr_p in curr_by_task.items():
        if k in prev_by_task:
            prev_p = prev_by_task[k]
            if curr_p.get("prompt_template") != prev_p.get("prompt_template"):
                changed.append({"task": k})

    return {"added": added, "removed": removed, "changed": changed}


def _print_eco_diff(diff: dict[str, Any]) -> None:
    """Print the github_scanned subset of the ecosystem diff."""
    added = diff["added"]
    removed = diff["removed"]
    changed = diff["changed"]

    print("\n=== ECOSYSTEM DIFF (github_scanned) ===")
    if added:
        print(f"  + ADDED ({len(added)} repo{'s' if len(added) != 1 else ''}):")
        for r in added:
            print(f"      {r.get('owner')}/{r.get('name')} — {r.get('description', '')[:60]}")
    if removed:
        print(f"  - REMOVED ({len(removed)} repo{'s' if len(removed) != 1 else ''}):")
        for r in removed:
            print(f"      {r.get('owner')}/{r.get('name')}")
    if changed:
        print(f"  ~ CHANGED ({len(changed)} repo{'s' if len(changed) != 1 else ''}):")
        for c in changed:
            print(f"      {c['repo']}:")
            for field, delta in c["diffs"].items():
                print(f"        {field}: {delta['prev']!r} → {delta['curr']!r}")
    if not added and not removed and not changed:
        print("  (no changes)")


def _print_eco_diff_human_picks(diff: dict[str, Any]) -> None:
    """Print the human-picked subset of the ecosystem diff.

    Shows curator metadata (`topic`, `notes_curatorial` truncated) on ADDED
    entries so the founder confirms visually that the curated picks landed
    correctly.
    """
    added = diff["added"]
    removed = diff["removed"]
    changed = diff["changed"]

    print("\n=== HUMAN PICKS DIFF (curated by founder) ===")
    if added:
        print(f"  + ADDED ({len(added)} pick{'s' if len(added) != 1 else ''}):")
        for r in added:
            topic = r.get("topic") or "(no topic)"
            url = r.get("url", "")
            print(f"      {url}")
            print(f"        topic: {topic}")
            notes = r.get("notes_curatorial") or ""
            if notes:
                first_line = notes.strip().splitlines()[0]
                snippet = first_line[:120]
                print(f"        notes : {snippet}{'...' if len(first_line) > 120 else ''}")
            tags = r.get("tags") or []
            if tags:
                print(f"        tags  : {', '.join(tags)}")
    if removed:
        print(f"  - REMOVED ({len(removed)} pick{'s' if len(removed) != 1 else ''}):")
        for r in removed:
            print(f"      {r.get('url') or r.get('owner', '')}")
    if changed:
        print(f"  ~ CHANGED ({len(changed)} pick{'s' if len(changed) != 1 else ''}):")
        for c in changed:
            print(f"      {c['repo']}:")
            for field, delta in c["diffs"].items():
                prev_repr = repr(delta['prev'])[:80]
                curr_repr = repr(delta['curr'])[:80]
                print(f"        {field}: {prev_repr} → {curr_repr}")
    if not added and not removed and not changed:
        print("  (no changes)")


def _diff_skills(prev: dict[str, Any] | None, curr: dict[str, Any]) -> dict[str, Any]:
    """Compute a structured diff between previous and current skills bulletins."""
    if prev is None:
        return {"added": curr.get("skills", []), "removed": [], "changed": []}

    prev_by_id = {s["id"]: s for s in prev.get("skills", [])}
    curr_by_id = {s["id"]: s for s in curr.get("skills", [])}

    added = [s for k, s in curr_by_id.items() if k not in prev_by_id]
    removed = [s for k, s in prev_by_id.items() if k not in curr_by_id]
    changed = []

    for k, curr_s in curr_by_id.items():
        if k in prev_by_id:
            prev_s = prev_by_id[k]
            diffs: dict[str, Any] = {}
            for field in (
                "tier",
                "jurisdiction",
                "italian_adaptation_status",
                "critical_alert",
                "description_it",
            ):
                if curr_s.get(field) != prev_s.get(field):
                    diffs[field] = {"prev": prev_s.get(field), "curr": curr_s.get(field)}
            if diffs:
                changed.append({"skill": k, "diffs": diffs})

    return {"added": added, "removed": removed, "changed": changed}


def _print_skl_diff(diff: dict[str, Any]) -> None:
    added = diff["added"]
    removed = diff["removed"]
    changed = diff["changed"]

    print("\n=== SKILLS DIFF ===")
    if added:
        print(f"  + ADDED ({len(added)} skill{'s' if len(added) != 1 else ''}):")
        for s in added:
            print(f"      [{s.get('tier')}] {s.get('id')} — {s.get('name', '')}")
    if removed:
        print(f"  - REMOVED ({len(removed)}):")
        for s in removed:
            print(f"      {s.get('id')}")
    if changed:
        print(f"  ~ CHANGED ({len(changed)} skill{'s' if len(changed) != 1 else ''}):")
        for c in changed:
            print(f"      {c['skill']}:")
            for field, delta in c["diffs"].items():
                print(f"        {field}: {delta['prev']!r} → {delta['curr']!r}")
    if not added and not removed and not changed:
        print("  (no changes)")


def _print_pat_diff(diff: dict[str, Any]) -> None:
    added = diff["added"]
    removed = diff["removed"]
    changed = diff["changed"]

    print("\n=== PATTERNS DIFF ===")
    if added:
        print(f"  + ADDED ({len(added)} pattern{'s' if len(added) != 1 else ''}):")
        for p in added:
            print(f"      [{p.get('extraction_confidence')}] {p.get('task_name')} "
                  f"← {p.get('source_repo')}")
    if removed:
        print(f"  - REMOVED ({len(removed)}):")
        for p in removed:
            print(f"      {p.get('task_name')}")
    if changed:
        print(f"  ~ CHANGED prompt_template ({len(changed)}):")
        for c in changed:
            print(f"      {c['task']}")
    if not added and not removed and not changed:
        print("  (no changes)")


def run(config_path: str = "config.yaml") -> bool:
    """
    Execute the review step. Returns True if the founder approved, False otherwise.
    On approval, writes the review_flag file.
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    eco_path = Path(cfg["output"]["ecosystem_path"])
    pat_path = Path(cfg["output"]["patterns_path"])
    skl_path = Path(cfg["output"].get("skills_path", "output/bulletin_skills.json"))
    prev_suffix = cfg["output"]["previous_suffix"]
    review_flag_path = Path(cfg["output"]["review_flag_path"])
    warn_pct = cfg["threshold_policy"]["warn_repos_changed_pct"]
    warn_pat_count = cfg["threshold_policy"]["warn_new_patterns_count"]
    warn_skl_count = cfg["threshold_policy"].get("warn_skills_changed_count", 3)
    warn_hp_count = cfg["threshold_policy"].get("warn_human_picks_changed_count", 3)

    if not eco_path.exists() or not pat_path.exists():
        print("Error: bulletin files not found. Run `updater build` first.")
        sys.exit(1)

    curr_eco = _load_bulletin_json(eco_path)
    curr_pat = _load_bulletin_json(pat_path)
    curr_skl = _load_bulletin_json(skl_path)  # may be None if file doesn't exist yet

    eco_prev_path = eco_path.with_suffix("").with_suffix(prev_suffix)
    pat_prev_path = pat_path.with_suffix("").with_suffix(prev_suffix)
    skl_prev_path = skl_path.with_suffix("").with_suffix(prev_suffix)

    prev_eco = _load_bulletin_json(eco_prev_path)
    prev_pat = _load_bulletin_json(pat_prev_path)
    prev_skl = _load_bulletin_json(skl_prev_path)

    # Split ecosystem bulletins by source_type so diffs / threshold gates
    # operate per-track. Forward-compat invariant: entries without source_type
    # in the previous bulletin default to 'github_scanned' (otherwise the
    # first diff after v1.0.0 → v1.1.0 upgrade would misclassify all previous
    # entries as human-picked).
    prev_eco_gh, prev_eco_hp = _split_eco_by_source_type(prev_eco)
    curr_eco_gh, curr_eco_hp = _split_eco_by_source_type(curr_eco)

    eco_diff = _diff_ecosystems(prev_eco_gh, curr_eco_gh or {"repos": []})
    eco_diff_hp = _diff_eco_human_picks(prev_eco_hp, curr_eco_hp or {"repos": []})
    pat_diff = _diff_patterns(prev_pat, curr_pat)
    skl_diff = _diff_skills(prev_skl, curr_skl) if curr_skl is not None else {"added": [], "removed": [], "changed": []}

    _print_eco_diff(eco_diff)
    _print_eco_diff_human_picks(eco_diff_hp)
    _print_pat_diff(pat_diff)
    _print_skl_diff(skl_diff)

    # Validate against pydantic schema
    try:
        BulletinEcosystem.model_validate(curr_eco)
        BulletinPatterns.model_validate(curr_pat)
        if curr_skl is not None:
            BulletinSkills.model_validate(curr_skl)
        print("\nSchema validation: OK")
    except Exception as exc:
        print(f"\nSchema validation FAILED: {exc}")
        sys.exit(1)

    # Threshold checks.
    # For github_scanned ecosystem: percentage-based threshold (the set is large
    # ~900+ forks, so absolute count is noisy). For human_picked: absolute count
    # threshold (the set is small, expected < few dozen entries, so percentage
    # would over-trigger).
    gh_total = len(curr_eco_gh.get("repos", [])) if curr_eco_gh else 0
    gh_changed_count = (
        len(eco_diff["added"]) + len(eco_diff["removed"]) + len(eco_diff["changed"])
    )
    gh_changed_pct = (gh_changed_count / gh_total * 100) if gh_total > 0 else 0

    hp_changed_count = (
        len(eco_diff_hp["added"]) + len(eco_diff_hp["removed"]) + len(eco_diff_hp["changed"])
    )

    new_patterns_count = len(pat_diff["added"])
    total_skills = len(curr_skl.get("skills", [])) if curr_skl else 0
    skill_changed_count = len(skl_diff["added"]) + len(skl_diff["removed"]) + len(skl_diff["changed"])
    skill_changed_pct = (skill_changed_count / total_skills * 100) if total_skills > 0 else 0

    warnings: list[str] = []
    if gh_changed_pct > warn_pct:
        warnings.append(
            f"WARNING: {gh_changed_pct:.0f}% of github_scanned repos changed "
            f"(threshold: {warn_pct}%)"
        )
    if hp_changed_count > warn_hp_count:
        warnings.append(
            f"WARNING: {hp_changed_count} human pick change(s) "
            f"(threshold: {warn_hp_count} absolute)"
        )
    if new_patterns_count > warn_pat_count:
        warnings.append(
            f"WARNING: {new_patterns_count} new patterns "
            f"(threshold: {warn_pat_count})"
        )
    if skill_changed_count > warn_skl_count:
        warnings.append(
            f"WARNING: {skill_changed_count} skill change(s) "
            f"(threshold: {warn_skl_count})"
        )
    if total_skills > 0 and skill_changed_pct > 30:
        warnings.append(
            f"WARNING: {skill_changed_pct:.0f}% of skills changed "
            f"(threshold: 30%)"
        )

    print()
    if warnings:
        for w in warnings:
            print(f"  *** {w}")
        print()
        print(
            "Large changes detected. Type 'conferma' and press Enter to proceed, "
            "or anything else to abort:"
        )
        answer = input("> ").strip().lower()
        if answer != "conferma":
            print("Aborted.")
            return False
    else:
        print("Press Enter to approve and set review flag, or Ctrl+C to abort:")
        answer = input("> ").strip().lower()
        if answer not in ("", "y", "yes", "si", "sì"):
            print("Aborted.")
            return False

    # Write review flag
    review_flag_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with open(review_flag_path, "w", encoding="utf-8") as fh:
        fh.write(timestamp)

    print(f"\nReview flag set at {timestamp}. You may now run `updater publish`.")
    return True
