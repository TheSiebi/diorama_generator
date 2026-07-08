"""Command-line entry point: coordinates + radius -> diorama (.blend / .3mf)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.text import Text

from .geocode import geocode_address
from .pipeline import generate
from .ui import print_outputs

app = typer.Typer(add_completion=False, help="Generate Swiss dioramas from an "
                  "address or coordinate + radius (swisstopo terrain/buildings "
                  "+ roads/water).")


@app.command()
def make(
    address: Optional[str] = typer.Option(
        None, "--address", "-a", help="Street address or place to geocode via "
        "Google (needs GOOGLE_MAPS_API_KEY). Alternative to --lat/--lon."),
    lat: Optional[float] = typer.Option(None, help="Latitude (WGS84). Ignored "
                                        "when --address is given."),
    lon: Optional[float] = typer.Option(None, help="Longitude (WGS84). Ignored "
                                        "when --address is given."),
    radius: float = typer.Option(400.0, help="AOI radius in metres."),
    out: Path = typer.Option(Path("out"), help="Output directory."),
    name: str = typer.Option("diorama", help="Base name for output files."),
    mesh_res: float = typer.Option(2.0, help="Terrain mesh resolution (m)."),
    base_thickness: float = typer.Option(12.0, help="Solid base thickness (m)."),
    features: bool = typer.Option(True, help="Include roads & water."),
    source: str = typer.Option("tlm", help="Feature source: 'tlm' (swissTLM3D, "
                               "~2.9 GB one-time download) or 'osm' (light)."),
    buildings: str = typer.Option("auto", help="Buildings data: 'auto' (Stadt "
                                  "Zürich weekly city model when the AOI is "
                                  "inside Zurich, else falls back to "
                                  "'latest'), 'zurich' (force the city model), "
                                  "'latest' (whole-country swissBUILDINGS3D "
                                  "3.0, ~14 GB once; also forces it inside "
                                  "Zurich, for separately colored roofs), "
                                  "'v2' (whole-country swissBUILDINGS3D 2.0, "
                                  "~3.6 GB once, no roof solids) or 'tiles' "
                                  "(small per-tile 3.0 files, stale where "
                                  "3.0 is stale)."),
    trees: bool = typer.Option(False, "--trees/--no-trees", help="Add a tree "
                               "layer (swissTLM3D forest fill + single trees)."),
    base: str = typer.Option("cylinder", help="Base style: 'cylinder' (plain "
                             "puck skirt, the default), 'table' (round compass "
                             "table from assets/table.fbx, headings aligned to "
                             "the diorama, if you have the asset) or a path to "
                             "your own mesh (.fbx/.glb/.gltf/.obj/.stl/.ply/"
                             ".blend; see 'Custom base meshes' in the README)."),
    preview: bool = typer.Option(True, help="Render a preview PNG."),
    blender: bool = typer.Option(True, help="Run Blender to build the .blend."),
):
    """Build a diorama and write .glb, .3mf, .blend (+ preview) to OUT."""
    load_dotenv()  # pick up GOOGLE_MAPS_API_KEY from a local .env, if present
    if address:
        try:
            lat, lon, resolved = geocode_address(address)
        except RuntimeError as exc:
            typer.echo(f"Geocoding failed: {exc}", err=True)
            raise typer.Exit(1)
        Console().print(Text.assemble(
            ("geocoded ", "bold cyan"), (address, "italic"), (" -> ", "dim"),
            (resolved, ""), (f"  ({lat:.6f}, {lon:.6f})", "dim")))
    if lat is None or lon is None:
        raise typer.BadParameter("Provide either --address or both --lat and --lon.")
    manifest = generate(
        lon=lon, lat=lat, radius_m=radius, out_dir=out, name=name,
        mesh_res_m=mesh_res, base_thickness_m=base_thickness,
        with_features=features, feature_source=source,
        buildings_vintage=buildings, with_trees=trees, base_style=base,
        render_preview=preview, run_blender=blender,
    )
    print_outputs(manifest)


def main():
    app()


if __name__ == "__main__":
    main()
