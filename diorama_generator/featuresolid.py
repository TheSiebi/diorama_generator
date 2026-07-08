"""Turn LV95 vector geometry into closed inlay *solids* for 3D printing.

Roads and water used to be zero-thickness draped sheets: a slicer reports them
as "objects with zero volume removed" and the coincident surface z-fights with
the terrain. Instead we extrude each footprint into a prism (top draped onto the
terrain, bottom a few metres below) so every part is a watertight, positive-
volume object. The same prisms are then subtracted from the terrain puck (see
``carve_terrain``) so terrain and inlay share a wall instead of overlapping ->
no z-fighting, and the colours stay cleanly separated for printing.
"""

from __future__ import annotations

import numpy as np
import trimesh
from shapely.geometry import box
from shapely.ops import transform as shp_transform

from .featuremesh import polys
from .geo import AOI
from .terrain import Terrain

_EXTRUDE_H = 1000.0   # tall scratch prism; remapped to draped z afterwards
_CLEAN_EPS = 0.05     # close-open radius (m) that dissolves self-touching pinches
_GRID_MULT = 4.0      # grid cell = seg_len * this (bounds every top-cap triangle)


def _grid_clip(poly, cell: float):
    """Yield the pieces of ``poly`` cut against an axis-aligned ``cell``-m grid.

    earcut (trimesh's only available triangulation engine here) inserts no
    interior points, so a long/curved footprint gets fanned into 300 m slivers
    whose corners drape to wildly different terrain heights -> near-vertical
    slabs. Cutting the footprint into <=cell-sized tiles first bounds every
    resulting triangle, so each prism top drapes cleanly onto the terrain. The
    coincident interior tile walls sit inside the carved inlay solid (they only
    ever meet the surface along the shared draped edge), so they aren't visible.
    """
    if cell <= 0:
        yield from polys(poly)
        return
    minx, miny, maxx, maxy = poly.bounds
    nx = max(1, int(np.ceil((maxx - minx) / cell)))
    ny = max(1, int(np.ceil((maxy - miny) / cell)))
    if nx * ny <= 1:
        yield from polys(poly)
        return
    for i in range(nx):
        x0 = minx + i * cell
        for j in range(ny):
            y0 = miny + j * cell
            piece = poly.intersection(box(x0, y0, x0 + cell, y0 + cell))
            if not piece.is_empty:
                yield from polys(piece)


def _prism_from_polygon(part_local, aoi: AOI, terrain: Terrain,
                        top_off: float, depth: float, flat: bool,
                        carve_lift: float, causeway: bool = False,
                        level_pct: float = 10.0):
    """Extrude one local-coordinate polygon into a closed, draped prism.

    ``carve_lift`` raises only the top cap, turning the prism into a taller
    "carve tool" that clears all terrain inside the footprint (used so flat water
    has no terrain spikes poking through its surface). It is 0 for display meshes.

    ``causeway`` builds a bridge/embankment: a flat deck at the ``level_pct``
    percentile terrain height (~the abutment level, so the deck spans the gap
    level) with the bottom dropped to the lowest terrain under the footprint, i.e.
    solid fill from the valley floor up to the deck.
    """
    try:
        mesh = trimesh.creation.extrude_polygon(part_local, height=_EXTRUDE_H)
    except Exception:
        return None
    v = np.asarray(mesh.vertices, dtype="float64")
    f = np.asarray(mesh.faces, dtype="int64")
    if len(v) == 0 or len(f) == 0:
        return None

    # extrude_polygon puts the bottom cap at z=0 and the top cap at z=_EXTRUDE_H;
    # remap each vertex's z so the top follows the terrain and the bottom sits
    # `depth` below it. Topology is untouched, so the prism stays watertight.
    is_top = v[:, 2] > _EXTRUDE_H * 0.5
    real_z = terrain.sample(v[:, 0] + aoi.cx, v[:, 1] + aoi.cy)
    if causeway:
        ref = real_z[is_top] if is_top.any() else real_z
        deck = terrain.to_diorama_z(float(np.percentile(ref, level_pct))) + top_off
        floor = terrain.to_diorama_z(float(real_z.min())) - depth
        v[:, 2] = np.where(is_top, deck + carve_lift, floor)
    elif flat:
        ref = real_z[is_top] if is_top.any() else real_z
        level = float(np.percentile(ref, level_pct))
        z_top = terrain.to_diorama_z(level) + top_off
        v[:, 2] = np.where(is_top, z_top + carve_lift, z_top - depth)
    else:
        z_top = terrain.to_diorama_z(real_z) + top_off
        v[:, 2] = np.where(is_top, z_top + carve_lift, z_top - depth)

    # Drop degenerate prisms (thin slivers from clipping collapse to a 2-face,
    # zero-volume body); a single one makes the whole tool fail the manifold
    # "is volume" check and silently un-carves the terrain.
    prism = trimesh.Trimesh(vertices=v, faces=f, process=False)
    if not prism.is_volume or abs(prism.volume) < 1.0:
        return None
    return v, f


def build_feature_solid(geom, aoi: AOI, terrain: Terrain, *,
                        top_off: float, depth: float, flat: bool = False,
                        carve_lift: float = 0.0, seg_len: float = 2.0,
                        grid_clip: bool = True, causeway: bool = False,
                        level_pct: float = 10.0):
    """Clip an LV95 geometry to the AOI circle and extrude -> (vertices, faces).

    Each disjoint footprint part becomes its own watertight prism; the union of
    parts (disjoint by construction, since callers ``unary_union`` first) is a
    valid manifold collection for the terrain boolean. ``carve_lift`` > 0 raises
    the top cap to produce a tall carve tool (see ``_prism_from_polygon``).

    ``seg_len`` densifies the footprint boundary (one vertex every ~seg_len m) so
    the draped prism top tracks the terrain instead of chording over it between
    sparse TLM vertices, which would otherwise leave the terrain biting lips into
    the road edges. Keep it <= the terrain mesh resolution.

    ``grid_clip`` tiles each footprint so no earcut triangle spans far enough to
    drape into a near-vertical slab (see ``_grid_clip``). It's needed for the wide
    connected ROAD network on steep terrain, but it fragments a thin footprint
    into dozens of un-welded prisms whose coincident walls make the terrain
    boolean unreliable (leftover terrain -> z-fighting). Rivers are thin and don't
    slab, so they pass ``grid_clip=False`` to stay one clean, carvable prism.
    """
    circle = aoi.circle_lv95
    all_v, all_f, offset = [], [], 0
    for poly in polys(geom):
        clipped = poly.intersection(circle)
        for part in polys(clipped):
            if part.area < 0.5:
                continue
            local = shp_transform(lambda x, y, z=None: (x - aoi.cx, y - aoi.cy), part)
            # A unioned road network self-touches at junctions; those pinch points
            # make extrude_polygon non-watertight. A tiny close-open separates the
            # boundary so every footprint extrudes to a clean volume.
            local = local.buffer(_CLEAN_EPS).buffer(-_CLEAN_EPS)
            if not local.is_valid:
                local = local.buffer(0)
            # Only draped (non-flat) footprints need tiling: a flat/causeway piece
            # takes a single level regardless of triangulation, and tiling it
            # would give each tile its own percentile level -> stepped surface.
            cell = (seg_len * _GRID_MULT
                    if (seg_len > 0 and not flat and not causeway and grid_clip)
                    else 0.0)
            for tile in _grid_clip(local, cell):
                if tile.area < 0.5:
                    continue
                # densify the tile boundary so the draped top tracks the terrain
                # instead of chording between sparse footprint vertices.
                piece = tile.segmentize(seg_len) if seg_len > 0 else tile
                res = _prism_from_polygon(piece, aoi, terrain, top_off, depth,
                                          flat, carve_lift, causeway=causeway,
                                          level_pct=level_pct)
                if res is None:
                    continue
                v, f = res
                all_v.append(v)
                all_f.append(f + offset)
                offset += len(v)
    if not all_v:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype="int64")
    return np.vstack(all_v), np.vstack(all_f)


def concat_vf(*vfs):
    """Merge several (vertices, faces) meshes into one, re-basing face indices."""
    verts, faces, offset = [], [], 0
    for v, f in vfs:
        if v is None or len(f) == 0:
            continue
        verts.append(v)
        faces.append(np.asarray(f) + offset)
        offset += len(v)
    if not verts:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype="int64")
    return np.vstack(verts), np.vstack(faces)


def carve_terrain(terrain_vf, *solid_vfs):
    """Subtract inlay solids from the terrain puck -> (vertices, faces).

    Uses the manifold engine (manifold3d); skips any tool that fails so a single
    bad boolean never aborts the whole diorama.
    """
    tv, tf = terrain_vf
    terrain = trimesh.Trimesh(vertices=np.asarray(tv, dtype="float64"),
                              faces=np.asarray(tf, dtype="int64"), process=False)
    for sv, sf in solid_vfs:
        if sv is None or len(sf) == 0:
            continue
        tool = trimesh.Trimesh(vertices=np.asarray(sv, dtype="float64"),
                               faces=np.asarray(sf, dtype="int64"), process=False)
        try:
            terrain = trimesh.boolean.difference([terrain, tool], engine="manifold")
        except Exception:
            # Fall back to subtracting only the watertight-volume components so a
            # stray degenerate body can't abort the whole carve.
            comps = [c for c in tool.split(only_watertight=False) if c.is_volume]
            try:
                terrain = trimesh.boolean.difference([terrain, *comps],
                                                     engine="manifold")
            except Exception as exc:  # keep un-carved terrain rather than failing
                print(f"[carve] terrain difference skipped: {exc}")
    return (np.asarray(terrain.vertices, dtype="float64"),
            np.asarray(terrain.faces, dtype="int64"))
