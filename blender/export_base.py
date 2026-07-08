"""Runs inside Blender (headless): mesh asset -> NPZ of per-object triangles.

    blender --background --python blender/export_base.py -- <in.mesh> <out.npz>

trimesh cannot read FBX (and importer axis conventions vary per format), so
the pipeline shells out to Blender once (cached) to convert a base asset.
Every mesh object is exported in world space (Blender Z-up), triangulated,
together with its name and slot-0 material name, so the pipeline can regroup
by material and align by marker objects without guessing.
"""

import sys
from pathlib import Path

import bpy
import numpy as np


def _import_asset(path: str):
    ext = Path(path).suffix.lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    else:
        raise SystemExit(f"unsupported base asset format {ext!r} "
                         "(use .fbx, .glb/.gltf, .obj, .stl, .ply or .blend)")


def main():
    argv = sys.argv
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    if len(args) != 2:
        raise SystemExit("usage: ... -- <in.mesh> <out.npz>")
    asset, out = args

    _import_asset(asset)

    deps = bpy.context.evaluated_depsgraph_get()
    data, names, mats = {}, [], []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        ev = obj.evaluated_get(deps)
        me = ev.to_mesh()
        n = len(me.vertices)
        v = np.empty(n * 3, dtype=np.float64)
        me.vertices.foreach_get("co", v)
        m = np.array(obj.matrix_world, dtype=np.float64)
        v = v.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
        t = len(me.loop_triangles)              # computed lazily on access
        f = np.empty(t * 3, dtype=np.int64)
        me.loop_triangles.foreach_get("vertices", f)
        i = len(names)
        data[f"v{i}"], data[f"f{i}"] = v, f.reshape(-1, 3)
        names.append(obj.name)
        slot = obj.material_slots[0].material if obj.material_slots else None
        mats.append(slot.name if slot else "")
        ev.to_mesh_clear()

    data["names"], data["mats"] = np.array(names), np.array(mats)
    np.savez_compressed(out, **data)
    print(f"[export_base] wrote {out}: {len(names)} meshes")


if __name__ == "__main__":
    main()
