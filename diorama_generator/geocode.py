"""Free-form address -> (lat, lon) via Google's Geocoding API.

Uses geopy's ``GoogleV3`` backend, which calls the Google Maps Geocoding API, so
a standard Google Maps Platform key with the *Geocoding API* enabled works. The
key is read from the ``GOOGLE_MAPS_API_KEY`` environment variable (typically set
in a local ``.env`` file — see ``.env.example``).
"""

from __future__ import annotations

import os

GEOCODE_ENV_KEY = "GOOGLE_MAPS_API_KEY"


def geocode_address(address: str, *, region: str = "ch",
                    timeout: float = 10.0) -> tuple[float, float, str]:
    """Return ``(lat, lon, formatted_address)`` for a free-form address.

    ``region`` biases ambiguous results (defaults to Switzerland, since the
    pipeline only has Swiss data). Raises ``RuntimeError`` with an actionable
    message if the key is missing or the address cannot be resolved.
    """
    from geopy.exc import GeocoderAuthenticationFailure, GeocoderServiceError
    from geopy.geocoders import GoogleV3

    key = os.environ.get(GEOCODE_ENV_KEY)
    if not key:
        raise RuntimeError(
            f"No Google Maps API key found. Set {GEOCODE_ENV_KEY} in a local "
            f".env file (copy .env.example) or in the environment."
        )

    geolocator = GoogleV3(api_key=key, timeout=timeout)
    try:
        location = geolocator.geocode(address, region=region)
    except GeocoderAuthenticationFailure as exc:
        raise RuntimeError(
            f"Google rejected the API key ({GEOCODE_ENV_KEY}); check that the "
            f"key is valid and the Geocoding API is enabled: {exc}"
        ) from exc
    except GeocoderServiceError as exc:
        raise RuntimeError(f"Geocoding request failed: {exc}") from exc

    if location is None:
        raise RuntimeError(f"Could not geocode address: {address!r}")
    return location.latitude, location.longitude, location.address
