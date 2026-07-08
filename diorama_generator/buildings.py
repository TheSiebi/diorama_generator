"""swissBUILDINGS3D 3.0 FileGDB -> building meshes in diorama coordinates."""

from __future__ import annotations

import glob

import fiona
import httpx
import numpy as np

from .download import download_and_unzip
from .geo import AOI
from .stac import (buildings_country_gdb_url, buildings_gdb_assets,
                   buildings_v2_country_gdb_url)
from .terrain import Terrain

# The 3.0 FileGDB ships both variants: Building_solid is the closed building
# body (walls + roof shape, no overhang); Roof_solid is the roof as its own
# solid *including overhangs*. Rendering both gives overhangs plus a separate
# roof colour. The Roof/Wall/Floor surface layers use an OGR geometry type
# fiona cannot read, so the solids are the usable pair.
LAYER_BUILDINGS = "Building_solid"
LAYER_ROOFS = "Roof_solid"
# The 2.0 FileGDB has a single solid layer (LOD2 body incl. roof shape, no
# separate roof solids -> no distinct roof colour with this source).
LAYER_V2 = "sB20"


def _gdb_path(extract_dir) -> str:
    hits = glob.glob(str(extract_dir / "*.gdb"))
    if not hits:
        raise RuntimeError(f"No .gdb found in {extract_dir}")
    return hits[0]


def _feature_triangles(geom: dict):
    """Yield triangles (each a list of 3 (x,y,z)) from a MultiPatch/MultiPolygon."""
    gtype = geom["type"]
    polys = geom["coordinates"] if gtype == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if not poly:
            continue
        ring = poly[0]
        pts = ring[:-1] if len(ring) > 3 and ring[0] == ring[-1] else ring
        if len(pts) < 3:
            continue
        # fan triangulation (faces from the GDB are already triangles/convex)
        for k in range(1, len(pts) - 1):
            yield [pts[0], pts[k], pts[k + 1]]


def _chain_edges(edges: np.ndarray):
    """Chain boundary edges (index pairs) into polylines.

    Yields (vertex index list, closed) — `closed` when the walk returns to its
    start. T-junction gaps in the source solids leave chains open; callers
    close them with a straight chord.
    """
    from collections import defaultdict

    adj = defaultdict(list)
    for k, (a, b) in enumerate(edges):
        adj[int(a)].append((int(b), k))
        adj[int(b)].append((int(a), k))
    used = np.zeros(len(edges), dtype=bool)
    # start open chains at odd-degree vertices first so they aren't split
    starts = sorted(adj, key=lambda vi: len(adj[vi]) % 2 == 0)
    for start in starts:
        for nxt, k0 in adj[start]:
            if used[k0]:
                continue
            used[k0] = True
            chain = [start, nxt]
            while True:
                step = next(((w, k) for w, k in adj[chain[-1]] if not used[k]),
                            None)
                if step is None:
                    break
                used[step[1]] = True
                chain.append(step[0])
                if step[0] == chain[0]:
                    break
            closed = chain[0] == chain[-1]
            yield (chain[:-1] if closed else chain), closed


def _cap_region_tris(region) -> list:
    """Triangulate a shapely (Multi)Polygon -> list of (3, 2) point triples."""
    import mapbox_earcut

    tris = []
    stack = [region]
    geoms = []
    while stack:                      # flatten (nested) collections and drop
        g = stack.pop()               # degenerate LineString/Point members
        if hasattr(g, "geoms"):
            stack.extend(g.geoms)
        elif g.geom_type == "Polygon" and not g.is_empty:
            geoms.append(g)
    for poly in geoms:
        rings = [np.asarray(poly.exterior.coords)[:-1]] + \
                [np.asarray(i.coords)[:-1] for i in poly.interiors]
        rings = [r for r in rings if len(r) >= 3]
        if not rings:
            continue
        pts = np.vstack(rings)
        ends = np.cumsum([len(r) for r in rings]).astype("uint32")
        idx = mapbox_earcut.triangulate_float64(pts, ends).reshape(-1, 3)
        tris.extend(pts[t] for t in idx)
    return tris


def _plane_caps(sliced, origins: np.ndarray, normals: np.ndarray):
    """Cap faces for the cut outline a slice left in each clip plane.

    The boundary (once-referenced) edges of the sliced mesh that lie in a clip
    plane are the cut cross-section. Closed outline loops combine even-odd (so
    courtyards stay holes); an OPEN outline — the usual case, since the
    building solids mostly lack floors — is closed with a straight chord,
    which runs along the missing floor line. Purely additive: the sliced
    geometry itself is never altered.
    """
    import trimesh
    from shapely.geometry import Polygon

    v = np.asarray(sliced.vertices, dtype="float64")
    once = trimesh.grouping.group_rows(sliced.edges_sorted, require_count=1)
    if len(once) == 0:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype="int64")
    boundary = sliced.edges_sorted[once]
    cap_v, cap_f, off = [], [], 0
    ez = np.array([0.0, 0.0, 1.0])
    for o, n in zip(origins, normals):
        d = (v - o) @ n
        near = np.abs(d) < 1e-6
        sel = near[boundary].all(axis=1)
        if not sel.any():
            continue
        t = np.cross(ez, n)                      # horizontal in-plane axis
        uz = np.column_stack([(v - o) @ t, v[:, 2]])
        closed_polys, open_polys = [], []
        for chain, closed in _chain_edges(boundary[sel]):
            if len(chain) < 3:
                continue
            poly = Polygon(uz[chain]).buffer(0)
            if poly.is_empty:
                continue
            (closed_polys if closed else open_polys).append(poly)
        region = None
        for p in closed_polys:                   # even-odd: nested loop = hole
            region = p if region is None else region.symmetric_difference(p)
        for p in open_polys:                     # chord-closed open outlines
            region = p if region is None else region.union(p)
        if region is None or region.is_empty or region.area < 1e-6:
            continue
        outward = -n                             # cap faces away from the AOI
        for tri2 in _cap_region_tris(region):
            p3 = o + tri2[:, :1] * t + tri2[:, 1:] * ez
            nrm = np.cross(p3[1] - p3[0], p3[2] - p3[0])
            cap_v.append(p3 if nrm @ outward >= 0 else p3[::-1])
            cap_f.append(np.arange(3, dtype="int64") + off)
            off += 3
    if not cap_v:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype="int64")
    return np.vstack(cap_v), np.vstack(cap_f)


_PRISM_FACES = np.array([[0, 1, 2], [5, 4, 3],
                         [0, 4, 1], [0, 3, 4],
                         [1, 5, 2], [1, 4, 5],
                         [2, 3, 0], [2, 5, 3]], dtype="int64")


def _extruded_solid(verts: np.ndarray, faces: np.ndarray):
    """Watertight solid from an open surface soup, by downward extrusion.

    Every face with a horizontal footprint is extruded straight down to just
    below the feature's lowest vertex and the prisms are unioned: the result
    is the volume under the outer surface. Same silhouette as the shell (an
    open passage under a building fills up), but a real solid that a slicer
    fills with material. Only rim-crossing features go through this.
    """
    import trimesh

    tris = verts[faces]
    nz = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])[:, 2]
    z0 = float(verts[:, 2].min()) - 0.05
    prisms = []
    for tri, z in zip(tris, nz):
        if abs(z) < 1e-6:                   # vertical face: no footprint
            continue
        top = tri if z > 0 else tri[::-1]   # CCW seen from above
        bot = top.copy()
        bot[:, 2] = z0
        prisms.append(trimesh.Trimesh(np.vstack([top, bot]), _PRISM_FACES,
                                      process=False))
    if not prisms:
        raise ValueError("no faces with a horizontal footprint")
    return trimesh.boolean.union(prisms, engine="manifold")


def _rim_prism(verts: np.ndarray, radius: float, sections: int):
    """Solid prism bounded by the same tangent planes that clip the shells."""
    import trimesh
    from shapely.geometry import Polygon

    # vertices at the circumradius so every edge is tangent to `radius` at
    # the section angles — identical cut planes to the slice path below
    ang = (np.arange(sections) + 0.5) * (2.0 * np.pi / sections)
    rv = radius / np.cos(np.pi / sections)
    poly = Polygon(np.column_stack([rv * np.cos(ang), rv * np.sin(ang)]))
    zmin = float(verts[:, 2].min()) - 1.0
    zmax = float(verts[:, 2].max()) + 1.0
    prism = trimesh.creation.extrude_polygon(poly, height=zmax - zmin)
    prism.apply_translation([0.0, 0.0, zmin])
    return prism


def _solid_clip(verts: np.ndarray, faces: np.ndarray, radius: float,
                sections: int, *, extrude: bool = True):
    """Cut ONE rim-crossing feature to the cylinder as a watertight SOLID.

    A capped shell still slices hollow — slicers only deposit material inside
    an enclosed volume, and the source bodies are zero-thickness surface
    soups. So the feature is turned into a genuine solid first (used as-is
    when the body already closes, else rebuilt by _extruded_solid) and the
    rim cut is a real boolean intersection: the cut face is backed by solid
    material all the way through.

    `extrude=False` (bridges) skips the _extruded_solid rebuild — filling
    everything under a bridge deck down to its lowest point would plug the
    span — and raises instead, so the caller falls back to the shell clip.
    """
    import trimesh

    mesh = trimesh.Trimesh(verts, faces, process=True)
    trimesh.repair.fix_normals(mesh)
    if not mesh.is_volume:
        if not extrude:
            raise ValueError("open shell and extrusion disabled")
        mesh = _extruded_solid(verts, faces)
    cut = trimesh.boolean.intersection(
        [mesh, _rim_prism(verts, radius, sections)], engine="manifold")
    if len(getattr(cut, "faces", ())) == 0:      # feature entirely outside
        return np.zeros((0, 3)), np.zeros((0, 3), dtype="int64")
    return (np.asarray(cut.vertices, dtype="float64"),
            np.asarray(cut.faces, dtype="int64"))


def _clip_feature(verts: np.ndarray, faces: np.ndarray, origins: np.ndarray,
                  normals: np.ndarray):
    """Fallback clip: slice ONE feature and cap the cut faces (shell only).

    Used when _solid_clip fails (degenerate geometry the boolean engine
    rejects). trimesh's own capping only closes watertight solids, and the
    swissBUILDINGS3D / city-model bodies mostly aren't (missing floors,
    T-junctions) — so the slice runs uncapped and the cut cross-sections are
    rebuilt from the cut outline (see _plane_caps), which works for every
    building. Returns (verts, faces, capped_ok).
    """
    import trimesh
    from trimesh.intersections import slice_mesh_plane

    mesh = trimesh.Trimesh(verts, faces, process=True)
    sliced = slice_mesh_plane(mesh, plane_normal=normals,
                              plane_origin=origins, cap=False)
    sliced.merge_vertices()      # weld cut verts so the outline chains connect
    sv = np.asarray(sliced.vertices, dtype="float64")
    sf = np.asarray(sliced.faces, dtype="int64")
    if len(sf) == 0:
        return sv, sf, True
    try:
        cv, cf = _plane_caps(sliced, origins, normals)
    except Exception:
        return sv, sf, False
    if len(cf) == 0:
        return sv, sf, True
    return np.vstack([sv, cv]), np.vstack([sf, cf + len(sv)]), True


class _MeshAcc:
    """Accumulates per-feature triangle soups; clips each feature separately.

    Per-feature clipping (instead of slicing one merged soup) is what allows
    the rim cut of every building to be capped: capping works per closed
    solid, and one open building in a merged mesh used to leave its cut — and
    only its cut — as bare walls.
    """

    def __init__(self):
        self.items: list[tuple[np.ndarray, np.ndarray]] = []
        self.count = 0

    def add(self, tris, aoi: AOI, terrain: Terrain):
        self.count += 1
        pts = np.asarray([p for tri in tris for p in tri], dtype="float64")
        v = np.column_stack([pts[:, 0] - aoi.cx, pts[:, 1] - aoi.cy,
                             terrain.to_diorama_z(pts[:, 2])])
        f = np.arange(len(v), dtype="int64").reshape(-1, 3)
        self.items.append((v, f))

    def clipped(self, radius: float, sections: int, *, extrude: bool = True):
        empty = np.zeros((0, 3)), np.zeros((0, 3), dtype="int64")
        if not self.items:
            return empty
        ang = np.linspace(0, 2 * np.pi, sections, endpoint=False)
        origins = np.column_stack([radius * np.cos(ang), radius * np.sin(ang),
                                   np.zeros(sections)])
        normals = np.column_stack([-np.cos(ang), -np.sin(ang),
                                   np.zeros(sections)])
        # features inside the inscribed radius of the sections-gon need no clip
        r_in = radius * np.cos(np.pi / sections)
        out_v, out_f, off, n_shell, n_open = [], [], 0, 0, 0
        for v, f in self.items:
            if np.sqrt((v[:, :2] ** 2).sum(1)).max() > r_in:
                try:
                    v, f = _solid_clip(v, f, radius, sections, extrude=extrude)
                except Exception:
                    n_shell += extrude   # shell clip is expected sans extrude
                    v, f, ok = _clip_feature(v, f, origins, normals)
                    n_open += not ok
                if len(f) == 0:
                    continue
            out_v.append(v)
            out_f.append(f + off)
            off += len(v)
        if n_shell:
            print(f"      [buildings] {n_shell} rim feature(s) could not be "
                  "solidified (cut as capped shells)")
        if n_open:
            print(f"      [buildings] {n_open} rim building(s) could not be "
                  "capped (left as open shells)")
        if not out_v:
            return empty
        return np.vstack(out_v), np.vstack(out_f)


def _read_layer(gdb: str, layer: str, acc: _MeshAcc, aoi: AOI,
                terrain: Terrain) -> None:
    bbox = aoi.bbox_lv95
    keep_r2 = aoi.radius_m ** 2
    with fiona.open(gdb, layer=layer) as src:
        for feat in src.filter(bbox=bbox):
            geom = dict(feat["geometry"])
            tris = list(_feature_triangles(geom))
            if not tris:
                continue
            pts = np.array([p for tri in tris for p in tri], dtype="float64")
            # keep if any part of the footprint reaches into the circle
            d2 = (pts[:, 0] - aoi.cx) ** 2 + (pts[:, 1] - aoi.cy) ** 2
            if d2.min() > keep_r2:
                continue
            acc.add(tris, aoi, terrain)


_SOURCE_LABEL = {
    "latest": "swissBUILDINGS3D 3.0 whole-country aggregate",
    "v2": "swissBUILDINGS3D 2.0 whole-country aggregate",
    "tiles": "swissBUILDINGS3D 3.0 per-tile",
}


def load_buildings(aoi: AOI, terrain: Terrain, client: httpx.Client,
                   *, clip_sections: int = 96, vintage: str = "auto"):
    """Return {'walls': (V, F), 'roofs': (V, F), 'count': n, 'source': str}.

    Buildings whose footprint intersects the AOI circle are included and then
    clipped flush to the cylinder, so nothing overhangs the terrain rim.
    'walls' is the Building_solid bodies; 'roofs' the Roof_solid overhang
    solids (empty arrays if the tile predates that layer).

    `vintage`: 'auto' uses the weekly Stadt Zürich city model when the AOI
    lies fully inside the Zurich city boundary, and otherwise (or if the city
    WFS is down) falls back to 'latest'. 'zurich' forces the city model and
    errors outside the city. 'latest' reads the yearly whole-country 3.0
    aggregate GDB (~14 GB one-time download; the per-tile items are frozen,
    e.g. Zurich on 2019 imagery — but note the aggregate carries the same
    regional vintage). 'v2' reads the whole-country swissBUILDINGS3D 2.0 GDB
    (~3.6 GB; own revision cycle, no separate roof colour). 'tiles' keeps the
    small per-tile 3.0 downloads.
    """
    if vintage in ("auto", "zurich"):
        from .buildings_zurich import load_city_buildings, zurich_covers
        if zurich_covers(aoi, client, strict=vintage == "zurich"):
            return load_city_buildings(aoi, terrain, client,
                                       clip_sections=clip_sections)
        if vintage == "zurich":
            raise RuntimeError("AOI is not fully inside the Zurich city "
                               "boundary; use --buildings latest/v2/tiles.")
        vintage = "latest"

    if vintage == "latest":
        urls = [buildings_country_gdb_url(aoi, client)]
    elif vintage == "v2":
        urls = [buildings_v2_country_gdb_url(aoi, client)]
    elif vintage == "tiles":
        urls = [a.href for a in buildings_gdb_assets(aoi, client)]
    else:
        raise ValueError(f"Unknown buildings vintage: {vintage!r}")
    walls, roofs = _MeshAcc(), _MeshAcc()

    for url in urls:
        extract = download_and_unzip(url, client=client)
        gdb = _gdb_path(extract)
        layers = set(fiona.listlayers(gdb))
        bl = LAYER_BUILDINGS if LAYER_BUILDINGS in layers else LAYER_V2
        _read_layer(gdb, bl, walls, aoi, terrain)
        if LAYER_ROOFS in layers:
            _read_layer(gdb, LAYER_ROOFS, roofs, aoi, terrain)

    wv, wf = walls.clipped(aoi.radius_m, clip_sections)
    rv, rf = roofs.clipped(aoi.radius_m, clip_sections)
    return {"walls": (wv, wf), "roofs": (rv, rf), "count": walls.count,
            "source": _SOURCE_LABEL[vintage]}
