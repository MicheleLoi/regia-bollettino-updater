# regia-bollettino-updater

Updater that monitors a configurable list of GitHub repositories in the
legal-AI open-source space, builds two JSON bulletin files, and publishes
them to a founder-operated VPS.

The bulletins are consumed by the **BeccarIA** plugin (`legal-tech-cowork`)
via its `ecosystem-scout` and `pattern-extractor` skills.

## What it does

1. **scan** — Queries the GitHub API for configured seed repositories and
   their forks. Writes a timestamped raw JSON file to `output/raw/`.
2. **build** — Reads the latest raw file, infers jurisdiction and legal-AI
   capabilities from README text, extracts prompt patterns, validates all
   output against Pydantic schemas, and writes two bulletin JSON files.
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

| Command          | Description                                        |
|------------------|----------------------------------------------------|
| `updater scan`   | Fetch GitHub API data → `output/raw/*.json`        |
| `updater build`  | Process raw data → two bulletin JSON files         |
| `updater review` | Show diff + threshold check + request confirmation |
| `updater publish`| Upload bulletins to VPS (requires review flag)     |

Each command accepts `--config path/to/config.yaml` (default: `config.yaml`).

## Configuration

Edit `config.yaml` before running:

```yaml
seeds:
  - owner: <github-owner>
    name: <repo-name>
    follow_forks: true   # also scan all forks

threshold_policy:
  active_window_days: 90          # repo older than this → is_active: false
  warn_repos_changed_pct: 30      # trigger typed confirm if >30% repos changed
  warn_new_patterns_count: 5      # trigger typed confirm if >5 new patterns
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

Key fields for `pattern-extractor` skill:
- `prompt_template`: verbatim text applied by Claude in conversation.
- `extraction_confidence`: `"high"` (clearly delimited in source), `"medium"`
  (plausible with minor interpretation), or `"low"` (heuristically reconstructed).
- `source_license`: SPDX identifier — required for AGPL attribution.

### Exporting JSON Schema

```bash
python -c "
import json
from src.schema.ecosystem import BulletinEcosystem
from src.schema.patterns import BulletinPatterns
with open('docs/bulletin_ecosystem.schema.json', 'w') as f:
    json.dump(BulletinEcosystem.model_json_schema(), f, indent=2)
with open('docs/bulletin_patterns.schema.json', 'w') as f:
    json.dump(BulletinPatterns.model_json_schema(), f, indent=2)
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
