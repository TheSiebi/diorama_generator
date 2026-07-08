"""Central configuration: endpoints, CRS codes, category colours, paths."""

from __future__ import annotations

import os
from pathlib import Path

# --- swisstopo STAC -----------------------------------------------------------
STAC_BASE = "https://data.geo.admin.ch/api/stac/v1"
COLL_BUILDINGS = "ch.swisstopo.swissbuildings3d_3_0"
COLL_BUILDINGS_V2 = "ch.swisstopo.swissbuildings3d_2"
COLL_ALTI = "ch.swisstopo.swissalti3d"
COLL_TLM = "ch.swisstopo.swisstlm3d"

# --- Stadt Zürich open data (WFS, LV95) ----------------------------------------
ZURICH_WFS = "https://www.ogd.stadt-zuerich.ch/wfs/geoportal"

# --- CRS ----------------------------------------------------------------------
WGS84 = 4326          # lon/lat
LV95 = 2056           # swiss projected metres (CH1903+/LV95)

# --- DTM ----------------------------------------------------------------------
# swissALTI3D ships 0.5 m and 2 m GeoTIFFs; 2 m is plenty for a diorama and tiny.
ALTI_SOURCE_RES = "2"     # token in the asset name ("0.5" or "2")

# --- OSM ----------------------------------------------------------------------
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Approx render width (metres) per OSM highway class, used to buffer road lines.
ROAD_WIDTHS_M = {
    "motorway": 12.0, "trunk": 11.0, "primary": 9.0, "secondary": 7.5,
    "tertiary": 6.0, "residential": 5.0, "living_street": 4.0,
    "unclassified": 5.0, "service": 3.5, "pedestrian": 4.0,
    "footway": 1.5, "path": 1.2, "track": 3.0, "cycleway": 2.0,
}
DEFAULT_ROAD_WIDTH_M = 4.0
WATERWAY_WIDTHS_M = {"river": 8.0, "stream": 3.0, "canal": 6.0, "ditch": 1.5}
DEFAULT_WATERWAY_WIDTH_M = 3.0

# --- Diorama category colours (linear RGB 0..1) -------------------------------
CATEGORY_COLORS = {
    "terrain":   (0.45, 0.42, 0.33),
    "buildings": (0.82, 0.80, 0.78),
    "roofs":     (0.58, 0.23, 0.16),
    "roads":     (0.18, 0.18, 0.20),
    "bridges":   (0.18, 0.18, 0.20),   # civil works read as road surface
    "water":     (0.20, 0.45, 0.75),
    "trees":     (0.12, 0.30, 0.12),
    # mesh base (--base table|<path>): body plus the optional "marking"
    # material split (e.g. the table's grey compass ring + heading letters)
    "base":          (0.72, 0.72, 0.72),
    "base_markings": (0.42, 0.42, 0.42),
}

# --- Paths --------------------------------------------------------------------
def cache_dir() -> Path:
    d = Path(os.environ.get("DIORAMA_CACHE", Path.home() / ".cache" / "diorama_generator"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def blender_exe() -> str:
    """Locate the Blender executable (override with DIORAMA_BLENDER)."""
    env = os.environ.get("DIORAMA_BLENDER")
    if env:
        return env
    candidates = [
        r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
        "blender",
    ]
    for c in candidates:
        if c == "blender" or Path(c).exists():
            return c
    return "blender"
