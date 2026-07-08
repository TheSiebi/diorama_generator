"""Projections and the area-of-interest (AOI) geometry."""

from __future__ import annotations

from dataclasses import dataclass

import pyproj
from shapely.geometry import Point, Polygon
from shapely.ops import transform as shp_transform

from .config import LV95, WGS84

_to_lv95 = pyproj.Transformer.from_crs(WGS84, LV95, always_xy=True).transform
_to_wgs = pyproj.Transformer.from_crs(LV95, WGS84, always_xy=True).transform


def lonlat_to_lv95(lon: float, lat: float) -> tuple[float, float]:
    return _to_lv95(lon, lat)


def lv95_polygon_to_wgs(poly: Polygon) -> Polygon:
    return shp_transform(_to_wgs, poly)


@dataclass(frozen=True)
class AOI:
    """A circular area of interest, defined in WGS84 but living in LV95 metres."""

    lon: float
    lat: float
    radius_m: float
    cx: float          # centre easting  (LV95)
    cy: float          # centre northing (LV95)

    @classmethod
    def from_lonlat(cls, lon: float, lat: float, radius_m: float) -> "AOI":
        cx, cy = lonlat_to_lv95(lon, lat)
        return cls(lon=lon, lat=lat, radius_m=radius_m, cx=cx, cy=cy)

    @property
    def circle_lv95(self) -> Polygon:
        """Circular AOI as an LV95 polygon (64-gon)."""
        return Point(self.cx, self.cy).buffer(self.radius_m, quad_segs=16)

    @property
    def bbox_lv95(self) -> tuple[float, float, float, float]:
        return self.circle_lv95.bounds

    @property
    def bbox_wgs(self) -> tuple[float, float, float, float]:
        """minlon, minlat, maxlon, maxlat for STAC / Overpass queries."""
        return lv95_polygon_to_wgs(self.circle_lv95).bounds
