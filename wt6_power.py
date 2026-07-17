#!/usr/bin/env python3
"""RTL-SDR power-meter primitives for WT6."""

from __future__ import annotations

import ctypes
import ctypes.util
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional


_UNSIGNED_IQ_POWER_TABLE = tuple(((value - 127.5) / 127.5) ** 2 for value in range(256))


@dataclass(frozen=True)
class PowerMeterConfig:
    center_frequency_hz: int = 1_200_000_000
    sample_rate_hz: int = 1_024_000
    measurement_bandwidth_hz: int = 1_024_000
    update_rate_hz: float = 10.0
    device_index: int = 0
    gain_db: Optional[float] = None
    frequency_correction_ppm: int = 0
    smoothing_samples: int = 3
    samples_per_read: Optional[int] = None
    discard_reads: int = 2

    def validate(self) -> None:
        if self.center_frequency_hz <= 0:
            raise ValueError("center frequency must be positive")
        if self.sample_rate_hz <= 0:
            raise ValueError("sample rate must be positive")
        if 300_000 < self.sample_rate_hz < 900_001:
            raise ValueError("RTL-SDR commonly rejects sample rates between 300000 and 900001 sps; try 1024000")
        if not (0 < self.measurement_bandwidth_hz <= self.sample_rate_hz):
            raise ValueError("measurement bandwidth must be greater than zero and no wider than sample rate")
        if not (1.0 <= self.update_rate_hz <= 50.0):
            raise ValueError("update rate must be 1..50 Hz")
        if self.smoothing_samples < 1:
            raise ValueError("smoothing samples must be at least 1")
        if self.samples_per_read is not None and self.samples_per_read < 256:
            raise ValueError("samples per read must be at least 256")
        if self.discard_reads < 0:
            raise ValueError("discard reads must not be negative")

    @property
    def samples_per_update(self) -> int:
        if self.samples_per_read is not None:
            return self.samples_per_read
        return max(1, int(round(self.sample_rate_hz / self.update_rate_hz)))


@dataclass(frozen=True)
class PowerReading:
    timestamp: datetime
    power_dbfs: float
    sample_count: int


class RtlSdrError(RuntimeError):
    pass


def power_dbfs(samples: Iterable[complex]) -> PowerReading:
    total = 0.0
    count = 0
    for sample in samples:
        total += sample.real * sample.real + sample.imag * sample.imag
        count += 1
    if count == 0:
        raise ValueError("at least one IQ sample is required")
    mean_power = total / count
    power = 10.0 * math.log10(max(mean_power, 1.0e-20))
    return PowerReading(datetime.now(timezone.utc), power, count)


def power_dbfs_from_unsigned_iq(data: bytes) -> PowerReading:
    if len(data) < 2:
        raise ValueError("at least one interleaved IQ sample is required")
    total = 0.0
    count = len(data) // 2
    limit = count * 2
    table = _UNSIGNED_IQ_POWER_TABLE
    for index in range(0, limit, 2):
        total += table[data[index]] + table[data[index + 1]]
    mean_power = total / count
    return PowerReading(datetime.now(timezone.utc), 10.0 * math.log10(max(mean_power, 1.0e-20)), count)


class RtlSdrDevice:
    """Small synchronous wrapper around librtlsdr."""

    def __init__(self, device_index: int = 0) -> None:
        self._lib = _load_librtlsdr()
        self._dev = ctypes.c_void_p()
        result = self._lib.rtlsdr_open(ctypes.byref(self._dev), ctypes.c_uint32(device_index))
        self._check(result, f"open RTL-SDR device {device_index}")

    def close(self) -> None:
        if self._dev:
            self._lib.rtlsdr_close(self._dev)
            self._dev = ctypes.c_void_p()

    def cancel(self) -> None:
        if self._dev:
            self._lib.rtlsdr_cancel_async(self._dev)

    def __enter__(self) -> "RtlSdrDevice":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def configure(self, config: PowerMeterConfig) -> None:
        config.validate()
        self._check(self._lib.rtlsdr_set_sample_rate(self._dev, ctypes.c_uint32(config.sample_rate_hz)), "set sample rate")
        if config.frequency_correction_ppm and hasattr(self._lib, "rtlsdr_set_freq_correction"):
            self._check(
                self._lib.rtlsdr_set_freq_correction(self._dev, ctypes.c_int(config.frequency_correction_ppm)),
                "set frequency correction",
            )
        self._check(
            self._lib.rtlsdr_set_center_freq(self._dev, ctypes.c_uint32(config.center_frequency_hz)),
            "set center frequency",
        )
        if hasattr(self._lib, "rtlsdr_set_agc_mode"):
            self._check(self._lib.rtlsdr_set_agc_mode(self._dev, 0), "disable RTL AGC")
        if config.gain_db is None:
            self._check(self._lib.rtlsdr_set_tuner_gain_mode(self._dev, 0), "enable automatic gain")
        else:
            self._check(self._lib.rtlsdr_set_tuner_gain_mode(self._dev, 1), "enable manual gain")
            self._check(self._lib.rtlsdr_set_tuner_gain(self._dev, int(round(config.gain_db * 10.0))), "set tuner gain")
        self._check(self._lib.rtlsdr_reset_buffer(self._dev), "reset buffer")

    def read_sync(self, byte_count: int) -> bytes:
        buffer = ctypes.create_string_buffer(byte_count)
        bytes_read = ctypes.c_int(0)
        result = self._lib.rtlsdr_read_sync(self._dev, buffer, ctypes.c_int(byte_count), ctypes.byref(bytes_read))
        self._check(result, "read samples")
        if bytes_read.value <= 0:
            raise RtlSdrError("RTL-SDR returned no samples")
        return buffer.raw[: bytes_read.value]

    def _check(self, result: int, action: str) -> None:
        if result < 0:
            raise RtlSdrError(f"Could not {action}: librtlsdr error {result}")


class RtlPowerMeter:
    def __init__(self, config: PowerMeterConfig) -> None:
        config.validate()
        self.config = config
        self.device = RtlSdrDevice(config.device_index)
        self.device.configure(config)
        self.discard_initial_samples()

    def close(self) -> None:
        self.device.close()

    def cancel(self) -> None:
        self.device.cancel()

    def __enter__(self) -> "RtlPowerMeter":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def read_power(self) -> PowerReading:
        data = self.device.read_sync(self.config.samples_per_update * 2)
        return power_dbfs_from_unsigned_iq(data)

    def discard_initial_samples(self) -> None:
        byte_count = self.config.samples_per_update * 2
        for _index in range(self.config.discard_reads):
            self.device.read_sync(byte_count)


def _load_librtlsdr():
    library_name = ctypes.util.find_library("rtlsdr")
    candidates = [library_name, "librtlsdr.so.0", "librtlsdr.so", "rtlsdr.dll"]
    last_error: Optional[OSError] = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            lib = ctypes.CDLL(candidate)
            _configure_librtlsdr_api(lib)
            return lib
        except OSError as exc:
            last_error = exc
    detail = f": {last_error}" if last_error else ""
    raise RtlSdrError(f"Could not load librtlsdr{detail}. Install rtl-sdr/librtlsdr first.")


def _configure_librtlsdr_api(lib) -> None:
    dev_p = ctypes.c_void_p
    lib.rtlsdr_open.argtypes = [ctypes.POINTER(dev_p), ctypes.c_uint32]
    lib.rtlsdr_open.restype = ctypes.c_int
    lib.rtlsdr_close.argtypes = [dev_p]
    lib.rtlsdr_close.restype = ctypes.c_int
    lib.rtlsdr_set_center_freq.argtypes = [dev_p, ctypes.c_uint32]
    lib.rtlsdr_set_center_freq.restype = ctypes.c_int
    lib.rtlsdr_set_sample_rate.argtypes = [dev_p, ctypes.c_uint32]
    lib.rtlsdr_set_sample_rate.restype = ctypes.c_int
    if hasattr(lib, "rtlsdr_set_freq_correction"):
        lib.rtlsdr_set_freq_correction.argtypes = [dev_p, ctypes.c_int]
        lib.rtlsdr_set_freq_correction.restype = ctypes.c_int
    if hasattr(lib, "rtlsdr_set_agc_mode"):
        lib.rtlsdr_set_agc_mode.argtypes = [dev_p, ctypes.c_int]
        lib.rtlsdr_set_agc_mode.restype = ctypes.c_int
    lib.rtlsdr_set_tuner_gain_mode.argtypes = [dev_p, ctypes.c_int]
    lib.rtlsdr_set_tuner_gain_mode.restype = ctypes.c_int
    lib.rtlsdr_set_tuner_gain.argtypes = [dev_p, ctypes.c_int]
    lib.rtlsdr_set_tuner_gain.restype = ctypes.c_int
    lib.rtlsdr_reset_buffer.argtypes = [dev_p]
    lib.rtlsdr_reset_buffer.restype = ctypes.c_int
    lib.rtlsdr_read_sync.argtypes = [dev_p, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.rtlsdr_read_sync.restype = ctypes.c_int
    lib.rtlsdr_cancel_async.argtypes = [dev_p]
    lib.rtlsdr_cancel_async.restype = ctypes.c_int




