"""Roads and water from swissTLM3D (swisstopo, authoritative LV95 vectors).

Reads the whole-country swissTLM3D FileGDB (downloaded + cached once, ~2.9 GB)
and extracts the AOI via a bbox-filtered query. Same return shape as the OSM
backend so the two are interchangeable.

OBJEKTART codes are integer domain ordinals (the GDB ships no coded-value
domains). The road width classes follow swisstopo's OBJEKTART scheme; the water
codes were pinned against real data (lakes = Stehende_Gewaesser, wide rivers =
Fliessgewaesser). See the diorama-pipeline memory for the derivation.
"""

from __future__ import annotations

import glob

import fiona
import httpx
from shapely.geometry import shape
from shapely.ops import unary_union

from .download import download_and_unzip
from .featuresolid import build_feature_solid, concat_vf
from .geo import AOI
from .stac import tlm3d_gdb_url
from .terrain import Terrain

L_ROADS = "TLM_STRASSE"
L_RIVERS = "TLM_FLIESSGEWAESSER"
L_LANDCOVER = "TLM_BODENBEDECKUNG"

# Full road width (m) per TLM_STRASSE.OBJEKTART (swisstopo width-class scheme,
# with "8m Strasse" inserted at code 8). Approximate; tweak to taste.
ROAD_WIDTH = {
    0: 5.0, 1: 5.0, 2: 9.0, 3: 6.0, 4: 4.0, 5: 3.5, 6: 3.5, 7: 10.0, 8: 8.0,
    9: 6.0, 10: 4.0, 11: 3.0, 12: 5.0, 13: 4.0, 15: 2.0, 16: 1.2, 17: 1.2,
    18: 2.0, 19: 2.5, 20: 8.0,
}
DEFAULT_ROAD_WIDTH = 3.5
SKIP_ROAD = {14}                 # Faehre (ferry route over water)
BRIDGE_KUNSTBAUTE = 200         # Bruecke; STUFE>0 also marks an elevated deck

RIVER_OBJEKTART = 4              # Fliessgewaesser (actual watercourse)
RIVER_VERLAUF_ABOVE = 100       # oberirdisch (skip underground culverts)
RIVER_LINE_WIDTH = 6.0          # buffer width for line-only (narrow) rivers
RIVER_LANDCOVER = {5}           # 5 Fliessgewaesser area (flowing -> follows slope)
LAKE_LANDCOVER = {10}           # 10 Stehende_Gewaesser (standing -> one flat level)
WATER_LANDCOVER = RIVER_LANDCOVER | LAKE_LANDCOVER


def _gdb_path(extract_dir) -> str:
    hits = glob.glob(str(extract_dir / "*.gdb"))
    if not hits:
        raise RuntimeError(f"No .gdb found in {extract_dir}")
    return hits[0]


def _roads_geom(gdb: str, bbox):
    """Return (roads, bridges): normal road footprints and elevated-deck bridges.

    Bridges (KUNSTBAUTE=Bruecke or STUFE>0) are kept apart so they can be built as
    a level causeway that spans the gap instead of draping into it.
    """
    roads, bridges = [], []
    with fiona.open(gdb, layer=L_ROADS) as src:
        for f in src.filter(bbox=bbox):
            p = f["properties"]
            oa = p.get("OBJEKTART")
            if oa in SKIP_ROAD:
                continue
            w = ROAD_WIDTH.get(oa, DEFAULT_ROAD_WIDTH)
            try:
                line = shape(f["geometry"])
            except Exception:
                continue
            buf = line.buffer(w / 2, cap_style=2, join_style=1)
            is_bridge = ((p.get("STUFE") or 0) > 0
                         or p.get("KUNSTBAUTE") == BRIDGE_KUNSTBAUTE)
            (bridges if is_bridge else roads).append(buf)
    return (unary_union(roads) if roads else None,
            unary_union(bridges) if bridges else None)


def _water_geoms(gdb: str, bbox):
    """Return (rivers_geom, lakes_geom).

    Rivers (line watercourses + Fliessgewaesser areas) flow downhill, so they are
    draped onto the terrain like roads. Lakes (Stehende_Gewaesser) are a single
    flat surface. Keeping them apart stops a mountain river from being flattened
    to one level and carved deep into the slope.
    """
    rivers, lakes = [], []
    with fiona.open(gdb, layer=L_RIVERS) as src:
        for f in src.filter(bbox=bbox):
            p = f["properties"]
            if p.get("OBJEKTART") != RIVER_OBJEKTART:
                continue
            if p.get("VERLAUF") != RIVER_VERLAUF_ABOVE:
                continue
            try:
                line = shape(f["geometry"])
            except Exception:
                continue
            rivers.append(line.buffer(RIVER_LINE_WIDTH / 2, cap_style=2))
    with fiona.open(gdb, layer=L_LANDCOVER) as src:
        for f in src.filter(bbox=bbox):
            oa = f["properties"].get("OBJEKTART")
            if oa not in WATER_LANDCOVER:
                continue
            try:
                poly = shape(f["geometry"])
            except Exception:
                continue
            if poly.is_valid and poly.area > 0:
                (lakes if oa in LAKE_LANDCOVER else rivers).append(poly)
    return (unary_union(rivers) if rivers else None,
            unary_union(lakes) if lakes else None)


def load_tlm_features(aoi: AOI, terrain: Terrain, client: httpx.Client,
                      *, road_top_off: float = 0.3, water_top_off: float = 0.0,
                      inlay_depth: float = 3.0, water_carve_lift: float = 60.0,
                      river_carve_lift: float = 2.5, bridge_level_pct: float = 80.0,
                      seg_len: float = 2.0):
    """Return dict of road/water inlay solids + the terrain carve tools.

    'roads'/'water' are the display solids; 'carve' are the tools subtracted from
    the terrain. Rivers drape onto the terrain like roads (they flow downhill);
    lakes are flat, and their carve tool is lifted so no submerged terrain pokes
    through the flat surface. ``seg_len`` densifies footprint edges to match the
    terrain mesh (avoids terrain biting lips into feature edges).

    Draped water sits at the smooth bilinear terrain height, but the terrain mesh
    is piecewise-linear at ``seg_len`` m, so a river carve tool cut exactly to the
    water surface leaves terrain slivers poking through -> z-fighting. ``river_
    carve_lift`` raises the river carve top a couple metres so the terrain surface
    under the water is fully removed (display water stays flush).
    """
    extract = download_and_unzip(tlm3d_gdb_url(client), client=client)
    gdb = _gdb_path(extract)
    bbox = aoi.bbox_lv95
    roads_geom, bridges_geom = _roads_geom(gdb, bbox)
    rivers_geom, lakes_geom = _water_geoms(gdb, bbox)

    roads = build_feature_solid(roads_geom, aoi, terrain, top_off=road_top_off,
                                depth=inlay_depth, seg_len=seg_len)
    # Bridges: a level deck at the abutment (percentile) height with solid fill
    # down to the valley floor, so the road spans the gap instead of dipping in.
    bridges = build_feature_solid(bridges_geom, aoi, terrain, top_off=road_top_off,
                                  depth=inlay_depth, causeway=True,
                                  level_pct=bridge_level_pct, seg_len=seg_len)
    # Rivers: draped (flat=False) so they follow the valley/slope, but NOT
    # grid-clipped -- a thin river doesn't slab, and tiling it into dozens of
    # prisms makes the terrain carve leave z-fighting remnants.
    rivers = build_feature_solid(rivers_geom, aoi, terrain, top_off=water_top_off,
                                 depth=inlay_depth, seg_len=seg_len,
                                 grid_clip=False)
    rivers_carve = build_feature_solid(rivers_geom, aoi, terrain,
                                       top_off=water_top_off, depth=inlay_depth,
                                       carve_lift=river_carve_lift,
                                       seg_len=seg_len, grid_clip=False)
    # Lakes: one flat surface; a lifted copy carves out any submerged terrain.
    lakes = build_feature_solid(lakes_geom, aoi, terrain, top_off=water_top_off,
                                depth=inlay_depth, flat=True, seg_len=seg_len)
    lakes_carve = build_feature_solid(lakes_geom, aoi, terrain,
                                      top_off=water_top_off, depth=inlay_depth,
                                      flat=True, carve_lift=water_carve_lift,
                                      seg_len=seg_len)
    water = concat_vf(rivers, lakes)
    roads_all = concat_vf(roads, bridges)   # bridges render as roads
    # Rivers and lakes both carve with a lifted tool so no terrain sliver survives
    # at the water surface; bridges carve out the gap they fill.
    return {"roads": roads_all, "water": water,
            "carve": [roads, bridges, rivers_carve, lakes_carve]}
