"""Roads and water from OpenStreetMap (Overpass), clipped + draped on terrain.

This is the light default source so the pipeline never needs the ~10 GB
whole-country swissTLM3D GeoPackage. The interface (returns local meshes) is
the seam where a swissTLM3D backend could be swapped in later.
"""

from __future__ import annotations

import httpx
import pyproj
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from .config import (DEFAULT_ROAD_WIDTH_M, DEFAULT_WATERWAY_WIDTH_M,
                     OVERPASS_URL, ROAD_WIDTHS_M, WATERWAY_WIDTHS_M)
from .featuresolid import build_feature_solid, concat_vf
from .geo import AOI
from .terrain import Terrain

_wgs_to_lv95 = pyproj.Transformer.from_crs(4326, 2056, always_xy=True).transform


def _overpass(aoi: AOI, client: httpx.Client) -> dict:
    minlon, minlat, maxlon, maxlat = aoi.bbox_wgs
    bbox = f"{minlat:.6f},{minlon:.6f},{maxlat:.6f},{maxlon:.6f}"
    q = f"""
    [out:json][timeout:90];
    (
      way["highway"]({bbox});
      way["waterway"]({bbox});
      way["natural"="water"]({bbox});
      way["landuse"="reservoir"]({bbox});
      relation["natural"="water"]({bbox});
    );
    out geom;
    """
    r = client.post(OVERPASS_URL, data={"data": q}, timeout=120.0)
    r.raise_for_status()
    return r.json()


def _way_line_lv95(el: dict) -> LineString | None:
    geom = el.get("geometry")
    if not geom or len(geom) < 2:
        return None
    pts = [_wgs_to_lv95(p["lon"], p["lat"]) for p in geom]
    return LineString(pts)


def _way_ring(el: dict):
    geom = el.get("geometry")
    if not geom or len(geom) < 3:
        return None
    return [_wgs_to_lv95(p["lon"], p["lat"]) for p in geom]


def _collect(data: dict):
    """Return (roads, rivers, lakes) as shapely geoms in LV95 (unclipped).

    Rivers are ``waterway`` lines (they flow downhill -> draped like roads); lakes
    are ``natural=water`` / reservoir polygons (a single flat surface).
    """
    road_buffers = []
    river_buffers = []
    lake_polys = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if el["type"] == "way" and "highway" in tags:
            line = _way_line_lv95(el)
            if line:
                w = ROAD_WIDTHS_M.get(tags["highway"], DEFAULT_ROAD_WIDTH_M)
                road_buffers.append(line.buffer(w / 2, cap_style=2, join_style=1))
        elif el["type"] == "way" and "waterway" in tags:
            line = _way_line_lv95(el)
            if line:
                w = WATERWAY_WIDTHS_M.get(tags["waterway"], DEFAULT_WATERWAY_WIDTH_M)
                river_buffers.append(line.buffer(w / 2, cap_style=2, join_style=1))
        elif el["type"] == "way" and (
            tags.get("natural") == "water" or tags.get("landuse") == "reservoir"):
            ring = _way_ring(el)
            if ring and len(ring) >= 3:
                poly = Polygon(ring)
                if poly.is_valid and poly.area > 0:
                    lake_polys.append(poly)
        elif el["type"] == "relation":
            for m in el.get("members", []):
                if m.get("role") == "outer" and m.get("geometry"):
                    ring = [_wgs_to_lv95(p["lon"], p["lat"]) for p in m["geometry"]]
                    if len(ring) >= 3:
                        poly = Polygon(ring)
                        if poly.is_valid and poly.area > 0:
                            lake_polys.append(poly)

    roads = unary_union(road_buffers) if road_buffers else None
    rivers = unary_union(river_buffers) if river_buffers else None
    lakes = unary_union(lake_polys) if lake_polys else None
    return roads, rivers, lakes


def load_osm_features(aoi: AOI, terrain: Terrain, client: httpx.Client,
                      *, road_top_off: float = 0.3, water_top_off: float = 0.0,
                      inlay_depth: float = 3.0, water_carve_lift: float = 60.0,
                      river_carve_lift: float = 2.5, seg_len: float = 2.0):
    """Return dict of road/water inlay solids + the terrain carve tools.

    'roads'/'water' are the display solids; 'carve' are the tools subtracted from
    the terrain. Rivers drape like roads; lakes are flat and their carve tool is
    lifted so no submerged terrain pokes through. ``seg_len`` densifies footprint
    edges to match the terrain mesh.
    """
    data = _overpass(aoi, client)
    roads_geom, rivers_geom, lakes_geom = _collect(data)
    roads = build_feature_solid(roads_geom, aoi, terrain, top_off=road_top_off,
                                depth=inlay_depth, seg_len=seg_len)
    # Rivers drape but are not grid-clipped (thin -> no slab; tiling would leave
    # z-fighting after the terrain carve). See build_feature_solid / features_tlm.
    rivers = build_feature_solid(rivers_geom, aoi, terrain, top_off=water_top_off,
                                 depth=inlay_depth, seg_len=seg_len,
                                 grid_clip=False)
    rivers_carve = build_feature_solid(rivers_geom, aoi, terrain,
                                       top_off=water_top_off, depth=inlay_depth,
                                       carve_lift=river_carve_lift,
                                       seg_len=seg_len, grid_clip=False)
    lakes = build_feature_solid(lakes_geom, aoi, terrain, top_off=water_top_off,
                                depth=inlay_depth, flat=True, seg_len=seg_len)
    lakes_carve = build_feature_solid(lakes_geom, aoi, terrain,
                                      top_off=water_top_off, depth=inlay_depth,
                                      flat=True, carve_lift=water_carve_lift,
                                      seg_len=seg_len)
    water = concat_vf(rivers, lakes)
    return {"roads": roads, "water": water,
            "carve": [roads, rivers_carve, lakes_carve]}
