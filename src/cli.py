# SPDX-License-Identifier: AGPL-3.0-only
"""
cli.py — Click entry point for regia-bollettino-updater.

Sub-commands: scan, build, review, publish.
Installed as `updater` via pyproject.toml [project.scripts].
"""

from __future__ import annotations

import click

from pathlib import Path

from . import scan, build, review, publish, legal_patterns


@click.group()
def cli() -> None:
    """regia-bollettino-updater — feeds BeccarIA plugin bollettini."""


@cli.command(name="scan")
@click.option(
    "--config",
    default="config.yaml",
    show_default=True,
    help="Path to config.yaml",
)
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help=(
        "Force full re-scan of all forks (ignores scan_state.json). "
        "Updates last_full_scan_at in state. "
        "Default: incremental (only new forks since last scan)."
    ),
)
def scan_cmd(config: str, full: bool) -> None:
    """Interroga GitHub API e raccoglie dati grezzi in output/raw/.

    Di default usa la scansione incrementale: solo i fork nuovi (non ancora
    visti in output/scan_state.json) vengono interrogati via API. Usa --full
    per ri-scansionare tutti i fork (utile per re-verifica periodica).
    """
    scan.run(config_path=config, full=full)


@cli.command(name="build")
@click.option(
    "--config",
    default="config.yaml",
    show_default=True,
    help="Path to config.yaml",
)
def build_cmd(config: str) -> None:
    """Elabora dati grezzi e produce bulletin_ecosystem.json e bulletin_patterns.json."""
    build.run(config_path=config)


@cli.command(name="review")
@click.option(
    "--config",
    default="config.yaml",
    show_default=True,
    help="Path to config.yaml",
)
def review_cmd(config: str) -> None:
    """Mostra diff vs bollettino precedente e richiede conferma typed."""
    review.run(config_path=config)


@cli.command(name="publish")
@click.option(
    "--config",
    default="config.yaml",
    show_default=True,
    help="Path to config.yaml",
)
def publish_cmd(config: str) -> None:
    """Carica i bollettini sul VPS (aborta se review non eseguito o stale)."""
    publish.run(config_path=config)


@cli.command(name="generate-legal-patterns")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run on a single target as sanity check (skip full batch).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap the number of targets processed (default: all).",
)
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, dir_okay=True, exists=True),
    default=".",
    show_default=True,
    help="Repo root containing .env, output/, and src/ (default: cwd).",
)
@click.option(
    "--coverage-path",
    type=click.Path(file_okay=True, dir_okay=False, exists=True),
    default=None,
    help="Override coverage targets file (default: output/pilot_domain_coverage_v1.json).",
)
@click.option(
    "--append",
    is_flag=True,
    default=False,
    help="Append-only doctrine: load existing patterns, skip target task_names already present, merge new with existing. (ratified 2026-05-28 SID-20260528-manual)",
)
def generate_legal_patterns_cmd(
    dry_run: bool,
    limit: int | None,
    repo_root: str,
    coverage_path: str | None,
    append: bool,
) -> None:
    """Genera pattern legali BeccarIA via Haiku batch (schema v2 scaffold-not-answer).

    Pipeline: legge i target da output/pilot_domain_coverage_v1.json (o --coverage-path)
    + prompt template da src/legal_patterns_prompt_v2.txt, invoca Haiku per ogni target,
    valida v2 con max 2 refinement round, scrive bulletin_legal_patterns.json
    + bulletin_legal_patterns_pending_review.json in output/.

    --append: doctrine ratificata 2026-05-28 SID-20260528-manual — il bullettino
    cresce per accumulo. Carica i pattern esistenti, salta target già coperti
    (matching su task_name derivato da pattern_id), genera SOLO i nuovi,
    mergea con esistente. Senza --append, behavior overwrite legacy.

    Hard cost cap $10 USD (expected ~$1.50). Requires ANTHROPIC_API_KEY in .env.
    """
    result = legal_patterns.run_batch(
        repo_root=Path(repo_root).resolve(),
        dry_run=dry_run,
        limit=limit,
        coverage_path=Path(coverage_path).resolve() if coverage_path else None,
        append_mode=append,
    )
    if result.aborted:
        raise click.ClickException(f"Batch aborted: {result.abort_reason}")


if __name__ == "__main__":
    cli()
