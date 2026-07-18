#!/usr/bin/env python3
"""Configuration helpers for WT6."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from wt6_antenna import AntennaConfig, Calibration, SafetyLimits


@dataclass
class SourceConfig:
    name: str
    ra_hours: float = 0.0
    dec_degrees: float = 0.0
    flux_4800_mhz: float = 0.0


@dataclass
class SiteConfig:
    latitude: float = -32.724000
    longitude: float = 152.130167
    selected_source: str = ""
    track_interval_seconds: float = 2.0
    az_track_tolerance_degrees: float = 0.10
    el_track_tolerance_degrees: float = 0.10
    az_stop_tolerance_degrees: float = 0.10
    el_stop_tolerance_degrees: float = 0.10
    az_slow_speed: int = 20
    el_slow_speed: int = 20
    az_slow_threshold_degrees: float = 3.0
    el_slow_threshold_degrees: float = 3.0
    log_retention_days: int = 14
    log_level: str = "INFO"
    timeout_enabled: bool = False
    timeout_minutes: float = 60.0
    timeout_action: str = "disconnect"


@dataclass
class PowerConfig:
    center_frequency_hz: int = 1_200_000_000
    sample_rate_hz: int = 1_024_000
    measurement_bandwidth_hz: int = 512_000
    gain_db: str = "29.7"
    gain_b_db: str = "29.7"
    frequency_correction_ppm: int = 0
    samples_per_read: str = "auto"
    update_rate_hz: float = 10.0
    smoothing_samples: int = 3
    warmup_seconds: float = 30.0
    clock_source: str = "internal"
    b210_device_args: str = "num_recv_frames=256"
    east_channel: str = "A"
    west_channel: str = "B"


@dataclass
class ScanConfig:
    span_degrees: float = 4.0
    increment_degrees: float = 0.5
    dwell_seconds: float = 1.0
    scan_count: int = 1
    antenna_name: str = ""
    az_scan_high_to_low: bool = True


@dataclass
class YFactorConfig:
    antenna_name: str = ""
    hot_target: str = "Sun"
    cold_mode: str = "Sun AZ / EL 80"
    cold_az: float = 0.0
    cold_el: float = 80.0
    cold_ra: float = 0.0
    cold_dec: float = 0.0
    count: int = 3
    dwell_seconds: float = 5.0


@dataclass
class RtlCalibration:
    frequency_hz: int
    sample_rate_hz: int
    gain_db: str
    points_dbfs_by_dbm: dict[int, float]


RTL_CAL_LEVELS_DBM = tuple(range(-40, -111, -10))


def _read_parser(parser: configparser.ConfigParser, path: Path) -> None:
    parser.read(path, encoding="utf-8-sig")


def load_site_config(path: Union[str, Path]) -> SiteConfig:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    old_tolerance = parser.getfloat("site", "track_tolerance_degrees", fallback=0.10)
    old_slow_speed = parser.getint("site", "slow_speed", fallback=20)
    old_slow_threshold = parser.getfloat("site", "slow_threshold_degrees", fallback=3.0)
    az_start_tolerance = parser.getfloat("site", "az_track_tolerance_degrees", fallback=old_tolerance)
    el_start_tolerance = parser.getfloat("site", "el_track_tolerance_degrees", fallback=old_tolerance)
    timeout_action = parser.get("site", "timeout_action", fallback="disconnect").strip() or "disconnect"
    if timeout_action not in ("disconnect", "park_disconnect"):
        timeout_action = "disconnect"
    return SiteConfig(
        latitude=parser.getfloat("site", "latitude", fallback=-32.724000),
        longitude=parser.getfloat("site", "longitude", fallback=152.130167),
        selected_source=parser.get("site", "selected_source", fallback="").strip(),
        track_interval_seconds=parser.getfloat("site", "track_interval_seconds", fallback=2.0),
        az_track_tolerance_degrees=az_start_tolerance,
        el_track_tolerance_degrees=el_start_tolerance,
        az_stop_tolerance_degrees=parser.getfloat(
            "site", "az_stop_tolerance_degrees", fallback=abs(az_start_tolerance)
        ),
        el_stop_tolerance_degrees=parser.getfloat(
            "site", "el_stop_tolerance_degrees", fallback=abs(el_start_tolerance)
        ),
        az_slow_speed=parser.getint("site", "az_slow_speed", fallback=old_slow_speed),
        el_slow_speed=parser.getint("site", "el_slow_speed", fallback=old_slow_speed),
        az_slow_threshold_degrees=parser.getfloat("site", "az_slow_threshold_degrees", fallback=old_slow_threshold),
        el_slow_threshold_degrees=parser.getfloat("site", "el_slow_threshold_degrees", fallback=old_slow_threshold),
        log_retention_days=parser.getint("site", "log_retention_days", fallback=14),
        log_level=parser.get("site", "log_level", fallback="INFO").strip().upper() or "INFO",
        timeout_enabled=parser.getboolean("site", "timeout_enabled", fallback=False),
        timeout_minutes=parser.getfloat("site", "timeout_minutes", fallback=60.0),
        timeout_action=timeout_action,
    )


def load_rtl_calibration(path: Union[str, Path], frequency_hz: int, sample_rate_hz: int, gain_db: str) -> RtlCalibration:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    gain_db = normalize_rtl_gain(gain_db)
    section = _rtl_cal_section(frequency_hz, sample_rate_hz, gain_db)
    points: dict[int, float] = {}
    for level_dbm in RTL_CAL_LEVELS_DBM:
        key = _rtl_cal_key(level_dbm)
        if parser.has_option(section, key):
            points[level_dbm] = parser.getfloat(section, key)
    return RtlCalibration(
        frequency_hz=frequency_hz,
        sample_rate_hz=sample_rate_hz,
        gain_db=gain_db,
        points_dbfs_by_dbm=points,
    )


def save_rtl_calibration(path: Union[str, Path], calibration: RtlCalibration) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    section = _rtl_cal_section(calibration.frequency_hz, calibration.sample_rate_hz, calibration.gain_db)
    parser[section] = {
        "frequency_hz": str(int(calibration.frequency_hz)),
        "sample_rate_hz": str(int(calibration.sample_rate_hz)),
        "gain_db": normalize_rtl_gain(calibration.gain_db),
    }
    for level_dbm in RTL_CAL_LEVELS_DBM:
        if level_dbm in calibration.points_dbfs_by_dbm:
            parser[section][_rtl_cal_key(level_dbm)] = f"{calibration.points_dbfs_by_dbm[level_dbm]:.3f}"
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def calibrated_dbm_from_dbfs(calibration: RtlCalibration, power_dbfs: float) -> tuple[float, bool] | None:
    points = sorted((dbfs, dbm) for dbm, dbfs in calibration.points_dbfs_by_dbm.items())
    if not points:
        return None
    if len(points) == 1:
        dbfs, dbm = points[0]
        return dbm + (power_dbfs - dbfs), True
    if power_dbfs <= points[0][0]:
        return _interpolate_calibration(points[0], points[1], power_dbfs), True
    if power_dbfs >= points[-1][0]:
        return _interpolate_calibration(points[-2], points[-1], power_dbfs), True
    for lower, upper in zip(points, points[1:]):
        if lower[0] <= power_dbfs <= upper[0]:
            return _interpolate_calibration(lower, upper, power_dbfs), False
    return None


def _interpolate_calibration(lower: tuple[float, int], upper: tuple[float, int], power_dbfs: float) -> float:
    lower_dbfs, lower_dbm = lower
    upper_dbfs, upper_dbm = upper
    if upper_dbfs == lower_dbfs:
        return float(lower_dbm)
    fraction = (power_dbfs - lower_dbfs) / (upper_dbfs - lower_dbfs)
    return lower_dbm + fraction * (upper_dbm - lower_dbm)


def _rtl_cal_section(frequency_hz: int, sample_rate_hz: int, gain_db: str) -> str:
    return f"rtl_cal:{int(frequency_hz)}:{int(sample_rate_hz)}:{_rtl_gain_key_part(gain_db)}"


def normalize_rtl_gain(gain_db: str) -> str:
    text = str(gain_db).strip().lower()
    if text in ("", "auto", "0"):
        return "auto"
    return f"{float(text):0.1f}"


def _rtl_gain_key_part(gain_db: str) -> str:
    return normalize_rtl_gain(gain_db).replace("-", "m").replace(".", "p")


def _rtl_cal_key(level_dbm: int) -> str:
    return f"dbm_{level_dbm}"


def load_scan_config(path: Union[str, Path]) -> ScanConfig:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    return ScanConfig(
        span_degrees=parser.getfloat("scan", "span_degrees", fallback=4.0),
        increment_degrees=parser.getfloat("scan", "increment_degrees", fallback=0.5),
        dwell_seconds=parser.getfloat("scan", "dwell_seconds", fallback=1.0),
        scan_count=parser.getint("scan", "scan_count", fallback=1),
        antenna_name=parser.get("scan", "antenna_name", fallback="").strip(),
        az_scan_high_to_low=parser.getboolean("scan", "az_scan_high_to_low", fallback=True),
    )


def save_scan_config(path: Union[str, Path], scan: ScanConfig) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    parser["scan"] = {
        "span_degrees": f"{scan.span_degrees:.3f}",
        "increment_degrees": f"{scan.increment_degrees:.3f}",
        "dwell_seconds": f"{scan.dwell_seconds:.3f}",
        "scan_count": str(max(1, int(scan.scan_count))),
        "antenna_name": scan.antenna_name,
        "az_scan_high_to_low": "yes" if scan.az_scan_high_to_low else "no",
    }
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def load_yfactor_config(path: Union[str, Path]) -> YFactorConfig:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    return YFactorConfig(
        antenna_name=parser.get("yfactor", "antenna_name", fallback="").strip(),
        hot_target=parser.get("yfactor", "hot_target", fallback="Sun").strip() or "Sun",
        cold_mode=parser.get("yfactor", "cold_mode", fallback="Sun AZ / EL 80").strip() or "Sun AZ / EL 80",
        cold_az=parser.getfloat("yfactor", "cold_az", fallback=0.0),
        cold_el=parser.getfloat("yfactor", "cold_el", fallback=80.0),
        cold_ra=parser.getfloat("yfactor", "cold_ra", fallback=0.0),
        cold_dec=parser.getfloat("yfactor", "cold_dec", fallback=0.0),
        count=parser.getint("yfactor", "count", fallback=3),
        dwell_seconds=parser.getfloat("yfactor", "dwell_seconds", fallback=5.0),
    )


def save_yfactor_config(path: Union[str, Path], config: YFactorConfig) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    parser["yfactor"] = {
        "antenna_name": config.antenna_name,
        "hot_target": config.hot_target,
        "cold_mode": config.cold_mode,
        "cold_az": f"{config.cold_az:.3f}",
        "cold_el": f"{config.cold_el:.3f}",
        "cold_ra": f"{config.cold_ra:.6f}",
        "cold_dec": f"{config.cold_dec:.3f}",
        "count": str(max(1, int(config.count))),
        "dwell_seconds": f"{config.dwell_seconds:.3f}",
    }
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def load_power_config(path: Union[str, Path]) -> PowerConfig:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    sample_rate_hz = parser.getint("power", "sample_rate_hz", fallback=1_024_000)
    return PowerConfig(
        center_frequency_hz=parser.getint("power", "center_frequency_hz", fallback=1_200_000_000),
        sample_rate_hz=sample_rate_hz,
        measurement_bandwidth_hz=parser.getint(
            "power", "measurement_bandwidth_hz", fallback=max(1, sample_rate_hz // 2)
        ),
        gain_db=parser.get("power", "gain_db", fallback="29.7").strip() or "auto",
        gain_b_db=parser.get(
            "power", "gain_b_db", fallback=parser.get("power", "gain_db", fallback="29.7")
        ).strip()
        or "auto",
        frequency_correction_ppm=parser.getint("power", "frequency_correction_ppm", fallback=0),
        samples_per_read=parser.get("power", "samples_per_read", fallback="auto").strip() or "auto",
        update_rate_hz=parser.getfloat("power", "update_rate_hz", fallback=10.0),
        smoothing_samples=parser.getint("power", "smoothing_samples", fallback=3),
        warmup_seconds=parser.getfloat("power", "warmup_seconds", fallback=30.0),
        clock_source=parser.get("power", "clock_source", fallback="internal").strip() or "internal",
        b210_device_args=parser.get("power", "b210_device_args", fallback="num_recv_frames=256").strip(),
        east_channel=normalize_power_channel(parser.get("power", "east_channel", fallback="A")),
        west_channel=normalize_power_channel(parser.get("power", "west_channel", fallback="B")),
    )


def save_power_config(path: Union[str, Path], power: PowerConfig) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    parser["power"] = {
        "center_frequency_hz": str(int(power.center_frequency_hz)),
        "sample_rate_hz": str(int(power.sample_rate_hz)),
        "measurement_bandwidth_hz": str(int(power.measurement_bandwidth_hz)),
        "gain_db": power.gain_db,
        "gain_b_db": power.gain_b_db,
        "frequency_correction_ppm": str(int(power.frequency_correction_ppm)),
        "samples_per_read": power.samples_per_read,
        "update_rate_hz": f"{power.update_rate_hz:.1f}",
        "smoothing_samples": str(max(1, int(power.smoothing_samples))),
        "warmup_seconds": f"{power.warmup_seconds:.1f}",
        "clock_source": power.clock_source,
        "b210_device_args": power.b210_device_args,
        "east_channel": normalize_power_channel(power.east_channel),
        "west_channel": normalize_power_channel(power.west_channel),
    }
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def normalize_power_channel(value: str) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    if text in ("B", "1", "CHB", "CHANNELB"):
        return "B"
    return "A"


def save_site_config(path: Union[str, Path], site: SiteConfig) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    parser["site"] = _site_section(site)
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def load_sources(path: Union[str, Path]) -> dict[str, SourceConfig]:
    path = Path(path)
    parser = configparser.ConfigParser()
    if not path.exists():
        return _default_sources()
    _read_parser(parser, path)

    sources: dict[str, SourceConfig] = {}
    for section in parser.sections():
        if not section.startswith("source:"):
            continue
        name = section.split(":", 1)[1].strip()
        if not name:
            continue
        sources[name] = SourceConfig(
            name=name,
            ra_hours=parser.getfloat(section, "ra_hours", fallback=0.0),
            dec_degrees=parser.getfloat(section, "dec_degrees", fallback=0.0),
            flux_4800_mhz=parser.getfloat(section, "flux_4800_mhz", fallback=0.0),
        )
    return sources or _default_sources()


def save_sources(path: Union[str, Path], sources: dict[str, SourceConfig], selected_source: str) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    if not parser.has_section("site"):
        parser["site"] = _site_section(SiteConfig(selected_source=selected_source))
    else:
        parser["site"]["selected_source"] = selected_source
    for section in list(parser.sections()):
        if section.startswith("source:"):
            parser.remove_section(section)
    for name, source in sources.items():
        parser[f"source:{name}"] = {
            "ra_hours": f"{source.ra_hours:.6f}",
            "dec_degrees": f"{source.dec_degrees:.6f}",
            "flux_4800_mhz": f"{source.flux_4800_mhz:.3f}",
        }
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def load_configs(path: Union[str, Path]) -> dict[str, AntennaConfig]:
    path = Path(path)
    parser = configparser.ConfigParser()
    if not path.exists():
        return {}
    _read_parser(parser, path)

    configs: dict[str, AntennaConfig] = {}
    for section in parser.sections():
        if not section.startswith("antenna:"):
            continue
        name = section.split(":", 1)[1].strip()
        port = parser.get(section, "port", fallback="").strip()
        if not name or not port:
            continue
        configs[name] = AntennaConfig(
            name=name,
            port=port,
            baud=parser.getint(section, "baud", fallback=9600),
            open_delay=parser.getfloat(section, "open_delay", fallback=5.0),
            gui_speed=parser.getint(section, "gui_speed", fallback=40),
            az_track_speed=parser.getint(section, "az_track_speed", fallback=parser.getint(section, "gui_speed", fallback=40)),
            el_track_speed=parser.getint(section, "el_track_speed", fallback=parser.getint(section, "gui_speed", fallback=40)),
            az_low_to_high_compensation=parser.getfloat(section, "az_low_to_high_compensation", fallback=0.0),
            park_az=parser.getfloat(section, "park_az", fallback=355.0),
            park_el=parser.getfloat(section, "park_el", fallback=80.0),
            calibration=Calibration(
                az_offset=parser.getfloat(section, "az_offset", fallback=0.0),
                el_offset=parser.getfloat(section, "el_offset", fallback=0.0),
            ),
            limits=SafetyLimits(
                az_min=parser.getfloat(section, "az_min", fallback=270.0),
                az_max=parser.getfloat(section, "az_max", fallback=265.0),
                el_min=parser.getfloat(section, "el_min", fallback=0.0),
                el_max=parser.getfloat(section, "el_max", fallback=87.0),
                az_margin=parser.getfloat(section, "az_margin", fallback=0.5),
                el_margin=parser.getfloat(section, "el_margin", fallback=0.5),
                max_jog_seconds=parser.getfloat(section, "max_jog_seconds", fallback=60.0),
                poll_interval=parser.getfloat(section, "poll_interval", fallback=0.2),
            ),
        )
    return configs


def save_configs(path: Union[str, Path], configs: dict[str, AntennaConfig]) -> None:
    path = Path(path)
    parser = configparser.ConfigParser()
    if path.exists():
        _read_parser(parser, path)
    if not parser.has_section("site"):
        parser["site"] = _site_section(SiteConfig())
    for section in list(parser.sections()):
        if section.startswith("antenna:"):
            parser.remove_section(section)
    for name, config in configs.items():
        section = f"antenna:{name}"
        parser[section] = {
            "port": config.port,
            "baud": str(config.baud),
            "open_delay": f"{config.open_delay:g}",
            "gui_speed": str(config.gui_speed),
            "az_track_speed": str(config.az_track_speed),
            "el_track_speed": str(config.el_track_speed),
            "az_low_to_high_compensation": f"{config.az_low_to_high_compensation:.3f}",
            "park_az": f"{config.park_az:.3f}",
            "park_el": f"{config.park_el:.3f}",
            "az_offset": f"{config.calibration.az_offset:.6f}",
            "el_offset": f"{config.calibration.el_offset:.6f}",
            "az_min": f"{config.limits.az_min:.3f}",
            "az_max": f"{config.limits.az_max:.3f}",
            "el_min": f"{config.limits.el_min:.3f}",
            "el_max": f"{config.limits.el_max:.3f}",
            "az_margin": f"{config.limits.az_margin:.3f}",
            "el_margin": f"{config.limits.el_margin:.3f}",
            "max_jog_seconds": f"{config.limits.max_jog_seconds:.3f}",
            "poll_interval": f"{config.limits.poll_interval:.3f}",
        }
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def _site_section(site: SiteConfig) -> dict[str, str]:
    return {
        "latitude": f"{site.latitude:.6f}",
        "longitude": f"{site.longitude:.6f}",
        "selected_source": site.selected_source,
        "track_interval_seconds": f"{site.track_interval_seconds:.1f}",
        "az_track_tolerance_degrees": f"{site.az_track_tolerance_degrees:.2f}",
        "el_track_tolerance_degrees": f"{site.el_track_tolerance_degrees:.2f}",
        "az_stop_tolerance_degrees": f"{site.az_stop_tolerance_degrees:.2f}",
        "el_stop_tolerance_degrees": f"{site.el_stop_tolerance_degrees:.2f}",
        "az_slow_speed": str(max(0, min(100, int(site.az_slow_speed)))),
        "el_slow_speed": str(max(0, min(100, int(site.el_slow_speed)))),
        "az_slow_threshold_degrees": f"{site.az_slow_threshold_degrees:.1f}",
        "el_slow_threshold_degrees": f"{site.el_slow_threshold_degrees:.1f}",
        "log_retention_days": str(max(1, int(site.log_retention_days))),
        "log_level": site.log_level.upper(),
        "timeout_enabled": "yes" if site.timeout_enabled else "no",
        "timeout_minutes": f"{site.timeout_minutes:.1f}",
        "timeout_action": site.timeout_action,
    }


def _default_sources() -> dict[str, SourceConfig]:
    return {
        "Virgo A": SourceConfig("Virgo A", ra_hours=12.5137, dec_degrees=12.3911, flux_4800_mhz=70.0),
        "Centaurus A": SourceConfig("Centaurus A", ra_hours=13.4241, dec_degrees=-43.0191, flux_4800_mhz=650.0),
        "Orion A": SourceConfig("Orion A", ra_hours=5.5881, dec_degrees=-5.3911, flux_4800_mhz=400.0),
    }




