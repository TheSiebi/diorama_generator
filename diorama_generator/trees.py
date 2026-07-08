"""Optional tree layer from swissTLM3D: forest fill + single trees -> ellipsoids.

Forest is `TLM_BODENBEDECKUNG` polygons with OBJEKTART=12 (Wald); we scatter
jittered points inside them. Solitary trees come from `TLM_EINZELBAUM` points.
TLM carries no tree height, so every tree is the same simple stretched ellipsoid
(a scaled icosphere) sitting on the terrain. See the swisstlm3d-codes memory.
"""

from __future__ import annotations

import fiona
import httpx
import numpy as np
import trimesh
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union
from shapely.prepared import prep

from .download import download_and_unzip
from .featuremesh import polys
from .features_tlm import L_LANDCOVER, _gdb_path, _roads_geom, _water_geoms
from .geo import AOI
from .stac import tlm3d_gdb_url
from .terrain import Terrain

L_TREES = "TLM_EINZELBAUM"
FOREST_LANDCOVER = {12}          # 12 = Wald (deep forest)
SINGLE_TREE_OBJEKTART = 1        # TLM_EINZELBAUM single tree


def _forest_geom(gdb: str, bbox):
    parts = []
    with fiona.open(gdb, layer=L_LANDCOVER) as src:
        for f in src.filter(bbox=bbox):
            if f["properties"].get("OBJEKTART") not in FOREST_LANDCOVER:
                continue
            try:
                g = shape(f["geometry"])
            except Exception:
                continue
            if g.is_valid and g.area > 0:
                parts.append(g)
    return unary_union(parts) if parts else None


def _exclusion_geom(gdb: str, bbox, clearance_m: float):
    """Road + water footprints (buffered) where no tree may stand.

    TLM forest polygons run right across forest roads and streams, so a plain
    scatter puts trees on the asphalt/water. Reuses the exact footprints the
    feature backend renders, so the clearance matches what ends up on the model.
    """
    roads, bridges = _roads_geom(gdb, bbox)
    rivers, lakes = _water_geoms(gdb, bbox)
    parts = [g for g in (roads, bridges, rivers, lakes) if g is not None]
    if not parts:
        return None
    return unary_union(parts).buffer(clearance_m)


def _single_tree_points(gdb: str, bbox):
    pts = []
    with fiona.open(gdb, layer=L_TREES) as src:
        for f in src.filter(bbox=bbox):
            if f["properties"].get("OBJEKTART") != SINGLE_TREE_OBJEKTART:
                continue
            try:
                p = shape(f["geometry"])
                pts.append((p.x, p.y))
            except Exception:
                continue
    return pts


def _scatter(poly_local, spacing: float, rng) -> list[tuple[float, float]]:
    """Jittered-grid points (local coords) inside a forest polygon."""
    minx, miny, maxx, maxy = poly_local.bounds
    pg = prep(poly_local)
    out = []
    j = spacing * 0.4
    for x in np.arange(minx, maxx, spacing):
        for y in np.arange(miny, maxy, spacing):
            px = x + rng.uniform(-j, j)
            py = y + rng.uniform(-j, j)
            if pg.contains(Point(px, py)):
                out.append((px, py))
    return out


def load_tlm_trees(aoi: AOI, terrain: Terrain, client: httpx.Client, *,
                   tree_height_m: float = 9.0, crown_radius_m: float = 3.5,
                   spacing_m: float = 9.0, embed_m: float = 1.0,
                   max_trees: int = 15000, seed: int = 0,
                   clearance_m: float = 1.5):
    """Return (vertices, faces) for all tree ellipsoids in the AOI.

    Forest polygons are scattered with a jittered grid; solitary TLM trees are
    added as-is. Each tree is one scaled icosphere resting on (and embedded a
    little into) the terrain so it prints connected. Road/water footprints
    (buffered by ``clearance_m``) are kept tree-free — crowns may still
    overhang a forest road, but no trunk stands on it.
    """
    gdb = _gdb_path(download_and_unzip(tlm3d_gdb_url(client), client=client))
    bbox = aoi.bbox_lv95
    forest = _forest_geom(gdb, bbox)
    singles = _single_tree_points(gdb, bbox)
    exclusion = _exclusion_geom(gdb, bbox, clearance_m)
    excl = prep(exclusion) if exclusion is not None else None

    circle = aoi.circle_lv95
    rng = np.random.default_rng(seed)
    positions: list[tuple[float, float]] = []   # LV95

    if forest is not None:
        keep = forest.intersection(circle)
        if exclusion is not None:
            keep = keep.difference(exclusion)
        for poly in polys(keep):
            local = shp_transform(lambda x, y, z=None: (x - aoi.cx, y - aoi.cy), poly)
            positions += [(px + aoi.cx, py + aoi.cy)
                          for px, py in _scatter(local, spacing_m, rng)]

    r2 = aoi.radius_m ** 2
    positions += [(x, y) for x, y in singles
                  if (x - aoi.cx) ** 2 + (y - aoi.cy) ** 2 <= r2
                  and (excl is None or not excl.contains(Point(x, y)))]

    if not positions:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype="int64")
    if len(positions) > max_trees:                # keep face count bounded
        idx = rng.choice(len(positions), max_trees, replace=False)
        positions = [positions[i] for i in idx]

    base = trimesh.creation.icosphere(subdivisions=1, radius=1.0)
    bv = np.asarray(base.vertices, dtype="float64")
    bf = np.asarray(base.faces, dtype="int64")
    scale = np.array([crown_radius_m, crown_radius_m, tree_height_m / 2.0])

    xs = np.array([p[0] for p in positions])
    ys = np.array([p[1] for p in positions])
    zc = terrain.to_diorama_z(terrain.sample(xs, ys)) + tree_height_m / 2.0 - embed_m

    all_v, all_f, off = [], [], 0
    for i in range(len(positions)):
        v = bv * scale + np.array([xs[i] - aoi.cx, ys[i] - aoi.cy, zc[i]])
        all_v.append(v)
        all_f.append(bf + off)
        off += len(v)
    return np.vstack(all_v), np.vstack(all_f).astype("int64")
