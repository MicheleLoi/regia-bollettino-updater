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


def _load_bulletin_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _diff_ecosystems(prev: dict[str, Any] | None, curr: dict[str, Any]) -> dict[str, Any]:
    """Compute a structured diff between previous and current ecosystem bulletins."""
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
            stars_delta = curr_r.get("stars", 0) - prev_r.get("stars", 0)
            if abs(stars_delta) >= 10:
                diffs["stars_delta"] = stars_delta
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
    added = diff["added"]
    removed = diff["removed"]
    changed = diff["changed"]

    print("\n=== ECOSYSTEM DIFF ===")
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
    prev_suffix = cfg["output"]["previous_suffix"]
    review_flag_path = Path(cfg["output"]["review_flag_path"])
    warn_pct = cfg["threshold_policy"]["warn_repos_changed_pct"]
    warn_pat_count = cfg["threshold_policy"]["warn_new_patterns_count"]

    if not eco_path.exists() or not pat_path.exists():
        print("Error: bulletin files not found. Run `updater build` first.")
        sys.exit(1)

    curr_eco = _load_bulletin_json(eco_path)
    curr_pat = _load_bulletin_json(pat_path)

    eco_prev_path = eco_path.with_suffix("").with_suffix(prev_suffix)
    pat_prev_path = pat_path.with_suffix("").with_suffix(prev_suffix)

    prev_eco = _load_bulletin_json(eco_prev_path)
    prev_pat = _load_bulletin_json(pat_prev_path)

    eco_diff = _diff_ecosystems(prev_eco, curr_eco)
    pat_diff = _diff_patterns(prev_pat, curr_pat)

    _print_eco_diff(eco_diff)
    _print_pat_diff(pat_diff)

    # Validate against pydantic schema
    try:
        BulletinEcosystem.model_validate(curr_eco)
        BulletinPatterns.model_validate(curr_pat)
        print("\nSchema validation: OK")
    except Exception as exc:
        print(f"\nSchema validation FAILED: {exc}")
        sys.exit(1)

    # Threshold checks
    total_repos = len(curr_eco.get("repos", [])) if curr_eco else 0
    changed_count = len(eco_diff["added"]) + len(eco_diff["removed"]) + len(eco_diff["changed"])
    changed_pct = (changed_count / total_repos * 100) if total_repos > 0 else 0
    new_patterns_count = len(pat_diff["added"])

    warnings: list[str] = []
    if changed_pct > warn_pct:
        warnings.append(
            f"WARNING: {changed_pct:.0f}% of repos changed "
            f"(threshold: {warn_pct}%)"
        )
    if new_patterns_count > warn_pat_count:
        warnings.append(
            f"WARNING: {new_patterns_count} new patterns "
            f"(threshold: {warn_pat_count})"
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
