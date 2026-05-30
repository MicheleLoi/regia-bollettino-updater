# regia-bollettino-updater

Updater that monitors a configurable list of GitHub repositories in the
legal-AI open-source space, builds three JSON bulletin files, and publishes
them to a founder-operated VPS.

The bulletins are consumed by the **BeccarIA** plugin (`legal-tech-cowork`)
via its `ecosystem-scout`, `schemi-di-ragionamento`, and `catalogo` skills.

## What it does

1. **scan** — Queries the GitHub API for configured seed repositories and
   their forks. Writes a timestamped raw JSON file to `output/raw/`.
2. **build** — Reads the latest raw file, infers jurisdiction and legal-AI
   capabilities from README text, extracts prompt patterns, builds the skill
   catalog from `skill_sources`, validates all output against Pydantic schemas,
   and writes three bulletin JSON files.
3. **review** — Shows a human-readable diff against the previous bulletin,
   checks configurable thresholds, and requests typed confirmation from the
   founder. Writes a review flag on approval.
4. **publish** — Verifies the review flag, backs up the existing remote
   bulletins to `*.previous.json`, uploads the new files via SSH/SFTP, and
   optionally verifies the public URL.

**Human-in-the-loop is mandatory.** Publish refuses to run without a fresh
review flag. There is no automation on the VPS side — the updater runs on
the founder's computer.

## Installation

```
python -m venv venv
venv\Scripts\activate          # Windows
# or: source venv/bin/activate   (Linux/macOS)
pip install -e .[dev]
```

## Commands

| Command               | Description                                        |
|-----------------------|----------------------------------------------------|
| `updater scan`        | Fetch GitHub API data → `output/raw/*.json` (incremental by default) |
| `updater scan --full` | Force full re-scan of all forks (ignores scan state) |
| `updater build`       | Process raw data → three bulletin JSON files       |
| `updater review`      | Show diff + threshold check + request confirmation |
| `updater publish`     | Upload bulletins to VPS (requires review flag)     |

Each command accepts `--config path/to/config.yaml` (default: `config.yaml`).

## Incremental scan

For seeds with `follow_forks: true`, `updater scan` uses **incremental mode** by
default: only *new* forks (not yet seen in previous scans) trigger full API
fetches.  Previously-seen forks are carried forward from the most recent raw
file, so `build.py` always sees the complete fork set and needs no changes.

### How it works

1. **Fork list** — always fetched in full (cheap: 1 API call per 100 forks).
2. **Delta** — IDs not in `output/scan_state.json` → full metadata fetch (meta + README + license).
3. **Carry forward** — already-seen fork data is copied from the prior raw file into the new raw.
4. **State update** — new fork IDs are appended to `seen_forks[seed]` in `scan_state.json`.

The 6 explicitly-named seeds (`rafal-fryc/mikelocal`, etc.) are always fetched
fresh regardless of state — they are curated editorial entries, not delta-tracked.

### `output/scan_state.json` schema

```json
{
  "schema_version": "1.0.0",
  "last_full_scan_at": "20260519T211600Z",
  "seen_forks": {
    "willchen96/mike": [12345678, 23456789, ...]
  }
}
```

The file is created automatically on first run.  It lives alongside the
bulletins in `output/` and should be committed to the repository so subsequent
runs on any machine start from the correct incremental baseline.

### `--full` override

```bash
updater scan --full
```

Ignores `scan_state.json` and re-fetches every fork.  Updates
`last_full_scan_at`.  Recommended for periodic re-verification (e.g. every 6
months) to detect changes in previously-seen forks.

### First-run migration (after a manual full scan)

If you ran a full scan with the old always-full code and want to initialize the
state file from the resulting raw output (so the *next* run is incremental):

```python
from pathlib import Path
from src.scan import seed_state_from_raw

seed_state_from_raw(
    "output/raw/20260519T211600Z.json",   # your most recent full raw
    "output/scan_state.json",
)
```

Run once, then commit `output/scan_state.json`.  Subsequent `updater scan`
runs will only fetch new forks.

## Configuration

Edit `config.yaml` before running:

```yaml
seeds:
  - owner: <github-owner>
    name: <repo-name>
    follow_forks: true   # also scan all forks

skill_sources:
  # Founder configures real sources here before first production scan.
  # Format: { owner, name, default_tier (1 or 2), default_jurisdiction (IT/EU/[?]/other) }
  # default_tier and default_jurisdiction are fallbacks when SKILL.md declares nothing.
  #
  # - owner: anthropics
  #   name: claude-skills
  #   default_tier: 1
  #   default_jurisdiction: "[?]"

threshold_policy:
  active_window_days: 90          # repo older than this → is_active: false
  warn_repos_changed_pct: 30      # trigger typed confirm if >30% repos changed
  warn_new_patterns_count: 5      # trigger typed confirm if >5 new patterns
  warn_skills_changed_count: 3    # trigger typed confirm if >3 skill changes
  review_flag_max_age_minutes: 120
```

Required environment variables for `publish`:

| Variable        | Description                              |
|-----------------|------------------------------------------|
| `GITHUB_TOKEN`  | GitHub personal access token (scan step) |
| `VPS_HOST`      | VPS hostname or IP                       |
| `VPS_USER`      | SSH username                             |
| `VPS_PATH`      | Remote directory (e.g. `/var/www/bulletins`) |
| `VPS_KEY_PATH`  | Path to SSH private key (optional)       |
| `VPS_BULLETIN_URL` | Public HTTPS URL of bulletins directory (optional, for post-upload verify) |

## Schema dei bollettini

These schemas are the **source of truth** for consumers (BeccarIA skills).
Schema version: `1.0.0`.

### `bulletin_ecosystem.json`

```json
{
  "schema_version": "1.0.0",
  "generated_at": "2026-04-15T10:00:00+00:00",
  "source_count": 2,
  "repos": [
    {
      "name": "example-repo",
      "owner": "example-org",
      "url": "https://github.com/example-org/example-repo",
      "description": "A legal-AI tool for contract review",
      "license": "AGPL-3.0",
      "inferred_jurisdiction": "IT",
      "inferred_capabilities": ["contract_review", "clause_extraction"],
      "last_activity": "2026-04-01T00:00:00+00:00",
      "stars": 42,
      "fork_count": 7,
      "is_active": true,
      "notes": null
    }
  ]
}
```

Key fields for `ecosystem-scout` skill:
- `inferred_jurisdiction`: `"IT"`, `"EU"`, `"US"`, `"CH"`, or `"Unknown"`.
- `inferred_capabilities`: list of capability tags such as `contract_review`,
  `clause_extraction`, `pseudonymization`, `case_summarization`, `legal_research`.
- `is_active`: `true` if `last_activity` is within `active_window_days`.

### `bulletin_patterns.json`

```json
{
  "schema_version": "1.0.0",
  "generated_at": "2026-04-15T10:00:00+00:00",
  "source_count": 1,
  "patterns": [
    {
      "task_name": "contract_clause_extraction",
      "description": "Extract and analyse contract clauses",
      "prompt_template": "Review the following clause:\n\n[CLAUSE]\n\nIdentify: 1) type 2) obligations 3) risks.",
      "example_input": null,
      "example_output": null,
      "source_repo": "example-org/example-repo",
      "source_owner": "example-org",
      "source_url": "https://github.com/example-org/example-repo",
      "source_commit": null,
      "source_license": "AGPL-3.0",
      "extraction_confidence": "high"
    }
  ]
}
```

Key fields for `schemi-di-ragionamento` skill:
- `prompt_template`: verbatim text applied by Claude in conversation.
- `extraction_confidence`: `"high"` (clearly delimited in source), `"medium"`
  (plausible with minor interpretation), or `"low"` (heuristically reconstructed).
- `source_license`: SPDX identifier — required for AGPL attribution.

### `bulletin_skills.json`

Consumed by the **BeccarIA `catalogo` skill** (autonomous fetch from
`bulletins.micheleloi.pro/bulletin_skills.json`).

Schema model: `BulletinSkills` in `src/schema/skills.py`. Backward-compatible
with `legal-tech-cowork/beccaria/bollettino.json` (BeccarIA v3.x legacy format).

```json
{
  "schema_version": "1.0.0",
  "generated_at": "2026-05-17T10:00:00+00:00",
  "source_count": 1,
  "skills": [
    {
      "id": "anthropics-knowledge-work-legal",
      "name": "anthropics/knowledge-work-plugins",
      "description_it": "Plugin legale ufficiale Anthropic per Claude Cowork.",
      "repo_url": "https://github.com/anthropics/knowledge-work-plugins",
      "skill_path": "legal/.claude-plugin/plugin.json",
      "source_repo": "anthropics/knowledge-work-plugins",
      "source_url": null,
      "area": "commerciale",
      "jurisdiction": "[?]",
      "tier": 1,
      "publisher": {
        "name": "Anthropic",
        "type": "anthropic-official",
        "italian_localized": false
      },
      "reputation": {
        "stars": 12267,
        "last_commit": "2026-05-16",
        "commit_frequency_30d": 0,
        "contributors": 0,
        "open_issues": 112,
        "license": "Apache-2.0",
        "computed_quality_stars": 5,
        "computed_trend": "in crescita"
      },
      "founder_disclaimer": "Plugin ufficiale Anthropic — verificare adattamento IT prima dell'uso.",
      "recommended_for": "Studi che vogliono un punto di partenza certificato Anthropic.",
      "added_to_bollettino": "2026-05-17",
      "last_seen": "2026-05-17",
      "italian_adaptation_status": "pending",
      "critical_alert": false,
      "critical_alert_message": null,
      "critical_alert_severity": null,
      "notes": null
    }
  ]
}
```

Key fields for `catalogo` skill consumer:
- `id`: stable slug key (`owner-name` kebab-case). Used for diff in review step.
- `tier`: `1` = Anthropic-official; `2` = community vetted. REFUSE entries are
  filtered upstream and never reach this file.
- `jurisdiction`: `IT` | `EU` skips the Italian adaptation prompt at install;
  `other` | `none` | `[?]` triggers it.
- `italian_adaptation_status`: `pending` | `ready` | `stale`.
- `critical_alert`: if `true`, the `catalogo` skill should surface the
  `critical_alert_message` prominently before suggesting installation.

The `updater scan` step now fetches SKILL.md for each `skill_sources` entry and
parses YAML frontmatter for `tier` / `jurisdiction` / `license` declarations.
Config `default_tier` / `default_jurisdiction` are fallbacks when frontmatter is
absent or incomplete.

### Exporting JSON Schema

```bash
python -c "
import json
from src.schema.ecosystem import BulletinEcosystem
from src.schema.patterns import BulletinPatterns
from src.schema.skills import BulletinSkills
with open('docs/bulletin_ecosystem.schema.json', 'w') as f:
    json.dump(BulletinEcosystem.model_json_schema(), f, indent=2)
with open('docs/bulletin_patterns.schema.json', 'w') as f:
    json.dump(BulletinPatterns.model_json_schema(), f, indent=2)
with open('docs/bulletin_skills.schema.json', 'w') as f:
    json.dump(BulletinSkills.model_json_schema(), f, indent=2)
"
```

## AGPL-3.0 compliance

This repository is licensed under **GNU Affero General Public License v3.0**
(AGPL-3.0-only). Because the bulletins are served from a VPS over HTTPS, the
VPS operation constitutes network use under AGPL §13. The founder, as operator,
must ensure that the source code of this updater is publicly accessible.

Practical requirement: the Nginx configuration on the VPS must include:

```
add_header X-Source-Code "https://github.com/<owner>/regia-bollettino-updater";
```

See `deploy/vps_setup.md` for the full VPS setup instructions.

## Deploy VPS

See [`deploy/vps_setup.md`](deploy/vps_setup.md) for step-by-step instructions
on configuring the Nginx location block, TLS, CORS, and the AGPL §13 source
header.

## Development

```bash
pytest -v               # run all tests (mock-based, no real API calls)
```

## Lifecycle

After smoke test passes, the founder runs:

```bash
gh repo create regia-bollettino-updater --public --source=. --remote=origin
git push -u origin main
```

Then configures the VPS per `deploy/vps_setup.md` and runs the first live scan.
