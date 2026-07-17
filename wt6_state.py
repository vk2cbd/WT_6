"""GUI-facing state model for WT6.

This module deliberately contains no Tkinter or hardware I/O. It gives the
controller code a single place to publish what the GUI should display, which is
the first step toward making the visual layer a replaceable skin over the
tracking/safety backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from threading import Lock
from typing import Callable, Optional


class AntennaRunState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    STOPPED = "STOPPED"
    SLEWING = "SLEWING"
    TRACKING = "TRACKING"
    SCANNING = "SCANNING"
    YFACTOR = "YFACTOR"
    PARKING = "PARKING"
    PARKED = "PARKED"
    FAULT = "FAULT"
    OFFLINE = "OFFLINE"


class SystemRunState(str, Enum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    TRACKING = "TRACKING"
    SCANNING = "SCANNING"
    YFACTOR = "YFACTOR"
    PARKING = "PARKING"
    FAULT = "FAULT"
    STOPPED = "STOPPED"


class PowerRunState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    WARMING = "WARMING"
    READY = "READY"
    RUNNING = "RUNNING"
    FAULT = "FAULT"


@dataclass(frozen=True)
class AntennaViewState:
    name: str
    run_state: AntennaRunState = AntennaRunState.DISCONNECTED
    azimuth: Optional[float] = None
    elevation: Optional[float] = None
    fault: str = ""


@dataclass(frozen=True)
class TargetViewState:
    name: str = "Target --"
    azimuth: Optional[float] = None
    elevation: Optional[float] = None
    hour_angle: str = "HA --"


@dataclass(frozen=True)
class PowerViewState:
    run_state: PowerRunState = PowerRunState.STOPPED
    value: Optional[float] = None
    unit: str = "dBFS"
    calibrated: bool = False
    extrapolated: bool = False
    message: str = "Stopped"


@dataclass(frozen=True)
class AppViewState:
    system_state: SystemRunState = SystemRunState.IDLE
    status: str = ""
    target: TargetViewState = field(default_factory=TargetViewState)
    antennas: dict[str, AntennaViewState] = field(default_factory=dict)
    power: PowerViewState = field(default_factory=PowerViewState)


class AppStateStore:
    """Thread-safe store for the GUI view state."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._state = AppViewState()

    def snapshot(self) -> AppViewState:
        with self._lock:
            return replace(self._state, antennas=dict(self._state.antennas))

    def set_status(self, status: str, system_state: Optional[SystemRunState] = None) -> None:
        def change(state: AppViewState) -> AppViewState:
            return replace(state, status=status, system_state=system_state or state.system_state)

        self._mutate(change)

    def set_target(
        self,
        name: str,
        azimuth: Optional[float],
        elevation: Optional[float],
        hour_angle: str = "HA --",
    ) -> None:
        target = TargetViewState(name=name, azimuth=azimuth, elevation=elevation, hour_angle=hour_angle)
        self._mutate(lambda state: replace(state, target=target))

    def set_antenna_state(self, name: str, run_state: AntennaRunState, fault: Optional[str] = None) -> None:
        def change(state: AppViewState) -> AppViewState:
            antennas = dict(state.antennas)
            current = antennas.get(name, AntennaViewState(name=name))
            antennas[name] = replace(current, run_state=run_state, fault=current.fault if fault is None else fault)
            return replace(state, antennas=antennas)

        self._mutate(change)

    def set_antenna_position(self, name: str, azimuth: float, elevation: float) -> None:
        def change(state: AppViewState) -> AppViewState:
            antennas = dict(state.antennas)
            current = antennas.get(name, AntennaViewState(name=name))
            antennas[name] = replace(current, azimuth=azimuth, elevation=elevation)
            return replace(state, antennas=antennas)

        self._mutate(change)

    def set_power(
        self,
        run_state: Optional[PowerRunState] = None,
        value: Optional[float] = None,
        unit: Optional[str] = None,
        calibrated: Optional[bool] = None,
        extrapolated: Optional[bool] = None,
        message: Optional[str] = None,
    ) -> None:
        def change(state: AppViewState) -> AppViewState:
            power = state.power
            return replace(
                state,
                power=replace(
                    power,
                    run_state=run_state or power.run_state,
                    value=power.value if value is None else value,
                    unit=power.unit if unit is None else unit,
                    calibrated=power.calibrated if calibrated is None else calibrated,
                    extrapolated=power.extrapolated if extrapolated is None else extrapolated,
                    message=power.message if message is None else message,
                ),
            )

        self._mutate(change)

    def reset_power(self, message: str = "Stopped") -> None:
        self._mutate(lambda state: replace(state, power=PowerViewState(message=message)))

    def _mutate(self, change: Callable[[AppViewState], AppViewState]) -> None:
        with self._lock:
            self._state = change(self._state)


def antenna_state_from_text(text: str) -> AntennaRunState:
    normalized = (text or "").strip().upper()
    mapping = {
        "CAL AZ": AntennaRunState.TRACKING,
        "CAL EL": AntennaRunState.TRACKING,
        "JOG": AntennaRunState.CONNECTED,
        "MANUAL": AntennaRunState.CONNECTED,
    }
    if normalized in mapping:
        return mapping[normalized]
    try:
        return AntennaRunState(normalized)
    except ValueError:
        if normalized.startswith("CAL "):
            return AntennaRunState.TRACKING
        return AntennaRunState.CONNECTED


