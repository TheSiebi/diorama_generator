"""Minimal swisstopo STAC client: find the tiles intersecting an AOI."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .config import STAC_BASE
from .geo import AOI


@dataclass
class Asset:
    item_id: str
    key: str
    href: str
    media_type: str


def _items(collection: str, bbox_wgs: tuple[float, float, float, float],
           client: httpx.Client) -> list[dict]:
    """All STAC items for a collection intersecting bbox (handles paging)."""
    url = f"{STAC_BASE}/collections/{collection}/items"
    params = {"bbox": ",".join(f"{c:.6f}" for c in bbox_wgs), "limit": 100}
    out: list[dict] = []
    while url:
        r = client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("features", []))
        url = next((l["href"] for l in data.get("links", [])
                    if l.get("rel") == "next"), None)
        params = None  # the `next` href already carries query params
    return out


# Tile id -> dedupe key + year for "latest". Collections use different tile
# schemes: swissALTI3D is "2679-1244" (4-4 LV95 km), swissBUILDINGS3D is
# "1091-32" (4-2 map sheet). Match the whole second block with "\d+" so both
# work: a fixed "\d{4}" drops the 4-2 building tiles, while a lazy "\d{1,2}"
# truncates the 4-4 alti northing and collapses adjacent tiles (…-1244 / …-1245)
# into one key -> half the AOI silently lost. Whole-country aggregates (no tile
# block, e.g. "swissbuildings3d_3_0_2024") don't match and are skipped.
_TILE_RE = re.compile(r"_(\d{4})_(\d{4}-\d+)")


def _latest_per_tile(items: list[dict]) -> list[dict]:
    """Keep the newest version of each *tiled* item.

    Some collections also publish a single whole-country aggregate (no tile
    token, e.g. ``swissbuildings3d_3_0_2023`` ~ 5 GB). Those are dropped here so
    we only ever fetch the small per-tile files intersecting the AOI.
    """
    best: dict[str, tuple[int, dict]] = {}
    for it in items:
        m = _TILE_RE.search(it["id"])
        if not m:
            continue  # skip whole-country / non-tiled items
        key, year = m.group(2), int(m.group(1))
        if key not in best or year > best[key][0]:
            best[key] = (year, it)
    return [v[1] for v in best.values()]


def _pick_asset(item: dict, *, suffix: str | None = None,
                type_substr: str | None = None,
                name_contains: str | None = None) -> Asset | None:
    for key, a in item["assets"].items():
        href = a.get("href", "")
        if suffix and not href.endswith(suffix):
            continue
        if type_substr and type_substr not in a.get("type", ""):
            continue
        if name_contains and name_contains not in key:
            continue
        return Asset(item_id=item["id"], key=key, href=href,
                     media_type=a.get("type", ""))
    return None


def buildings_gdb_assets(aoi: AOI, client: httpx.Client) -> list[Asset]:
    from .config import COLL_BUILDINGS
    items = _latest_per_tile(_items(COLL_BUILDINGS, aoi.bbox_wgs, client))
    assets = [_pick_asset(it, suffix=".gdb.zip") for it in items]
    return [a for a in assets if a]


# Whole-country aggregate ids carry a bare version, no tile token:
# 3.0: "swissbuildings3d_3_0_2026"   vs tiled "swissbuildings3d_3_0_2019_1091-23"
# 2.0: "swissbuildings3d_2_2024-05"  vs tiled "swissbuildings3d_2_2021-05_1091-23"
_COUNTRY_RE_30 = re.compile(r"_3_0_(\d{4})$")
_COUNTRY_RE_2 = re.compile(r"_2_(\d{4}-\d{2})$")


def _country_gdb_url(collection: str, country_re: re.Pattern,
                     aoi: AOI, client: httpx.Client) -> str:
    """URL of a collection's latest whole-country FileGDB zip.

    Aggregates intersect every bbox, so the AOI query returns them alongside
    the local tiles; the version tokens sort lexicographically.
    """
    items = _items(collection, aoi.bbox_wgs, client)
    aggs = [(m.group(1), it) for it in items
            if (m := country_re.search(it["id"]))]
    if not aggs:
        raise RuntimeError(f"No whole-country aggregate found in {collection}.")
    latest = max(aggs, key=lambda t: t[0])[1]
    a = _pick_asset(latest, suffix=".gdb.zip")
    if not a:
        raise RuntimeError(f"No .gdb.zip asset on {latest['id']}.")
    return a.href


def buildings_country_gdb_url(aoi: AOI, client: httpx.Client) -> str:
    """Latest whole-country swissBUILDINGS3D 3.0 FileGDB zip (~14 GB).

    The per-tile 3.0 items are no longer refreshed (e.g. Zurich is frozen on
    2019 imagery); since 2023 current data ships only as a yearly aggregate.
    Note the aggregate reuses each region's last processing — a region stale
    in the tiles can be equally stale here.
    """
    from .config import COLL_BUILDINGS
    return _country_gdb_url(COLL_BUILDINGS, _COUNTRY_RE_30, aoi, client)


def buildings_v2_country_gdb_url(aoi: AOI, client: httpx.Client) -> str:
    """Latest whole-country swissBUILDINGS3D 2.0 FileGDB zip (~3.6 GB).

    2.0 is the older LOD2 product but on its own revision cycle, so for some
    regions (e.g. Zurich as of 2026) it is fresher than 3.0.
    """
    from .config import COLL_BUILDINGS_V2
    return _country_gdb_url(COLL_BUILDINGS_V2, _COUNTRY_RE_2, aoi, client)


def tlm3d_gdb_url(client: httpx.Client) -> str:
    """URL of the latest whole-country swissTLM3D FileGDB zip."""
    from .config import COLL_TLM
    r = client.get(f"{STAC_BASE}/collections/{COLL_TLM}/items", params={"limit": 100})
    r.raise_for_status()
    items = r.json()["features"]
    latest = max(items, key=lambda f: f["id"])
    for a in latest["assets"].values():
        if a.get("href", "").endswith(".gdb.zip"):
            return a["href"]
    raise RuntimeError("No .gdb.zip asset on the latest swissTLM3D item.")


def alti_tif_assets(aoi: AOI, client: httpx.Client, res_token: str) -> list[Asset]:
    from .config import COLL_ALTI
    items = _latest_per_tile(_items(COLL_ALTI, aoi.bbox_wgs, client))
    out: list[Asset] = []
    for it in items:
        a = _pick_asset(it, name_contains=f"_{res_token}_2056",
                        type_substr="geotiff")
        if a:
            out.append(a)
    return out
