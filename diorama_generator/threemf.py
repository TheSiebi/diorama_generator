"""Write a colour-per-object 3MF (core spec + base materials).

trimesh can emit a 3MF but drops colour, so we write the package directly. Each
category becomes one <object> bound to a <base> material whose ``displaycolor``
slicers (PrusaSlicer, Bambu, Cura) honour.

The file is declared ``unit="millimeter"`` with the diorama coordinates written
as-is, i.e. 1 m of diorama becomes 1 mm of print. Slicer support for other unit
attributes is inconsistent (the PrusaSlicer family assumes millimetres), so
this makes every slicer agree on the size; rescale to taste from there.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""

_MODEL_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def _srgb_hex(rgb) -> str:
    """Linear RGB 0..1 -> sRGB #RRGGBBFF hex."""
    def enc(c):
        c = max(0.0, min(1.0, float(c)))
        s = 1.055 * c ** (1 / 2.4) - 0.055 if c > 0.0031308 else 12.92 * c
        return int(round(s * 255))
    r, g, b = (enc(x) for x in rgb[:3])
    return f"#{r:02X}{g:02X}{b:02X}FF"


def _weld(v: np.ndarray, f: np.ndarray):
    """Merge exactly coincident vertices; drop collapsed faces.

    The pipeline accumulates triangle soups with one vertex per corner, so a
    closed solid still LOOKS open to a slicer (every edge referenced once).
    Welding restores the shared indexing that mesh-error checks expect. Two
    deliberate limits: only bitwise-equal positions merge (tolerance welding
    fuses close-but-distinct vertices and turns clean solids non-manifold),
    and parts with no once-referenced edges are passed through untouched —
    boolean outputs (terrain, roads, base) are already perfectly indexed but
    contain coincident self-touch vertex pairs that welding would fuse into
    non-manifold edges.
    """
    v = np.asarray(v, dtype="float64")
    f = np.asarray(f, dtype="int64")
    edges = np.sort(np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]),
                    axis=1)
    _e, counts = np.unique(edges, axis=0, return_counts=True)
    if not (counts == 1).any():
        return v, f
    uniq, inv = np.unique(v, axis=0, return_inverse=True)
    f = inv[f]
    keep = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])
    return uniq, f[keep]


def write_3mf(path: Path, parts: list[tuple[str, np.ndarray, np.ndarray, tuple]],
              *, unit: str = "millimeter", scale: float = 1.0) -> Path:
    """parts: list of (name, vertices Nx3, faces Mx3, rgb). Writes a colored 3MF."""
    parts = [(n, v, f, c) for (n, v, f, c) in parts if v is not None and len(f)]
    out = []
    out.append(f'<?xml version="1.0" encoding="UTF-8"?>')
    out.append(f'<model unit="{unit}" xml:lang="en-US" xmlns="{_MODEL_NS}">')
    out.append(" <resources>")

    # one basematerials group; one <base> per part
    out.append('  <basematerials id="1">')
    for name, _v, _f, rgb in parts:
        out.append(f'   <base name="{_xml(name)}" displaycolor="{_srgb_hex(rgb)}"/>')
    out.append("  </basematerials>")

    obj_ids = []
    for idx, (name, v, f, _rgb) in enumerate(parts):
        oid = idx + 2  # ids 2.. (1 is the materials group)
        obj_ids.append(oid)
        out.append(f'  <object id="{oid}" name="{_xml(name)}" type="model" '
                   f'pid="1" pindex="{idx}">')
        out.append("   <mesh>")
        out.append("    <vertices>")
        vs, fs = _weld(np.asarray(v, dtype="float64") * scale, f)
        out.extend(f'     <vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>'
                   for x, y, z in vs)
        out.append("    </vertices>")
        out.append("    <triangles>")
        out.extend(f'     <triangle v1="{int(a)}" v2="{int(b)}" v3="{int(c)}"/>'
                   for a, b, c in fs)
        out.append("    </triangles>")
        out.append("   </mesh>")
        out.append("  </object>")

    out.append(" </resources>")
    out.append(" <build>")
    out.extend(f'  <item objectid="{oid}"/>' for oid in obj_ids)
    out.append(" </build>")
    out.append("</model>")
    model_xml = "\n".join(out)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model_xml)
    return path


def _xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
