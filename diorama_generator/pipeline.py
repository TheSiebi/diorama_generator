"""End-to-end orchestration: data -> GLB -> Blender (.blend + .3mf)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import numpy as np

from .assemble_scene import build_glb
from .base import SUPPORTED_FORMATS, TABLE_FBX, load_base_asset
from .buildings import load_buildings
from .config import CATEGORY_COLORS, blender_exe
from .features_osm import load_osm_features
from .features_tlm import load_tlm_features
from .featuresolid import carve_terrain
from .geo import AOI
from .terrain import load_terrain, terrain_disc
from .threemf import write_3mf
from .trees import load_tlm_trees
from .ui import PipelineUI

_BLENDER_SCRIPT = Path(__file__).resolve().parent.parent / "blender" / "assemble.py"


def generate(lon: float, lat: float, radius_m: float, out_dir: Path, *,
             name: str = "diorama", mesh_res_m: float = 2.0,
             base_thickness_m: float = 12.0, with_features: bool = True,
             feature_source: str = "tlm", buildings_vintage: str = "auto",
             with_trees: bool = False, base_style: str = "cylinder",
             render_preview: bool = True, run_blender: bool = True) -> dict:
    # base_style: "cylinder" (plain puck skirt), "table" (assets/table.fbx),
    # or a path to any mesh asset. Validated up front, before any downloads.
    base_asset: Path | None = None
    if base_style == "table":
        base_asset = TABLE_FBX
        if not base_asset.exists():
            raise ValueError(
                f"--base table needs {base_asset} — a third-party asset that "
                "is not shipped with this repo. Use the default cylinder "
                "base, or pass --base <path-to-your-own-mesh> "
                "(see 'Custom base meshes' in the README).")
    elif base_style != "cylinder":
        base_asset = Path(base_style)
        if not base_asset.exists():
            raise ValueError(f"base mesh not found: {base_asset} (--base "
                             "takes 'cylinder', 'table' or a mesh path)")
        if base_asset.suffix.lower() not in SUPPORTED_FORMATS:
            raise ValueError(f"base mesh {base_asset.name}: unsupported "
                             f"format (use one of {', '.join(SUPPORTED_FORMATS)})")
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    aoi = AOI.from_lonlat(lon, lat, radius_m)

    # one UI step per pipeline stage that actually runs
    n_steps = (2                                 # terrain + buildings
               + (2 if with_features else 0)     # roads & water + carve
               + (1 if with_trees else 0)
               + (1 if base_asset is not None else 0)
               + 1                               # assemble GLB + 3MF
               + (1 if run_blender else 0))

    with PipelineUI(n_steps, title=name) as ui:
        log = ui.log
        with httpx.Client(timeout=180.0, follow_redirects=True,
                          headers={"User-Agent": "diorama-generator/0.1"}) as client:
            ui.step("Terrain (swissALTI3D)")
            terrain = load_terrain(aoi, client, base_thickness_m=base_thickness_m)
            tverts, tfaces = terrain_disc(aoi, terrain, mesh_res_m=mesh_res_m)
            ui.detail(f"merged DTM, zmin={terrain.zmin:.1f} m")

            bsrc = {"auto": "Stadt Zürich city model if the AOI is inside "
                            "Zurich, else swissBUILDINGS3D 3.0 (~14 GB once)",
                    "zurich": "Stadt Zürich city model (weekly)",
                    "latest": "swissBUILDINGS3D 3.0 whole-country aggregate "
                              "(downloads ~14 GB once)",
                    "v2": "swissBUILDINGS3D 2.0 whole-country aggregate "
                          "(downloads ~3.6 GB once)",
                    "tiles": "swissBUILDINGS3D 3.0 per-tile"}
            ui.step("Buildings")
            ui.detail(bsrc.get(buildings_vintage, buildings_vintage))
            bld = load_buildings(aoi, terrain, client, vintage=buildings_vintage)
            (bverts, bfaces), (rfv, rff), n_b = bld["walls"], bld["roofs"], bld["count"]
            bridges = bld.get("bridges")
            n_brf = len(bridges[1]) if bridges is not None else 0
            ui.detail(f"{n_b} buildings from "
                      f"{bld.get('source', buildings_vintage)}")

            roads = water = None
            carve_tools = []
            if with_features:
                if feature_source == "tlm":
                    ui.step("Roads & water (swissTLM3D)")
                    ui.detail("whole-country GDB, ~2.9 GB one-time download")
                    feats = load_tlm_features(aoi, terrain, client,
                                              seg_len=mesh_res_m)
                else:
                    ui.step("Roads & water (OpenStreetMap)")
                    ui.detail("Overpass query, may take a moment")
                    feats = load_osm_features(aoi, terrain, client,
                                              seg_len=mesh_res_m)
                roads, water = feats["roads"], feats["water"]
                carve_tools = feats.get("carve", [roads, water])

            trees = None
            if with_trees:
                ui.step("Trees")
                ui.detail("swissTLM3D forest fill + single trees")
                trees = load_tlm_trees(aoi, terrain, client)

        # Inlay: subtract the road/water solids from the terrain so each part
        # is a watertight volume sharing a wall with the terrain (no
        # z-fighting, no "zero-volume object removed" in the slicer).
        if with_features:
            ui.step("Carve inlays")
            inlays = [vf for vf in carve_tools if vf is not None and len(vf[1])]
            if inlays:
                ui.detail(f"subtracting {len(inlays)} road/water solid(s) "
                          "from the terrain")
                tverts, tfaces = carve_terrain((tverts, tfaces), *inlays)
            else:
                ui.detail("nothing to carve")

        # Base: a mesh asset (converted once via Blender, cached). The puck
        # sinks into a pocket in the top face deep enough that the lowest
        # terrain-SURFACE point ON THE RIM — the visible seam — sits flush
        # with it. Anchoring on the rim, not the global zmin, matters when
        # the AOI's lowest spot is in the interior (riverbed): the rim would
        # otherwise float above the base by the difference. Only the DTM is
        # sampled — building foundations and road/river prism bottoms reach
        # lower but live inside the pocket. The diorama is raised by `lift`
        # so the whole assembly stays at z >= 0.
        base_parts = []
        if base_asset is not None:
            ui.step(f"Base ({base_asset.name})")
            n_rim = max(48, int(round(2 * np.pi * radius_m / mesh_res_m)))
            ang = np.linspace(0.0, 2.0 * np.pi, n_rim, endpoint=False)
            rim_z = terrain.to_diorama_z(terrain.sample(
                aoi.cx + radius_m * np.cos(ang), aoi.cy + radius_m * np.sin(ang)))
            tb = load_base_asset(radius_m, path=base_asset,
                                 inset_depth=float(rim_z.min()))
            base_parts = tb["parts"]
            lift = tb["lift"]
            ui.detail(f"rotated {tb['rotation_deg']:.1f} deg, "
                      f"scaled {tb['scale']:.1f}x, "
                      f"pocket {tb['inset_depth']:.1f} m, "
                      f"lifted {lift:.1f} m")
            off = np.array([0.0, 0.0, lift])

            def _lift(vf):
                if vf is None or len(vf[0]) == 0:
                    return vf
                return np.asarray(vf[0], dtype="float64") + off, vf[1]

            tverts, tfaces = _lift((tverts, tfaces))
            (bverts, bfaces), (rfv, rff) = _lift((bverts, bfaces)), _lift((rfv, rff))
            bridges, roads, water, trees = (_lift(bridges), _lift(roads),
                                            _lift(water), _lift(trees))

        ui.step("Assemble GLB + 3MF")
        has_trees = trees is not None and len(trees[1])
        glb = out_dir / f"{name}.glb"
        bp = {nm: (v, f) for nm, v, f in base_parts}
        build_glb(glb, terrain=(tverts, tfaces),
                  buildings=(bverts, bfaces) if n_b else None,
                  roofs=(rfv, rff) if len(rff) else None,
                  bridges=bridges if n_brf else None,
                  roads=roads, water=water, trees=trees if has_trees else None,
                  base=bp.get("base"), base_markings=bp.get("base_markings"))

        parts = [("terrain", tverts, tfaces, CATEGORY_COLORS["terrain"])]
        if n_b:
            parts.append(("buildings", bverts, bfaces, CATEGORY_COLORS["buildings"]))
        if len(rff):
            parts.append(("roofs", rfv, rff, CATEGORY_COLORS["roofs"]))
        if n_brf:
            parts.append(("bridges", bridges[0], bridges[1],
                          CATEGORY_COLORS["bridges"]))
        if roads is not None and len(roads[1]):
            parts.append(("roads", roads[0], roads[1], CATEGORY_COLORS["roads"]))
        if water is not None and len(water[1]):
            parts.append(("water", water[0], water[1], CATEGORY_COLORS["water"]))
        if has_trees:
            parts.append(("trees", trees[0], trees[1], CATEGORY_COLORS["trees"]))
        for nm, v, f in base_parts:
            parts.append((nm, v, f, CATEGORY_COLORS[nm]))
        threemf_path = out_dir / f"{name}.3mf"
        write_3mf(threemf_path, parts)
        ui.model_table(parts)
        ui.detail(f"wrote {glb.name} and {threemf_path.name} "
                  f"({len(parts)} colored objects)")

        manifest = {
            "name": name,
            "radius_m": radius_m,
            "base_thickness_m": base_thickness_m,
            "base_style": base_style,
            "colors": CATEGORY_COLORS,
            "glb": str(glb),
            "blend": str(out_dir / f"{name}.blend"),
            "threemf": str(out_dir / f"{name}.3mf"),
            "preview": str(out_dir / f"{name}.png"),
            "render_preview": render_preview,
            "center_lv95": [aoi.cx, aoi.cy],
            "lonlat": [lon, lat],
        }
        manifest_path = out_dir / f"{name}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        if run_blender:
            ui.step("Blender render")
            ui.detail(".blend + preview PNG, headless")
            cmd = [blender_exe(), "--background", "--python",
                   str(_BLENDER_SCRIPT), "--", str(manifest_path)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                log(res.stdout[-4000:])
                log(res.stderr[-4000:])
                raise RuntimeError("Blender step failed.")
            for line in [l for l in res.stdout.splitlines() if l.strip()][-3:]:
                ui.detail(line.strip())
        else:
            ui.detail("Blender skipped (run_blender=False)")

    return manifest
