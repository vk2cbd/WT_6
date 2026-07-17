#!/usr/bin/env python3
"""B210 dual-channel power-meter primitives for WT6."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class B210PowerMeterConfig:
    center_frequency_hz: int = 1_200_000_000
    sample_rate_hz: int = 1_024_000
    measurement_bandwidth_hz: int = 512_000
    update_rate_hz: float = 10.0
    gain_a_db: float = 48.0
    gain_b_db: float = 48.0
    samples_per_read: Optional[int] = None
    clock_source: str = "internal"
    device_args: str = "num_recv_frames=256"
    read_timeout_ms: int = 1000
    discard_reads: int = 2

    def validate(self) -> None:
        if self.center_frequency_hz <= 0:
            raise ValueError("center frequency must be positive")
        if self.sample_rate_hz <= 0:
            raise ValueError("sample rate must be positive")
        if not (0 < self.measurement_bandwidth_hz <= self.sample_rate_hz):
            raise ValueError("B210 bandwidth must be greater than zero and no wider than sample rate")
        if not (0.1 <= self.update_rate_hz <= 50.0):
            raise ValueError("GUI update rate must be 0.1..50 Hz")
        if self.gain_a_db < 0 or self.gain_b_db < 0:
            raise ValueError("B210 gains must not be negative")
        if self.samples_per_read is not None and self.samples_per_read < 256:
            raise ValueError("samples per read must be at least 256")
        if self.read_timeout_ms < 100:
            raise ValueError("B210 read timeout must be at least 100 ms")
        if self.discard_reads < 0:
            raise ValueError("discard reads must not be negative")

    @property
    def samples_per_update(self) -> int:
        if self.samples_per_read is not None:
            return self.samples_per_read
        return max(256, int(round(self.sample_rate_hz / self.update_rate_hz)))


@dataclass(frozen=True)
class B210PowerReading:
    timestamp: datetime
    power_a_dbfs: float
    power_b_dbfs: float
    sample_count: int

    @property
    def power_dbfs(self) -> float:
        """Legacy single-channel value used by existing scan/Y-factor code."""

        return self.power_a_dbfs


class B210PowerMeter:
    def __init__(self, config: B210PowerMeterConfig) -> None:
        config.validate()
        self.config = config
        self._sdr = None
        self._rx_stream = None
        self._np = None

    def __enter__(self) -> "B210PowerMeter":
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def start(self) -> None:
        try:
            import numpy as np  # type: ignore
            import SoapySDR  # type: ignore
            from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_HAS_TIME, SOAPY_SDR_RX  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "SoapySDR/numpy is not installed. Install UHD and SoapySDR Python bindings for B210 power."
            ) from exc

        self._np = np
        sdr = None
        rx_stream = None
        try:
            device_args = {"driver": "uhd", **parse_device_args(self.config.device_args)}
            sdr = run_b210_step("open B210 device", lambda: SoapySDR.Device(device_args))
            time.sleep(0.25)

            clock_source = normalize_clock_source(self.config.clock_source)
            if clock_source:
                run_b210_step("set clock source", lambda: sdr.setClockSource(clock_source))

            for channel, gain in ((0, self.config.gain_a_db), (1, self.config.gain_b_db)):
                run_b210_step(
                    f"set channel {channel} sample rate",
                    lambda channel=channel: sdr.setSampleRate(SOAPY_SDR_RX, channel, self.config.sample_rate_hz),
                )
                run_b210_step(
                    f"set channel {channel} RF bandwidth",
                    lambda channel=channel: sdr.setBandwidth(
                        SOAPY_SDR_RX, channel, self.config.measurement_bandwidth_hz
                    ),
                )
                run_b210_step(
                    f"tune channel {channel}",
                    lambda channel=channel: sdr.setFrequency(
                        SOAPY_SDR_RX, channel, self.config.center_frequency_hz
                    ),
                )
                run_b210_step(
                    f"disable channel {channel} AGC",
                    lambda channel=channel: sdr.setGainMode(SOAPY_SDR_RX, channel, False),
                )
                run_b210_step(
                    f"set channel {channel} gain",
                    lambda channel=channel, gain=gain: sdr.setGain(SOAPY_SDR_RX, channel, gain),
                )

            rx_stream = run_b210_step(
                "create two-channel RX stream",
                lambda: sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [0, 1]),
            )
            activate_b210_stream_with_timed_start(sdr, rx_stream, SOAPY_SDR_HAS_TIME)
        except Exception:
            if sdr is not None and rx_stream is not None:
                try:
                    sdr.closeStream(rx_stream)
                except Exception:
                    pass
            raise

        self._sdr = sdr
        self._rx_stream = rx_stream
        self.discard_initial_reads()

    def close(self) -> None:
        if self._sdr is not None and self._rx_stream is not None:
            try:
                self._sdr.deactivateStream(self._rx_stream)
            except Exception:
                pass
            try:
                self._sdr.closeStream(self._rx_stream)
            except Exception:
                pass
        self._sdr = None
        self._rx_stream = None

    def cancel(self) -> None:
        self.close()

    def read_power(self) -> B210PowerReading:
        if self._sdr is None or self._rx_stream is None or self._np is None:
            raise RuntimeError("B210 power meter is not running.")

        sample_count = self.config.samples_per_update
        buffs = [
            self._np.empty(sample_count, dtype=self._np.complex64),
            self._np.empty(sample_count, dtype=self._np.complex64),
        ]
        timeout_us = self.config.read_timeout_ms * 1000
        result = self._sdr.readStream(self._rx_stream, buffs, sample_count, timeoutUs=timeout_us)
        if result.ret == -4:
            raise RuntimeError("B210 RX overflow while reading power.")
        if result.ret == -1:
            raise RuntimeError("B210 RX timeout while reading power.")
        if result.ret <= 0:
            raise RuntimeError(f"B210 read failed with code {result.ret}.")

        used = int(result.ret)
        return B210PowerReading(
            timestamp=datetime.now(timezone.utc),
            power_a_dbfs=array_power_dbfs(buffs[0][:used]),
            power_b_dbfs=array_power_dbfs(buffs[1][:used]),
            sample_count=used,
        )

    def discard_initial_reads(self) -> None:
        for _index in range(self.config.discard_reads):
            self.read_power()


def array_power_dbfs(samples) -> float:
    if len(samples) == 0:
        raise ValueError("at least one IQ sample is required")
    mean_power = float((abs(samples) ** 2).mean())
    return 10.0 * math.log10(max(mean_power, 1.0e-20))


def parse_device_args(raw_args: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw_args.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"B210 device arg must be key=value: {item}")
        key, value = item.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def normalize_clock_source(clock_source: str) -> str:
    text = (clock_source or "internal").strip().lower()
    if text in ("", "int", "internal"):
        return "internal"
    if text in ("ext", "external"):
        return "external"
    return text


def activate_b210_stream_with_timed_start(sdr, rx_stream, has_time_flag: int) -> None:
    # UHD requires a timed command for a multi-channel B210 stream so both RX
    # channels align instead of starting independently.
    run_b210_step("reset B210 hardware time", lambda: sdr.setHardwareTime(0))
    start_time_ns = run_b210_step("read B210 hardware time", lambda: sdr.getHardwareTime())
    start_time_ns += 100_000_000
    run_b210_step(
        "activate time-aligned two-channel RX stream",
        lambda: sdr.activateStream(rx_stream, flags=has_time_flag, timeNs=start_time_ns),
    )


def run_b210_step(step_name: str, action):
    try:
        return action()
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise RuntimeError(f"B210 failed while trying to {step_name}: {detail}") from exc
