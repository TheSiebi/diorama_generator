"""Runs inside Blender (headless): GLB -> colored .blend + preview PNG.

    blender --background --python blender/assemble.py -- <manifest.json>

Geometry (incl. the circular cut) is already final in the GLB; this script only
imports it, assigns one material per category by object name, frames a camera,
saves the .blend and optionally renders a preview.
"""

import json
import math
import sys

import bpy
import mathutils


def argv_after_ddash():
    argv = sys.argv
    return argv[argv.index("--") + 1:] if "--" in argv else []


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def fix_gltf_axes():
    """Undo the trimesh->glTF->Blender axis round-trip.

    That round-trip maps our source (x, y, z) -> Blender (x, z, -y), i.e. it
    lays the model on its side (our north +Y becomes Blender up +Z). Rotating
    imported roots by -90 deg about X restores Z-up / +Y-north.
    """
    R = mathutils.Matrix.Rotation(math.radians(-90), 4, "X")
    for obj in bpy.context.scene.objects:
        if obj.parent is None:
            obj.matrix_world = R @ obj.matrix_world


def category_of(obj_name: str) -> str:
    base = obj_name.lower().split(".")[0]
    # "base_markings" before "base": the match is prefix-based
    for cat in ("terrain", "buildings", "roofs", "roads", "water", "trees",
                "bridges", "base_markings", "base"):
        if base.startswith(cat):
            return cat
    return ""


def make_material(name, rgb):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.diffuse_color = (rgb[0], rgb[1], rgb[2], 1.0)   # viewport color
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 0.85
    return mat


def assign_materials(colors):
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        cat = category_of(obj.name)
        if not cat:
            continue
        rgb = colors.get(cat, (0.7, 0.7, 0.7))
        mat = make_material(cat, rgb)
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        # smooth-shade natural surfaces so the triangulation doesn't read as
        # false creases; keep buildings/roads/water faceted. Water is draped +
        # grid-tiled like roads, so smooth-shading it inflates every tile into a
        # cushion; faceting matches the road look and keeps rivers reading clean.
        if cat in ("terrain", "trees", "base"):
            obj.data.polygons.foreach_set("use_smooth",
                                          [True] * len(obj.data.polygons))
            obj.data.update()


RES_X, RES_Y = 1280, 960


def scene_bounds():
    mins = mathutils.Vector((1e18, 1e18, 1e18))
    maxs = mathutils.Vector((-1e18, -1e18, -1e18))
    found = False
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        found = True
        for corner in obj.bound_box:
            w = obj.matrix_world @ mathutils.Vector(corner)
            mins = mathutils.Vector((min(mins[i], w[i]) for i in range(3)))
            maxs = mathutils.Vector((max(maxs[i], w[i]) for i in range(3)))
    if not found:
        return mathutils.Vector((0, 0, 0)), mathutils.Vector((1, 1, 1))
    return mins, maxs


def gather_fit_coords():
    """Sampled world-space mesh vertices (flat list) for camera fitting.

    Fitting against real vertices instead of the scene bounding box matters:
    the diorama is a round puck, so the box's square corners are phantom
    points that push the camera back and off-centre (half the frame ends up
    empty). Per-object box corners are appended so thin extremes (an antenna
    tip) survive the subsampling.
    """
    import numpy as np

    coords: list[float] = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        n = len(obj.data.vertices)
        if not n:
            continue
        arr = np.empty(n * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", arr)
        pts = arr.reshape(-1, 3)[:: max(1, n // 2000)]
        m = np.array(obj.matrix_world)
        coords.extend((pts @ m[:3, :3].T + m[:3, 3]).reshape(-1).tolist())
        for corner in obj.bound_box:
            coords.extend(obj.matrix_world @ mathutils.Vector(corner))
    return coords


def setup_camera_and_light():
    scene = bpy.context.scene
    scene.render.resolution_x = RES_X          # aspect must be set before fit
    scene.render.resolution_y = RES_Y
    mins, maxs = scene_bounds()
    center = (mins + maxs) / 2
    diag = max((maxs - mins).length, 1.0)

    cam_data = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    # Due south of and above the centre, looking north: keeps north up with no
    # roll (x-component 0). ~35 deg elevation gives an angled, 3D product-shot
    # view rather than a flat bird's-eye one.
    direction = mathutils.Vector((0.0, -1.0, 0.72)).normalized()
    cam.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
    cam.location = center + direction * diag
    scene.camera = cam
    bpy.context.view_layer.update()

    # Native tight fit (keeps rotation, recenters), then pull back for margin.
    coords = gather_fit_coords()
    if coords:
        deps = bpy.context.evaluated_depsgraph_get()
        loc, _scale = cam.camera_fit_coords(deps, coords)
        cam.location = loc + direction * (loc - center).length * 0.06
    dist = (cam.location - center).length
    cam_data.clip_start = max(0.1, dist * 0.01)
    cam_data.clip_end = dist + diag * 2

    sun_data = bpy.data.lights.new("Sun", type="SUN")
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("Sun", sun_data)
    sun.rotation_euler = (math.radians(50), math.radians(15), math.radians(40))
    bpy.context.scene.collection.objects.link(sun)

    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.05, 0.06, 0.08, 1.0)


def render_preview(path):
    scene = bpy.context.scene
    engines = scene.render.bl_rna.properties["engine"].enum_items.keys()
    scene.render.engine = ("BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines
                           else "BLENDER_EEVEE")
    scene.render.resolution_x = RES_X
    scene.render.resolution_y = RES_Y
    scene.render.filepath = path
    scene.render.image_settings.file_format = "PNG"
    # transparent background (the world still lights the scene)
    scene.render.film_transparent = True
    scene.render.image_settings.color_mode = "RGBA"
    bpy.ops.render.render(write_still=True)


def main():
    args = argv_after_ddash()
    if not args:
        raise SystemExit("manifest path required after --")
    manifest = json.loads(open(args[0], "r", encoding="utf-8").read())

    clear_scene()
    bpy.ops.import_scene.gltf(filepath=manifest["glb"])
    fix_gltf_axes()
    assign_materials({k: tuple(v) for k, v in manifest["colors"].items()})
    setup_camera_and_light()

    bpy.ops.wm.save_as_mainfile(filepath=manifest["blend"])
    print(f"[blender] saved {manifest['blend']}")

    if manifest.get("render_preview"):
        try:
            render_preview(manifest["preview"])
            print(f"[blender] rendered {manifest['preview']}")
        except Exception as exc:  # rendering is non-essential
            print(f"[blender] preview render skipped: {exc}")


if __name__ == "__main__":
    main()
