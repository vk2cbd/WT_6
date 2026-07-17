#!/usr/bin/env python3
"""Safety primitives for WT6 antenna control.

The concrete drive guard logic currently lives in :mod:`wt6_antenna` so the
hardware-facing code and the safety checks remain synchronized. This module is
the public safety facade for new WT6 work.
"""

from __future__ import annotations

from wt6_antenna import (
    Axis,
    Direction,
    Position,
    SafetyError,
    SafetyLimits,
    clockwise_angle_delta,
    normalize_degrees,
    shortest_angle_delta,
)


def validate_pointing(limits: SafetyLimits, azimuth: float, elevation: float) -> None:
    """Raise :class:`SafetyError` if a requested pointing is outside limits."""
    limits.assert_position_allowed(azimuth, elevation)


def azimuth_drive_error(limits: SafetyLimits, current_azimuth: float, target_azimuth: float) -> float:
    """Return the safe signed azimuth delta for the configured allowed arc."""
    return limits.azimuth_delta_to_target(current_azimuth, target_azimuth)




