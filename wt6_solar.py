#!/usr/bin/env python3
"""Small solar position helper for WT6.

The calculation follows the common NOAA solar position approximation. It is
adequate for initial antenna tracking tests without adding a heavy dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class SunPosition:
    azimuth: float
    elevation: float


@dataclass(frozen=True)
class SunEquatorialPosition:
    ra_hours: float
    dec_degrees: float


def sun_position(latitude: float, longitude: float, when: Optional[datetime] = None) -> SunPosition:
    if when is None:
        when = datetime.now(timezone.utc)
    when = when.astimezone(timezone.utc)
    equatorial = sun_equatorial(when)

    jd = _julian_day(when)
    centuries = (jd - 2451545.0) / 36525.0
    geom_mean_long = _wrap_degrees(280.46646 + centuries * (36000.76983 + centuries * 0.0003032))
    geom_mean_anom = 357.52911 + centuries * (35999.05029 - 0.0001537 * centuries)
    eccent = 0.016708634 - centuries * (0.000042037 + 0.0000001267 * centuries)

    sun_eq_center = (
        math.sin(_rad(geom_mean_anom)) * (1.914602 - centuries * (0.004817 + 0.000014 * centuries))
        + math.sin(_rad(2.0 * geom_mean_anom)) * (0.019993 - 0.000101 * centuries)
        + math.sin(_rad(3.0 * geom_mean_anom)) * 0.000289
    )
    sun_true_long = geom_mean_long + sun_eq_center
    omega = 125.04 - 1934.136 * centuries
    sun_app_long = sun_true_long - 0.00569 - 0.00478 * math.sin(_rad(omega))

    mean_obliq = 23.0 + (26.0 + ((21.448 - centuries * (46.815 + centuries * (0.00059 - centuries * 0.001813)))) / 60.0) / 60.0
    obliq_corr = mean_obliq + 0.00256 * math.cos(_rad(omega))

    var_y = math.tan(_rad(obliq_corr / 2.0)) ** 2
    equation_time = 4.0 * _deg(
        var_y * math.sin(2.0 * _rad(geom_mean_long))
        - 2.0 * eccent * math.sin(_rad(geom_mean_anom))
        + 4.0 * eccent * var_y * math.sin(_rad(geom_mean_anom)) * math.cos(2.0 * _rad(geom_mean_long))
        - 0.5 * var_y * var_y * math.sin(4.0 * _rad(geom_mean_long))
        - 1.25 * eccent * eccent * math.sin(2.0 * _rad(geom_mean_anom))
    )

    minutes = when.hour * 60.0 + when.minute + when.second / 60.0 + when.microsecond / 60000000.0
    true_solar_time = (minutes + equation_time + 4.0 * longitude) % 1440.0
    hour_angle = true_solar_time / 4.0 - 180.0
    if hour_angle < -180.0:
        hour_angle += 360.0

    lat_rad = _rad(latitude)
    decl_rad = _rad(equatorial.dec_degrees)
    hour_rad = _rad(hour_angle)

    cos_zenith = (
        math.sin(lat_rad) * math.sin(decl_rad)
        + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(hour_rad)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = _deg(math.acos(cos_zenith))
    elevation = 90.0 - zenith

    azimuth = _deg(
        math.atan2(
            math.sin(hour_rad),
            math.cos(hour_rad) * math.sin(lat_rad) - math.tan(decl_rad) * math.cos(lat_rad),
        )
    )
    azimuth = _wrap_degrees(azimuth + 180.0)
    return SunPosition(azimuth=azimuth, elevation=elevation)


def sun_equatorial(when: Optional[datetime] = None) -> SunEquatorialPosition:
    if when is None:
        when = datetime.now(timezone.utc)
    when = when.astimezone(timezone.utc)

    jd = _julian_day(when)
    centuries = (jd - 2451545.0) / 36525.0
    geom_mean_long = _wrap_degrees(280.46646 + centuries * (36000.76983 + centuries * 0.0003032))
    geom_mean_anom = 357.52911 + centuries * (35999.05029 - 0.0001537 * centuries)
    sun_eq_center = (
        math.sin(_rad(geom_mean_anom)) * (1.914602 - centuries * (0.004817 + 0.000014 * centuries))
        + math.sin(_rad(2.0 * geom_mean_anom)) * (0.019993 - 0.000101 * centuries)
        + math.sin(_rad(3.0 * geom_mean_anom)) * 0.000289
    )
    sun_true_long = geom_mean_long + sun_eq_center
    omega = 125.04 - 1934.136 * centuries
    sun_app_long = sun_true_long - 0.00569 - 0.00478 * math.sin(_rad(omega))
    mean_obliq = 23.0 + (26.0 + ((21.448 - centuries * (46.815 + centuries * (0.00059 - centuries * 0.001813)))) / 60.0) / 60.0
    obliq_corr = mean_obliq + 0.00256 * math.cos(_rad(omega))
    ra_hours = _wrap_degrees(
        _deg(math.atan2(math.cos(_rad(obliq_corr)) * math.sin(_rad(sun_app_long)), math.cos(_rad(sun_app_long))))
    ) / 15.0
    declination = _deg(math.asin(math.sin(_rad(obliq_corr)) * math.sin(_rad(sun_app_long))))
    return SunEquatorialPosition(ra_hours=ra_hours, dec_degrees=declination)


def _julian_day(when: datetime) -> float:
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




