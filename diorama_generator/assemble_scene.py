"""Combine category meshes into a single GLB with named objects."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh


def _mesh(v: np.ndarray, f: np.ndarray) -> trimesh.Trimesh | None:
    if v is None or len(v) == 0 or len(f) == 0:
        return None
    return trimesh.Trimesh(vertices=np.asarray(v, dtype="float64"),
                           faces=np.asarray(f, dtype="int64"), process=False)


def build_glb(out_path: Path, *, terrain=None, buildings=None, roofs=None,
              bridges=None, roads=None, water=None, trees=None,
              base=None, base_markings=None) -> Path:
    """Each argument is a (vertices, faces) tuple. Writes a GLB, returns path."""
    scene = trimesh.Scene()
    parts = {"terrain": terrain, "buildings": buildings, "roofs": roofs,
             "bridges": bridges, "roads": roads, "water": water, "trees": trees,
             "base": base, "base_markings": base_markings}
    for name, vf in parts.items():
        if vf is None:
            continue
        m = _mesh(*vf)
        if m is not None and len(m.faces):
            scene.add_geometry(m, geom_name=name, node_name=name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scene.export(out_path)
    return out_path
