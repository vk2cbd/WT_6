#!/usr/bin/env python3
"""
WT6 hardware driver and safety model.

This file contains the decoded WinTrak Arduino protocol plus calibration and
software-limit helpers used by the GUI.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

try:
    import serial
except ImportError:
    serial = None


class WinTrakProtocolError(RuntimeError):
    """Raised when the controller replies with an unexpected value."""


class SafetyError(RuntimeError):
    """Raised when a requested move would violate software safety limits."""


class Axis(str, Enum):
    AZIMUTH = "azimuth"
    ELEVATION = "elevation"


class Direction(str, Enum):
    AZ_CW = "az-cw"
    AZ_CCW = "az-ccw"
    EL_UP = "el-up"
    EL_DOWN = "el-down"


@dataclass(frozen=True)
class AxisMap:
    prefix: int
    positive_channel: int
    negative_channel: int


@dataclass
class Calibration:
    az_offset: float = 0.0
    el_offset: float = 0.0

    def apply_az(self, raw_azimuth: float) -> float:
        return normalize_degrees(raw_azimuth + self.az_offset)

    def apply_el(self, raw_elevation: float) -> float:
        return raw_elevation + self.el_offset


@dataclass
class SafetyLimits:
    az_min: float = 270.0
    az_max: float = 265.0
    el_min: float = 0.0
    el_max: float = 87.0
    az_margin: float = 0.5
    el_margin: float = 0.5
    max_jog_seconds: float = 60.0
    poll_interval: float = 0.2

    def is_az_allowed(self, azimuth: float) -> bool:
        azimuth = normalize_degrees(azimuth)
        if self._az_full_circle():
            return True
        if self.az_min <= self.az_max:
            return self.az_min <= azimuth <= self.az_max
        return azimuth >= self.az_min or azimuth <= self.az_max

    def is_el_allowed(self, elevation: float) -> bool:
        return self.el_min <= elevation <= self.el_max

    def assert_position_allowed(self, azimuth: float, elevation: float) -> None:
        if not self.is_az_allowed(azimuth):
            raise SafetyError(f"Azimuth {azimuth:0.2f} outside limits {self.az_min:0.2f}..{self.az_max:0.2f}")
        if not self.is_el_allowed(elevation):
            raise SafetyError(f"Elevation {elevation:0.2f} outside limits {self.el_min:0.2f}..{self.el_max:0.2f}")

    def assert_move_allowed(self, direction: Direction, azimuth: float, elevation: float) -> None:
        self.assert_position_allowed(azimuth, elevation)

        if direction == Direction.EL_UP and elevation >= self.el_max - self.el_margin:
            raise SafetyError(f"Elevation {elevation:0.2f} too close to upper limit {self.el_max:0.2f}")
        if direction == Direction.EL_DOWN and elevation <= self.el_min + self.el_margin:
            raise SafetyError(f"Elevation {elevation:0.2f} too close to lower limit {self.el_min:0.2f}")

        if self._az_full_circle():
            return

        azimuth = normalize_degrees(azimuth)
        if self.az_min <= self.az_max:
            if direction == Direction.AZ_CW and azimuth >= self.az_max - self.az_margin:
                raise SafetyError(f"Azimuth {azimuth:0.2f} too close to CW limit {self.az_max:0.2f}")
            if direction == Direction.AZ_CCW and azimuth <= self.az_min + self.az_margin:
                raise SafetyError(f"Azimuth {azimuth:0.2f} too close to CCW limit {self.az_min:0.2f}")
        else:
            if direction == Direction.AZ_CW and azimuth <= self.az_max and azimuth >= self.az_max - self.az_margin:
                raise SafetyError(f"Azimuth {azimuth:0.2f} too close to CW wrap limit {self.az_max:0.2f}")
            if direction == Direction.AZ_CCW and azimuth >= self.az_min and azimuth <= self.az_min + self.az_margin:
                raise SafetyError(f"Azimuth {azimuth:0.2f} too close to CCW wrap limit {self.az_min:0.2f}")

    def azimuth_delta_to_target(self, current_azimuth: float, target_azimuth: float) -> float:
        """Return signed AZ delta using only the configured allowed azimuth arc."""
        current_azimuth = normalize_degrees(current_azimuth)
        target_azimuth = normalize_degrees(target_azimuth)
        if not self.is_az_allowed(current_azimuth):
            raise SafetyError(f"Azimuth {current_azimuth:0.2f} outside limits {self.az_min:0.2f}..{self.az_max:0.2f}")
        if not self.is_az_allowed(target_azimuth):
            raise SafetyError(f"Target azimuth {target_azimuth:0.2f} outside limits {self.az_min:0.2f}..{self.az_max:0.2f}")
        if self._az_full_circle():
            return shortest_angle_delta(current_azimuth, target_azimuth)

        start = normalize_degrees(self.az_min)
        current_offset = clockwise_angle_delta(start, current_azimuth)
        target_offset = clockwise_angle_delta(start, target_azimuth)
        if target_offset >= current_offset:
            return target_offset - current_offset
        return -(current_offset - target_offset)

    def _az_full_circle(self) -> bool:
        return abs(self.az_max - self.az_min) >= 360.0


@dataclass
class AntennaConfig:
    name: str
    port: str
    baud: int = 9600
    open_delay: float = 5.0
    gui_speed: int = 40
    az_track_speed: int = 40
    el_track_speed: int = 40
    az_low_to_high_compensation: float = 0.0
    park_az: float = 355.0
    park_el: float = 80.0
    calibration: Calibration = None
    limits: SafetyLimits = None

    def __post_init__(self) -> None:
        if self.calibration is None:
            self.calibration = Calibration()
        if self.limits is None:
            self.limits = SafetyLimits()


@dataclass
class Position:
    raw_azimuth: float
    raw_elevation: float
    azimuth: float
    elevation: float


@dataclass
class EncoderInfo:
    axis: Axis
    address: int
    encoder_type: str
    model: int
    version: str
    config: str
    serial: int
    date: str
    resolution: int
    position: float
    mode: int
    range_count: int
    pulses_per_revolution: int
    index_mode: int


AXIS_MAPS = {
    Axis.AZIMUTH: AxisMap(prefix=0xF0, positive_channel=0x04, negative_channel=0x03),
    Axis.ELEVATION: AxisMap(prefix=0xF1, positive_channel=0x01, negative_channel=0x02),
}


class WinTrakController:
    """Low-level serial controller for the decoded WinTrak Arduino protocol."""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 0.5,
        write_timeout: float = 0.5,
        open_delay: float = 2.5,
        read_retries: int = 1,
        retry_delay: float = 0.05,
        trace: Optional[Callable[[str, bytes], None]] = None,
    ) -> None:
        if serial is None:
            raise RuntimeError("pyserial is required. Install with: python3 -m pip install pyserial")
        self.serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            write_timeout=write_timeout,
        )
        self.trace = trace
        self.read_retries = max(0, int(read_retries))
        self.retry_delay = max(0.0, float(retry_delay))
        if open_delay > 0:
            time.sleep(open_delay)
            self.serial.reset_input_buffer()

    def close(self) -> None:
        self.serial.close()

    def initialize(self) -> None:
        self._write_read(bytes([0x6F]), 0)
        self._write_read(bytes([0xF0, 0x09]), 3)
        self._write_read(bytes([0xF0, 0x0B]), 2)
        self._write_read(bytes([0xF0, 0x28]), 5)
        self.read_azimuth()
        self._write_read(bytes([0xF1, 0x09]), 3)
        self._write_read(bytes([0xF1, 0x0B]), 2)
        self._write_read(bytes([0xF1, 0x28]), 5)
        self.read_elevation()

    def read_azimuth(self) -> float:
        return self._read_angle(bytes([0x10]))

    def read_elevation(self) -> float:
        return self._read_angle(bytes([0x11]))

    def read_encoder_info(self, axis: Axis) -> EncoderInfo:
        prefix = AXIS_MAPS[axis].prefix
        info = self._write_read(bytes([prefix, 0x08]), 15)
        resolution = int.from_bytes(self._write_read(bytes([prefix, 0x09]), 3)[:2], byteorder="big", signed=False)
        mode = self._write_read(bytes([prefix, 0x0B]), 2)[0]
        range_count = int.from_bytes(self._write_read(bytes([prefix, 0x28]), 5)[:4], byteorder="big", signed=False)
        pulses_reply = self._write_read(bytes([prefix, 0x2A]), 6)
        return EncoderInfo(
            axis=axis,
            address=info[0],
            encoder_type="SEI",
            model=info[1],
            version=f"{info[2]}.{info[3]:02d}",
            config=f"{info[4]}.{info[5]:02d}",
            serial=int.from_bytes(info[6:10], byteorder="big", signed=False),
            date=f"{info[10]:02d}-{info[11]:02d}-{int.from_bytes(info[12:14], byteorder='big', signed=False)}",
            resolution=resolution,
            position=self.read_axis_position(axis),
            mode=mode,
            range_count=range_count,
            pulses_per_revolution=int.from_bytes(pulses_reply[:4], byteorder="big", signed=False),
            index_mode=pulses_reply[4],
        )

    def read_axis_position(self, axis: Axis) -> float:
        if axis == Axis.AZIMUTH:
            return self.read_azimuth()
        return self.read_elevation()

    def set_axis_position(self, axis: Axis, position_degrees: float) -> float:
        position_counts = int(round(position_degrees * 100.0))
        if not (0 <= position_counts <= 0xFFFF):
            raise ValueError("Position must encode into 0..655.35 degrees.")
        prefix = AXIS_MAPS[axis].prefix
        payload = position_counts.to_bytes(2, byteorder="big", signed=False)
        self._write_read(bytes([prefix, 0x02]) + payload, 1)
        return self.read_axis_position(axis)

    def azimuth_cw(self, speed: int = 100) -> None:
        self._move(Axis.AZIMUTH, AXIS_MAPS[Axis.AZIMUTH].positive_channel, speed)

    def azimuth_ccw(self, speed: int = 100) -> None:
        self._move(Axis.AZIMUTH, AXIS_MAPS[Axis.AZIMUTH].negative_channel, speed)

    def elevation_up(self, speed: int = 100) -> None:
        self._move(Axis.ELEVATION, AXIS_MAPS[Axis.ELEVATION].positive_channel, speed)

    def elevation_down(self, speed: int = 100) -> None:
        self._move(Axis.ELEVATION, AXIS_MAPS[Axis.ELEVATION].negative_channel, speed)

    def stop_azimuth(self) -> None:
        self._stop_axis(Axis.AZIMUTH)

    def stop_elevation(self) -> None:
        self._stop_axis(Axis.ELEVATION)

    def stop_all(self) -> None:
        for axis in (Axis.ELEVATION, Axis.AZIMUTH):
            mapping = AXIS_MAPS[axis]
            self._set_enable(axis, mapping.positive_channel, 0)
            self._set_enable(axis, mapping.negative_channel, 0)
        for axis in (Axis.ELEVATION, Axis.AZIMUTH):
            mapping = AXIS_MAPS[axis]
            self._set_speed(axis, mapping.positive_channel, 0)
            self._set_speed(axis, mapping.negative_channel, 0)

    def oled_write(self, prefix: int, column: int, row: int, text: str, width: Optional[int] = None) -> None:
        if width is not None:
            text = text[:width].ljust(width)
        payload = text.encode("ascii", errors="replace") + b"\x00"
        command = bytes([prefix, 0x35, column & 0xFF, row & 0xFF, len(payload) & 0xFF]) + payload
        self._send_ack(command)

    def oled_status(
        self,
        label: str,
        position: Position,
        mode: str,
        fault: str = "",
        target_azimuth: Optional[float] = None,
        target_elevation: Optional[float] = None,
        activity: str = "",
    ) -> None:
        state = fault[:7].upper() if fault else "SAFE"
        self.oled_write(0xF0, 0, 0, label.upper(), width=8)
        self.oled_write(0xF0, 10, 0, state, width=6)
        self.oled_write(0xF0, 0, 1, "AZ ", width=3)
        self.oled_write(0xF1, 0, 2, "EL ", width=3)
        self.oled_write(0xF0, 3, 1, f"{position.azimuth:6.2f}", width=6)
        self.oled_write(0xF1, 3, 2, f"{position.elevation:6.2f}", width=6)
        self.oled_write(0xF0, 0, 3, activity.upper(), width=8)
        self.oled_write(0xF0, 0, 5, mode.upper(), width=8)
        self.oled_write(0xF0, 0, 6, "AZ", width=2)
        self.oled_write(0xF1, 0, 7, "EL", width=2)
        self.oled_write(0xF0, 3, 6, f"{(target_azimuth if target_azimuth is not None else position.azimuth):6.2f}", width=6)
        self.oled_write(0xF1, 3, 7, f"{(target_elevation if target_elevation is not None else position.elevation):6.2f}", width=6)

    def oled_position(
        self,
        position: Position,
        target_azimuth: Optional[float] = None,
        target_elevation: Optional[float] = None,
        activity: Optional[str] = None,
    ) -> None:
        self.oled_write(0xF0, 3, 1, f"{position.azimuth:6.2f}", width=6)
        self.oled_write(0xF1, 3, 2, f"{position.elevation:6.2f}", width=6)
        if activity is not None:
            self.oled_write(0xF0, 0, 3, activity.upper(), width=8)
        self.oled_write(0xF0, 3, 6, f"{(target_azimuth if target_azimuth is not None else position.azimuth):6.2f}", width=6)
        self.oled_write(0xF1, 3, 7, f"{(target_elevation if target_elevation is not None else position.elevation):6.2f}", width=6)

    def oled_activity(self, activity: str) -> None:
        self.oled_write(0xF0, 0, 3, activity.upper(), width=8)

    def _move(self, axis: Axis, channel: int, speed: int) -> None:
        mapping = AXIS_MAPS[axis]
        self._set_speed(axis, mapping.positive_channel, 0)
        self._set_speed(axis, mapping.negative_channel, 0)
        self._set_speed(axis, channel, clamp_speed(speed))
        self._set_enable(axis, channel, 1)

    def _stop_axis(self, axis: Axis) -> None:
        mapping = AXIS_MAPS[axis]
        self._set_enable(axis, mapping.positive_channel, 0)
        self._set_enable(axis, mapping.negative_channel, 0)
        self._set_speed(axis, mapping.positive_channel, 0)
        self._set_speed(axis, mapping.negative_channel, 0)

    def _set_enable(self, axis: Axis, channel: int, enabled: int) -> None:
        self._send_ack(bytes([AXIS_MAPS[axis].prefix, 0x32, channel, 0x01 if enabled else 0x00]))

    def _set_speed(self, axis: Axis, channel: int, speed: int) -> None:
        self._send_ack(bytes([AXIS_MAPS[axis].prefix, 0x33, channel, clamp_speed(speed)]))

    def _send_ack(self, command: bytes) -> None:
        reply = self._write_read(command, 1)
        if reply != b"\x00":
            raise WinTrakProtocolError(f"Expected ACK 00 for {command.hex(' ')}, got {reply.hex(' ')}")

    def _read_angle(self, command: bytes) -> float:
        reply = self._write_read(command, 2)
        if len(reply) != 2:
            raise WinTrakProtocolError(f"Expected 2-byte angle, got {reply.hex(' ')}")
        return int.from_bytes(reply, byteorder="big", signed=False) / 100.0

    def _write_read(self, command: bytes, reply_length: int) -> bytes:
        if reply_length == 0:
            self.serial.reset_input_buffer()
            if self.trace:
                self.trace("TX", command)
            self.serial.write(command)
            self.serial.flush()
            return b""

        last_reply = b""
        attempts = self.read_retries + 1
        for attempt in range(attempts):
            self.serial.reset_input_buffer()
            if self.trace:
                self.trace("TX", command)
            self.serial.write(command)
            self.serial.flush()
            reply = self.serial.read(reply_length)
            if self.trace:
                self.trace("RX", reply)
            if len(reply) == reply_length:
                return reply
            last_reply = reply
            if attempt < attempts - 1 and self.retry_delay > 0:
                time.sleep(self.retry_delay)

        raise WinTrakProtocolError(
            f"Command {command.hex(' ')} expected {reply_length} byte(s), got {len(last_reply)} "
            f"after {attempts} attempt(s): {last_reply.hex(' ')}"
        )


class SafeAntenna:
    """Thread-safe high-level antenna wrapper with calibration and limits."""

    def __init__(self, config: AntennaConfig, motion_logger: Optional[Callable[[str, object], None]] = None) -> None:
        self.config = config
        self.motion_logger = motion_logger
        self.controller = WinTrakController(config.port, baudrate=config.baud, open_delay=config.open_delay)
        self.lock = threading.RLock()
        self.last_position: Optional[Position] = None
        self.fault = ""
        with self.lock:
            self.controller.initialize()
            self.last_position = self.read_position_locked()
            self.config.limits.assert_position_allowed(self.last_position.azimuth, self.last_position.elevation)

    def _motion_event(self, event: str, **fields: object) -> None:
        if self.motion_logger is None:
            return
        try:
            self.motion_logger(event, fields)
        except Exception:
            pass

    def close(self) -> None:
        with self.lock:
            try:
                self.controller.stop_all()
            finally:
                self.controller.close()

    def read_position(self) -> Position:
        with self.lock:
            return self.read_position_locked()

    def read_position_locked(self) -> Position:
        raw_az = self.controller.read_azimuth()
        raw_el = self.controller.read_elevation()
        pos = Position(
            raw_azimuth=raw_az,
            raw_elevation=raw_el,
            azimuth=self.config.calibration.apply_az(raw_az),
            elevation=self.config.calibration.apply_el(raw_el),
        )
        self.config.limits.assert_position_allowed(pos.azimuth, pos.elevation)
        self.last_position = pos
        self.fault = ""
        return pos

    def calibrate(self, actual_azimuth: float, actual_elevation: float) -> Position:
        with self.lock:
            raw_az = self.controller.read_azimuth()
            raw_el = self.controller.read_elevation()
            self.config.calibration.az_offset = shortest_angle_delta(raw_az, actual_azimuth)
            self.config.calibration.el_offset = actual_elevation - raw_el
            return self.read_position_locked()

    def calibrate_axis(self, axis: Axis, actual_degrees: float) -> Position:
        with self.lock:
            if axis == Axis.AZIMUTH:
                raw_az = self.controller.read_azimuth()
                self.config.calibration.az_offset = shortest_angle_delta(raw_az, actual_degrees)
            else:
                raw_el = self.controller.read_elevation()
                self.config.calibration.el_offset = actual_degrees - raw_el
            return self.read_position_locked()

    def scan_encoders(self) -> dict[Axis, EncoderInfo]:
        with self.lock:
            return {
                Axis.AZIMUTH: self.controller.read_encoder_info(Axis.AZIMUTH),
                Axis.ELEVATION: self.controller.read_encoder_info(Axis.ELEVATION),
            }

    def set_encoder_position(self, axis: Axis, position_degrees: float) -> Position:
        with self.lock:
            self.controller.stop_all()
            readback = self.controller.set_axis_position(axis, position_degrees)
            if abs(readback - position_degrees) > 0.01:
                raise WinTrakProtocolError(f"Set {axis.value} to {position_degrees:0.2f}, read back {readback:0.2f}")
            if axis == Axis.AZIMUTH:
                self.config.calibration.az_offset = 0.0
            else:
                self.config.calibration.el_offset = 0.0
            return self.read_position_locked()

    def guarded_jog(
        self,
        direction: Direction,
        speed: int,
        seconds: Optional[float],
        stop_event: threading.Event,
        update_callback: Optional[Callable[[Position], None]] = None,
    ) -> None:
        deadline = time.monotonic() + self.config.limits.max_jog_seconds
        if seconds is not None:
            seconds = min(max(0.0, seconds), self.config.limits.max_jog_seconds)
            deadline = time.monotonic() + seconds
        axis = Axis.AZIMUTH if direction in (Direction.AZ_CW, Direction.AZ_CCW) else Axis.ELEVATION
        try:
            with self.lock:
                pos = self.read_position_locked()
                self.config.limits.assert_move_allowed(direction, pos.azimuth, pos.elevation)
                self._start_direction(direction, speed)

            while not stop_event.is_set() and time.monotonic() < deadline:
                time.sleep(self.config.limits.poll_interval)
                with self.lock:
                    pos = self.read_position_locked()
                    self.config.limits.assert_move_allowed(direction, pos.azimuth, pos.elevation)
                if update_callback:
                    update_callback(pos)
            if not stop_event.is_set() and seconds is None:
                raise SafetyError(f"Maximum held-jog time {self.config.limits.max_jog_seconds:0.1f}s reached")
        except Exception as exc:
            self.fault = str(exc)
            self._motion_event(
                "SLEW_EXCEPTION",
                error=str(exc),
                target_az=target_azimuth,
                target_el=target_elevation,
                last_az=self.last_position.azimuth if self.last_position else None,
                last_el=self.last_position.elevation if self.last_position else None,
            )
            raise
        finally:
            with self.lock:
                if axis == Axis.AZIMUTH:
                    self.controller.stop_azimuth()
                else:
                    self.controller.stop_elevation()

    def guarded_slew_to(
        self,
        target_azimuth: float,
        target_elevation: float,
        az_speed: int,
        el_speed: int,
        stop_event: threading.Event,
        az_start_tolerance: float = 0.5,
        el_start_tolerance: float = 0.5,
        az_stop_tolerance: Optional[float] = None,
        el_stop_tolerance: Optional[float] = None,
        az_slow_speed: Optional[int] = None,
        el_slow_speed: Optional[int] = None,
        az_slow_threshold: float = 3.0,
        el_slow_threshold: float = 3.0,
        update_callback: Optional[Callable[[Position], None]] = None,
        target_callback: Optional[Callable[[Position], tuple[float, float]]] = None,
    ) -> Position:
        target_azimuth = normalize_degrees(target_azimuth)
        az_start_tolerance = max(0.01, abs(float(az_start_tolerance)))
        el_start_tolerance = max(0.01, abs(float(el_start_tolerance)))
        az_stop_tolerance = _clamp_signed_stop_tolerance(az_stop_tolerance, az_start_tolerance)
        el_stop_tolerance = _clamp_signed_stop_tolerance(el_stop_tolerance, el_start_tolerance)
        az_slow_threshold = max(az_start_tolerance, float(az_slow_threshold))
        el_slow_threshold = max(el_start_tolerance, float(el_slow_threshold))
        az_fast_speed = clamp_speed(az_speed)
        el_fast_speed = clamp_speed(el_speed)
        az_slow_speed = clamp_speed(az_fast_speed if az_slow_speed is None else az_slow_speed)
        el_slow_speed = clamp_speed(el_fast_speed if el_slow_speed is None else el_slow_speed)
        self.config.limits.assert_position_allowed(target_azimuth, target_elevation)

        pos = self.read_position()
        az_error = self.config.limits.azimuth_delta_to_target(pos.azimuth, target_azimuth)
        el_error = target_elevation - pos.elevation
        active: dict[Axis, dict[str, object]] = {}
        if abs(az_error) > az_start_tolerance:
            active[Axis.AZIMUTH] = {
                "direction": Direction.AZ_CW if az_error > 0 else Direction.AZ_CCW,
                "target": target_azimuth,
                "tolerance": az_stop_tolerance,
                "direction_sign": 1.0 if az_error > 0.0 else -1.0,
                "fast_speed": az_fast_speed,
                "slow_speed": az_slow_speed,
                "slow_threshold": az_slow_threshold,
                "slow": abs(az_error) <= az_slow_threshold,
                "start_error": az_error,
            }
        if abs(el_error) > el_start_tolerance:
            active[Axis.ELEVATION] = {
                "direction": Direction.EL_UP if el_error > 0 else Direction.EL_DOWN,
                "target": target_elevation,
                "tolerance": el_stop_tolerance,
                "direction_sign": 1.0 if el_error > 0.0 else -1.0,
                "fast_speed": el_fast_speed,
                "slow_speed": el_slow_speed,
                "slow_threshold": el_slow_threshold,
                "slow": abs(el_error) <= el_slow_threshold,
                "start_error": el_error,
            }
        self._motion_event(
            "SLEW_PLAN",
            target_az=target_azimuth,
            target_el=target_elevation,
            start_az=pos.azimuth,
            start_el=pos.elevation,
            start_raw_az=pos.raw_azimuth,
            start_raw_el=pos.raw_elevation,
            az_error=az_error,
            el_error=el_error,
            az_start_tolerance=az_start_tolerance,
            el_start_tolerance=el_start_tolerance,
            az_stop_tolerance=az_stop_tolerance,
            el_stop_tolerance=el_stop_tolerance,
            az_fast_speed=az_fast_speed,
            el_fast_speed=el_fast_speed,
            az_slow_speed=az_slow_speed,
            el_slow_speed=el_slow_speed,
            active_axes=",".join(axis.value for axis in active),
        )
        if not active:
            self._motion_event("SLEW_NOOP", reason="within_start_tolerance", az_error=az_error, el_error=el_error)
            return pos

        deadline = time.monotonic() + self.config.limits.max_jog_seconds
        no_progress_seconds = max(2.0, self.config.limits.poll_interval * 5.0)
        no_progress_fault_seconds = max(6.0, self.config.limits.poll_interval * 15.0)
        min_progress_degrees = 0.01
        try:
            with self.lock:
                pos = self.read_position_locked()
                for state in active.values():
                    direction = state["direction"]
                    self.config.limits.assert_move_allowed(direction, pos.azimuth, pos.elevation)
                    start_speed = state["slow_speed"] if state["slow"] else state["fast_speed"]
                    self._start_direction(direction, start_speed)
                    axis = Axis.AZIMUTH if direction in (Direction.AZ_CW, Direction.AZ_CCW) else Axis.ELEVATION
                    axis_position = pos.azimuth if axis == Axis.AZIMUTH else pos.elevation
                    now = time.monotonic()
                    state["last_position"] = axis_position
                    state["last_progress_position"] = axis_position
                    state["last_progress_time"] = now
                    state["last_log_time"] = now
                    self._motion_event(
                        "AXIS_START",
                        axis=axis.value,
                        direction=direction.value,
                        speed=start_speed,
                        slow=bool(state["slow"]),
                        target=state["target"],
                        start_position=axis_position,
                        start_az=pos.azimuth,
                        start_el=pos.elevation,
                        start_error=state["start_error"],
                    )

            while active and not stop_event.is_set() and time.monotonic() < deadline:
                time.sleep(self.config.limits.poll_interval)
                with self.lock:
                    pos = self.read_position_locked()
                    if target_callback:
                        target_azimuth, target_elevation = target_callback(pos)
                        target_azimuth = normalize_degrees(target_azimuth)
                        self.config.limits.assert_position_allowed(target_azimuth, target_elevation)
                        az_error = self.config.limits.azimuth_delta_to_target(pos.azimuth, target_azimuth)
                        el_error = target_elevation - pos.elevation
                        if Axis.AZIMUTH in active:
                            active[Axis.AZIMUTH]["target"] = target_azimuth
                        elif abs(az_error) > az_start_tolerance:
                            direction = Direction.AZ_CW if az_error > 0 else Direction.AZ_CCW
                            self.config.limits.assert_move_allowed(direction, pos.azimuth, pos.elevation)
                            slow = abs(az_error) <= az_slow_threshold
                            self._start_direction(direction, az_slow_speed if slow else az_fast_speed)
                            active[Axis.AZIMUTH] = {
                                "direction": direction,
                                "target": target_azimuth,
                                "tolerance": az_stop_tolerance,
                                "direction_sign": 1.0 if az_error > 0.0 else -1.0,
                                "fast_speed": az_fast_speed,
                                "slow_speed": az_slow_speed,
                                "slow_threshold": az_slow_threshold,
                                "slow": slow,
                                "start_error": az_error,
                                "last_position": pos.azimuth,
                                "last_progress_position": pos.azimuth,
                                "last_progress_time": time.monotonic(),
                                "last_log_time": time.monotonic(),
                            }
                            self._motion_event(
                                "AXIS_START",
                                axis=Axis.AZIMUTH.value,
                                direction=direction.value,
                                speed=az_slow_speed if slow else az_fast_speed,
                                slow=slow,
                                target=target_azimuth,
                                start_position=pos.azimuth,
                                start_az=pos.azimuth,
                                start_el=pos.elevation,
                                start_error=az_error,
                                reason="target_callback",
                            )
                        if Axis.ELEVATION in active:
                            active[Axis.ELEVATION]["target"] = target_elevation
                        elif abs(el_error) > el_start_tolerance:
                            direction = Direction.EL_UP if el_error > 0 else Direction.EL_DOWN
                            self.config.limits.assert_move_allowed(direction, pos.azimuth, pos.elevation)
                            slow = abs(el_error) <= el_slow_threshold
                            self._start_direction(direction, el_slow_speed if slow else el_fast_speed)
                            active[Axis.ELEVATION] = {
                                "direction": direction,
                                "target": target_elevation,
                                "tolerance": el_stop_tolerance,
                                "direction_sign": 1.0 if el_error > 0.0 else -1.0,
                                "fast_speed": el_fast_speed,
                                "slow_speed": el_slow_speed,
                                "slow_threshold": el_slow_threshold,
                                "slow": slow,
                                "start_error": el_error,
                                "last_position": pos.elevation,
                                "last_progress_position": pos.elevation,
                                "last_progress_time": time.monotonic(),
                                "last_log_time": time.monotonic(),
                            }
                            self._motion_event(
                                "AXIS_START",
                                axis=Axis.ELEVATION.value,
                                direction=direction.value,
                                speed=el_slow_speed if slow else el_fast_speed,
                                slow=slow,
                                target=target_elevation,
                                start_position=pos.elevation,
                                start_az=pos.azimuth,
                                start_el=pos.elevation,
                                start_error=el_error,
                                reason="target_callback",
                            )
                    for axis, state in list(active.items()):
                        direction = state["direction"]
                        self.config.limits.assert_move_allowed(direction, pos.azimuth, pos.elevation)
                        target = state["target"]
                        tolerance = state["tolerance"]
                        slow_threshold = state["slow_threshold"]
                        slow_speed = state["slow_speed"]
                        error = (
                            self.config.limits.azimuth_delta_to_target(pos.azimuth, target)
                            if axis == Axis.AZIMUTH
                            else target - pos.elevation
                        )
                        axis_position = pos.azimuth if axis == Axis.AZIMUTH else pos.elevation
                        now = time.monotonic()
                        last_progress_position = float(state.get("last_progress_position", axis_position))
                        last_progress_time = float(state.get("last_progress_time", now))
                        last_log_time = float(state.get("last_log_time", now))
                        position_delta = axis_position - last_progress_position
                        if axis == Axis.AZIMUTH:
                            position_delta = shortest_angle_delta(last_progress_position, axis_position)
                        if abs(position_delta) >= min_progress_degrees:
                            state["last_progress_position"] = axis_position
                            state["last_progress_time"] = now
                            state["no_progress_since"] = None
                        elif now - last_progress_time >= no_progress_seconds:
                            no_progress_since = state.get("no_progress_since")
                            if no_progress_since is None:
                                no_progress_since = last_progress_time
                                state["no_progress_since"] = no_progress_since
                            self._motion_event(
                                "AXIS_NO_PROGRESS",
                                axis=axis.value,
                                direction=direction.value,
                                target=target,
                                position=axis_position,
                                error=error,
                                seconds_without_progress=now - last_progress_time,
                                seconds_since_first_no_progress=now - float(no_progress_since),
                                min_progress_degrees=min_progress_degrees,
                                fault_seconds=no_progress_fault_seconds,
                                az=pos.azimuth,
                                el=pos.elevation,
                            )
                            state["last_progress_time"] = now
                            if now - float(no_progress_since) >= no_progress_fault_seconds:
                                self._motion_event(
                                    "AXIS_STOP",
                                    axis=axis.value,
                                    reason="no_progress",
                                    target=target,
                                    position=axis_position,
                                    error=error,
                                    seconds_since_first_no_progress=now - float(no_progress_since),
                                    az=pos.azimuth,
                                    el=pos.elevation,
                                )
                                raise SafetyError(
                                    f"{axis.value} no progress for {now - float(no_progress_since):0.1f}s "
                                    f"while driving {direction.value}; error {error:0.2f} deg"
                                )
                        if now - last_log_time >= 1.0:
                            self._motion_event(
                                "AXIS_PROGRESS",
                                axis=axis.value,
                                direction=direction.value,
                                target=target,
                                position=axis_position,
                                error=error,
                                az=pos.azimuth,
                                el=pos.elevation,
                                raw_az=pos.raw_azimuth,
                                raw_el=pos.raw_elevation,
                            )
                            state["last_log_time"] = now
                        if _reached_stop_tolerance(error, tolerance, state["direction_sign"]):
                            self._stop_axis(axis)
                            active.pop(axis)
                            self._motion_event(
                                "AXIS_STOP",
                                axis=axis.value,
                                reason="stop_tolerance",
                                target=target,
                                position=axis_position,
                                error=error,
                                tolerance=tolerance,
                                az=pos.azimuth,
                                el=pos.elevation,
                            )
                            continue
                        if not state["slow"] and abs(error) <= slow_threshold:
                            self._set_axis_direction_speed(axis, direction, slow_speed)
                            state["slow"] = True
                            self._motion_event(
                                "AXIS_SLOW",
                                axis=axis.value,
                                direction=direction.value,
                                speed=slow_speed,
                                target=target,
                                position=axis_position,
                                error=error,
                                slow_threshold=slow_threshold,
                            )
                if update_callback:
                    update_callback(pos)
            if active and not stop_event.is_set():
                for axis, state in active.items():
                    self._motion_event(
                        "AXIS_STOP",
                        axis=axis.value,
                        reason="timeout",
                        target=state.get("target"),
                        az=pos.azimuth,
                        el=pos.elevation,
                        max_jog_seconds=self.config.limits.max_jog_seconds,
                    )
                raise SafetyError(f"Slew timed out after {self.config.limits.max_jog_seconds:0.1f}s")
            if active and stop_event.is_set():
                for axis, state in active.items():
                    self._motion_event(
                        "AXIS_STOP",
                        axis=axis.value,
                        reason="external_stop_event",
                        target=state.get("target"),
                        az=pos.azimuth,
                        el=pos.elevation,
                    )
            return self.read_position()
        except Exception as exc:
            self.fault = str(exc)
            raise
        finally:
            with self.lock:
                self.controller.stop_all()

    def guarded_slew_axis_to(
        self,
        axis: Axis,
        target_degrees: float,
        speed: int,
        stop_event: threading.Event,
        start_tolerance: float = 0.5,
        stop_tolerance: Optional[float] = None,
        slow_speed: Optional[int] = None,
        slow_threshold: float = 3.0,
        update_callback: Optional[Callable[[Position], None]] = None,
    ) -> Position:
        start_tolerance = max(0.01, abs(float(start_tolerance)))
        stop_tolerance = _clamp_signed_stop_tolerance(stop_tolerance, start_tolerance)
        slow_threshold = max(start_tolerance, float(slow_threshold))
        fast_speed = clamp_speed(speed)
        slow_speed = clamp_speed(fast_speed if slow_speed is None else slow_speed)
        if axis == Axis.AZIMUTH:
            target_degrees = normalize_degrees(target_degrees)

        pos = self.read_position()
        if axis == Axis.AZIMUTH:
            self.config.limits.assert_position_allowed(target_degrees, pos.elevation)
            error = self.config.limits.azimuth_delta_to_target(pos.azimuth, target_degrees)
            direction = Direction.AZ_CW if error > 0.0 else Direction.AZ_CCW
        else:
            self.config.limits.assert_position_allowed(pos.azimuth, target_degrees)
            error = target_degrees - pos.elevation
            direction = Direction.EL_UP if error > 0.0 else Direction.EL_DOWN
        if abs(error) <= start_tolerance:
            return pos

        direction_sign = 1.0 if error > 0.0 else -1.0
        using_slow = abs(error) <= slow_threshold
        deadline = time.monotonic() + self.config.limits.max_jog_seconds
        try:
            with self.lock:
                pos = self.read_position_locked()
                self.config.limits.assert_move_allowed(direction, pos.azimuth, pos.elevation)
                self._start_direction(direction, slow_speed if using_slow else fast_speed)

            while not stop_event.is_set() and time.monotonic() < deadline:
                time.sleep(self.config.limits.poll_interval)
                with self.lock:
                    pos = self.read_position_locked()
                    self.config.limits.assert_move_allowed(direction, pos.azimuth, pos.elevation)
                    error = (
                        self.config.limits.azimuth_delta_to_target(pos.azimuth, target_degrees)
                        if axis == Axis.AZIMUTH
                        else target_degrees - pos.elevation
                    )
                    if _reached_stop_tolerance(error, stop_tolerance, direction_sign):
                        self._stop_axis(axis)
                        if update_callback:
                            update_callback(pos)
                        return pos
                    if not using_slow and abs(error) <= slow_threshold:
                        self._set_axis_direction_speed(axis, direction, slow_speed)
                        using_slow = True
                if update_callback:
                    update_callback(pos)
            if not stop_event.is_set():
                raise SafetyError(f"Axis slew timed out after {self.config.limits.max_jog_seconds:0.1f}s")
            return self.read_position()
        except Exception as exc:
            self.fault = str(exc)
            raise
        finally:
            with self.lock:
                self._stop_axis(axis)

    def stop_all(self) -> None:
        with self.lock:
            self.controller.stop_all()

    def update_oled(
        self,
        mode: str = "MANUAL",
        target_azimuth: Optional[float] = None,
        target_elevation: Optional[float] = None,
        activity: str = "",
    ) -> None:
        with self.lock:
            pos = self.last_position or self.read_position_locked()
            self.controller.oled_status(
                self.config.name, pos, mode, self.fault, target_azimuth, target_elevation, activity
            )

    def update_oled_position(
        self,
        target_azimuth: Optional[float] = None,
        target_elevation: Optional[float] = None,
        activity: Optional[str] = None,
    ) -> None:
        with self.lock:
            pos = self.last_position or self.read_position_locked()
            self.controller.oled_position(pos, target_azimuth, target_elevation, activity)

    def update_oled_activity(self, activity: str) -> None:
        with self.lock:
            self.controller.oled_activity(activity)

    def _start_direction(self, direction: Direction, speed: int) -> None:
        if direction == Direction.AZ_CW:
            self.controller.azimuth_cw(speed)
        elif direction == Direction.AZ_CCW:
            self.controller.azimuth_ccw(speed)
        elif direction == Direction.EL_UP:
            self.controller.elevation_up(speed)
        elif direction == Direction.EL_DOWN:
            self.controller.elevation_down(speed)
        else:
            raise ValueError(f"Unsupported direction: {direction}")

    def _stop_axis(self, axis: Axis) -> None:
        if axis == Axis.AZIMUTH:
            self.controller.stop_azimuth()
        else:
            self.controller.stop_elevation()

    def _set_axis_direction_speed(self, axis: Axis, direction: Direction, speed: int) -> None:
        if axis == Axis.AZIMUTH:
            channel = AXIS_MAPS[axis].positive_channel if direction == Direction.AZ_CW else AXIS_MAPS[axis].negative_channel
        else:
            channel = AXIS_MAPS[axis].positive_channel if direction == Direction.EL_UP else AXIS_MAPS[axis].negative_channel
        self.controller._set_speed(axis, channel, speed)


def normalize_degrees(value: float) -> float:
    value = math.fmod(value, 360.0)
    if value < 0:
        value += 360.0
    return value


def shortest_angle_delta(raw: float, actual: float) -> float:
    return ((normalize_degrees(actual) - normalize_degrees(raw) + 540.0) % 360.0) - 180.0


def clockwise_angle_delta(start: float, end: float) -> float:
    return (normalize_degrees(end) - normalize_degrees(start)) % 360.0


def _clamp_signed_stop_tolerance(stop_tolerance: Optional[float], start_tolerance: float) -> float:
    if stop_tolerance is None:
        return start_tolerance
    stop_tolerance = float(stop_tolerance)
    if stop_tolerance == 0.0:
        return 0.0
    sign = 1.0 if stop_tolerance > 0.0 else -1.0
    magnitude = min(max(0.01, abs(stop_tolerance)), start_tolerance)
    return sign * magnitude


def _reached_stop_tolerance(current_error: float, stop_tolerance: float, direction_sign: float) -> bool:
    return current_error * direction_sign <= stop_tolerance


def clamp_speed(speed: int) -> int:
    return max(0, min(100, int(speed)))




