"""Stadt Zürich open-data 3D city model -> building meshes (WFS, LV95).

Data source: "Bauten - Kombinierte Darstellung heute (3D)" on the city's
geoportal — the photogrammetric roof model (LoD2) combined with the weekly
updated block model (LoD1), so it is far fresher than swissBUILDINGS3D in
Zurich (where swisstopo's last processing is 2019 imagery). Served by a QGIS
WFS that supports bbox queries in EPSG:2056, so only the AOI is downloaded
(no whole-city file). One solid mesh per building, no separate roof solids
-> no distinct roof colour from this source.

fiona/OGR cannot read the server's GML (2.5D-unknown geometry type), so the
fixed QGIS-server GML structure is parsed directly.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import mapbox_earcut
import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .config import ZURICH_WFS, cache_dir
from .geo import AOI
from .terrain import Terrain

_GML = "{http://www.opengis.net/gml}"
_QGS = "{http://www.qgis.org/gml}"

DATASET = "Bauten___Kombinierte_Darstellung_heute"
LAYER = "kombi_lod1_2_heute_3d"
# art codes that are civil works, not buildings: rendered road-grey instead
# of building-white (EO08 = Bruecke_Passerelle, EO12 = Pfeiler/bridge piers).
BRIDGE_ARTS = {"EO08", "EO12"}
# Union of the 12 Stadtkreis polygons = the city territory the model covers.
BOUNDARY_DATASET = "Stadtkreise"
BOUNDARY_LAYER = "adm_stadtkreise_a"


def _wfs_fetch(dataset: str, layer: str, client: httpx.Client, *,
               bbox: tuple[float, float, float, float] | None = None) -> Path:
    """GET a WFS layer as GML into the download cache; returns the file path.

    Cached by URL like every other download, so a given AOI keeps its first
    weekly snapshot until the cache is cleared.
    """
    url = (f"{ZURICH_WFS}/{dataset}?SERVICE=WFS&VERSION=1.1.0&REQUEST=GetFeature"
           f"&TYPENAME={layer}&SRSNAME=EPSG:2056&MAXFEATURES=100000")
    if bbox:
        url += "&BBOX={:.1f},{:.1f},{:.1f},{:.1f},EPSG:2056".format(*bbox)
    digest = hashlib.sha1(url.encode()).hexdigest()[:10]
    dest = cache_dir() / "downloads" / f"{digest}_{layer}.gml"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = client.get(url)
    r.raise_for_status()
    if b"FeatureCollection" not in r.content[:1024]:
        raise RuntimeError(f"Unexpected WFS response from {dataset}/{layer} "
                           f"(not a FeatureCollection).")
    dest.write_bytes(r.content)
    return dest


def _ring_of(elem) -> np.ndarray | None:
    """Ring coordinates ((N, dim) array) under an <exterior>/<interior> element.

    Handles both ring encodings the server uses: a single LinearRing posList
    (buildings) and CompositeCurve/LineString segments (Stadtkreise).
    """
    if elem is None:
        return None
    parts = [np.array(pl.text.split(), dtype="float64").reshape(
                 -1, int(pl.get("srsDimension", "2")))
             for pl in elem.iter(f"{_GML}posList") if pl.text]
    return np.vstack(parts) if parts else None


def _polygons(path: Path):
    """Yield (art code | None, [(shell, [holes]), ...]) per GML feature."""
    root = ET.parse(path).getroot()
    for member in root.iter(f"{_GML}featureMember"):
        polys = []
        for poly in member.iter(f"{_GML}Polygon"):
            shell = _ring_of(poly.find(f"{_GML}exterior"))
            if shell is None:
                continue
            holes = [h for h in (_ring_of(i)
                                 for i in poly.findall(f"{_GML}interior"))
                     if h is not None]
            polys.append((shell, holes))
        art = member.find(f".//{_QGS}art")
        yield (art.text if art is not None else None), polys


def _drop_closing_point(ring: np.ndarray) -> np.ndarray:
    if len(ring) > 3 and np.allclose(ring[0], ring[-1]):
        return ring[:-1]
    return ring


def _triangulate(shell: np.ndarray, holes: list[np.ndarray]) -> list:
    """Triangulate a planar 3D polygon that may be concave and have holes.

    A naive fan from the first vertex is only valid for convex rings; the
    LoD1 block parts of the city model carry whole (concave) footprint
    polygons as roof/floor faces, so the ring is projected onto its best-fit
    plane (Newell normal) and earcut-triangulated there.
    """
    rings = [_drop_closing_point(shell)] + \
            [h for h in map(_drop_closing_point, holes) if len(h) >= 3]
    shell = rings[0]
    if len(shell) < 3:
        return []
    if len(shell) == 3 and len(rings) == 1:
        return [shell]
    # Newell normal of the shell
    q = np.roll(shell, -1, axis=0)
    n = np.array([np.sum((shell[:, 1] - q[:, 1]) * (shell[:, 2] + q[:, 2])),
                  np.sum((shell[:, 2] - q[:, 2]) * (shell[:, 0] + q[:, 0])),
                  np.sum((shell[:, 0] - q[:, 0]) * (shell[:, 1] + q[:, 1]))])
    norm = np.linalg.norm(n)
    if norm < 1e-12:  # degenerate sliver
        return []
    n /= norm
    a = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(n, a)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    pts = np.vstack(rings)
    pts2 = np.column_stack([pts @ u, pts @ v])
    ends = np.cumsum([len(r) for r in rings]).astype("uint32")
    idx = mapbox_earcut.triangulate_float64(pts2, ends).reshape(-1, 3)
    return [pts[t] for t in idx]


def zurich_covers(aoi: AOI, client: httpx.Client, *, strict: bool = False) -> bool:
    """True if the AOI circle lies fully inside the Zurich city boundary.

    With strict=False a failing boundary fetch just returns False (callers
    fall back to swissBUILDINGS3D instead of aborting the whole run).
    """
    try:
        path = _wfs_fetch(BOUNDARY_DATASET, BOUNDARY_LAYER, client)
        polys = [Polygon(shell[:, :2], [h[:, :2] for h in holes])
                 for _, feature in _polygons(path)
                 for shell, holes in feature if len(shell) >= 3]
        boundary = unary_union(polys)
    except (httpx.HTTPError, ET.ParseError, RuntimeError):
        if strict:
            raise
        print("      Zurich city-boundary check failed; "
              "falling back to swissBUILDINGS3D")
        return False
    return boundary.contains(aoi.circle_lv95)


def load_city_buildings(aoi: AOI, terrain: Terrain, client: httpx.Client,
                        *, clip_sections: int = 96) -> dict:
    """Return {'walls', 'roofs', 'bridges': (V, F), 'count': n, 'source': str}.

    Same contract as buildings.load_buildings plus a 'bridges' mesh (bridge
    decks and piers, rendered road-grey). 'roofs' is always empty since the
    combined city model has one solid per building.
    """
    from .buildings import _MeshAcc

    path = _wfs_fetch(DATASET, LAYER, client, bbox=aoi.bbox_lv95)
    walls, bridges = _MeshAcc(), _MeshAcc()
    keep_r2 = aoi.radius_m ** 2
    for art, feature in _polygons(path):
        tris = []
        for shell, holes in feature:
            tris += _triangulate(shell, holes)
        if not tris:
            continue
        pts = np.array([p for tri in tris for p in tri], dtype="float64")
        # keep if any part of the footprint reaches into the circle
        d2 = (pts[:, 0] - aoi.cx) ** 2 + (pts[:, 1] - aoi.cy) ** 2
        if d2.min() > keep_r2:
            continue
        (bridges if art in BRIDGE_ARTS else walls).add(tris, aoi, terrain)
    wv, wf = walls.clipped(aoi.radius_m, clip_sections)
    # no extrusion-solidify for bridges: filling under a rim-cut deck down to
    # its lowest point would plug the span (closed bodies still cut solid)
    gv, gf = bridges.clipped(aoi.radius_m, clip_sections, extrude=False)
    empty = (np.zeros((0, 3)), np.zeros((0, 3), dtype="int64"))
    return {"walls": (wv, wf), "roofs": empty, "bridges": (gv, gf),
            "count": walls.count, "source": "Stadt Zürich city model (weekly)"}
