"""Mesh diorama base: an asset file -> aligned, scaled mesh parts.

Any Blender-importable mesh (see SUPPORTED_FORMATS) can serve as the base.
Blender (headless) converts the asset once into a cached NPZ of world-space
meshes (see blender/export_base.py); the pipeline then centres it, scales it
so the terrain puck lands on its top face, flattens its underside so it
stands flat, and carves a pocket into the top so the puck's rim seam sits
flush.

Two optional conventions unlock extra behaviour:

- Materials: objects whose material name contains "marking" (case-insensitive)
  are split into a separately coloured "base_markings" part; everything else
  becomes "base". Assets without materials come back as a single "base" part.
- Compass alignment: if the asset contains the four marker objects named in
  CARDINAL_MARKERS (Table3=E, Table5=S, Table7=W, Table9=N, as in the
  reference table.fbx), the base is rotated so those headings match diorama
  coordinates (+X east, +Y north), and the marker material defines the
  markings part. Without them the asset is used unrotated.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import numpy as np
import trimesh

from .config import blender_exe, cache_dir

TABLE_FBX = Path(__file__).resolve().parent.parent / "assets" / "table.fbx"
SUPPORTED_FORMATS = (".fbx", ".glb", ".gltf", ".obj", ".stl", ".ply", ".blend")
_EXPORT_SCRIPT = (Path(__file__).resolve().parent.parent
                  / "blender" / "export_base.py")

# subpart name -> compass azimuth (deg CCW from +X = east) of its heading letter.
CARDINAL_MARKERS = {"Table3": 0.0,     # East
                    "Table9": 90.0,    # North
                    "Table7": 180.0,   # West
                    "Table5": -90.0}   # South


def _asset_to_npz(asset: Path) -> Path:
    """Convert the asset via headless Blender, cached by content hash."""
    digest = hashlib.sha1(asset.read_bytes()).hexdigest()[:12]
    dest = cache_dir() / "base" / f"{asset.stem}_{digest}.npz"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + "_tmp.npz")
    cmd = [blender_exe(), "--background", "--python", str(_EXPORT_SCRIPT),
           "--", str(asset), str(tmp)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not tmp.exists():
        raise RuntimeError("Blender conversion of the base asset failed:\n"
                           + res.stdout[-2000:] + res.stderr[-2000:])
    tmp.replace(dest)
    return dest


def _solve_rotation(meshes: dict) -> float:
    """Z-rotation (rad) that puts each cardinal marker at its compass azimuth."""
    offs = []
    for name, want_deg in CARDINAL_MARKERS.items():
        c = meshes[name][0].mean(axis=0)
        offs.append(np.radians(want_deg) - np.arctan2(c[1], c[0]))
    offs = np.asarray(offs)
    mean = np.arctan2(np.mean(np.sin(offs)), np.mean(np.cos(offs)))
    # residual per marker after the common rotation; a mirrored or re-labelled
    # asset cannot be fixed by rotation alone and must be caught here.
    resid = np.degrees(np.abs(np.angle(np.exp(1j * (offs - mean)))))
    if resid.max() > 3.0:
        raise RuntimeError(
            "base asset: cardinal markers are not one rigid rotation away from "
            f"the diorama axes (residuals {np.round(resid, 1)} deg for "
            f"{list(CARDINAL_MARKERS)}) — mirrored or re-labelled asset?")
    return float(mean)


def _concat(parts: list[tuple[np.ndarray, np.ndarray]]):
    vs, fs, off = [], [], 0
    for v, f in parts:
        vs.append(v)
        fs.append(np.asarray(f, dtype="int64") + off)
        off += len(v)
    return np.vstack(vs), np.vstack(fs)


def load_base_asset(radius_m: float, *, path: Path | None = None,
                    marking_gap: float = 0.02, rim_margin: float = 0.10,
                    inset_depth: float = 0.0) -> dict:
    """Build a mesh base for a terrain puck of ``radius_m``.

    ``path`` is any asset in SUPPORTED_FORMATS (default: the reference
    ``assets/table.fbx``). Returns ``{"parts": [("base", v, f)] (+ optionally
    ("base_markings", v, f)), "lift": z, "inset_depth": ...,
    "rotation_deg": ..., "scale": ...}``. The base sits at z >= 0; the caller
    must raise the rest of the diorama by ``lift``.

    Scaling picks a reference radius: with a markings part, the innermost
    marking radius (the puck rim lands ``marking_gap`` inside it, as a
    fraction of radius); without one, the widest radius of the asset's top
    tenth (the puck rim lands ``rim_margin`` inside the top rim).

    ``inset_depth`` > 0 carves a cylindrical pocket that deep into the
    top face and seats the puck in it, so a puck whose lowest terrain-surface
    point sits ``inset_depth`` above its bottom comes out flush with the
    top instead of towering above it. The pocket is clamped to leave at
    least a quarter of the base's height under the puck.
    """
    asset = Path(path) if path else TABLE_FBX
    if not asset.exists():
        raise RuntimeError(f"base asset not found: {asset}")
    z = np.load(_asset_to_npz(asset))
    names = [str(s) for s in z["names"]]
    mats = [str(s) for s in z["mats"]]
    meshes = {n: (z[f"v{i}"], z[f"f{i}"], mats[i]) for i, n in enumerate(names)}
    if not meshes:
        raise RuntimeError(f"base asset {asset.name}: no mesh objects inside")

    # markings split: by the cardinal markers' material when the markers are
    # present (the reference table), else by material name.
    have_markers = all(n in meshes for n in CARDINAL_MARKERS)
    if have_markers:
        marker_mat = meshes[next(iter(CARDINAL_MARKERS))][2]
        def is_marking(m: str) -> bool: return m == marker_mat
    else:
        def is_marking(m: str) -> bool: return "marking" in m.lower()
    if all(is_marking(m) for _v, _f, m in meshes.values()):
        def is_marking(m: str) -> bool: return False

    # centre on the body's bbox before measuring marker azimuths
    body_v = np.vstack([v for v, _f, m in meshes.values() if not is_marking(m)])
    center = (body_v[:, :2].min(0) + body_v[:, :2].max(0)) / 2
    for n, (v, f, m) in meshes.items():
        v = v.astype("float64").copy()
        v[:, :2] -= center
        meshes[n] = (v, f, m)

    rot = _solve_rotation(meshes) if have_markers else 0.0
    if rot:
        c, s = np.cos(rot), np.sin(rot)
        R = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        for n, (v, f, m) in meshes.items():
            meshes[n] = (v @ R.T, f, m)

    body = [(v, f) for v, f, m in meshes.values() if not is_marking(m)]
    marks = [(v, f) for v, f, m in meshes.values() if is_marking(m)]

    # an asset's underside may dip below its outer rim (the table's is a
    # shallow cone), so it would rest on the tip; flatten the bottom at the
    # rim's lowest point (the outer-diameter height is the reference plane)
    body_v = np.vstack([v for v, _f in body])
    rad = np.sqrt((body_v[:, :2] ** 2).sum(1))
    z_flat = float(body_v[rad >= 0.98 * rad.max(), 2].min())
    for v, _f in body:
        np.maximum(v[:, 2], z_flat, out=v[:, 2])
    np.maximum(body_v[:, 2], z_flat, out=body_v[:, 2])

    # scale so the puck rim lands just inside the reference radius: the
    # innermost marking when the asset has markings, else the top rim
    if marks:
        mark_v = np.vstack([v for v, _f in marks])
        r_ref = float(np.sqrt((mark_v[:, :2] ** 2).sum(1)).min())
        gap = marking_gap
    else:
        z_hi = body_v[:, 2].max()
        top = body_v[body_v[:, 2] >= z_hi - 0.10 * (z_hi - z_flat)]
        r_ref = float(np.sqrt((top[:, :2] ** 2).sum(1)).max())
        gap = rim_margin
    scale = radius_m * (1.0 + gap) / r_ref

    # seat = highest body point under the puck footprint; the puck bottom
    # (z=0 in diorama coords) must rest there, so the whole diorama is lifted
    # by the base height below the seat.
    seat_r = radius_m / scale
    seat_z = float(body_v[rad <= seat_r, 2].max())
    lift = (seat_z - float(body_v[:, 2].min())) * scale

    def finish(v: np.ndarray) -> np.ndarray:
        v = v * scale
        v[:, 2] += lift - seat_z * scale      # base bottom -> 0, seat -> lift
        return v

    bv, bf = _concat([(finish(v), f) for v, f in body])
    parts = [("base", bv, bf)]
    if marks:
        mv, mf = _concat([(finish(v), f) for v, f in marks])
        parts.append(("base_markings", mv, mf))

    # pocket: sink the puck into the top face so the terrain surface sits
    # flush with it (anything at r > radius_m, e.g. markings, is untouched)
    depth = min(float(inset_depth), 0.75 * lift) if inset_depth > 0 else 0.0
    if depth > 0:
        pad = max(0.05, radius_m * 2e-4)   # pocket wall just clear of the rim
        h = depth + 2.0                    # overshoot the top for a clean cut
        cutter = trimesh.creation.cylinder(radius=radius_m + pad, height=h,
                                           sections=512)
        cutter.apply_translation([0.0, 0.0, lift - depth + h / 2])
        body_mesh = trimesh.Trimesh(bv, bf, process=True)
        trimesh.repair.fix_normals(body_mesh)   # imports may flip faces
        try:
            cut = trimesh.boolean.difference([body_mesh, cutter],
                                             engine="manifold")
            bv = np.asarray(cut.vertices, dtype="float64")
            bf = np.asarray(cut.faces, dtype="int64")
            parts[0] = ("base", bv, bf)
        except Exception as exc:   # keep the puck on top rather than
            depth = 0.0            # intersecting an uncut base body
            print(f"[base] top-face pocket skipped ({exc}); puck sits on top")

    return {"parts": parts,
            "lift": float(lift - depth), "inset_depth": float(depth),
            "rotation_deg": float(np.degrees(rot)), "scale": float(scale)}
