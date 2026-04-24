from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from dirt_finder.analysis import analyze_sites
from dirt_finder.config import hangzhou_config, load_config, write_config
from dirt_finder.fetch import fetch_data
from dirt_finder.render import render_map

app = typer.Typer(
    help="Find reconnaissance candidate sites for a dirt-bike park near Hangzhou.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def init(
    preset: str = typer.Option("hangzhou", help="Config preset to write. Only 'hangzhou' exists."),
    output: Path = typer.Option(Path("configs/hangzhou.toml"), help="Output TOML config path."),
    overwrite: bool = typer.Option(False, help="Replace an existing config file."),
) -> None:
    """Write a starter config file."""
    if preset != "hangzhou":
        raise typer.BadParameter("Only the 'hangzhou' preset is supported.")
    if output.exists() and not overwrite:
        raise typer.BadParameter(f"{output} already exists. Pass --overwrite to replace it.")
    write_config(hangzhou_config(), output)
    console.print(f"[green]Wrote config[/green] {output}")


@app.command()
def fetch(config: Path = typer.Option(Path("configs/hangzhou.toml"), help="TOML config path.")) -> None:
    """Fetch/cache OSM, DEM, and land-cover inputs."""
    cfg = load_config(config)
    fetch_data(cfg)
    console.print("[green]Fetch complete[/green]")


@app.command()
def analyze(config: Path = typer.Option(Path("configs/hangzhou.toml"), help="TOML config path.")) -> None:
    """Analyze cached inputs and produce candidate files."""
    cfg = load_config(config)
    result = analyze_sites(cfg)
    console.print(f"[green]Analysis complete[/green] {len(result)} candidates")


@app.command()
def render(config: Path = typer.Option(Path("configs/hangzhou.toml"), help="TOML config path.")) -> None:
    """Render candidate results to an interactive HTML map."""
    cfg = load_config(config)
    output = render_map(cfg)
    console.print(f"[green]Rendered map[/green] {output}")


@app.command()
def run(config: Path = typer.Option(Path("configs/hangzhou.toml"), help="TOML config path.")) -> None:
    """Run fetch, analysis, and rendering."""
    cfg = load_config(config)
    fetch_data(cfg)
    result = analyze_sites(cfg)
    output = render_map(cfg)
    console.print(f"[green]Run complete[/green] {len(result)} candidates, map: {output}")
