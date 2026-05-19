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
def scan_cmd(config: str) -> None:
    """Interroga GitHub API e raccoglie dati grezzi in output/raw/."""
    scan.run(config_path=config)


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
