#!/usr/bin/env python3
"""Astronomical position helpers for WT6."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class TargetPosition:
    name: str
    azimuth: float
    elevation: float


@dataclass(frozen=True)
class EquatorialPosition:
    ra_hours: float
    dec_degrees: float


def source_position(
    name: str,
    ra_hours: float,
    dec_degrees: float,
    latitude: float,
    longitude: float,
    when: Optional[datetime] = None,
) -> TargetPosition:
    azimuth, elevation = equatorial_to_horizontal(ra_hours, dec_degrees, latitude, longitude, when)
    return TargetPosition(name=name, azimuth=azimuth, elevation=elevation)


def moon_position(latitude: float, longitude: float, when: Optional[datetime] = None) -> TargetPosition:
    """Return topocentric Moon azimuth/elevation.

    This uses a compact lunar model with the largest evection, variation,
    annual-equation, and node corrections, then applies a topocentric parallax
    correction. It is intended for antenna pointing without an online ephemeris.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    when = when.astimezone(timezone.utc)
    equatorial, distance = moon_equatorial(when)

    azimuth, elevation = equatorial_to_horizontal(
        equatorial.ra_hours,
        equatorial.dec_degrees,
        latitude,
        longitude,
        when,
    )
    parallax = _deg(math.asin(1.0 / distance))
    elevation -= parallax * math.cos(_rad(elevation))
    return TargetPosition(name="Moon", azimuth=azimuth, elevation=elevation)


def moon_equatorial(when: Optional[datetime] = None) -> tuple[EquatorialPosition, float]:
    if when is None:
        when = datetime.now(timezone.utc)
    when = when.astimezone(timezone.utc)
    jd = julian_day(when)
    days = jd - 2451543.5

    node = _wrap_degrees(125.1228 - 0.0529538083 * days)
    inclination = 5.1454
    arg_perigee = _wrap_degrees(318.0634 + 0.1643573223 * days)
    semi_major_axis = 60.2666
    eccentricity = 0.054900
    mean_anomaly = _wrap_degrees(115.3654 + 13.0649929509 * days)

    eccentric_anomaly = mean_anomaly + _deg(eccentricity * math.sin(_rad(mean_anomaly)) * (1.0 + eccentricity * math.cos(_rad(mean_anomaly))))
    xv = semi_major_axis * (math.cos(_rad(eccentric_anomaly)) - eccentricity)
    yv = semi_major_axis * math.sqrt(1.0 - eccentricity * eccentricity) * math.sin(_rad(eccentric_anomaly))
    true_anomaly = _wrap_degrees(_deg(math.atan2(yv, xv)))
    distance = math.sqrt(xv * xv + yv * yv)

    lon = _wrap_degrees(true_anomaly + arg_perigee)
    xh = distance * (
        math.cos(_rad(node)) * math.cos(_rad(lon))
        - math.sin(_rad(node)) * math.sin(_rad(lon)) * math.cos(_rad(inclination))
    )
    yh = distance * (
        math.sin(_rad(node)) * math.cos(_rad(lon))
        + math.cos(_rad(node)) * math.sin(_rad(lon)) * math.cos(_rad(inclination))
    )
    zh = distance * math.sin(_rad(lon)) * math.sin(_rad(inclination))

    ecliptic_lon = _wrap_degrees(_deg(math.atan2(yh, xh)))
    ecliptic_lat = _deg(math.atan2(zh, math.sqrt(xh * xh + yh * yh)))

    sun_mean_anomaly = _wrap_degrees(356.0470 + 0.9856002585 * days)
    sun_mean_longitude = _wrap_degrees(280.460 + 0.9856474 * days)
    moon_mean_longitude = _wrap_degrees(node + arg_perigee + mean_anomaly)
    elongation = _wrap_degrees(moon_mean_longitude - sun_mean_longitude)
    argument_latitude = _wrap_degrees(moon_mean_longitude - node)

    ecliptic_lon += (
        -1.274 * math.sin(_rad(mean_anomaly - 2.0 * elongation))
        + 0.658 * math.sin(_rad(2.0 * elongation))
        - 0.186 * math.sin(_rad(sun_mean_anomaly))
        - 0.059 * math.sin(_rad(2.0 * mean_anomaly - 2.0 * elongation))
        - 0.057 * math.sin(_rad(mean_anomaly - 2.0 * elongation + sun_mean_anomaly))
        + 0.053 * math.sin(_rad(mean_anomaly + 2.0 * elongation))
        + 0.046 * math.sin(_rad(2.0 * elongation - sun_mean_anomaly))
        + 0.041 * math.sin(_rad(mean_anomaly - sun_mean_anomaly))
        - 0.035 * math.sin(_rad(elongation))
        - 0.031 * math.sin(_rad(mean_anomaly + sun_mean_anomaly))
        - 0.015 * math.sin(_rad(2.0 * argument_latitude - 2.0 * elongation))
        + 0.011 * math.sin(_rad(mean_anomaly - 4.0 * elongation))
    )
    ecliptic_lat += (
        -0.173 * math.sin(_rad(argument_latitude - 2.0 * elongation))
        - 0.055 * math.sin(_rad(mean_anomaly - argument_latitude - 2.0 * elongation))
        - 0.046 * math.sin(_rad(mean_anomaly + argument_latitude - 2.0 * elongation))
        + 0.033 * math.sin(_rad(argument_latitude + 2.0 * elongation))
        + 0.017 * math.sin(_rad(2.0 * mean_anomaly + argument_latitude))
    )
    distance += (
        -0.58 * math.cos(_rad(mean_anomaly - 2.0 * elongation))
        - 0.46 * math.cos(_rad(2.0 * elongation))
    )

    obliquity = 23.4393 - 3.563e-7 * days
    xe = math.cos(_rad(ecliptic_lon)) * math.cos(_rad(ecliptic_lat))
    ye = math.sin(_rad(ecliptic_lon)) * math.cos(_rad(ecliptic_lat))
    ze = math.sin(_rad(ecliptic_lat))
    xeq = xe
    yeq = ye * math.cos(_rad(obliquity)) - ze * math.sin(_rad(obliquity))
    zeq = ye * math.sin(_rad(obliquity)) + ze * math.cos(_rad(obliquity))
    ra = _wrap_degrees(_deg(math.atan2(yeq, xeq))) / 15.0
    dec = _deg(math.atan2(zeq, math.sqrt(xeq * xeq + yeq * yeq)))
    return EquatorialPosition(ra, dec), distance


def equatorial_to_horizontal(
    ra_hours: float,
    dec_degrees: float,
    latitude: float,
    longitude: float,
    when: Optional[datetime] = None,
) -> tuple[float, float]:
    if when is None:
        when = datetime.now(timezone.utc)
    when = when.astimezone(timezone.utc)

    lst = local_sidereal_time(longitude, when)
    hour_angle = _wrap_degrees(lst - ra_hours * 15.0)
    if hour_angle > 180.0:
        hour_angle -= 360.0

    lat_rad = _rad(latitude)
    dec_rad = _rad(dec_degrees)
    ha_rad = _rad(hour_angle)
    sin_alt = math.sin(dec_rad) * math.sin(lat_rad) + math.cos(dec_rad) * math.cos(lat_rad) * math.cos(ha_rad)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    elevation = _deg(math.asin(sin_alt))
    azimuth = _wrap_degrees(
        _deg(
            math.atan2(
                -math.sin(ha_rad),
                math.tan(dec_rad) * math.cos(lat_rad) - math.sin(lat_rad) * math.cos(ha_rad),
            )
        )
    )
    return azimuth, elevation


def local_sidereal_time(longitude: float, when: datetime) -> float:
    jd = julian_day(when)
    centuries = (jd - 2451545.0) / 36525.0
    gmst = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * centuries * centuries
        - centuries * centuries * centuries / 38710000.0
    )
    return _wrap_degrees(gmst + longitude)


def julian_day(when: datetime) -> float:
    when = when.astimezone(timezone.utc)
    year = when.year
    month = when.month
    day = when.day + (when.hour + (when.minute + (when.second + when.microsecond / 1000000.0) / 60.0) / 60.0) / 24.0
    if month <= 2:
        year -= 1
        month += 12
    a_val = math.floor(year / 100.0)
    b_val = 2 - a_val + math.floor(a_val / 4.0)
    return math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + b_val - 1524.5


def _wrap_degrees(value: float) -> float:
    value = math.fmod(value, 360.0)
    if value < 0:
        value += 360.0
    return value


def _rad(value: float) -> float:
    return math.radians(value)


def _deg(value: float) -> float:
    return math.degrees(value)




