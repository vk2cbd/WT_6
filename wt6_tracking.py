#!/usr/bin/env python3
"""Tracking target helpers for WT6."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from wt6_astro import TargetPosition, local_sidereal_time, moon_position, source_position
from wt6_config import SiteConfig, SourceConfig
from wt6_solar import sun_position


@dataclass(frozen=True)
class TrackingTarget:
    kind: str
    name: str
    position: TargetPosition


def compute_target(
    kind: str,
    site: SiteConfig,
    source: Optional[SourceConfig] = None,
    when: Optional[datetime] = None,
) -> TrackingTarget:
    """Compute Sun, Moon, or configured source az/el for the supplied site."""
    normalized = kind.strip().lower()
    if normalized == "sun":
        position = sun_position(site.latitude, site.longitude, when)
        return TrackingTarget("sun", "Sun", TargetPosition("Sun", position.azimuth, position.elevation, 0.0, 0.0))
    if normalized == "moon":
        position = moon_position(site.latitude, site.longitude, when)
        return TrackingTarget("moon", "Moon", position)
    if normalized == "source" and source is not None:
        position = source_position(source.name, source.ra_hours, source.dec_degrees, site.latitude, site.longitude, when)
        return TrackingTarget("source", source.name, position)
    raise ValueError("kind must be sun, moon, or source with a source record")


__all__ = ["TargetPosition", "TrackingTarget", "compute_target", "local_sidereal_time"]




