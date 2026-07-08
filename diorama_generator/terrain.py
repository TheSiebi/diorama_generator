"""swissALTI3D -> merged DTM, a height sampler, and a solid terrain block."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import numpy as np
import rasterio
from rasterio.merge import merge as rio_merge

from .config import ALTI_SOURCE_RES
from .download import download
from .geo import AOI
from .stac import alti_tif_assets


@dataclass
class Terrain:
    arr: np.ndarray            # elevation grid (north-up), nan where no data
    transform: rasterio.Affine  # pixel<->LV95 transform
    zmin: float                # min elevation over AOI (datum reference)
    base_thickness_m: float    # solid slab height below lowest terrain point

    # --- height sampling ------------------------------------------------------
    def sample(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Bilinear elevation (real metres) at LV95 coords x, y."""
        inv = ~self.transform
        col, row = inv * (x, y)               # fractional pixel coords (corner)
        col, row = col - 0.5, row - 0.5       # shift to pixel-centre convention
        col = np.clip(col, 0, self.arr.shape[1] - 1.001)
        row = np.clip(row, 0, self.arr.shape[0] - 1.001)
        c0, r0 = np.floor(col).astype(int), np.floor(row).astype(int)
        fc, fr = col - c0, row - r0
        a = self.arr
        v = (a[r0, c0] * (1 - fc) * (1 - fr) + a[r0, c0 + 1] * fc * (1 - fr)
             + a[r0 + 1, c0] * (1 - fc) * fr + a[r0 + 1, c0 + 1] * fc * fr)
        return v

    def to_diorama_z(self, real_z: np.ndarray | float):
        """Map real elevation -> diorama Z (lowest terrain sits at base_thickness)."""
        return np.asarray(real_z) - self.zmin + self.base_thickness_m


def load_terrain(aoi: AOI, client: httpx.Client, *, base_thickness_m: float = 12.0,
                 res_token: str = ALTI_SOURCE_RES) -> Terrain:
    assets = alti_tif_assets(aoi, client, res_token)
    if not assets:
        raise RuntimeError("No swissALTI3D tiles found for this AOI.")
    paths = [download(a.href, client=client) for a in assets]
    srcs = [rasterio.open(p) for p in paths]
    try:
        bb = aoi.bbox_lv95
        pad = 5.0
        arr, transform = rio_merge(
            srcs, bounds=(bb[0] - pad, bb[1] - pad, bb[2] + pad, bb[3] + pad),
            nodata=srcs[0].nodata,
        )
        nodata = srcs[0].nodata
    finally:
        for s in srcs:
            s.close()
    arr = arr[0].astype("float64")
    if nodata is not None:
        arr[arr == nodata] = np.nan

    # zmin within the circular AOI only (ignore corners outside the disc).
    cols, rows = np.meshgrid(np.arange(arr.shape[1]), np.arange(arr.shape[0]))
    xs, ys = transform * (cols + 0.5, rows + 0.5)
    inside = (xs - aoi.cx) ** 2 + (ys - aoi.cy) ** 2 <= aoi.radius_m ** 2
    vals = arr[inside & np.isfinite(arr)]
    zmin = float(np.nanmin(vals)) if vals.size else float(np.nanmin(arr))

    # Fill any residual gaps so the mesh stays watertight.
    if np.isnan(arr).any():
        arr = np.where(np.isnan(arr), zmin, arr)
    return Terrain(arr=arr, transform=transform, zmin=zmin,
                   base_thickness_m=base_thickness_m)


def terrain_disc(aoi: AOI, terrain: Terrain, *, mesh_res_m: float = 2.0):
    """Build a closed, manifold circular terrain puck (top + skirt + bottom).

    Returns (vertices Nx3, faces Mx3) in diorama coordinates centred on the AOI.
    The disc is triangulated directly (Delaunay over a grid plus a dense boundary
    ring), so the circular rim is clean and no boolean operation is needed.
    """
    from scipy.spatial import Delaunay

    r = aoi.radius_m
    n_ring = max(48, int(round(2 * np.pi * r / mesh_res_m)))
    ang = np.linspace(0, 2 * np.pi, n_ring, endpoint=False)
    ring = np.column_stack([r * np.cos(ang), r * np.sin(ang)])

    rin = r - mesh_res_m * 0.6
    g = np.arange(-rin, rin + 1e-9, mesh_res_m)
    GX, GY = np.meshgrid(g, g)
    inside = (GX ** 2 + GY ** 2) <= rin ** 2
    interior = np.column_stack([GX[inside], GY[inside]])

    top2d = np.vstack([ring, interior])           # ring occupies indices 0..n_ring-1
    tri = Delaunay(top2d)
    top_faces = tri.simplices.copy()
    # enforce CCW winding so top normals point up (+z)
    a, b, c = top2d[top_faces[:, 0]], top2d[top_faces[:, 1]], top2d[top_faces[:, 2]]
    ab, ac = b - a, c - a
    flip = (ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]) < 0   # 2D cross z-component
    top_faces[flip] = top_faces[flip][:, [0, 2, 1]]

    z_real = terrain.sample(top2d[:, 0] + aoi.cx, top2d[:, 1] + aoi.cy)
    z_top = terrain.to_diorama_z(z_real)
    top3d = np.column_stack([top2d, z_top])

    N = len(top3d)
    bottom_ring = np.column_stack([ring, np.zeros(n_ring)])
    center = np.array([[0.0, 0.0, 0.0]])
    verts = np.vstack([top3d, bottom_ring, center])
    b_idx = N + np.arange(n_ring)
    c_idx = N + n_ring

    faces = list(map(tuple, top_faces))
    for i in range(n_ring):
        t0, t1 = i, (i + 1) % n_ring
        b0, b1 = int(b_idx[i]), int(b_idx[(i + 1) % n_ring])
        faces.append((t0, b0, t1))            # outward-facing skirt
        faces.append((t1, b0, b1))
    for i in range(n_ring):                   # bottom cap (faces down)
        b0, b1 = int(b_idx[i]), int(b_idx[(i + 1) % n_ring])
        faces.append((c_idx, b1, b0))

    return verts.astype("float64"), np.array(faces, dtype="int64")
