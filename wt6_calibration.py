#!/usr/bin/env python3
"""Calibration maths for WT6."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class GaussianFit:
    amplitude: float
    center: float
    sigma: float
    baseline: float
    slope: float

    def value_at(self, offset_degrees: float) -> float:
        sigma = max(abs(self.sigma), 1.0e-9)
        return (
            self.baseline
            + self.slope * offset_degrees
            + self.amplitude * math.exp(-0.5 * ((offset_degrees - self.center) / sigma) ** 2)
        )


def weighted_peak_estimate(points: Iterable[tuple[float, float]]) -> GaussianFit:
    """Estimate a peak with a Gaussian-like center and linear baseline.

    This lightweight fallback is intentionally dependency-free. The GUI scan
    plot can use richer fitting when scipy is available, but this gives tests
    and headless use a stable calibration primitive.
    """
    data = sorted((float(x), float(y)) for x, y in points)
    if len(data) < 3:
        raise ValueError("at least three scan points are required")
    first_x, first_y = data[0]
    last_x, last_y = data[-1]
    slope = (last_y - first_y) / (last_x - first_x) if last_x != first_x else 0.0
    baseline_at_zero = first_y - slope * first_x
    corrected = [(x, y - (baseline_at_zero + slope * x)) for x, y in data]
    floor = min(y for _x, y in corrected)
    weights = [(x, max(0.0, y - floor)) for x, y in corrected]
    total_weight = sum(weight for _x, weight in weights)
    if total_weight <= 0.0:
        peak_x, peak_y = max(data, key=lambda item: item[1])
        return GaussianFit(0.0, peak_x, max((last_x - first_x) / 6.0, 0.1), peak_y, slope)
    center = sum(x * weight for x, weight in weights) / total_weight
    variance = sum(weight * (x - center) ** 2 for x, weight in weights) / total_weight
    sigma = math.sqrt(max(variance, 1.0e-6))
    amplitude = max(y for _x, y in corrected) - floor
    return GaussianFit(amplitude, center, sigma, baseline_at_zero, slope)




