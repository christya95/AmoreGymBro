"""Read-only Garmin Personal API integration; no credential discovery or storage."""

from .client import GarminClient, GarminError

__all__ = ["GarminClient", "GarminError"]
