# SPDX-License-Identifier: AGPL-3.0-only
"""
cli.py — Click entry point for regia-bollettino-updater.

Sub-commands: scan, build, review, publish.
Installed as `updater` via pyproject.toml [project.scripts].
"""

from __future__ import annotations

import click

from . import scan, build, review, publish


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


if __name__ == "__main__":
    cli()
