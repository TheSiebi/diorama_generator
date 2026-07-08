"""Shared geometry helpers for the road/water feature backends."""

from __future__ import annotations

from shapely.geometry import MultiPolygon, Polygon


def polys(geom):
    """Flatten a geometry to a list of shapely Polygons."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out.extend(polys(g))
        return out
    return []
