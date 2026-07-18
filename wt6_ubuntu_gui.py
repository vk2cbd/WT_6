#!/usr/bin/env python3
"""WT6 two-antenna safety/calibration GUI."""

from __future__ import annotations

import argparse
import csv
import math
import queue
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

from wt6_astro import TargetPosition, local_sidereal_time, moon_equatorial, moon_position, source_position
from wt6_config import (
    B210Calibration,
    B210_CAL_LEVELS_DBM,
    PowerConfig,
    RtlCalibration,
    RTL_CAL_LEVELS_DBM,
    ScanConfig,
    SiteConfig,
    SourceConfig,
    YFactorConfig,
    calibrated_dbm_from_dbfs,
    load_configs,
    load_b210_calibration,
    load_power_config,
    load_rtl_calibration,
    load_scan_config,
    load_site_config,
    load_sources,
    load_yfactor_config,
    normalize_rtl_gain,
    save_b210_calibration,
    save_configs,
    save_power_config,
    save_rtl_calibration,
    save_scan_config,
    save_site_config,
    save_sources,
    save_yfactor_config,
)
from wt6_antenna import AntennaConfig, Axis, Direction, EncoderInfo, Position, SafeAntenna, SafetyError, shortest_angle_delta
from wt6_logging import EventLogger
from wt6_b210_power import B210PowerMeter, B210PowerMeterConfig, B210PowerReading
from wt6_solar import sun_equatorial, sun_position
from wt6_state import AppStateStore, AntennaRunState, PowerRunState, SystemRunState, antenna_state_from_text


APP_VERSION = "v0.2-alpha"


def axis_label(axis: Axis) -> str:
    return "AZ" if axis == Axis.AZIMUTH else "EL"


class LimitsDialog(tk.Toplevel):
    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Antenna Limits")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.entries: dict[str, dict[str, tk.StringVar]] = {}
        self.park_entries: dict[str, dict[str, tk.StringVar]] = {}

        tabs = ttk.Notebook(self)
        tabs.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        for name, config in app.configs.items():
            frame = ttk.Frame(tabs, padding=10)
            tabs.add(frame, text=name)
            self.entries[name] = self._build_limit_fields(frame, config)

        park_frame = ttk.Frame(tabs, padding=10)
        tabs.add(park_frame, text="Park")
        self._build_park_fields(park_frame)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self.save).grid(row=0, column=2)

    def _build_limit_fields(self, frame: ttk.Frame, config: AntennaConfig) -> dict[str, tk.StringVar]:
        values = {
            "az_min": tk.StringVar(value=f"{config.limits.az_min:0.0f}"),
            "az_max": tk.StringVar(value=f"{config.limits.az_max:0.0f}"),
            "el_min": tk.StringVar(value=f"{config.limits.el_min:0.0f}"),
            "el_max": tk.StringVar(value=f"{config.limits.el_max:0.0f}"),
            "az_margin": tk.StringVar(value=f"{config.limits.az_margin:0.1f}"),
            "el_margin": tk.StringVar(value=f"{config.limits.el_margin:0.1f}"),
            "max_jog_seconds": tk.StringVar(value=f"{config.limits.max_jog_seconds:0.0f}"),
            "poll_interval": tk.StringVar(value=f"{config.limits.poll_interval:0.1f}"),
        }
        labels = [
            ("AZ min", "az_min"),
            ("AZ max", "az_max"),
            ("EL min", "el_min"),
            ("EL max", "el_max"),
            ("AZ margin", "az_margin"),
            ("EL margin", "el_margin"),
            ("Max jog sec", "max_jog_seconds"),
            ("Poll sec", "poll_interval"),
        ]
        for row, (label, key) in enumerate(labels):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=values[key], width=9).grid(row=row, column=1, sticky="w", pady=2)
        return values

    def _build_park_fields(self, frame: ttk.Frame) -> None:
        ttk.Label(frame, text="Antenna").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(frame, text="Park AZ").grid(row=0, column=1, sticky="w", pady=(0, 4))
        ttk.Label(frame, text="Park EL").grid(row=0, column=2, sticky="w", pady=(0, 4))
        for row, (name, config) in enumerate(self.app.configs.items(), start=1):
            values = {
                "park_az": tk.StringVar(value=f"{config.park_az:0.0f}"),
                "park_el": tk.StringVar(value=f"{config.park_el:0.0f}"),
            }
            self.park_entries[name] = values
            ttk.Label(frame, text=name).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=values["park_az"], width=9).grid(row=row, column=1, sticky="w", padx=(8, 0), pady=2)
            ttk.Entry(frame, textvariable=values["park_el"], width=9).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=2)

    def save(self) -> None:
        parsed: dict[str, dict[str, float]] = {}
        parsed_park: dict[str, dict[str, float]] = {}
        try:
            for name, values in self.entries.items():
                parsed[name] = {key: float(value.get()) for key, value in values.items()}
                self._validate_limits(name, parsed[name])
            for name, values in self.park_entries.items():
                parsed_park[name] = {key: float(value.get()) for key, value in values.items()}
                self._validate_park(name, parsed_park[name], parsed[name])
        except ValueError:
            messagebox.showerror("Invalid Limits", "All limit values must be numeric.", parent=self)
            return
        except RuntimeError as exc:
            messagebox.showerror("Invalid Limits", str(exc), parent=self)
            return

        for name, values in parsed.items():
            limits = self.app.configs[name].limits
            limits.az_min = values["az_min"]
            limits.az_max = values["az_max"]
            limits.el_min = values["el_min"]
            limits.el_max = values["el_max"]
            limits.az_margin = values["az_margin"]
            limits.el_margin = values["el_margin"]
            limits.max_jog_seconds = values["max_jog_seconds"]
            limits.poll_interval = values["poll_interval"]
            self.app.configs[name].park_az = parsed_park[name]["park_az"]
            self.app.configs[name].park_el = parsed_park[name]["park_el"]
            if name in self.app.panels:
                self.app.panels[name].sync_config_settings()

        self._format_fields(parsed)
        self._format_park_fields(parsed_park)
        self.app.save_config("Limits saved.")
        self.destroy()

    def _format_fields(self, parsed: dict[str, dict[str, float]]) -> None:
        formats = {
            "az_min": "{:0.0f}",
            "az_max": "{:0.0f}",
            "el_min": "{:0.0f}",
            "el_max": "{:0.0f}",
            "az_margin": "{:0.1f}",
            "el_margin": "{:0.1f}",
            "max_jog_seconds": "{:0.0f}",
            "poll_interval": "{:0.1f}",
        }
        for name, values in parsed.items():
            for key, value in values.items():
                self.entries[name][key].set(formats[key].format(value))

    def _format_park_fields(self, parsed: dict[str, dict[str, float]]) -> None:
        for name, values in parsed.items():
            self.park_entries[name]["park_az"].set(f"{values['park_az']:0.0f}")
            self.park_entries[name]["park_el"].set(f"{values['park_el']:0.0f}")

    def _validate_limits(self, name: str, values: dict[str, float]) -> None:
        if not (0.0 <= values["az_min"] <= 360.0 and 0.0 <= values["az_max"] <= 360.0):
            raise RuntimeError(f"{name}: AZ limits must be 0..360 degrees.")
        if values["el_min"] >= values["el_max"]:
            raise RuntimeError(f"{name}: EL min must be less than EL max.")
        if not (0.0 <= values["el_min"] <= 90.0 and 0.0 <= values["el_max"] <= 90.0):
            raise RuntimeError(f"{name}: EL limits must be 0..90 degrees.")
        if values["az_margin"] < 0.0 or values["el_margin"] < 0.0:
            raise RuntimeError(f"{name}: margins cannot be negative.")
        if not (1.0 <= values["max_jog_seconds"] <= 600.0):
            raise RuntimeError(f"{name}: max jog must be 1..600 seconds.")
        if not (0.05 <= values["poll_interval"] <= 5.0):
            raise RuntimeError(f"{name}: poll interval must be 0.05..5.0 seconds.")

    def _validate_park(self, name: str, values: dict[str, float], limits: dict[str, float]) -> None:
        if not (0.0 <= values["park_az"] <= 360.0):
            raise RuntimeError(f"{name}: park AZ must be 0..360 degrees.")
        if not (0.0 <= values["park_el"] <= 90.0):
            raise RuntimeError(f"{name}: park EL must be 0..90 degrees.")
        test_limits = self.app.configs[name].limits
        old_values = (
            test_limits.az_min,
            test_limits.az_max,
            test_limits.el_min,
            test_limits.el_max,
        )
        try:
            test_limits.az_min = limits["az_min"]
            test_limits.az_max = limits["az_max"]
            test_limits.el_min = limits["el_min"]
            test_limits.el_max = limits["el_max"]
            test_limits.assert_position_allowed(values["park_az"], values["park_el"])
        except Exception as exc:
            raise RuntimeError(f"{name}: park position is outside limits: {exc}") from exc
        finally:
            (
                test_limits.az_min,
                test_limits.az_max,
                test_limits.el_min,
                test_limits.el_max,
            ) = old_values


class ObserverDialog(tk.Toplevel):
    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Observer")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.latitude_var = tk.StringVar(value=f"{app.site.latitude:0.6f}")
        self.longitude_var = tk.StringVar(value=f"{app.site.longitude:0.6f}")

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text="Latitude").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.latitude_var, width=12).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(body, text="Longitude").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.longitude_var, width=12).grid(row=1, column=1, sticky="w", pady=2)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self.save).grid(row=0, column=2)

    def save(self) -> None:
        try:
            site = SiteConfig(
                latitude=float(self.latitude_var.get()),
                longitude=float(self.longitude_var.get()),
                selected_source=self.app.site.selected_source,
                track_interval_seconds=self.app.site.track_interval_seconds,
                az_track_tolerance_degrees=self.app.site.az_track_tolerance_degrees,
                el_track_tolerance_degrees=self.app.site.el_track_tolerance_degrees,
                az_stop_tolerance_degrees=self.app.site.az_stop_tolerance_degrees,
                el_stop_tolerance_degrees=self.app.site.el_stop_tolerance_degrees,
                az_slow_speed=self.app.site.az_slow_speed,
                el_slow_speed=self.app.site.el_slow_speed,
                az_slow_threshold_degrees=self.app.site.az_slow_threshold_degrees,
                el_slow_threshold_degrees=self.app.site.el_slow_threshold_degrees,
                log_retention_days=self.app.site.log_retention_days,
                log_level=self.app.site.log_level,
                timeout_enabled=self.app.site.timeout_enabled,
                timeout_minutes=self.app.site.timeout_minutes,
                timeout_action=self.app.site.timeout_action,
            )
            self.app.validate_observer(site)
        except ValueError:
            messagebox.showerror("Invalid Observer", "Observer location must be numeric.", parent=self)
            return
        except RuntimeError as exc:
            messagebox.showerror("Invalid Observer", str(exc), parent=self)
            return
        self.app.site = site
        self.app.save_site_settings("Observer saved.")
        self.destroy()


class SourcesDialog(tk.Toplevel):
    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Sources")
        self.resizable(True, False)
        self.transient(app)
        self.grab_set()
        self.sources = {name: SourceConfig(source.name, source.ra_hours, source.dec_degrees, source.flux_4800_mhz) for name, source in app.sources.items()}
        self.name_var = tk.StringVar()
        self.ra_var = tk.StringVar()
        self.dec_var = tk.StringVar()
        self.flux_var = tk.StringVar()
        self.position_after_id: Optional[str] = None
        self.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(body, columns=("source", "ra", "dec", "az", "el", "flux"), show="headings", height=7)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.heading("source", text="Source")
        self.tree.heading("ra", text="RA h")
        self.tree.heading("dec", text="Dec deg")
        self.tree.heading("az", text="AZ")
        self.tree.heading("el", text="EL")
        self.tree.heading("flux", text="4800 MHz")
        self.tree.column("source", width=120, anchor="w")
        self.tree.column("ra", width=80, anchor="e")
        self.tree.column("dec", width=80, anchor="e")
        self.tree.column("az", width=70, anchor="e")
        self.tree.column("el", width=70, anchor="e")
        self.tree.column("flux", width=90, anchor="e")
        self.tree.grid(row=0, column=0, columnspan=4, sticky="nsew", pady=(0, 8))
        scrollbar.grid(row=0, column=4, sticky="ns", pady=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", self.load_selected)

        fields = ttk.Frame(body)
        fields.grid(row=1, column=0, columnspan=5, sticky="ew")
        self._field(fields, "Name", self.name_var, 0, 16)
        self._field(fields, "RA h", self.ra_var, 1, 10)
        self._field(fields, "Dec deg", self.dec_var, 2, 10)
        self._field(fields, "4800 MHz flux", self.flux_var, 3, 10)

        ttk.Button(body, text="Add/Update", command=self.add_update).grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(body, text="Remove", command=self.remove).grid(row=2, column=1, sticky="ew", pady=(8, 0), padx=(6, 0))
        ttk.Button(body, text="Select", command=self.select_source).grid(row=2, column=2, sticky="ew", pady=(8, 0), padx=(6, 0))
        ttk.Button(body, text="Close", command=self.close).grid(row=2, column=3, sticky="ew", pady=(8, 0), padx=(6, 0))
        self.refresh_tree()
        self.update_current_position()

    def _field(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int, width: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="w", pady=2)

    def refresh_tree(self) -> None:
        selection = self.tree.selection()
        focus = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        for name in sorted(self.sources):
            source = self.sources[name]
            self.tree.insert("", "end", iid=name, values=self.source_row_values(source))
        if selection and selection[0] in self.sources:
            self.tree.selection_set(selection[0])
            self.tree.focus(selection[0])
        elif focus in self.sources:
            self.tree.focus(focus)
        elif self.app.site.selected_source in self.sources:
            self.tree.selection_set(self.app.site.selected_source)
            self.tree.focus(self.app.site.selected_source)

    def source_row_values(self, source: SourceConfig) -> tuple[str, str, str, str, str, str]:
        position = source_position(
            source.name,
            source.ra_hours,
            source.dec_degrees,
            self.app.site.latitude,
            self.app.site.longitude,
        )
        return (
            source.name,
            f"{source.ra_hours:0.6f}",
            f"{source.dec_degrees:0.4f}",
            f"{position.azimuth:0.2f}",
            f"{position.elevation:0.2f}",
            f"{source.flux_4800_mhz:0.1f}",
        )

    def load_selected(self, _event: Optional[object] = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        source = self.sources[selection[0]]
        self.name_var.set(source.name)
        self.ra_var.set(f"{source.ra_hours:0.6f}")
        self.dec_var.set(f"{source.dec_degrees:0.4f}")
        self.flux_var.set(f"{source.flux_4800_mhz:0.1f}")

    def update_current_position(self) -> None:
        for name, source in self.sources.items():
            if self.tree.exists(name):
                self.tree.item(name, values=self.source_row_values(source))
        self.position_after_id = self.after(1000, self.update_current_position)

    def add_update(self) -> None:
        try:
            name = self.name_var.get().strip()
            if not name:
                raise RuntimeError("Source name is required.")
            source = SourceConfig(
                name=name,
                ra_hours=float(self.ra_var.get()),
                dec_degrees=float(self.dec_var.get()),
                flux_4800_mhz=float(self.flux_var.get()),
            )
            self.validate_source(source)
        except ValueError:
            messagebox.showerror("Invalid Source", "RA, Dec, and flux must be numeric.", parent=self)
            return
        except RuntimeError as exc:
            messagebox.showerror("Invalid Source", str(exc), parent=self)
            return
        self.sources[source.name] = source
        self.refresh_tree()
        self.tree.selection_set(source.name)
        self.tree.focus(source.name)

    def remove(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.sources.pop(selection[0], None)
        if self.app.site.selected_source == selection[0]:
            self.app.site.selected_source = ""
        self.name_var.set("")
        self.ra_var.set("")
        self.dec_var.set("")
        self.flux_var.set("")
        self.refresh_tree()

    def select_source(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showerror("No Source", "Select a source first.", parent=self)
            return
        was_tracking_source = self.app.tracking_active and self.app.tracking_kind == "source"
        self.app.site.selected_source = selection[0]
        self.save_to_app(f"Selected source {selection[0]}.")
        if was_tracking_source:
            self.app.start_tracking("source")

    def close(self) -> None:
        if self.position_after_id is not None:
            self.after_cancel(self.position_after_id)
            self.position_after_id = None
        self.save_to_app("Sources saved.")
        self.destroy()

    def save_to_app(self, message: str) -> None:
        self.app.sources = self.sources
        save_sources(self.app.config_path, self.app.sources, self.app.site.selected_source)
        self.app.save_site_settings(message)

    def validate_source(self, source: SourceConfig) -> None:
        if not (0.0 <= source.ra_hours < 24.0):
            raise RuntimeError("RA must be 0.0 <= RA < 24.0 hours.")
        if not (-90.0 <= source.dec_degrees <= 90.0):
            raise RuntimeError("Dec must be -90..90 degrees.")
        if source.flux_4800_mhz < 0.0:
            raise RuntimeError("Flux cannot be negative.")


class CalibrationDialog(tk.Toplevel):
    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.closed = False
        self.title("Calibration")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.entries: dict[str, dict[str, tk.StringVar]] = {}
        self.tab_names: dict[str, tk.Widget] = {}
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.tabs = ttk.Notebook(self)
        self.tabs.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        for name, panel in app.panels.items():
            frame = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(frame, text=name)
            self.tab_names[name] = frame
            az_var = tk.StringVar()
            el_var = tk.StringVar()
            raw_az_var = tk.StringVar(value="--")
            raw_el_var = tk.StringVar(value="--")
            az_offset_var = tk.StringVar()
            el_offset_var = tk.StringVar()
            position = panel.session.last_position if panel.session else None
            if position:
                az_var.set(f"{position.azimuth:0.2f}")
                el_var.set(f"{position.elevation:0.2f}")
                raw_az_var.set(f"{position.raw_azimuth:0.2f}")
                raw_el_var.set(f"{position.raw_elevation:0.2f}")
            config = panel.session.config if panel.session else panel.config or app.configs.get(name)
            if config:
                az_offset_var.set(f"{config.calibration.az_offset:0.2f}")
                el_offset_var.set(f"{config.calibration.el_offset:0.2f}")
            ttk.Label(frame, text="Actual AZ").grid(row=0, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=az_var, width=8).grid(row=0, column=1, sticky="w", pady=2)
            ttk.Label(frame, text="Actual EL").grid(row=1, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=el_var, width=8).grid(row=1, column=1, sticky="w", pady=2)
            ttk.Separator(frame, orient="horizontal").grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
            ttk.Label(frame, text="Raw AZ").grid(row=3, column=0, sticky="w", pady=2)
            ttk.Label(frame, textvariable=raw_az_var).grid(row=3, column=1, sticky="w", pady=2)
            ttk.Label(frame, text="Raw EL").grid(row=4, column=0, sticky="w", pady=2)
            ttk.Label(frame, textvariable=raw_el_var).grid(row=4, column=1, sticky="w", pady=2)
            ttk.Separator(frame, orient="horizontal").grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)
            ttk.Label(frame, text="AZ offset").grid(row=6, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=az_offset_var, width=8).grid(row=6, column=1, sticky="w", pady=2)
            ttk.Label(frame, text="EL offset").grid(row=7, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=el_offset_var, width=8).grid(row=7, column=1, sticky="w", pady=2)
            ttk.Button(frame, text="Calibrate Manual", command=lambda n=name: self.calibrate_manual(n)).grid(
                row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0)
            )
            ttk.Button(frame, text="Calibrate From Target", command=lambda n=name: self.calibrate_from_target(n)).grid(
                row=9, column=0, columnspan=2, sticky="ew", pady=(6, 0)
            )
            ttk.Button(frame, text="Apply Offsets", command=lambda n=name: self.apply_offsets(n)).grid(
                row=10, column=0, columnspan=2, sticky="ew", pady=(6, 0)
            )
            self.entries[name] = {
                "actual_az": az_var,
                "actual_el": el_var,
                "raw_az": raw_az_var,
                "raw_el": raw_el_var,
                "az_offset": az_offset_var,
                "el_offset": el_offset_var,
            }

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Close", command=self.close).grid(row=0, column=1)
        self.refresh_live_positions()

    def refresh_live_positions(self) -> None:
        for name, panel in self.app.panels.items():
            if not panel.session:
                continue
            self.app.run_worker(
                panel.session.read_position,
                lambda position, n=name: self.app.refresh_calibration_views(n, position),
                lambda text, n=name: self.app.set_status(f"{n}: {text}"),
            )

    def select_antenna(self, name: str) -> None:
        frame = self.tab_names.get(name)
        if frame:
            self.tabs.select(frame)

    def refresh_offsets(self, name: Optional[str] = None, position: Optional[Position] = None) -> None:
        if self.closed:
            return
        names = [name] if name else list(self.entries)
        for entry_name in names:
            values = self.entries.get(entry_name)
            panel = self.app.panels.get(entry_name)
            config = panel.session.config if panel and panel.session else self.app.configs.get(entry_name)
            if not values or not config:
                continue
            values["az_offset"].set(f"{config.calibration.az_offset:0.2f}")
            values["el_offset"].set(f"{config.calibration.el_offset:0.2f}")
            panel_position = position if entry_name == name else panel.session.last_position if panel and panel.session else None
            if panel_position:
                values["actual_az"].set(f"{panel_position.azimuth:0.2f}")
                values["actual_el"].set(f"{panel_position.elevation:0.2f}")
                values["raw_az"].set(f"{panel_position.raw_azimuth:0.2f}")
                values["raw_el"].set(f"{panel_position.raw_elevation:0.2f}")

    def close(self) -> None:
        self.closed = True
        if self.app.calibration_dialog is self:
            self.app.calibration_dialog = None
        self.destroy()

    def calibrate_manual(self, name: str) -> None:
        values = self.entries[name]
        try:
            actual_az = float(values["actual_az"].get())
            actual_el = float(values["actual_el"].get())
        except ValueError:
            messagebox.showerror("Calibration", "Actual AZ and EL must be numeric.", parent=self)
            return
        self.calibrate_to_position(name, actual_az, actual_el)

    def calibrate_from_target(self, name: str) -> None:
        target = self.app.current_target
        if target is None:
            messagebox.showerror("Calibration", "No current target is available.", parent=self)
            return
        self.calibrate_to_position(name, target.azimuth, target.elevation)

    def calibrate_to_position(self, name: str, actual_az: float, actual_el: float) -> None:
        panel = self.app.panels.get(name)
        if not panel or not panel.session:
            messagebox.showerror("Calibration", f"{name} is not connected.", parent=self)
            return
        if not (0.0 <= actual_az <= 360.0 and 0.0 <= actual_el <= 90.0):
            messagebox.showerror("Calibration", "Calibration AZ must be 0..360 and EL 0..90.", parent=self)
            return

        def work() -> Position:
            position = panel.session.calibrate(actual_az, actual_el)
            self.app.save_config("Calibration saved.")
            self.app.event_log.info(
                "CALIBRATION_SAVE",
                antenna=name,
                method="manual_or_target",
                actual_az=actual_az,
                actual_el=actual_el,
                az_offset=panel.session.config.calibration.az_offset,
                el_offset=panel.session.config.calibration.el_offset,
            )
            panel.session.update_oled("CAL", activity="STOPPED")
            return position

        self.app.run_worker(
            work,
            lambda position, n=name, p=panel: self.finish_calibration(n, p, position),
            lambda text: messagebox.showerror("Calibration", text, parent=self),
        )

    def apply_offsets(self, name: str) -> None:
        panel = self.app.panels.get(name)
        config = self.app.configs.get(name)
        if config is None:
            messagebox.showerror("Calibration", f"{name} has no config.", parent=self)
            return
        values = self.entries[name]
        try:
            az_offset = float(values["az_offset"].get())
            el_offset = float(values["el_offset"].get())
        except ValueError:
            messagebox.showerror("Calibration", "Offsets must be numeric.", parent=self)
            return
        if not (-360.0 <= az_offset <= 360.0 and -90.0 <= el_offset <= 90.0):
            messagebox.showerror("Calibration", "AZ offset must be -360..360 and EL offset -90..90.", parent=self)
            return

        def work() -> Optional[Position]:
            config.calibration.az_offset = az_offset
            config.calibration.el_offset = el_offset
            self.app.save_config("Calibration offsets saved.")
            self.app.event_log.info(
                "CALIBRATION_OFFSET_APPLY",
                antenna=name,
                az_offset=az_offset,
                el_offset=el_offset,
            )
            if panel and panel.session:
                with panel.session.lock:
                    position = panel.session.read_position_locked()
                    panel.session.update_oled("CAL", activity="STOPPED")
                    return position
            return None

        self.app.run_worker(
            work,
            lambda position, n=name, p=panel: self.finish_offset_apply(n, p, position),
            lambda text: messagebox.showerror("Calibration", text, parent=self),
        )

    def finish_calibration(
        self,
        name: str,
        panel: "AntennaPanel",
        position: Position,
    ) -> None:
        values = self.entries[name]
        values["actual_az"].set(f"{position.azimuth:0.2f}")
        values["actual_el"].set(f"{position.elevation:0.2f}")
        values["raw_az"].set(f"{position.raw_azimuth:0.2f}")
        values["raw_el"].set(f"{position.raw_elevation:0.2f}")
        values["az_offset"].set(f"{panel.session.config.calibration.az_offset:0.2f}")
        values["el_offset"].set(f"{panel.session.config.calibration.el_offset:0.2f}")
        panel.clear_message()
        panel.update_position(position)
        self.app.refresh_calibration_views(name, position)

    def finish_offset_apply(self, name: str, panel: Optional["AntennaPanel"], position: Optional[Position]) -> None:
        config = self.app.configs[name]
        values = self.entries[name]
        values["az_offset"].set(f"{config.calibration.az_offset:0.2f}")
        values["el_offset"].set(f"{config.calibration.el_offset:0.2f}")
        if panel and position:
            values["actual_az"].set(f"{position.azimuth:0.2f}")
            values["actual_el"].set(f"{position.elevation:0.2f}")
            values["raw_az"].set(f"{position.raw_azimuth:0.2f}")
            values["raw_el"].set(f"{position.raw_elevation:0.2f}")
            panel.clear_message()
            panel.update_position(position)
        self.app.refresh_calibration_views(name, position)


class PeakCalibrationDialog(tk.Toplevel):
    SOURCE_LABELS = ("Sun", "Moon", "Selected Source")

    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Peak Calibration")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.track_stop_event = threading.Event()
        self.jog_stop_event = threading.Event()
        self.tracking_axis: Optional[Axis] = None
        self.tracking_session: Optional[SafeAntenna] = None
        self.jog_thread_active = False
        self.closed = False

        connected_names = list(app.sessions) or list(app.configs)
        self.antenna_var = tk.StringVar(value=connected_names[0] if connected_names else "")
        self.source_var = tk.StringVar(value=app.default_peak_cal_source_label())
        self.status_var = tk.StringVar(value="Select source and antenna.")
        self.target_var = tk.StringVar(value="Source AZ -- EL --")
        self.position_var = tk.StringVar(value="Antenna AZ -- EL --")
        self.raw_var = tk.StringVar(value="Raw AZ -- EL --")
        self.offset_var = tk.StringVar(value="Offsets AZ -- EL --")

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")

        ttk.Label(body, text="Source").grid(row=0, column=0, sticky="w", pady=2)
        source_combo = ttk.Combobox(
            body,
            textvariable=self.source_var,
            values=self.SOURCE_LABELS,
            width=18,
            state="readonly",
        )
        source_combo.grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(body, text="Antenna").grid(row=1, column=0, sticky="w", pady=2)
        antenna_combo = ttk.Combobox(
            body,
            textvariable=self.antenna_var,
            values=connected_names,
            width=18,
            state="readonly",
        )
        antenna_combo.grid(row=1, column=1, sticky="w", pady=2)
        source_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_display(live=True))
        antenna_combo.bind("<<ComboboxSelected>>", lambda _event: self.antenna_changed())

        ttk.Separator(body, orient="horizontal").grid(row=2, column=0, columnspan=4, sticky="ew", pady=8)
        ttk.Label(body, textvariable=self.target_var).grid(row=3, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(body, textvariable=self.position_var).grid(row=4, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(body, textvariable=self.raw_var).grid(row=5, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(body, textvariable=self.offset_var).grid(row=6, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(body, textvariable=self.status_var, foreground="red").grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))

        tracking = ttk.LabelFrame(body, text="Axis Tracking")
        tracking.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(tracking, text="Track AZ Only", command=lambda: self.start_axis_tracking(Axis.AZIMUTH)).grid(
            row=0, column=0, sticky="ew", padx=2, pady=2
        )
        ttk.Button(tracking, text="Track EL Only", command=lambda: self.start_axis_tracking(Axis.ELEVATION)).grid(
            row=0, column=1, sticky="ew", padx=2, pady=2
        )
        ttk.Button(tracking, text="Stop Tracking", command=self.stop_axis_tracking).grid(row=0, column=2, sticky="ew", padx=2, pady=2)

        jog = ttk.LabelFrame(body, text="Manual Peak Jog")
        jog.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        for col in range(3):
            jog.columnconfigure(col, weight=1)
        self._hold_button(jog, "EL+", Direction.EL_UP).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(jog, "AZ-", Direction.AZ_CCW).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(jog, text="STOP", command=self.stop_jog).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(jog, "AZ+", Direction.AZ_CW).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        self._hold_button(jog, "EL-", Direction.EL_DOWN).grid(row=2, column=1, sticky="ew", padx=2, pady=2)

        locks = ttk.LabelFrame(body, text="Calibration Lock")
        locks.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(locks, text="LOCK AZ CAL", command=lambda: self.lock_axis_calibration(Axis.AZIMUTH)).grid(
            row=0, column=0, sticky="ew", padx=2, pady=2
        )
        ttk.Button(locks, text="LOCK EL CAL", command=lambda: self.lock_axis_calibration(Axis.ELEVATION)).grid(
            row=0, column=1, sticky="ew", padx=2, pady=2
        )

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Close", command=self.close).grid(row=0, column=1)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_display(live=True)

    def _hold_button(self, master: tk.Misc, text: str, direction: Direction) -> ttk.Button:
        button = ttk.Button(master, text=text)
        button.bind("<ButtonPress-1>", lambda _event: self.start_jog(direction))
        button.bind("<ButtonRelease-1>", lambda _event: self.stop_jog())
        return button

    def source_kind(self) -> str:
        label = self.source_var.get()
        if label == "Sun":
            return "sun"
        if label == "Moon":
            return "moon"
        return "source"

    def selected_session(self) -> Optional[SafeAntenna]:
        return self.app.sessions.get(self.antenna_var.get())

    def selected_panel(self) -> Optional["AntennaPanel"]:
        return self.app.panels.get(self.antenna_var.get())

    def selected_config(self) -> Optional[AntennaConfig]:
        session = self.selected_session()
        if session:
            return session.config
        return self.app.configs.get(self.antenna_var.get())

    def current_peak_target(self) -> TargetPosition:
        return self.app.target_for_kind(self.source_kind())

    def set_source_label(self, label: str) -> None:
        if label in self.SOURCE_LABELS:
            self.source_var.set(label)
            self.refresh_display(live=True)

    def antenna_changed(self) -> None:
        self.app.select_calibration_antenna(self.antenna_var.get())
        self.refresh_display(live=True)

    def refresh_display(self, live: bool = False) -> None:
        if self.closed:
            return
        try:
            target = self.current_peak_target()
            self.target_var.set(f"{target.name} AZ {target.azimuth:0.2f} EL {target.elevation:0.2f}")
        except Exception as exc:
            self.target_var.set(f"Source unavailable: {exc}")
        session = self.selected_session()
        config = self.selected_config()
        if live and session:
            self.app.run_worker(
                session.read_position,
                lambda position, n=self.antenna_var.get(): self.finish_live_refresh(n, position),
                self.show_status,
            )
            if config:
                self.offset_var.set(
                    f"Offsets AZ {config.calibration.az_offset:+0.2f} EL {config.calibration.el_offset:+0.2f}"
                )
            self.after(1000, self.refresh_display)
            return
        position = session.last_position if session else None
        if position:
            self.position_var.set(f"Antenna AZ {position.azimuth:0.2f} EL {position.elevation:0.2f}")
            self.raw_var.set(f"Raw AZ {position.raw_azimuth:0.2f} EL {position.raw_elevation:0.2f}")
        else:
            self.position_var.set("Antenna AZ -- EL --")
            self.raw_var.set("Raw AZ -- EL --")
        if config:
            self.offset_var.set(
                f"Offsets AZ {config.calibration.az_offset:+0.2f} EL {config.calibration.el_offset:+0.2f}"
            )
        self.after(1000, self.refresh_display)

    def finish_live_refresh(self, name: str, position: Position) -> None:
        if self.closed or name != self.antenna_var.get():
            return
        panel = self.selected_panel()
        if panel:
            panel.update_position(position)
        self.app.refresh_calibration_views(name, position)
        self.status_var.set("Ready.")

    def refresh_offsets(self, name: Optional[str] = None, position: Optional[Position] = None) -> None:
        if self.closed:
            return
        selected_name = self.antenna_var.get()
        if name is not None and name != selected_name:
            return
        config = self.selected_config()
        if config:
            self.offset_var.set(
                f"Offsets AZ {config.calibration.az_offset:+0.2f} EL {config.calibration.el_offset:+0.2f}"
            )
        if position:
            self.position_var.set(f"Antenna AZ {position.azimuth:0.2f} EL {position.elevation:0.2f}")
            self.raw_var.set(f"Raw AZ {position.raw_azimuth:0.2f} EL {position.raw_elevation:0.2f}")

    def start_axis_tracking(self, axis: Axis) -> None:
        session = self.selected_session()
        if session is None:
            self.status_var.set("Connect the selected antenna first.")
            return
        try:
            self.app.prepare_peak_calibration_owner()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        try:
            self.app.validate_site(self.app.site)
            self.current_peak_target()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        if self.tracking_axis is not None:
            self.status_var.set("Stop current Peak Cal tracking before starting another axis.")
            return
        self.track_stop_event = threading.Event()
        self.tracking_axis = axis
        self.tracking_session = session
        threading.Thread(target=self.axis_tracking_loop, args=(session, axis, self.track_stop_event), daemon=True).start()
        self.status_var.set(f"Tracking {axis_label(axis)} only. Manually peak the other axis.")

    def axis_tracking_loop(self, session: SafeAntenna, axis: Axis, stop_event: threading.Event) -> None:
        panel = self.selected_panel()
        try:
            while not stop_event.is_set():
                target = self.current_peak_target()
                target_value = target.azimuth if axis == Axis.AZIMUTH else target.elevation
                self.app.events.put(("ok", self.app.apply_target_position, target))
                if panel:
                    self.app.events.put(("ok", panel.set_tracking_status, f"CAL {axis_label(axis)}"))

                def progress(position: Position) -> None:
                    if panel:
                        self.app.events.put(("position", panel.update_position, position))
                    session.update_oled_position(target.azimuth, target.elevation, f"CAL {axis_label(axis)}")

                position = session.guarded_slew_axis_to(
                    axis,
                    target_value,
                    session.config.az_track_speed if axis == Axis.AZIMUTH else session.config.el_track_speed,
                    stop_event,
                    self.app.az_tracking_start_tolerance() if axis == Axis.AZIMUTH else self.app.el_tracking_start_tolerance(),
                    self.app.az_tracking_stop_tolerance() if axis == Axis.AZIMUTH else self.app.el_tracking_stop_tolerance(),
                    self.app.site.az_slow_speed if axis == Axis.AZIMUTH else self.app.site.el_slow_speed,
                    self.app.site.az_slow_threshold_degrees if axis == Axis.AZIMUTH else self.app.site.el_slow_threshold_degrees,
                    progress,
                )
                if panel:
                    self.app.events.put(("position", panel.update_position, position))
                session.update_oled(target.name[:8].upper(), target.azimuth, target.elevation, f"CAL {axis_label(axis)}")
                wait_until = time.monotonic() + max(0.1, self.app.site.track_interval_seconds)
                while not stop_event.is_set() and time.monotonic() < wait_until:
                    time.sleep(0.05)
        except Exception as exc:
            self.app.events.put(("error", self.show_status, str(exc)))
        finally:
            if panel:
                self.app.events.put(("ok", panel.set_tracking_status, "STOPPED"))

    def stop_axis_tracking(self) -> None:
        self.track_stop_event.set()
        session = self.tracking_session
        if session and self.tracking_axis is not None:
            self.app.run_worker(lambda s=session: (s.stop_all(), s.update_oled_activity("STOPPED")), lambda _result: None, self.show_status)
        self.tracking_axis = None
        self.tracking_session = None
        self.status_var.set("Peak calibration tracking stopped.")

    def start_jog(self, direction: Direction) -> None:
        session = self.selected_session()
        if session is None or self.jog_thread_active:
            return
        try:
            self.app.prepare_peak_calibration_owner()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        jog_axis = Axis.AZIMUTH if direction in (Direction.AZ_CW, Direction.AZ_CCW) else Axis.ELEVATION
        if self.tracking_axis == jog_axis:
            self.status_var.set(f"Stop {axis_label(jog_axis)} tracking before jogging that axis.")
            return
        self.jog_stop_event.clear()
        self.jog_thread_active = True
        panel = self.selected_panel()
        speed = panel.speed_value if panel else session.config.gui_speed

        def progress(position: Position) -> None:
            if panel:
                self.app.events.put(("position", panel.update_position, position))
            session.update_oled_position(activity=f"PEAK {axis_label(jog_axis)}")

        def work() -> Position:
            session.update_oled("PEAK", activity=f"PEAK {axis_label(jog_axis)}")
            session.guarded_jog(direction, speed, None, self.jog_stop_event, progress)
            position = session.read_position()
            session.update_oled_activity("STOPPED")
            return position

        self.app.run_worker(work, self.finish_jog, self.finish_jog_fault)

    def stop_jog(self) -> None:
        self.jog_stop_event.set()

    def finish_jog(self, position: Position) -> None:
        self.jog_thread_active = False
        panel = self.selected_panel()
        if panel:
            panel.update_position(position)
        self.status_var.set("Peak jog ready.")

    def finish_jog_fault(self, text: str) -> None:
        self.jog_thread_active = False
        self.status_var.set(text)

    def lock_axis_calibration(self, axis: Axis) -> None:
        session = self.selected_session()
        if session is None:
            self.status_var.set("Connect the selected antenna first.")
            return
        try:
            self.app.prepare_peak_calibration_owner()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        if self.tracking_axis == axis:
            self.status_var.set(f"Track the other axis before locking {axis_label(axis)} calibration.")
            return

        def work() -> tuple[Position, TargetPosition, float, float]:
            target = self.current_peak_target()
            actual = target.azimuth if axis == Axis.AZIMUTH else target.elevation
            old_offset = (
                session.config.calibration.az_offset if axis == Axis.AZIMUTH else session.config.calibration.el_offset
            )
            position = session.calibrate_axis(axis, actual)
            self.app.save_config("Peak calibration saved.")
            session.update_oled(target.name[:8].upper(), target.azimuth, target.elevation, f"CAL {axis_label(axis)}")
            new_offset = (
                session.config.calibration.az_offset if axis == Axis.AZIMUTH else session.config.calibration.el_offset
            )
            self.app.event_log.info(
                "PEAK_CAL_LOCK",
                antenna=self.antenna_var.get(),
                axis=axis_label(axis),
                target=target.name,
                target_az=target.azimuth,
                target_el=target.elevation,
                old_offset=old_offset,
                new_offset=new_offset,
            )
            return position, target, old_offset, new_offset

        self.app.run_worker(
            work,
            lambda result, a=axis: self.finish_axis_lock(a, result),
            self.show_status,
        )

    def finish_axis_lock(self, axis: Axis, result: tuple[Position, TargetPosition, float, float]) -> None:
        position, target, old_offset, new_offset = result
        panel = self.selected_panel()
        if panel:
            panel.update_position(position)
            panel.clear_message()
        self.app.refresh_calibration_views(self.antenna_var.get(), position)
        self.status_var.set(
            f"Locked {axis_label(axis)} to {target.name}: offset {old_offset:+0.2f} -> {new_offset:+0.2f}."
        )

    def show_status(self, text: str) -> None:
        self.status_var.set(text)

    def close(self) -> None:
        self.closed = True
        self.track_stop_event.set()
        self.jog_stop_event.set()
        if self.app.peak_calibration_dialog is self:
            self.app.peak_calibration_dialog = None
        self.destroy()


class EncodersDialog(tk.Toplevel):
    COLUMNS = (
        "Antenna",
        "Axis",
        "Addr",
        "Type",
        "Model",
        "Version",
        "Config",
        "Serial",
        "Date",
        "Resolution",
        "Position",
        "Mode",
    )

    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Encoders")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.position_vars: dict[tuple[str, Axis], tk.StringVar] = {}
        self.row_widgets: list[tk.Widget] = []

        self.body = ttk.Frame(self, padding=10)
        self.body.grid(row=0, column=0, sticky="nsew")
        for column, title in enumerate(self.COLUMNS):
            ttk.Label(self.body, text=title, font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=column, sticky="w", padx=3, pady=(0, 4)
            )

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Scan", command=self.scan).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Close", command=self.destroy).grid(row=0, column=2)
        self.scan()

    def scan(self) -> None:
        if not self.app.sessions:
            self.show_error("Connect antennas before encoder scan.")
            return

        def work() -> dict[str, dict[Axis, EncoderInfo]]:
            return {name: session.scan_encoders() for name, session in self.app.sessions.items()}

        self.app.run_worker(work, self.finish_scan, self.show_error)

    def finish_scan(self, result: dict[str, dict[Axis, EncoderInfo]]) -> None:
        for widget in self.row_widgets:
            widget.destroy()
        self.row_widgets.clear()
        self.position_vars.clear()
        row = 1
        for name, axes in result.items():
            for axis in (Axis.AZIMUTH, Axis.ELEVATION):
                info = axes[axis]
                self.add_row(row, name, info)
                row += 1

    def add_row(self, row: int, name: str, info: EncoderInfo) -> None:
        values = (
            name,
            "AZ" if info.axis == Axis.AZIMUTH else "EL",
            str(info.address),
            info.encoder_type,
            str(info.model),
            info.version,
            info.config,
            str(info.serial),
            info.date,
            str(info.resolution),
            f"{info.position:0.2f}",
            str(info.mode),
        )
        for column, value in enumerate(values):
            if self.COLUMNS[column] == "Position":
                var = tk.StringVar(value=value)
                entry = ttk.Entry(self.body, textvariable=var, width=8)
                entry.grid(row=row, column=column, sticky="ew", padx=3, pady=2)
                self.position_vars[(name, info.axis)] = var
                self.row_widgets.append(entry)
            else:
                label = ttk.Label(self.body, text=value)
                label.grid(row=row, column=column, sticky="w", padx=3, pady=2)
                self.row_widgets.append(label)
        button = ttk.Button(self.body, text="Set", command=lambda n=name, a=info.axis: self.set_position(n, a))
        button.grid(row=row, column=len(self.COLUMNS), sticky="ew", padx=3, pady=2)
        self.row_widgets.append(button)

    def set_position(self, name: str, axis: Axis) -> None:
        panel = self.app.panels.get(name)
        session = self.app.sessions.get(name)
        if panel is None or session is None:
            self.show_error(f"{name} is not connected.")
            return
        try:
            position = float(self.position_vars[(name, axis)].get())
        except ValueError:
            self.show_error("Position must be numeric.")
            return
        axis_label = "AZ" if axis == Axis.AZIMUTH else "EL"
        if axis == Axis.AZIMUTH and not (0.0 <= position <= 360.0):
            self.show_error("AZ position must be 0..360 degrees.")
            return
        if axis == Axis.ELEVATION and not (0.0 <= position <= 90.0):
            self.show_error("EL Arduino position must be 0..90 degrees.")
            return
        if not messagebox.askyesno(
            "Set Encoder Position",
            f"Set {name} {axis_label} Arduino position to {position:0.2f}?\n\n"
            "This resets the WT6 software calibration offset for this axis to zero.",
            parent=self,
        ):
            return

        def work() -> Position:
            updated = session.set_encoder_position(axis, position)
            self.app.save_config("Encoder position saved.")
            session.update_oled("CAL", activity="STOPPED")
            return updated

        self.app.run_worker(
            work,
            lambda updated, p=panel: self.finish_set(p, updated),
            self.show_error,
        )

    def finish_set(self, panel: "AntennaPanel", position: Position) -> None:
        panel.clear_message()
        panel.update_position(position)
        self.scan()

    def show_error(self, text: str) -> None:
        messagebox.showerror("Encoders", text, parent=self)


class TrackingDialog(tk.Toplevel):
    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Tracking")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.az_speed_vars: dict[str, tk.StringVar] = {}
        self.el_speed_vars: dict[str, tk.StringVar] = {}
        self.max_jog_vars: dict[str, tk.StringVar] = {}
        self.az_lh_comp_vars: dict[str, tk.StringVar] = {}
        self.interval_var = tk.StringVar(value=f"{app.site.track_interval_seconds:0.1f}")
        self.log_retention_var = tk.StringVar(value=str(app.site.log_retention_days))
        self.az_tolerance_var = tk.StringVar(value=f"{app.site.az_track_tolerance_degrees:0.2f}")
        self.el_tolerance_var = tk.StringVar(value=f"{app.site.el_track_tolerance_degrees:0.2f}")
        self.az_stop_tolerance_var = tk.StringVar(value=f"{app.site.az_stop_tolerance_degrees:0.2f}")
        self.el_stop_tolerance_var = tk.StringVar(value=f"{app.site.el_stop_tolerance_degrees:0.2f}")
        self.az_slow_speed_var = tk.StringVar(value=str(app.site.az_slow_speed))
        self.el_slow_speed_var = tk.StringVar(value=str(app.site.el_slow_speed))
        self.az_slow_threshold_var = tk.StringVar(value=f"{app.site.az_slow_threshold_degrees:0.1f}")
        self.el_slow_threshold_var = tk.StringVar(value=f"{app.site.el_slow_threshold_degrees:0.1f}")
        self.timeout_enabled_var = tk.BooleanVar(value=app.site.timeout_enabled)
        self.timeout_minutes_var = tk.StringVar(value=f"{app.site.timeout_minutes:0.1f}")
        self.timeout_action_var = tk.StringVar(value=app.site.timeout_action)

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        self._spin_field(body, "Interval sec", self.interval_var, 0, 0.1, 10.0, 0.1, width=7)
        self._spin_field(body, "Log retention days", self.log_retention_var, 1, 1, 365, 1, width=7)

        ttk.Separator(body, orient="horizontal").grid(row=2, column=0, columnspan=5, sticky="ew", pady=8)
        ttk.Label(body, text="Axis").grid(row=3, column=0, sticky="w")
        ttk.Label(body, text="Start tol").grid(row=3, column=1, sticky="w")
        ttk.Label(body, text="Stop tol").grid(row=3, column=2, sticky="w")
        ttk.Label(body, text="Slow speed").grid(row=3, column=3, sticky="w")
        ttk.Label(body, text="Slow deg").grid(row=3, column=4, sticky="w")
        ttk.Label(body, text="AZ").grid(row=4, column=0, sticky="w", pady=2)
        self._spin_only(body, self.az_tolerance_var, 4, 1, -0.20, 0.20, 0.01, width=7)
        self._spin_only(body, self.az_stop_tolerance_var, 4, 2, -0.20, 0.20, 0.01, width=7)
        ttk.Entry(body, textvariable=self.az_slow_speed_var, width=7).grid(row=4, column=3, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.az_slow_threshold_var, width=7).grid(row=4, column=4, sticky="w", pady=2)
        ttk.Label(body, text="EL").grid(row=5, column=0, sticky="w", pady=2)
        self._spin_only(body, self.el_tolerance_var, 5, 1, -0.20, 0.20, 0.01, width=7)
        self._spin_only(body, self.el_stop_tolerance_var, 5, 2, -0.20, 0.20, 0.01, width=7)
        ttk.Entry(body, textvariable=self.el_slow_speed_var, width=7).grid(row=5, column=3, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.el_slow_threshold_var, width=7).grid(row=5, column=4, sticky="w", pady=2)

        ttk.Separator(body, orient="horizontal").grid(row=6, column=0, columnspan=5, sticky="ew", pady=8)
        ttk.Checkbutton(body, text="Timeout", variable=self.timeout_enabled_var).grid(row=7, column=0, sticky="w", pady=2)
        self._spin_only(body, self.timeout_minutes_var, 7, 1, 1.0, 1440.0, 1.0, width=7)
        ttk.Label(body, text="minutes").grid(row=7, column=2, sticky="w", pady=2)
        ttk.Combobox(
            body,
            textvariable=self.timeout_action_var,
            values=("disconnect", "park_disconnect"),
            width=16,
            state="readonly",
        ).grid(row=7, column=3, columnspan=2, sticky="w", pady=2)

        ttk.Separator(body, orient="horizontal").grid(row=8, column=0, columnspan=5, sticky="ew", pady=8)
        ttk.Label(body, text="Antenna").grid(row=9, column=0, sticky="w")
        ttk.Label(body, text="AZ speed").grid(row=9, column=1, sticky="w")
        ttk.Label(body, text="EL speed").grid(row=9, column=2, sticky="w")
        ttk.Label(body, text="Max jog").grid(row=9, column=3, sticky="w")
        ttk.Label(body, text="AZ L->H comp").grid(row=9, column=4, sticky="w")
        for row, (name, config) in enumerate(self.app.configs.items(), start=10):
            self.az_speed_vars[name] = tk.StringVar(value=str(config.az_track_speed))
            self.el_speed_vars[name] = tk.StringVar(value=str(config.el_track_speed))
            self.max_jog_vars[name] = tk.StringVar(value=f"{config.limits.max_jog_seconds:0.0f}")
            self.az_lh_comp_vars[name] = tk.StringVar(value=f"{config.az_low_to_high_compensation:0.2f}")
            ttk.Label(body, text=name).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(body, textvariable=self.az_speed_vars[name], width=7).grid(row=row, column=1, sticky="w", pady=2)
            ttk.Entry(body, textvariable=self.el_speed_vars[name], width=7).grid(row=row, column=2, sticky="w", pady=2)
            ttk.Entry(body, textvariable=self.max_jog_vars[name], width=7).grid(row=row, column=3, sticky="w", pady=2)
            ttk.Entry(body, textvariable=self.az_lh_comp_vars[name], width=9).grid(row=row, column=4, sticky="w", pady=2)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self.save).grid(row=0, column=2)

    def _field(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int, width: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="w", pady=2)

    def _spin_field(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        from_value: float,
        to_value: float,
        increment: float,
        width: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Spinbox(
            parent,
            textvariable=variable,
            from_=from_value,
            to=to_value,
            increment=increment,
            width=width,
            format="%0.2f" if increment < 0.1 else "%0.1f",
        ).grid(row=row, column=1, sticky="w", pady=2)

    def _spin_only(
        self,
        parent: ttk.Frame,
        variable: tk.StringVar,
        row: int,
        column: int,
        from_value: float,
        to_value: float,
        increment: float,
        width: int,
    ) -> None:
        tk.Spinbox(
            parent,
            textvariable=variable,
            from_=from_value,
            to=to_value,
            increment=increment,
            width=width,
            format="%0.2f" if increment < 0.1 else "%0.1f",
        ).grid(row=row, column=column, sticky="w", pady=2)

    def save(self) -> None:
        try:
            site = SiteConfig(
                latitude=self.app.site.latitude,
                longitude=self.app.site.longitude,
                selected_source=self.app.site.selected_source,
                track_interval_seconds=round(float(self.interval_var.get()), 1),
                az_track_tolerance_degrees=round(float(self.az_tolerance_var.get()), 2),
                el_track_tolerance_degrees=round(float(self.el_tolerance_var.get()), 2),
                az_stop_tolerance_degrees=round(float(self.az_stop_tolerance_var.get()), 2),
                el_stop_tolerance_degrees=round(float(self.el_stop_tolerance_var.get()), 2),
                az_slow_speed=int(self.az_slow_speed_var.get()),
                el_slow_speed=int(self.el_slow_speed_var.get()),
                az_slow_threshold_degrees=round(float(self.az_slow_threshold_var.get()), 1),
                el_slow_threshold_degrees=round(float(self.el_slow_threshold_var.get()), 1),
                log_retention_days=int(float(self.log_retention_var.get())),
                log_level=self.app.site.log_level,
                timeout_enabled=bool(self.timeout_enabled_var.get()),
                timeout_minutes=round(float(self.timeout_minutes_var.get()), 1),
                timeout_action=self.timeout_action_var.get(),
            )
            self.app.validate_site(site)
            antenna_values = {
                name: (
                    int(self.az_speed_vars[name].get()),
                    int(self.el_speed_vars[name].get()),
                    float(self.max_jog_vars[name].get()),
                    float(self.az_lh_comp_vars[name].get()),
                )
                for name in self.app.configs
            }
            self._validate_antennas(antenna_values, site)
        except ValueError:
            messagebox.showerror("Invalid Tracking", "Tracking values must be numeric.", parent=self)
            return
        except RuntimeError as exc:
            messagebox.showerror("Invalid Tracking", str(exc), parent=self)
            return

        self.app.site = site
        for name, (az_speed, el_speed, max_jog, az_lh_comp) in antenna_values.items():
            config = self.app.configs[name]
            config.az_track_speed = az_speed
            config.el_track_speed = el_speed
            config.limits.max_jog_seconds = max_jog
            config.az_low_to_high_compensation = az_lh_comp
            if name in self.app.panels:
                self.app.panels[name].sync_config_settings()
        self.app.save_tracking_and_config("Tracking saved.")
        self.destroy()

    def _validate_antennas(self, values: dict[str, tuple[int, int, float, float]], site: SiteConfig) -> None:
        for name, (az_speed, el_speed, max_jog, az_lh_comp) in values.items():
            if not (1 <= az_speed <= 100 and 1 <= el_speed <= 100):
                raise RuntimeError(f"{name}: AZ and EL speeds must be 1..100.")
            if site.az_slow_speed >= az_speed:
                raise RuntimeError(f"{name}: AZ slow speed must be lower than AZ speed.")
            if site.el_slow_speed >= el_speed:
                raise RuntimeError(f"{name}: EL slow speed must be lower than EL speed.")
            if not (1.0 <= max_jog <= 600.0):
                raise RuntimeError(f"{name}: max jog must be 1..600 seconds.")
            if not (-5.0 <= az_lh_comp <= 5.0):
                raise RuntimeError(f"{name}: AZ L->H compensation must be -5..5 degrees.")


class AntennaPanel(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "WT6App", name: str, config: Optional[AntennaConfig] = None) -> None:
        super().__init__(master, padding=8, relief="solid", borderwidth=1)
        self.app = app
        self.name = name
        self.config = config
        self.session: Optional[SafeAntenna] = None
        self.stop_event = threading.Event()

        self.status_var = tk.StringVar(value="DISCONNECTED")
        self.cal_az_var = tk.StringVar(value="--")
        self.cal_el_var = tk.StringVar(value="--")
        self.az_error_var = tk.StringVar(value="--")
        self.el_error_var = tk.StringVar(value="--")
        self.target_var = tk.StringVar(value="--")
        self.mode_var = tk.StringVar(value="--")
        self.limits_var = tk.StringVar(value="SAFE")
        self.fault_var = tk.StringVar(value="")

        initial_speed = config.gui_speed if config else 40
        initial_max_jog = config.limits.max_jog_seconds if config else 60.0
        self.speed_value = initial_speed
        self.max_jog_value = initial_max_jog
        self.speed_var = tk.StringVar(value=str(initial_speed))
        self.max_jog_var = tk.StringVar(value=f"{initial_max_jog:0.1f}")
        self.jog_thread_active = False
        self.manual_jog_active = False

        self.columnconfigure(0, weight=1)
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=name.upper(), font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var).grid(row=0, column=1, sticky="e")

        content = ttk.Frame(self)
        content.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        content.columnconfigure(0, weight=0)
        content.columnconfigure(1, weight=0, minsize=78)
        content.columnconfigure(2, weight=1)
        content.columnconfigure(3, weight=0)
        content.columnconfigure(4, weight=0)
        position_font = ("TkDefaultFont", 15, "bold")

        ttk.Label(content, text="AZ").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Label(content, textvariable=self.cal_az_var, font=position_font, width=6, anchor="e").grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Label(content, text="AZ err").grid(row=0, column=2, sticky="w")
        ttk.Label(content, textvariable=self.az_error_var).grid(row=0, column=2, sticky="w", padx=(52, 0))
        ttk.Label(content, text="Limits").grid(row=0, column=3, sticky="w", padx=(14, 4))
        ttk.Label(content, textvariable=self.limits_var).grid(row=0, column=3, sticky="w", padx=(62, 0))

        ttk.Label(content, text="EL").grid(row=1, column=0, sticky="w", padx=(0, 6))
        ttk.Label(content, textvariable=self.cal_el_var, font=position_font, width=6, anchor="e").grid(row=1, column=1, sticky="w", padx=(0, 8))
        ttk.Label(content, text="EL err").grid(row=1, column=2, sticky="w")
        ttk.Label(content, textvariable=self.el_error_var).grid(row=1, column=2, sticky="w", padx=(52, 0))
        ttk.Label(content, text="Mode").grid(row=1, column=3, sticky="w", padx=(14, 4))
        ttk.Label(content, textvariable=self.mode_var).grid(row=1, column=3, sticky="w", padx=(62, 0))

        ttk.Label(content, text="Target").grid(row=2, column=2, sticky="w")
        ttk.Label(content, textvariable=self.target_var).grid(row=2, column=2, columnspan=2, sticky="w", padx=(52, 0))

        manual = ttk.Frame(content)
        manual.grid(row=0, column=4, rowspan=3, sticky="e", padx=(14, 0))
        for col in range(3):
            manual.columnconfigure(col, minsize=64)
        for row in range(3):
            manual.rowconfigure(row, minsize=30)
        self._hold_button(manual, "EL+", Direction.EL_UP).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(manual, "AZ-", Direction.AZ_CCW).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(manual, text="STOP", command=self.stop).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        self._hold_button(manual, "AZ+", Direction.AZ_CW).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        self._hold_button(manual, "EL-", Direction.EL_DOWN).grid(row=2, column=1, sticky="ew", padx=2, pady=2)

        self.reference_frame: Optional[ttk.Frame] = None
        ttk.Label(self, textvariable=self.fault_var, foreground="red", wraplength=360).grid(
            row=2, column=0, sticky="ew", pady=(6, 0)
        )

    def add_reference_block(
        self,
        sun_var: tk.StringVar,
        moon_var: tk.StringVar,
        local_time_var: tk.StringVar,
        lmst_var: tk.StringVar,
        utc_var: tk.StringVar,
    ) -> None:
        self.reference_frame = ttk.Frame(self)
        self.reference_frame.grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Label(self.reference_frame, textvariable=sun_var).grid(row=0, column=0, sticky="w")
        ttk.Label(self.reference_frame, textvariable=moon_var).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(self.reference_frame, textvariable=local_time_var).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Label(self.reference_frame, textvariable=lmst_var).grid(row=4, column=0, sticky="w", pady=(2, 0))
        ttk.Label(self.reference_frame, textvariable=utc_var).grid(row=5, column=0, sticky="w", pady=(2, 0))

    def _hold_button(self, master: tk.Misc, text: str, direction: Direction) -> ttk.Button:
        button = ttk.Button(master, text=text, width=6)
        button.bind("<ButtonPress-1>", lambda _event: self.start_jog(direction))
        button.bind("<ButtonRelease-1>", lambda _event: self.stop_jog())
        button.bind("<Leave>", lambda _event: self.stop_jog())
        return button

    def attach(self, session: SafeAntenna) -> None:
        self.session = session
        self.sync_config_settings()
        self.status_var.set("STOPPED")
        self.fault_var.set("")
        self.mode_var.set("Auto")
        self.app.state_store.set_antenna_state(self.name, AntennaRunState.STOPPED, "")
        self.update_position(session.last_position)

    def detach(self, status: str = "DISCONNECTED", message: str = "") -> None:
        self.stop_event.set()
        self.session = None
        self.jog_thread_active = False
        self.manual_jog_active = False
        self.status_var.set(status)
        self.fault_var.set(message)
        self.mode_var.set("--")
        self.app.state_store.set_antenna_state(self.name, antenna_state_from_text(status), message)
        self.clear_position_fields()

    def clear_position_fields(self) -> None:
        self.cal_az_var.set("--")
        self.cal_el_var.set("--")
        self.az_error_var.set("--")
        self.el_error_var.set("--")
        self.target_var.set("--")
        self.limits_var.set("SAFE")

    def sync_config_settings(self) -> None:
        config = self.session.config if self.session else self.config
        if not config:
            return
        self.speed_value = config.gui_speed
        self.max_jog_value = config.limits.max_jog_seconds
        self.speed_var.set(str(self.speed_value))
        self.max_jog_var.set(f"{self.max_jog_value:0.1f}")

    def update_position(self, position: Optional[Position]) -> None:
        if position is None:
            return
        self.cal_az_var.set(f"{position.azimuth:0.2f}")
        self.cal_el_var.set(f"{position.elevation:0.2f}")
        self.update_target_error(position)
        self.app.state_store.set_antenna_position(self.name, position.azimuth, position.elevation)

    def update_target_error(self, position: Position) -> None:
        target = self.app.current_target
        if not target:
            self.az_error_var.set("--")
            self.el_error_var.set("--")
            self.target_var.set("--")
            return
        try:
            limits = self.session.config.limits if self.session else self.config.limits if self.config else None
            az_error = limits.azimuth_delta_to_target(position.azimuth, target.azimuth) if limits else shortest_angle_delta(position.azimuth, target.azimuth)
        except Exception:
            az_error = shortest_angle_delta(position.azimuth, target.azimuth)
        el_error = target.elevation - position.elevation
        self.az_error_var.set(f"{az_error:+0.2f}")
        self.el_error_var.set(f"{el_error:+0.2f}")
        self.target_var.set(f"{target.azimuth:0.2f} / {target.elevation:0.2f}")

    def set_fault(self, text: str) -> None:
        self.fault_var.set(text)
        self.status_var.set("FAULT" if text else "STOPPED")
        self.limits_var.set("FAULT" if text else "SAFE")
        self.app.state_store.set_antenna_state(self.name, AntennaRunState.FAULT if text else AntennaRunState.STOPPED, text)

    def set_tracking_status(self, text: str) -> None:
        if self.session and not self.fault_var.get():
            self.status_var.set(text)
            self.mode_var.set("Auto" if text in ("TRACKING", "SLEWING", "PARKING", "YFACTOR") else text.title())
            self.app.state_store.set_antenna_state(self.name, antenna_state_from_text(text), "")

    def set_message(self, text: str) -> None:
        self.fault_var.set(text)
        if self.session:
            self.status_var.set("STOPPED")
            self.app.state_store.set_antenna_state(self.name, AntennaRunState.STOPPED, text)

    def clear_message(self) -> None:
        self.fault_var.set("")
        if self.session:
            self.status_var.set("STOPPED")
            self.app.state_store.set_antenna_state(self.name, AntennaRunState.STOPPED, "")

    def refresh(self) -> None:
        if not self.session:
            return
        self.app.run_worker(lambda: self.session.read_position(), self.finish_refresh, self.finish_refresh_fault)

    def commit_speed(self, _event: Optional[object] = None) -> bool:
        try:
            value = max(0, min(100, int(self.speed_var.get())))
        except ValueError:
            self.speed_var.set(str(self.speed_value))
            self.set_message("Speed must be a whole number from 0 to 100.")
            return False
        self.speed_value = value
        self.speed_var.set(str(value))
        config = self.session.config if self.session else self.config
        if config:
            config.gui_speed = value
            self.app.save_config("Settings saved.")
        self.clear_message()
        return True

    def commit_max_jog(self, _event: Optional[object] = None) -> bool:
        try:
            value = max(1.0, min(600.0, float(self.max_jog_var.get())))
        except ValueError:
            self.max_jog_var.set(f"{self.max_jog_value:0.1f}")
            self.set_message("Max jog must be a number from 1 to 600 seconds.")
            return False
        self.max_jog_value = value
        self.max_jog_var.set(f"{value:0.1f}")
        config = self.session.config if self.session else self.config
        if config:
            config.limits.max_jog_seconds = value
            self.app.save_config("Settings saved.")
        self.clear_message()
        return True

    def start_jog(self, direction: Direction) -> None:
        if not self.session or self.jog_thread_active:
            return
        block_reason = self.app.manual_jog_block_reason()
        if block_reason:
            self.app.status_var.set(block_reason)
            self.app.event_log.warn("MANUAL_JOG_BLOCKED", antenna=self.name, direction=direction.value, reason=block_reason)
            return
        session = self.session
        self.stop_event.clear()
        speed = self.speed_value
        self.jog_thread_active = True
        self.manual_jog_active = True
        self.mode_var.set("Manual")

        def realtime_update(position: Position) -> None:
            self.queue_position_update(position)
            session.update_oled_position(activity="JOG")

        def work() -> Position:
            session.update_oled("MANUAL", activity="JOG")
            session.guarded_jog(direction, speed, None, self.stop_event, realtime_update)
            position = session.read_position()
            session.update_oled("MANUAL", activity="STOPPED")
            return position

        self.app.run_worker(work, self.finish_jog, self.finish_jog_fault)

    def queue_position_update(self, position: Position) -> None:
        self.app.events.put(("position", self.update_position, position))

    def finish_refresh(self, position: Position) -> None:
        self.clear_message()
        self.update_position(position)

    def finish_refresh_fault(self, text: str) -> None:
        self.app.handle_controller_fault(self.name, text)

    def finish_jog(self, position: Position) -> None:
        self.jog_thread_active = False
        self.manual_jog_active = False
        self.mode_var.set("Auto")
        self.clear_message()
        self.update_position(position)

    def finish_jog_fault(self, text: str) -> None:
        self.jog_thread_active = False
        self.manual_jog_active = False
        self.set_message(text)

    def stop_jog(self) -> None:
        if self.manual_jog_active:
            self.stop_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        if self.session:
            self.app.run_worker(lambda: self.session.stop_all(), lambda _result: None, self.set_fault)

class PowerMeterPanel(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "WT6App") -> None:
        super().__init__(master, padding=8, relief="solid", borderwidth=1)
        self.app = app
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.meter: Optional[B210PowerMeter] = None
        self.active_meter_config: Optional[B210PowerMeterConfig] = None
        self.power_values: list[float] = []
        self.power_b_values: list[float] = []
        self.last_reading_time = 0.0
        self.latest_power_dbfs: Optional[float] = None
        self.latest_power_b_dbfs: Optional[float] = None
        self.power_started_at = 0.0
        self.warmup_seconds = 0.0
        self.history_values: list[float] = []
        self.history_display_values: list[float] = []
        self.latest_power_value: Optional[float] = None
        self.latest_power_b_value: Optional[float] = None
        self.latest_power_unit = "dBFS"
        self.latest_power_calibrated = False
        self.latest_power_extrapolated = False
        self.active_calibrations: dict[str, B210Calibration] = {}
        self.log_handle = None
        self.log_writer: Optional[csv.writer] = None
        self.log_path: Optional[Path] = None

        power = app.power_config
        self.freq_var = tk.StringVar(value=f"{power.center_frequency_hz / 1_000_000:0.1f}")
        self.rate_var = tk.StringVar(value=f"{power.sample_rate_hz / 1000:0.0f}")
        self.gain_var = tk.StringVar(value=power.gain_db)
        self.gain_b_var = tk.StringVar(value=power.gain_b_db)
        self.ppm_var = tk.StringVar(value=str(power.frequency_correction_ppm))
        self.bandwidth_var = tk.StringVar(value=f"{power.measurement_bandwidth_hz / 1000:0.0f}")
        self.clock_var = tk.StringVar(value=power.clock_source)
        self.samples_var = tk.StringVar(value=self.samples_display_value(power.samples_per_read))
        self.update_var = tk.StringVar(value=f"{power.update_rate_hz:0.0f}")
        self.smooth_var = tk.StringVar(value=str(power.smoothing_samples))
        self.warmup_var = tk.StringVar(value=f"{power.warmup_seconds:0.0f}")
        self.power_var = tk.StringVar(value="--.- dBFS")
        self.power_b_var = tk.StringVar(value="--.- dBFS")
        self.status_var = tk.StringVar(value="SDR RELEASED")
        self.stats_var = tk.StringVar(value="Avg -- Min -- Max --")
        self.stats_b_var = tk.StringVar(value="Avg -- Min -- Max --")
        self.owner_var = tk.StringVar(value="SDR released for other apps")

        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="B210", font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="nw", padx=(0, 12))
        ttk.Label(self, textvariable=self.status_var).grid(row=1, column=0, sticky="nw", padx=(0, 12))

        channels = ttk.Frame(self)
        channels.grid(row=0, column=1, sticky="ew")
        channels.columnconfigure(0, weight=1)
        channels.columnconfigure(1, weight=1)
        self._channel_panel(channels, 0, "CH A", self.power_var, self.gain_var, self.stats_var)
        self._channel_panel(channels, 1, "CH B", self.power_b_var, self.gain_b_var, self.stats_b_var)

        fields = ttk.Frame(self)
        fields.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        fields.columnconfigure(12, weight=1)
        self._entry(fields, "Freq MHz", self.freq_var, 0, width=7)
        self._entry(fields, "Rate ksps", self.rate_var, 2, width=6)
        self._entry(fields, "BW kHz", self.bandwidth_var, 4, width=6)
        self._entry(fields, "Clock", self.clock_var, 6, width=8)
        self._entry(fields, "Avg", self.smooth_var, 8, width=4)
        self._entry(fields, "GUI Hz", self.update_var, 10, width=5)

        actions = ttk.Frame(fields)
        actions.grid(row=1, column=0, columnspan=13, sticky="w", pady=(6, 0))
        ttk.Button(actions, text="SDR Power On", command=self.start).pack(side="left")
        ttk.Button(actions, text="Release SDR", command=self.stop).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Cal", command=self.app.open_b210_calibration).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Start Log", command=self.start_log).pack(side="left", padx=(14, 0))
        ttk.Button(actions, text="Stop Log", command=self.stop_log).pack(side="left", padx=(6, 0))

    def _channel_panel(
        self,
        parent: tk.Misc,
        column: int,
        title: str,
        power_var: tk.StringVar,
        gain_var: tk.StringVar,
        stats_var: tk.StringVar,
    ) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=column, sticky="ew", padx=(0, 18 if column == 0 else 0))
        ttk.Label(frame, text=title, font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, textvariable=power_var, font=("TkDefaultFont", 13, "bold")).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(frame, text="Gain").grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Entry(frame, textvariable=gain_var, width=6).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(2, 0))
        ttk.Label(frame, textvariable=stats_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

    def _entry(self, parent: tk.Misc, label: str, variable: tk.StringVar, column: int, width: int) -> None:
        ttk.Label(parent, text=label).grid(row=0, column=column, sticky="w", padx=(0, 2))
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=0, column=column + 1, sticky="w", padx=(0, 5))

    def load_active_calibrations(self, power: PowerConfig) -> dict[str, B210Calibration]:
        calibrations: dict[str, B210Calibration] = {}
        for channel in ("A", "B"):
            calibration = load_b210_calibration(
                self.app.config_path,
                power.center_frequency_hz,
                power.sample_rate_hz,
                power.measurement_bandwidth_hz,
                power.gain_db,
                power.gain_b_db,
                channel,
            )
            if len(calibration.points_dbfs_by_dbm) >= 2:
                calibrations[channel] = calibration
        return calibrations

    def samples_display_value(self, stored_value: str) -> str:
        text = stored_value.strip().lower()
        if text in ("", "auto", "0"):
            return "auto"
        try:
            return f"{int(round(int(text) / 1000)):d}"
        except ValueError:
            return stored_value

    def samples_stored_value(self) -> str:
        text = self.samples_var.get().strip().lower()
        if text in ("", "auto", "0"):
            return "auto"
        return str(int(round(float(text) * 1000)))

    def power_config_from_fields(self) -> PowerConfig:
        freq_hz = int(round(float(self.freq_var.get()) * 1_000_000))
        sample_rate_hz = int(round(float(self.rate_var.get()) * 1000))
        bandwidth_hz = int(round(float(self.bandwidth_var.get()) * 1000))
        return PowerConfig(
            center_frequency_hz=freq_hz,
            sample_rate_hz=sample_rate_hz,
            measurement_bandwidth_hz=bandwidth_hz,
            gain_db=self.gain_var.get().strip() or "auto",
            gain_b_db=self.gain_b_var.get().strip() or self.gain_var.get().strip() or "auto",
            frequency_correction_ppm=int(self.ppm_var.get() or "0"),
            samples_per_read=self.samples_stored_value(),
            update_rate_hz=float(self.update_var.get()),
            smoothing_samples=max(1, int(self.smooth_var.get())),
            warmup_seconds=max(0.0, float(self.warmup_var.get())),
            clock_source=self.clock_var.get().strip() or "internal",
            b210_device_args=self.app.power_config.b210_device_args,
            east_channel=self.app.power_config.east_channel,
            west_channel=self.app.power_config.west_channel,
        )

    def meter_config_from_fields(self) -> B210PowerMeterConfig:
        power = self.power_config_from_fields()
        gain_a_text = self.gain_var.get().strip().lower()
        gain_b_text = self.gain_b_var.get().strip().lower()
        if gain_a_text in ("", "auto") or gain_b_text in ("", "auto"):
            raise ValueError("B210 power requires numeric gain values for both channels")
        samples_text = power.samples_per_read.strip().lower()
        samples = None if samples_text in ("", "auto", "0") else int(samples_text)
        config = B210PowerMeterConfig(
            center_frequency_hz=power.center_frequency_hz,
            sample_rate_hz=power.sample_rate_hz,
            measurement_bandwidth_hz=power.measurement_bandwidth_hz,
            update_rate_hz=power.update_rate_hz,
            gain_a_db=float(gain_a_text),
            gain_b_db=float(gain_b_text),
            samples_per_read=samples,
            clock_source=power.clock_source,
            device_args=power.b210_device_args,
            read_timeout_ms=max(100, min(1000, int(round(2000.0 / max(power.update_rate_hz, 0.1))))),
        )
        config.validate()
        return config

    def save_settings(self) -> None:
        try:
            self.app.power_config = self.power_config_from_fields()
        except Exception:
            return
        save_power_config(self.app.config_path, self.app.power_config)

    def format_fields(self, power: PowerConfig) -> None:
        self.freq_var.set(f"{power.center_frequency_hz / 1_000_000:0.1f}")
        self.rate_var.set(f"{power.sample_rate_hz / 1000:0.0f}")
        self.gain_var.set(power.gain_db)
        self.gain_b_var.set(power.gain_b_db)
        self.ppm_var.set(str(power.frequency_correction_ppm))
        self.bandwidth_var.set(f"{power.measurement_bandwidth_hz / 1000:0.0f}")
        self.samples_var.set(self.samples_display_value(power.samples_per_read))
        self.update_var.set(f"{power.update_rate_hz:0.0f}")
        self.smooth_var.set(str(power.smoothing_samples))
        self.clock_var.set(power.clock_source)
        self.warmup_var.set(f"{power.warmup_seconds:0.0f}")

    def start_log(self) -> None:
        if self.log_writer:
            self.status_var.set(f"Logging {self.log_path.name if self.log_path else ''}".strip())
            return
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = Path(f"wt6_power_{timestamp}.csv")
        self.log_handle = self.log_path.open("w", newline="", encoding="utf-8")
        self.log_writer = csv.writer(self.log_handle)
        self.log_writer.writerow(self.log_header())
        self.status_var.set(f"Logging {self.log_path.name}")

    def stop_log(self) -> None:
        if self.log_handle:
            self.log_handle.close()
        self.log_handle = None
        self.log_writer = None
        self.log_path = None
        if not (self.thread and self.thread.is_alive()):
            self.status_var.set("SDR RELEASED")

    def log_header(self) -> list[str]:
        header = [
            "local_time",
            "utc_time",
            "power_dbfs",
            "power_b_dbfs",
            "power_value",
            "power_b_value",
            "power_unit",
            "power_calibrated",
            "power_extrapolated",
            "target_name",
            "target_az",
            "target_el",
        ]
        for name in self.app.panels:
            header.extend([f"{name}_az", f"{name}_el", f"{name}_raw_az", f"{name}_raw_el"])
        return header

    def log_reading(self, power_dbfs: float, power_b_dbfs: Optional[float] = None) -> None:
        if not self.log_writer:
            return
        now_local = datetime.now().astimezone()
        now_utc = datetime.now(timezone.utc)
        target = self.app.current_target
        row: list[object] = [
            now_local.isoformat(timespec="milliseconds"),
            now_utc.isoformat(timespec="milliseconds"),
            f"{power_dbfs:0.2f}",
            f"{power_b_dbfs:0.2f}" if power_b_dbfs is not None else "",
            f"{self.latest_power_value:0.2f}" if self.latest_power_value is not None else "",
            f"{self.latest_power_b_value:0.2f}" if self.latest_power_b_value is not None else "",
            self.latest_power_unit,
            "yes" if self.latest_power_calibrated else "no",
            "yes" if self.latest_power_extrapolated else "no",
            self.app.target_name_var.get().replace("Target ", ""),
            f"{target.azimuth:0.3f}" if target else "",
            f"{target.elevation:0.3f}" if target else "",
        ]
        for panel in self.app.panels.values():
            position = panel.session.last_position if panel.session else None
            if position:
                row.extend(
                    [
                        f"{position.azimuth:0.3f}",
                        f"{position.elevation:0.3f}",
                        f"{position.raw_azimuth:0.3f}",
                        f"{position.raw_elevation:0.3f}",
                    ]
                )
            else:
                row.extend(["", "", "", ""])
        self.log_writer.writerow(row)
        if self.log_handle:
            self.log_handle.flush()

    def start(self) -> None:
        try:
            power_config = self.power_config_from_fields()
            meter_config = self.meter_config_from_fields()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        if self.thread and self.thread.is_alive():
            if meter_config == self.active_meter_config:
                self.status_var.set("SDR POWER ON")
                return
            self.status_var.set("Restarting B210 with new settings...")
            self.stop_event.set()
            self.wait_for_stop(timeout=3.0)
            if self.thread and self.thread.is_alive():
                self.status_var.set("B210 is still releasing; press SDR Power On again in a moment.")
                return
        self.app.power_config = power_config
        self.format_fields(power_config)
        save_power_config(self.app.config_path, self.app.power_config)
        self.reset_measurements(clear_history=True)
        self.warmup_seconds = power_config.warmup_seconds
        self.active_calibrations = self.load_active_calibrations(power_config)
        self.stop_event.clear()
        self.power_started_at = 0.0
        self.status_var.set("Starting B210...")
        self.owner_var.set("WT6 owns B210 while SDR power is on")
        self.app.state_store.set_power(PowerRunState.STARTING, message="Starting B210")
        self.active_meter_config = meter_config
        self.thread = threading.Thread(target=self.power_loop, args=(meter_config,), name="B210PowerMeter", daemon=True)
        self.thread.start()
        self.app.event_log.info(
            "B210_POWER_START",
            frequency_hz=power_config.center_frequency_hz,
            sample_rate_hz=power_config.sample_rate_hz,
            bandwidth_hz=power_config.measurement_bandwidth_hz,
            gain_a=self.gain_var.get().strip(),
            gain_b=self.gain_b_var.get().strip(),
            update_rate_hz=power_config.update_rate_hz,
            clock=power_config.clock_source,
            east_channel=power_config.east_channel,
            west_channel=power_config.west_channel,
        )

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.status_var.set("Releasing SDR...")
        else:
            self.thread = None
            self.meter = None
            self.active_meter_config = None
            self.status_var.set("SDR RELEASED")
            self.owner_var.set("SDR released for other apps")
            self.app.state_store.reset_power("SDR RELEASED")
        self.app.event_log.info("B210_POWER_UI_STOP")

    def wait_for_stop(self, timeout: float = 2.0) -> None:
        thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def pending_hardware_config_message(self) -> str:
        if not (self.thread and self.thread.is_alive()):
            return "Start B210 power before measurement."
        try:
            pending = self.meter_config_from_fields()
        except Exception as exc:
            return str(exc)
        if pending != self.active_meter_config:
            return "B210 settings changed; press SDR Power On to restart with the new settings."
        return ""

    def power_loop(self, config: B210PowerMeterConfig) -> None:
        try:
            with B210PowerMeter(config) as meter:
                self.meter = meter
                self.power_started_at = time.monotonic()
                self.app.events.put(("ok", self.refresh_warmup_status, None))
                while not self.stop_event.is_set():
                    reading = meter.read_power()
                    self.app.events.put(("ok", self.update_power, reading))
        except Exception as exc:
            if not self.stop_event.is_set():
                self.app.events.put(("error", self.set_status, str(exc)))
        finally:
            self.meter = None
            self.app.events.put(("ok", self.finish_stopped, None))

    def update_power(self, reading: B210PowerReading) -> None:
        try:
            smoothing = max(1, int(self.smooth_var.get()))
        except ValueError:
            smoothing = 1
        self.power_values.append(reading.power_a_dbfs)
        self.power_b_values.append(reading.power_b_dbfs)
        self.last_reading_time = time.monotonic()
        self.power_values = self.power_values[-smoothing:]
        self.power_b_values = self.power_b_values[-smoothing:]
        average_a = sum(self.power_values) / len(self.power_values)
        average_b = sum(self.power_b_values) / len(self.power_b_values)
        self.latest_power_dbfs = average_a
        self.latest_power_b_dbfs = average_b
        measurement = self.display_measurement(average_a, "A")
        measurement_b = self.display_measurement(average_b, "B")
        self.latest_power_value = float(measurement["power_value"])
        self.latest_power_b_value = float(measurement_b["power_value"])
        self.latest_power_unit = str(measurement["power_unit"])
        self.latest_power_calibrated = bool(measurement["power_calibrated"])
        self.latest_power_extrapolated = bool(measurement["power_extrapolated"])
        suffix = " EXT" if self.latest_power_extrapolated else ""
        suffix_b = " EXT" if measurement_b["power_extrapolated"] else ""
        self.power_var.set(f"{self.latest_power_value:0.1f} {self.latest_power_unit}{suffix}")
        self.power_b_var.set(f"{float(measurement_b['power_value']):0.1f} {measurement_b['power_unit']}{suffix_b}")
        self.history_values.append(average_a)
        self.history_values = self.history_values[-600:]
        self.history_display_values.append(self.latest_power_value)
        self.history_display_values = self.history_display_values[-600:]
        history_average = sum(self.history_display_values) / len(self.history_display_values)
        self.stats_var.set(
            f"Avg {history_average:0.1f} Min {min(self.history_display_values):0.1f} "
            f"Max {max(self.history_display_values):0.1f}"
        )
        history_b = self.power_b_values[-600:]
        self.stats_b_var.set(f"Avg {sum(history_b) / len(history_b):0.1f} Min {min(history_b):0.1f} Max {max(history_b):0.1f}")
        self.log_reading(average_a, average_b)
        self.refresh_warmup_status(None)
        self.app.state_store.set_power(
            run_state=PowerRunState.WARMING if self.is_warming() else PowerRunState.READY,
            value=self.latest_power_value,
            unit=self.latest_power_unit,
            calibrated=self.latest_power_calibrated,
            extrapolated=self.latest_power_extrapolated,
            message=self.status_var.get(),
        )

    def display_measurement(self, power_dbfs: float, channel: str = "A") -> dict[str, object]:
        calibration = self.active_calibrations.get(self.normalize_power_channel(channel))
        if calibration:
            converted = calibrated_dbm_from_dbfs(calibration, power_dbfs)
            if converted:
                power_dbm, extrapolated = converted
                return {
                    "power_dbfs": power_dbfs,
                    "power_value": power_dbm,
                    "power_unit": "dBm",
                    "power_calibrated": True,
                    "power_extrapolated": extrapolated,
                }
        return {
            "power_dbfs": power_dbfs,
            "power_value": power_dbfs,
            "power_unit": "dBFS",
            "power_calibrated": False,
            "power_extrapolated": False,
        }

    def current_power_measurement(self, antenna_name: str = "") -> Optional[dict[str, object]]:
        if self.is_warming():
            return None
        channel = self.power_channel_for_antenna(antenna_name)
        if channel == "B":
            if self.latest_power_b_dbfs is None:
                return None
            measurement = self.display_measurement(self.latest_power_b_dbfs, "B")
            measurement["power_channel"] = "B"
            return measurement
        if self.latest_power_dbfs is None:
            return None
        measurement = self.display_measurement(self.latest_power_dbfs, "A")
        measurement["power_channel"] = "A"
        return measurement

    def power_channel_for_antenna(self, antenna_name: str = "") -> str:
        normalized = (antenna_name or "").strip().lower()
        power = getattr(getattr(self, "app", None), "power_config", None)
        if normalized == "west":
            return self.normalize_power_channel(getattr(power, "west_channel", "B"))
        return self.normalize_power_channel(getattr(power, "east_channel", "A"))

    @staticmethod
    def normalize_power_channel(value: str) -> str:
        text = str(value or "").strip().upper().replace(" ", "")
        if text in ("B", "1", "CHB", "CHANNELB"):
            return "B"
        return "A"

    def is_warming(self) -> bool:
        if not self.power_started_at:
            return False
        return time.monotonic() - self.power_started_at < self.warmup_seconds

    def refresh_warmup_status(self, _unused: object) -> None:
        if self.stop_event.is_set() or not self.power_started_at:
            return
        remaining = self.warmup_seconds - (time.monotonic() - self.power_started_at)
        suffix = self.calibration_status_suffix()
        if remaining > 0:
            self.status_var.set(f"Warming {remaining:0.0f}s {suffix}".strip())
        else:
            self.status_var.set(f"SDR POWER ON {suffix}".strip())
        self.app.state_store.set_power(
            run_state=PowerRunState.WARMING if remaining > 0 else PowerRunState.READY,
            message=self.status_var.get(),
        )

    def calibration_status_suffix(self) -> str:
        if self.latest_power_calibrated or self.active_calibrations:
            return "CAL EXT" if self.latest_power_extrapolated else "CAL"
        return "UNCAL"

    def set_status(self, text: str) -> None:
        self.active_meter_config = None
        self.status_var.set(f"SDR FAULT: {text}")
        self.owner_var.set("SDR fault; release before other apps use B210")
        self.app.state_store.set_power(PowerRunState.FAULT, message=text)
        self.app.event_log.error("B210_POWER_FAULT", error=text)

    def finish_stopped(self, _unused: object) -> None:
        self.thread = None
        if self.stop_event.is_set():
            self.reset_measurements(clear_history=True)
            self.active_calibrations = {}
            self.active_meter_config = None
            self.status_var.set("SDR RELEASED")
            self.owner_var.set("SDR released for other apps")
            self.app.state_store.reset_power("SDR RELEASED")

    def reset_measurements(self, clear_history: bool = False) -> None:
        self.power_values.clear()
        self.power_b_values.clear()
        self.last_reading_time = 0.0
        self.latest_power_dbfs = None
        self.latest_power_b_dbfs = None
        self.latest_power_value = None
        self.latest_power_b_value = None
        self.latest_power_unit = "dBFS"
        self.latest_power_calibrated = False
        self.latest_power_extrapolated = False
        self.power_var.set("--.- dBFS")
        self.power_b_var.set("--.- dBFS")
        self.stats_var.set("Avg -- Min -- Max --")
        self.stats_b_var.set("Avg -- Min -- Max --")
        if clear_history:
            self.history_values.clear()
            self.history_display_values.clear()

class B210CalibrationDialog(tk.Toplevel):
    LEVELS_DBM = B210_CAL_LEVELS_DBM

    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("B210 Calibration")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.frequency_var = tk.StringVar(value=app.power_panel.freq_var.get())
        self.sample_rate_var = tk.StringVar(value=app.power_panel.rate_var.get())
        self.bandwidth_var = tk.StringVar(value=app.power_panel.bandwidth_var.get())
        self.gain_a_var = tk.StringVar(value=app.power_panel.gain_var.get())
        self.gain_b_var = tk.StringVar(value=app.power_panel.gain_b_var.get())
        self.status_var = tk.StringVar(value="Set signal generator level, then capture each row.")
        self.level_vars_a: dict[int, tk.StringVar] = {level: tk.StringVar(value="--") for level in self.LEVELS_DBM}
        self.level_vars_b: dict[int, tk.StringVar] = {level: tk.StringVar(value="--") for level in self.LEVELS_DBM}

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text="Freq MHz").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.frequency_var, width=9).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(body, text="Rate ksps").grid(row=0, column=2, sticky="w", padx=(8, 0), pady=2)
        ttk.Entry(body, textvariable=self.sample_rate_var, width=8).grid(row=0, column=3, sticky="w", pady=2)
        ttk.Label(body, text="BW kHz").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.bandwidth_var, width=9).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(body, text="Gain A").grid(row=1, column=2, sticky="w", padx=(8, 0), pady=2)
        ttk.Entry(body, textvariable=self.gain_a_var, width=8).grid(row=1, column=3, sticky="w", pady=2)
        ttk.Label(body, text="Gain B").grid(row=2, column=2, sticky="w", padx=(8, 0), pady=2)
        ttk.Entry(body, textvariable=self.gain_b_var, width=8).grid(row=2, column=3, sticky="w", pady=2)
        ttk.Button(body, text="Load", command=self.load_frequency).grid(row=0, column=4, rowspan=3, sticky="nsw", padx=(8, 0), pady=2)

        table = ttk.Frame(body)
        table.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        ttk.Label(table, text="Source dBm").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(table, text="CH A dBFS").grid(row=0, column=1, sticky="w", padx=(0, 12))
        ttk.Label(table, text="CH B dBFS").grid(row=0, column=2, sticky="w", padx=(0, 12))
        for row, level in enumerate(self.LEVELS_DBM, start=1):
            ttk.Label(table, text=f"{level:d}").grid(row=row, column=0, sticky="w", pady=2)
            ttk.Label(table, textvariable=self.level_vars_a[level], width=10).grid(row=row, column=1, sticky="w", pady=2)
            ttk.Label(table, textvariable=self.level_vars_b[level], width=10).grid(row=row, column=2, sticky="w", pady=2)
            ttk.Button(table, text="Capture", command=lambda l=level: self.capture_level(l)).grid(
                row=row, column=3, sticky="ew", pady=2
            )

        ttk.Label(body, textvariable=self.status_var, foreground="red", wraplength=460).grid(
            row=4, column=0, columnspan=5, sticky="ew", pady=(8, 0)
        )
        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons, text="Save", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.load_frequency()

    def frequency_hz(self) -> int:
        return int(round(float(self.frequency_var.get()) * 1_000_000))

    def sample_rate_hz(self) -> int:
        return int(round(float(self.sample_rate_var.get()) * 1000))

    def bandwidth_hz(self) -> int:
        return int(round(float(self.bandwidth_var.get()) * 1000))

    def load_frequency(self) -> None:
        try:
            cal_a = load_b210_calibration(
                self.app.config_path,
                self.frequency_hz(),
                self.sample_rate_hz(),
                self.bandwidth_hz(),
                self.gain_a_var.get(),
                self.gain_b_var.get(),
                "A",
            )
            cal_b = load_b210_calibration(
                self.app.config_path,
                self.frequency_hz(),
                self.sample_rate_hz(),
                self.bandwidth_hz(),
                self.gain_a_var.get(),
                self.gain_b_var.get(),
                "B",
            )
        except ValueError:
            self.status_var.set("Frequency, sample rate, bandwidth, and gains must be valid.")
            return
        for level in self.LEVELS_DBM:
            value_a = cal_a.points_dbfs_by_dbm.get(level)
            value_b = cal_b.points_dbfs_by_dbm.get(level)
            self.level_vars_a[level].set(f"{value_a:0.2f}" if value_a is not None else "--")
            self.level_vars_b[level].set(f"{value_b:0.2f}" if value_b is not None else "--")
        self.status_var.set(
            f"Loaded B210 calibration at {cal_a.frequency_hz / 1_000_000:0.1f} MHz, "
            f"{cal_a.sample_rate_hz / 1000:0.0f} ksps, BW {cal_a.bandwidth_hz / 1000:0.0f} kHz."
        )

    def capture_level(self, level_dbm: int) -> None:
        panel = self.app.power_panel
        if panel.latest_power_dbfs is None or panel.latest_power_b_dbfs is None:
            self.status_var.set("Start B210 power and wait for CH A and CH B readings before capture.")
            return
        if panel.is_warming():
            self.status_var.set("B210 power meter is still warming; wait for Ready before capture.")
            return
        self.level_vars_a[level_dbm].set(f"{panel.latest_power_dbfs:0.2f}")
        self.level_vars_b[level_dbm].set(f"{panel.latest_power_b_dbfs:0.2f}")
        self.status_var.set(
            f"Captured {level_dbm:d} dBm: CH A {panel.latest_power_dbfs:0.2f} dBFS, "
            f"CH B {panel.latest_power_b_dbfs:0.2f} dBFS."
        )

    def save(self) -> None:
        try:
            frequency_hz = self.frequency_hz()
            sample_rate_hz = self.sample_rate_hz()
            bandwidth_hz = self.bandwidth_hz()
            gain_a = self.gain_a_var.get().strip()
            gain_b = self.gain_b_var.get().strip()
            points_a = self.points_from_fields(self.level_vars_a)
            points_b = self.points_from_fields(self.level_vars_b)
        except ValueError as exc:
            self.status_var.set(str(exc))
            return
        if len(points_a) < 2 or len(points_b) < 2:
            self.status_var.set("Capture at least two calibration points for both channels before saving.")
            return
        save_b210_calibration(
            self.app.config_path,
            B210Calibration(frequency_hz, sample_rate_hz, bandwidth_hz, gain_a, gain_b, "A", points_a),
        )
        save_b210_calibration(
            self.app.config_path,
            B210Calibration(frequency_hz, sample_rate_hz, bandwidth_hz, gain_a, gain_b, "B", points_b),
        )
        self.app.power_panel.active_calibrations = self.app.power_panel.load_active_calibrations(self.app.power_config)
        self.app.event_log.info(
            "B210_CAL_SAVE",
            frequency_hz=frequency_hz,
            sample_rate_hz=sample_rate_hz,
            bandwidth_hz=bandwidth_hz,
            gain_a=gain_a,
            gain_b=gain_b,
            points_a=len(points_a),
            points_b=len(points_b),
        )
        self.status_var.set(f"Saved B210 calibration: CH A {len(points_a)} points, CH B {len(points_b)} points.")

    def points_from_fields(self, variables: dict[int, tk.StringVar]) -> dict[int, float]:
        points: dict[int, float] = {}
        for level, variable in variables.items():
            text = variable.get().strip()
            if not text or text == "--":
                continue
            points[level] = float(text)
        return points

    def close(self) -> None:
        if self.app.b210_calibration_dialog is self:
            self.app.b210_calibration_dialog = None
        self.destroy()


class RtlCalibrationDialog(tk.Toplevel):
    LEVELS_DBM = RTL_CAL_LEVELS_DBM

    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("RTL Calibration")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.frequency_var = tk.StringVar(value=app.power_panel.freq_var.get())
        self.sample_rate_var = tk.StringVar(value=app.power_panel.rate_var.get())
        self.gain_var = tk.StringVar(value=app.power_panel.gain_var.get())
        self.status_var = tk.StringVar(value="Set signal source level, then capture each row.")
        self.level_vars: dict[int, tk.StringVar] = {level: tk.StringVar(value="--") for level in self.LEVELS_DBM}

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text="Frequency MHz").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.frequency_var, width=10).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(body, text="Sample ksps").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.sample_rate_var, width=10).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(body, text="Gain").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(body, textvariable=self.gain_var, width=10).grid(row=2, column=1, sticky="w", pady=2)
        ttk.Button(body, text="Load", command=self.load_frequency).grid(row=0, column=2, rowspan=3, sticky="nsw", padx=(6, 0), pady=2)

        table = ttk.Frame(body)
        table.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(table, text="Source dBm").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(table, text="Measured dBFS").grid(row=0, column=1, sticky="w", padx=(0, 12))
        for row, level in enumerate(self.LEVELS_DBM, start=1):
            ttk.Label(table, text=f"{level:d}").grid(row=row, column=0, sticky="w", pady=2)
            ttk.Label(table, textvariable=self.level_vars[level], width=10).grid(row=row, column=1, sticky="w", pady=2)
            ttk.Button(table, text="Capture", command=lambda l=level: self.capture_level(l)).grid(
                row=row, column=2, sticky="ew", pady=2
            )

        ttk.Label(body, textvariable=self.status_var, foreground="red", wraplength=360).grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )
        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons, text="Save", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.load_frequency()

    def frequency_hz(self) -> int:
        return int(round(float(self.frequency_var.get()) * 1_000_000))

    def sample_rate_hz(self) -> int:
        return int(round(float(self.sample_rate_var.get()) * 1000))

    def gain_text(self) -> str:
        return normalize_rtl_gain(self.gain_var.get())

    def load_frequency(self) -> None:
        try:
            calibration = load_rtl_calibration(
                self.app.config_path,
                self.frequency_hz(),
                self.sample_rate_hz(),
                self.gain_text(),
            )
        except ValueError:
            self.status_var.set("Frequency, sample rate, and gain must be valid.")
            return
        for level in self.LEVELS_DBM:
            value = calibration.points_dbfs_by_dbm.get(level)
            self.level_vars[level].set(f"{value:0.2f}" if value is not None else "--")
        self.status_var.set(
            f"Loaded calibration for {calibration.frequency_hz / 1_000_000:0.1f} MHz, "
            f"{calibration.sample_rate_hz / 1000:0.0f} ksps, gain {calibration.gain_db}."
        )

    def capture_level(self, level_dbm: int) -> None:
        power = self.app.power_panel.latest_power_dbfs
        if power is None:
            self.status_var.set("Start B210 power and wait for a reading before capture.")
            return
        if self.app.power_panel.is_warming():
            self.status_var.set("B210 power meter is still warming; wait for Ready before capture.")
            return
        self.level_vars[level_dbm].set(f"{power:0.2f}")
        self.status_var.set(f"Captured {level_dbm:d} dBm as {power:0.2f} dBFS.")

    def save(self) -> None:
        try:
            frequency_hz = self.frequency_hz()
            sample_rate_hz = self.sample_rate_hz()
            gain_db = self.gain_text()
            points = self.points_from_fields()
        except ValueError as exc:
            self.status_var.set(str(exc))
            return
        if len(points) < 2:
            self.status_var.set("Capture at least two calibration points before saving.")
            return
        if gain_db == "auto":
            self.status_var.set("Use a fixed numeric RTL gain before saving calibration.")
            return
        save_rtl_calibration(
            self.app.config_path,
            RtlCalibration(
                frequency_hz=frequency_hz,
                sample_rate_hz=sample_rate_hz,
                gain_db=gain_db,
                points_dbfs_by_dbm=points,
            ),
        )
        self.app.power_panel.active_calibrations = self.app.power_panel.load_active_calibrations(self.app.power_config)
        self.app.event_log.info(
            "RTL_CAL_SAVE",
            frequency_hz=frequency_hz,
            sample_rate_hz=sample_rate_hz,
            gain=gain_db,
            points=len(points),
        )
        self.status_var.set(
            f"Saved {len(points)} points at {frequency_hz / 1_000_000:0.1f} MHz, "
            f"{sample_rate_hz / 1000:0.0f} ksps, gain {gain_db}."
        )

    def points_from_fields(self) -> dict[int, float]:
        points: dict[int, float] = {}
        for level, variable in self.level_vars.items():
            text = variable.get().strip()
            if text in ("", "--"):
                continue
            try:
                points[level] = float(text)
            except ValueError as exc:
                raise ValueError(f"{level:d} dBm reading must be numeric.") from exc
        return points

    def close(self) -> None:
        if self.app.rtl_calibration_dialog is self:
            self.app.rtl_calibration_dialog = None
        self.destroy()


class ScanCalibrationDialog(tk.Toplevel):
    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Scan Calibration")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.status_var = tk.StringVar(value="Track a source and start B210 power before scanning.")
        antenna_names = list(app.configs) or list(app.panels)
        default_antenna = app.scan_config.antenna_name if app.scan_config.antenna_name in antenna_names else ""
        if not default_antenna and antenna_names:
            default_antenna = antenna_names[0]
        self.antenna_var = tk.StringVar(value=default_antenna)
        self.span_var = tk.StringVar(value=f"{app.scan_config.span_degrees:0.1f}")
        self.increment_var = tk.StringVar(value=f"{app.scan_config.increment_degrees:0.2f}")
        self.dwell_var = tk.StringVar(value=f"{app.scan_config.dwell_seconds:0.1f}")
        self.count_var = tk.StringVar(value=str(app.scan_config.scan_count))
        self.az_high_to_low_var = tk.BooleanVar(value=app.scan_config.az_scan_high_to_low)

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text="Antenna").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Combobox(body, textvariable=self.antenna_var, values=antenna_names, width=12, state="readonly").grid(
            row=0, column=1, sticky="w", pady=2
        )
        self._entry(body, "Span +/- deg", self.span_var, 1)
        self._entry(body, "Increment deg", self.increment_var, 2)
        self._entry(body, "Dwell sec", self.dwell_var, 3)
        self._entry(body, "Scans", self.count_var, 4)
        ttk.Checkbutton(
            body,
            text="AZ scan high to low",
            variable=self.az_high_to_low_var,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(body, textvariable=self.status_var, foreground="red", wraplength=360).grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons, text="AZ Scan", command=lambda: self.start_scan(Axis.AZIMUTH)).pack(side="left")
        ttk.Button(buttons, text="EL Scan", command=lambda: self.start_scan(Axis.ELEVATION)).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Stop Scan", command=app.stop_scan).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _entry(self, parent: tk.Misc, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=9).grid(row=row, column=1, sticky="w", pady=2)

    def start_scan(self, axis: Axis) -> None:
        try:
            config = ScanConfig(
                span_degrees=float(self.span_var.get()),
                increment_degrees=float(self.increment_var.get()),
                dwell_seconds=float(self.dwell_var.get()),
                scan_count=int(self.count_var.get()),
                antenna_name=self.antenna_var.get().strip(),
                az_scan_high_to_low=bool(self.az_high_to_low_var.get()),
            )
            self.app.validate_scan_config(config)
        except ValueError:
            self.status_var.set("Scan parameters must be numeric.")
            return
        except RuntimeError as exc:
            self.status_var.set(str(exc))
            return
        self.span_var.set(f"{config.span_degrees:0.1f}")
        self.increment_var.set(f"{config.increment_degrees:0.2f}")
        self.dwell_var.set(f"{config.dwell_seconds:0.1f}")
        self.count_var.set(str(config.scan_count))
        self.app.start_calibration_scan(axis, config, self)

    def set_status(self, text: str) -> None:
        if self.winfo_exists():
            self.status_var.set(text)

    def close(self) -> None:
        if self.app.scan_dialog is self:
            self.app.scan_dialog = None
        self.destroy()


class ScanGraphDialog(tk.Toplevel):
    def __init__(
        self,
        app: "WT6App",
        axis: Axis,
        rows: list[dict[str, object]],
        csv_path: Path,
        antenna_name: str,
    ) -> None:
        super().__init__(app)
        plot_name = axis_label(axis)
        self.title(f"{antenna_name} {plot_name} Scan")
        self.resizable(False, False)
        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text=f"{antenna_name} {plot_name} scan saved to {csv_path.name}").grid(
            row=0, column=0, sticky="w"
        )
        self.coordinate_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.coordinate_var).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.summary_var = tk.StringVar(value="Fit --")
        canvas = tk.Canvas(body, width=520, height=300, background="white")
        canvas.grid(row=2, column=0, pady=(8, 0))
        self.draw_plot(canvas, axis, rows)
        ttk.Label(body, textvariable=self.summary_var).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Button(body, text="Close", command=self.destroy).grid(row=4, column=0, sticky="e", pady=(8, 0))

    def draw_plot(self, canvas: tk.Canvas, axis: Axis, rows: list[dict[str, object]]) -> None:
        width = int(canvas["width"])
        height = int(canvas["height"])
        left, right, top, bottom = 55, width - 20, 20, height - 45
        scan_points = [
            (float(row["offset_degrees"]), float(row.get("power_value", row["power_dbfs"])))
            for row in rows
            if row.get("power_value", row.get("power_dbfs")) is not None
        ]
        if not scan_points:
            canvas.create_text(width / 2, height / 2, text="No scan data")
            return
        offsets = [point[0] for point in scan_points]
        powers = [point[1] for point in scan_points]
        fit = self.fit_gaussian_with_slope(scan_points)
        fit_points: list[tuple[float, float]] = []
        if fit:
            min_fit_x, max_fit_x = min(offsets), max(offsets)
            for index in range(101):
                x_value = min_fit_x + (max_fit_x - min_fit_x) * index / 100.0
                fit_points.append((x_value, self.evaluate_fit(fit, x_value)))
            powers.extend(y for _x, y in fit_points)
        min_x, max_x = min(offsets), max(offsets)
        min_y, max_y = min(powers), max(powers)
        if min_x == max_x:
            min_x -= 1.0
            max_x += 1.0
        if min_y == max_y:
            min_y -= 0.5
            max_y += 0.5
        self.draw_graticule(canvas, left, right, top, bottom, min_x, max_x, min_y, max_y)
        canvas.create_line(left, bottom, right, bottom)
        canvas.create_line(left, top, left, bottom)
        canvas.create_text((left + right) / 2, height - 15, text=f"{axis_label(axis)} offset degrees")
        self.coordinate_var.set(f"{axis_label(axis)} scan coordinate = commanded {axis_label(axis)} offset")
        power_unit = next((str(row.get("power_unit", "dBFS")) for row in rows if row.get("power_value", row.get("power_dbfs")) is not None), "dBFS")
        canvas.create_text(18, (top + bottom) / 2, text=power_unit, angle=90)
        self.draw_boresight(canvas, left, right, top, bottom, min_x, max_x)

        if fit_points:
            canvas_fit_points = [
                self.canvas_point(x_value, y_value, left, right, top, bottom, min_x, max_x, min_y, max_y)
                for x_value, y_value in fit_points
            ]
            for start, end in zip(canvas_fit_points, canvas_fit_points[1:]):
                canvas.create_line(start[0], start[1], end[0], end[1], fill="#d62728", width=2)
            fwhm = 2.35482 * fit["sigma"]
            self.summary_var.set(
                f"Fit {axis_label(axis)} centre {fit['center']:+0.3f} deg, FWHM {fwhm:0.3f} deg, "
                f"peak {fit['peak']:0.2f} {power_unit}, RMS {fit['rms']:0.3f} dB"
            )
        else:
            self.summary_var.set("Fit unavailable")

        points = [
            self.canvas_point(x_value, y_value, left, right, top, bottom, min_x, max_x, min_y, max_y)
            for x_value, y_value in scan_points
        ]
        for start, end in zip(points, points[1:]):
            canvas.create_line(start[0], start[1], end[0], end[1], fill="#0057b8", width=2)
        for x, y in points:
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#0057b8", outline="")

    def canvas_point(
        self,
        x_value: float,
        y_value: float,
        left: int,
        right: int,
        top: int,
        bottom: int,
        min_x: float,
        max_x: float,
        min_y: float,
        max_y: float,
    ) -> tuple[float, float]:
        x = left + (x_value - min_x) / (max_x - min_x) * (right - left)
        y = bottom - (y_value - min_y) / (max_y - min_y) * (bottom - top)
        return x, y

    def draw_boresight(
        self,
        canvas: tk.Canvas,
        left: int,
        right: int,
        top: int,
        bottom: int,
        min_x: float,
        max_x: float,
    ) -> None:
        if not (min_x <= 0.0 <= max_x):
            return
        x, _y = self.canvas_point(0.0, 0.0, left, right, top, bottom, min_x, max_x, 0.0, 1.0)
        canvas.create_line(x, top, x, bottom, fill="#444444", dash=(4, 3), width=2)
        canvas.create_text(x + 4, top + 10, text="boresight", anchor="w", fill="#444444")

    def fit_gaussian_with_slope(self, points: list[tuple[float, float]]) -> Optional[dict[str, float]]:
        if len(points) < 5:
            return None
        points = sorted(points)
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        min_x, max_x = min(xs), max(xs)
        span = max_x - min_x
        if span <= 0.0:
            return None
        peak_x = xs[ys.index(max(ys))]
        sigma_min = max(span / 30.0, 0.02)
        sigma_max = max(span, sigma_min * 2.0)
        center_start = max(min_x, peak_x - span * 0.25)
        center_stop = min(max_x, peak_x + span * 0.25)
        best: Optional[dict[str, float]] = None
        for center in self.fit_range(center_start, center_stop, 41):
            for sigma in self.fit_range(sigma_min, sigma_max, 50):
                fit = self.solve_linear_fit(points, center, sigma)
                if fit and fit["amplitude"] > 0.0 and (best is None or fit["sse"] < best["sse"]):
                    best = fit
        if not best:
            return None
        for center_width, sigma_factor in ((span * 0.08, 0.35), (span * 0.03, 0.18)):
            center_start = max(min_x, best["center"] - center_width)
            center_stop = min(max_x, best["center"] + center_width)
            sigma_start = max(sigma_min, best["sigma"] * (1.0 - sigma_factor))
            sigma_stop = min(sigma_max, best["sigma"] * (1.0 + sigma_factor))
            for center in self.fit_range(center_start, center_stop, 41):
                for sigma in self.fit_range(sigma_start, sigma_stop, 41):
                    fit = self.solve_linear_fit(points, center, sigma)
                    if fit and fit["amplitude"] > 0.0 and fit["sse"] < best["sse"]:
                        best = fit
        best["rms"] = math.sqrt(best["sse"] / len(points))
        best["peak"] = self.evaluate_fit(best, best["center"])
        return best

    def fit_range(self, start: float, stop: float, count: int) -> list[float]:
        if count <= 1 or start == stop:
            return [start]
        return [start + (stop - start) * index / (count - 1) for index in range(count)]

    def solve_linear_fit(
        self,
        points: list[tuple[float, float]],
        center: float,
        sigma: float,
    ) -> Optional[dict[str, float]]:
        rows = []
        for x_value, y_value in points:
            gaussian = math.exp(-0.5 * ((x_value - center) / sigma) ** 2)
            rows.append((1.0, x_value, gaussian, y_value))
        normal = [[0.0 for _ in range(3)] for _ in range(3)]
        rhs = [0.0, 0.0, 0.0]
        for row in rows:
            values = row[:3]
            y_value = row[3]
            for i in range(3):
                rhs[i] += values[i] * y_value
                for j in range(3):
                    normal[i][j] += values[i] * values[j]
        solution = self.solve_3x3(normal, rhs)
        if solution is None:
            return None
        baseline, slope, amplitude = solution
        sse = 0.0
        for x_value, y_value in points:
            predicted = baseline + slope * x_value + amplitude * math.exp(-0.5 * ((x_value - center) / sigma) ** 2)
            sse += (y_value - predicted) ** 2
        return {
            "baseline": baseline,
            "slope": slope,
            "amplitude": amplitude,
            "center": center,
            "sigma": sigma,
            "sse": sse,
        }

    def solve_3x3(self, matrix: list[list[float]], rhs: list[float]) -> Optional[list[float]]:
        augmented = [matrix[row][:] + [rhs[row]] for row in range(3)]
        for column in range(3):
            pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
            if abs(augmented[pivot][column]) < 1e-12:
                return None
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
            pivot_value = augmented[column][column]
            for item in range(column, 4):
                augmented[column][item] /= pivot_value
            for row in range(3):
                if row == column:
                    continue
                factor = augmented[row][column]
                for item in range(column, 4):
                    augmented[row][item] -= factor * augmented[column][item]
        return [augmented[row][3] for row in range(3)]

    def evaluate_fit(self, fit: dict[str, float], x_value: float) -> float:
        gaussian = math.exp(-0.5 * ((x_value - fit["center"]) / fit["sigma"]) ** 2)
        return fit["baseline"] + fit["slope"] * x_value + fit["amplitude"] * gaussian

    def draw_graticule(
        self,
        canvas: tk.Canvas,
        left: int,
        right: int,
        top: int,
        bottom: int,
        min_x: float,
        max_x: float,
        min_y: float,
        max_y: float,
    ) -> None:
        divisions = 5
        grid_color = "#d9d9d9"
        for index in range(divisions + 1):
            fraction = index / divisions
            x = left + fraction * (right - left)
            x_value = min_x + fraction * (max_x - min_x)
            canvas.create_line(x, top, x, bottom, fill=grid_color)
            canvas.create_text(x, bottom + 14, text=f"{x_value:0.1f}", anchor="n")

            y = bottom - fraction * (bottom - top)
            y_value = min_y + fraction * (max_y - min_y)
            canvas.create_line(left, y, right, y, fill=grid_color)
            canvas.create_text(left - 8, y, text=f"{y_value:0.1f}", anchor="e")


class YFactorDialog(tk.Toplevel):
    def __init__(self, app: "WT6App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Y Factor")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        antenna_names = list(app.configs) or list(app.panels)
        config = app.yfactor_config
        antenna_name = config.antenna_name if config.antenna_name in antenna_names else (antenna_names[0] if antenna_names else "")
        self.antenna_var = tk.StringVar(value=antenna_name)
        self.target_var = tk.StringVar(value=config.hot_target if config.hot_target in ("Sun", "Moon", "Source") else "Sun")
        self.cold_mode_var = tk.StringVar(value=config.cold_mode)
        self.cold_az_var = tk.StringVar(value=f"{config.cold_az:0.1f}")
        self.cold_el_var = tk.StringVar(value=f"{config.cold_el:0.1f}")
        self.cold_ra_var = tk.StringVar(value=f"{config.cold_ra:0.4f}")
        self.cold_dec_var = tk.StringVar(value=f"{config.cold_dec:0.1f}")
        self.count_var = tk.StringVar(value=str(config.count))
        self.dwell_var = tk.StringVar(value=f"{config.dwell_seconds:0.1f}")
        self.status_var = tk.StringVar(value="Start B210 power before measuring.")

        body = ttk.Frame(self, padding=10)
        body.grid(row=0, column=0, sticky="nsew")
        ttk.Label(body, text="Hot target").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Combobox(body, textvariable=self.target_var, values=("Sun", "Moon", "Source"), width=16, state="readonly").grid(
            row=0, column=1, sticky="w", pady=2
        )
        ttk.Label(body, text="Antenna").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Combobox(body, textvariable=self.antenna_var, values=antenna_names, width=16, state="readonly").grid(
            row=1, column=1, sticky="w", pady=2
        )
        ttk.Label(body, text="Cold sky").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Combobox(
            body,
            textvariable=self.cold_mode_var,
            values=("Sun AZ / EL 80", "Moon AZ / EL 80", "AZ/EL", "RA/Dec"),
            width=16,
            state="readonly",
        ).grid(row=2, column=1, sticky="w", pady=2)
        self._entry(body, "Cold AZ", self.cold_az_var, 3)
        self._entry(body, "Cold EL", self.cold_el_var, 4)
        self._entry(body, "Cold RA h", self.cold_ra_var, 5)
        self._entry(body, "Cold Dec", self.cold_dec_var, 6)
        self._entry(body, "Measurements", self.count_var, 7)
        self._entry(body, "Dwell sec", self.dwell_var, 8)
        ttk.Label(body, textvariable=self.status_var, foreground="red", wraplength=360).grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons, text="Start", command=self.start).pack(side="left")
        ttk.Button(buttons, text="Stop", command=app.stop_yfactor).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        self.target_var.trace_add("write", self.on_hot_target_changed)
        self.on_hot_target_changed()
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _entry(self, parent: tk.Misc, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=12).grid(row=row, column=1, sticky="w", pady=2)

    def on_hot_target_changed(self, *_args) -> None:
        current_cold = self.cold_mode_var.get()
        if current_cold not in ("Sun AZ / EL 80", "Moon AZ / EL 80"):
            return
        if self.target_var.get() == "Sun":
            self.cold_mode_var.set("Sun AZ / EL 80")
        elif self.target_var.get() == "Moon":
            self.cold_mode_var.set("Moon AZ / EL 80")

    def start(self) -> None:
        try:
            count = int(self.count_var.get())
            dwell = float(self.dwell_var.get())
            cold_az = float(self.cold_az_var.get())
            cold_el = float(self.cold_el_var.get())
            cold_ra = float(self.cold_ra_var.get())
            cold_dec = float(self.cold_dec_var.get())
        except ValueError:
            self.status_var.set("Y Factor fields must be numeric.")
            return
        self.app.yfactor_config = YFactorConfig(
            antenna_name=self.antenna_var.get().strip(),
            hot_target=self.target_var.get(),
            cold_mode=self.cold_mode_var.get(),
            cold_az=cold_az,
            cold_el=cold_el,
            cold_ra=cold_ra,
            cold_dec=cold_dec,
            count=count,
            dwell_seconds=dwell,
        )
        save_yfactor_config(self.app.config_path, self.app.yfactor_config)
        self.app.start_yfactor(
            dialog=self,
            antenna_name=self.antenna_var.get().strip(),
            target_label=self.target_var.get(),
            cold_mode=self.cold_mode_var.get(),
            cold_az=cold_az,
            cold_el=cold_el,
            cold_ra=cold_ra,
            cold_dec=cold_dec,
            count=count,
            dwell_seconds=dwell,
        )

    def set_status(self, text: str) -> None:
        if self.winfo_exists():
            self.status_var.set(text)

    def close(self) -> None:
        if self.app.yfactor_dialog is self:
            self.app.yfactor_dialog = None
        self.destroy()


class WT6App(tk.Tk):
    def __init__(self, config_path: str) -> None:
        super().__init__()
        self.title(f"WT6 Antenna Controller {APP_VERSION}")
        self.geometry("1080x650")
        self.minsize(1080, 650)
        self.config_path = config_path
        self.configs = load_configs(config_path)
        self.site = load_site_config(config_path)
        self.sources = load_sources(config_path)
        self.power_config = load_power_config(config_path)
        self.scan_config = load_scan_config(config_path)
        self.yfactor_config = load_yfactor_config(config_path)
        self.event_log = EventLogger(Path(config_path).parent, self.site.log_retention_days, self.site.log_level)
        self.state_store = AppStateStore()
        self.event_log.info("APP_START", version=APP_VERSION, config=str(config_path))
        self.sessions: dict[str, SafeAntenna] = {}
        self.events: queue.Queue[tuple[str, object, object]] = queue.Queue()
        self.connecting_active = False
        self.health_check_active = False
        self.last_health_check = 0.0
        self.health_check_interval = 2.0
        self.tracking_stop_event = threading.Event()
        self.park_stop_event = threading.Event()
        self.scan_stop_event = threading.Event()
        self.yfactor_stop_event = threading.Event()
        self.motion_lock = threading.Lock()
        self.scan_offset_lock = threading.Lock()
        self.tracking_active = False
        self.tracking_thread: Optional[threading.Thread] = None
        self.scan_thread: Optional[threading.Thread] = None
        self.yfactor_thread: Optional[threading.Thread] = None
        self.tracking_last_update = 0.0
        self.parking_active = False
        self.scan_active = False
        self.yfactor_active = False
        self.yfactor_target_label = ""
        self.timeout_in_progress = False
        self.last_user_activity = time.monotonic()
        self.scan_antenna_name = ""
        self.scan_axis: Optional[Axis] = None
        self.scan_offset_degrees = 0.0
        self.tracking_kind = ""
        self.current_target: Optional[TargetPosition] = None
        self.state_store.set_status("Load config, connect antennas, then use guarded jogs.", SystemRunState.IDLE)
        self.target_name_var = tk.StringVar(value="Target --")
        self.target_az_var = tk.StringVar(value="AZ --")
        self.target_el_var = tk.StringVar(value="EL --")
        self.target_ha_var = tk.StringVar(value="HA --")
        self.timeout_var = tk.StringVar(value="Timeout off")
        self.sun_ref_var = tk.StringVar(value="Sun AZ -- EL --")
        self.moon_ref_var = tk.StringVar(value="Moon AZ -- EL --")
        self.local_time_var = tk.StringVar(value="Local --")
        self.lmst_var = tk.StringVar(value="LMST --")
        self.utc_var = tk.StringVar(value="UTC --")
        self.calibration_dialog: Optional[CalibrationDialog] = None
        self.peak_calibration_dialog: Optional[PeakCalibrationDialog] = None
        self.scan_dialog: Optional[ScanCalibrationDialog] = None
        self.yfactor_dialog: Optional[YFactorDialog] = None
        self.rtl_calibration_dialog: Optional[RtlCalibrationDialog] = None
        self.b210_calibration_dialog: Optional[B210CalibrationDialog] = None

        self.status_var = tk.StringVar(value="Load config, connect antennas, then use guarded jogs.")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(self, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        top_row_1 = ttk.Frame(top)
        top_row_1.pack(fill="x")
        top_row_2 = ttk.Frame(top)
        top_row_2.pack(fill="x", pady=(4, 0))
        ttk.Button(top_row_1, text="Connect", command=self.connect_all).pack(side="left")
        ttk.Button(top_row_1, text="Disconnect", command=self.disconnect_all).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Limits", command=self.open_limits).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Observer", command=self.open_observer).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Tracking", command=self.open_tracking).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Sources", command=self.open_sources).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Calibration", command=self.open_calibration).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Peak Cal", command=self.open_peak_calibration).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Scan Cal", command=self.open_scan_calibration).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Y Factor", command=self.open_yfactor).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="Encoders", command=self.open_encoders).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_1, text="STOP ALL", command=self.stop_all).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Track Sun", command=lambda: self.start_tracking("sun")).pack(side="left")
        ttk.Button(top_row_2, text="Track Moon", command=lambda: self.start_tracking("moon")).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Track Source", command=lambda: self.start_tracking("source")).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Stop Track", command=self.stop_sun_tracking).pack(side="left", padx=(6, 0))
        ttk.Button(top_row_2, text="Park", command=self.park_all).pack(side="left", padx=(6, 0))

        summary = ttk.Frame(self, padding=(8, 2, 8, 2))
        summary.pack(fill="x")
        summary.columnconfigure(0, weight=1)
        summary.columnconfigure(1, weight=2)

        source_panel = ttk.Frame(summary, relief="solid", borderwidth=1, padding=8)
        source_panel.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Label(source_panel, text="SOURCE", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Label(source_panel, textvariable=self.target_name_var).pack(side="left", padx=(8, 0))
        ttk.Label(source_panel, textvariable=self.target_az_var, font=("TkDefaultFont", 12, "bold")).pack(side="left", padx=(18, 0))
        ttk.Label(source_panel, textvariable=self.target_el_var, font=("TkDefaultFont", 12, "bold")).pack(side="left", padx=(18, 0))
        ttk.Label(source_panel, textvariable=self.target_ha_var, font=("TkDefaultFont", 12, "bold")).pack(side="left", padx=(18, 0))

        reference_panel = ttk.Frame(summary, relief="solid", borderwidth=1, padding=8)
        reference_panel.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        reference_top = ttk.Frame(reference_panel)
        reference_top.pack(fill="x")
        ttk.Label(reference_top, textvariable=self.lmst_var).pack(side="left")
        ttk.Label(reference_top, textvariable=self.utc_var).pack(side="left", padx=(18, 0))
        ttk.Label(reference_top, textvariable=self.local_time_var).pack(side="left", padx=(18, 0))
        reference_bottom = ttk.Frame(reference_panel)
        reference_bottom.pack(fill="x", pady=(6, 0))
        ttk.Label(reference_bottom, textvariable=self.sun_ref_var, font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Label(reference_bottom, textvariable=self.moon_ref_var, font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=(24, 0))

        timeout_bar = ttk.Frame(self, padding=(8, 0, 8, 2))
        timeout_bar.pack(fill="x")
        ttk.Label(timeout_bar, textvariable=self.timeout_var).pack(side="left")
        ttk.Label(timeout_bar, textvariable=self.status_var, foreground="red").pack(side="left", padx=(18, 0))

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=0)
        body.rowconfigure(1, weight=0)

        self.panels: dict[str, AntennaPanel] = {}
        names = list(self.configs) or ["antenna_a", "antenna_b"]
        for index, name in enumerate(names[:2]):
            panel = AntennaPanel(body, self, name, self.configs.get(name))
            panel.grid(row=0, column=index, sticky="ew", padx=4, pady=(0, 8))
            self.panels[name] = panel
            self.state_store.set_antenna_state(name, AntennaRunState.DISCONNECTED)

        self.power_panel = PowerMeterPanel(body, self)
        self.power_panel.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(2, 0))
        if not self.configs:
            self.status_var.set(f"No antennas found in {config_path}. Copy wt6_ubuntu.ini.example to wt6_ubuntu.ini.")

        self.bind_all("<Button>", self.note_user_activity, add="+")
        self.bind_all("<Key>", self.note_user_activity, add="+")
        self.after(100, self.process_events)
        self.update_reference_positions()
        self.after(1500, self.periodic_refresh)

    def note_user_activity(self, _event=None) -> None:
        self.last_user_activity = time.monotonic()
        if not self.sessions:
            self.timeout_in_progress = False

    def manual_jog_block_reason(self) -> str:
        if self.connecting_active:
            return "Wait for connection to finish before manual jog."
        if self.parking_active:
            return "Stop Park before manual jog."
        if self.scan_active:
            return "Stop Scan Cal before manual jog."
        if self.yfactor_active:
            return "Stop Y Factor before manual jog."
        if self.tracking_active:
            return "Stop tracking before manual jog."
        return ""

    def connect_all(self) -> None:
        if self.connecting_active:
            self.status_var.set("Connection already in progress.")
            return
        pending = [(name, config) for name, config in self.configs.items() if name not in self.sessions]
        if not pending:
            self.status_var.set("Already connected.")
            return
        self.connecting_active = True
        for name, _config in pending:
            panel = self.panels.get(name)
            if panel:
                panel.status_var.set("CONNECTING")
                panel.fault_var.set("")
        self.status_var.set(f"Connecting {len(pending)} antenna(s)...")
        self.event_log.info("CONNECT_START", antennas=[name for name, _config in pending])
        self.run_worker(lambda: self.connect_sessions_parallel(pending), self.finish_connect_all, self.finish_connect_fault)

    def connect_session(self, config) -> SafeAntenna:
        session: Optional[SafeAntenna] = None
        try:
            session = SafeAntenna(config, motion_logger=lambda event, fields, name=config.name: self.log_motion(name, event, fields))
            session.update_oled("MANUAL", activity="STOPPED")
            return session
        except Exception:
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            raise

    def log_motion(self, antenna_name: str, event: str, fields: object) -> None:
        payload = fields if isinstance(fields, dict) else {"detail": fields}
        event_name = f"MOTION_{event}"
        if event == "SLEW_EXCEPTION":
            self.event_log.error(event_name, antenna=antenna_name, **payload)
            return
        if event == "AXIS_NO_PROGRESS":
            self.event_log.warn(event_name, antenna=antenna_name, **payload)
            return
        if event == "AXIS_STOP" and payload.get("reason") in {"no_progress", "timeout", "external_stop_event"}:
            self.event_log.warn(event_name, antenna=antenna_name, **payload)
            return
        self.event_log.debug(event_name, antenna=antenna_name, **payload)

    def connect_sessions_parallel(self, pending: list[tuple[str, AntennaConfig]]) -> tuple[list[str], list[str]]:
        connected: list[str] = []
        errors: list[str] = []
        lock = threading.Lock()

        def connect_one(name: str, config: AntennaConfig) -> None:
            last_error = ""
            for attempt in range(1, 4):
                try:
                    session = self.connect_session(config)
                    with lock:
                        connected.append(name)
                    self.events.put(("ok", self.finish_connect_one_success, (name, session)))
                    return
                except Exception as exc:
                    last_error = str(exc)
                    self.event_log.warn("CONNECT_ATTEMPT_FAIL", antenna=name, attempt=attempt, error=last_error)
                    if attempt < 3:
                        time.sleep(max(1.0, config.open_delay * 0.5))
            error = f"{name}: {last_error}"
            with lock:
                errors.append(error)
            self.events.put(("ok", self.finish_connect_one_failure, error))

        threads = [threading.Thread(target=connect_one, args=(name, config), daemon=True) for name, config in pending]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return connected, errors

    def finish_connect_one_success(self, result: tuple[str, SafeAntenna]) -> None:
        name, session = result
        self.attach_session(name, session, update_status=False)
        self.event_log.info("CONNECT_OK", antenna=name)
        self.last_user_activity = time.monotonic()
        self.timeout_in_progress = False
        connected = len(self.sessions)
        total = len(self.configs)
        self.status_var.set(f"Stopped. Connected {connected}/{total} antennas.")

    def finish_connect_one_failure(self, error: str) -> None:
        name = error.split(":", 1)[0]
        self.event_log.error("CONNECT_FAIL", antenna=name, error=error)
        panel = self.panels.get(name)
        if panel:
            panel.detach("DISCONNECTED", error)
        connected = len(self.sessions)
        total = len(self.configs)
        self.status_var.set(f"Connected {connected}/{total}; connect fault: {error}")

    def finish_connect_all(self, result: tuple[list[str], list[str]]) -> None:
        self.connecting_active = False
        connected_names, errors = result
        connected = len(self.sessions)
        total = len(self.configs)
        if errors:
            self.status_var.set(f"Connected {connected}/{total}; connect fault: {'; '.join(errors)}")
        elif connected_names:
            self.status_var.set(f"Stopped. Connected {connected}/{total} antennas.")
        else:
            self.status_var.set("No antenna connections were attempted.")

    def finish_connect_fault(self, text: str) -> None:
        self.connecting_active = False
        self.event_log.error("CONNECT_FAULT", error=text)
        self.status_var.set(f"Connect fault: {text}")

    def attach_session(self, name: str, session: SafeAntenna, update_status: bool = True) -> None:
        self.sessions[name] = session
        if name in self.panels:
            self.panels[name].attach(session)
        if update_status:
            connected = len(self.sessions)
            total = len(self.configs)
            self.status_var.set(f"Stopped. Connected {connected}/{total} antennas.")

    def disconnect_all(self) -> None:
        if self.parking_active:
            self.status_var.set("Parking in progress.")
            return
        if not self.sessions:
            self.status_var.set("Already disconnected.")
            return
        if not self.stop_tracking_for_operation(
            "disconnecting",
            "Disconnecting",
            "DISCONNECT",
            "Tracking is still stopping; try Disconnect again in a moment.",
        ):
            return
        for panel in self.panels.values():
            panel.stop_event.set()
        self.event_log.info("DISCONNECT_START", antennas=list(self.sessions))
        for name, session in list(self.sessions.items()):
            self.run_worker(
                lambda s=session: s.close(),
                lambda _result, n=name: self.detach_session(n),
                self.set_status,
            )
        self.status_var.set("Disconnecting...")

    def detach_session(self, name: str) -> None:
        self.sessions.pop(name, None)
        if name in self.panels:
            self.panels[name].detach()
        connected = len(self.sessions)
        total = len(self.configs)
        if connected:
            self.status_var.set(f"Stopped. Connected {connected}/{total} antennas.")
        else:
            self.status_var.set("Disconnected.")
        self.event_log.info("DISCONNECT_OK", antenna=name, connected=connected, total=total)

    def handle_controller_fault(self, name: str, message: str) -> None:
        session = self.sessions.pop(name, None)
        panel = self.panels.get(name)
        if panel:
            panel.detach("OFFLINE", message)
        if session is None:
            return

        self.tracking_stop_event.set()
        self.scan_stop_event.set()
        self.park_stop_event.set()
        self.yfactor_stop_event.set()
        self.tracking_active = False
        self.scan_active = False
        self.yfactor_active = False
        self.parking_active = False
        self.set_scan_offset(None)
        self.status_var.set(f"{name} controller offline: {message}")
        self.event_log.error("CONTROLLER_OFFLINE", antenna=name, error=message)

        self.run_worker(lambda s=session: s.close(), lambda _result: None, lambda _text: None)
        for other_name, other_session in list(self.sessions.items()):
            other_panel = self.panels.get(other_name)
            if other_panel:
                other_panel.status_var.set("STOPPED")
            self.run_worker(
                lambda s=other_session: (s.stop_all(), s.update_oled_activity("STOPPED")),
                lambda _result: None,
                lambda text, n=other_name: self.handle_controller_fault(n, text),
            )

    def handle_controller_fault_event(self, payload: tuple[str, str]) -> None:
        name, message = payload
        self.handle_controller_fault(name, message)

    def read_positions_for_health(self) -> tuple[dict[str, Position], list[tuple[str, str]]]:
        positions: dict[str, Position] = {}
        errors: list[tuple[str, str]] = []
        for name, session in list(self.sessions.items()):
            try:
                positions[name] = session.read_position()
            except Exception as exc:
                errors.append((name, str(exc)))
        return positions, errors

    def check_controller_health(self, force: bool = False) -> None:
        if self.health_check_active or not self.sessions:
            return
        now = time.monotonic()
        if not force and now - self.last_health_check < self.health_check_interval:
            return
        self.last_health_check = now
        self.health_check_active = True
        self.run_worker(self.read_positions_for_health, self.finish_controller_health, self.finish_controller_health_fault)

    def finish_controller_health(self, result: tuple[dict[str, Position], list[tuple[str, str]]]) -> None:
        self.health_check_active = False
        positions, errors = result
        for name, position in positions.items():
            if name in self.sessions and name in self.panels:
                self.panels[name].update_position(position)
        for name, message in errors:
            self.handle_controller_fault(name, message)

    def finish_controller_health_fault(self, message: str) -> None:
        self.health_check_active = False
        self.event_log.error("HEALTH_CHECK_FAULT", error=message)
        self.status_var.set(f"Health check fault: {message}")

    def refresh_all(self) -> None:
        for panel in self.panels.values():
            panel.refresh()

    def oled_all(self) -> None:
        for session in self.sessions.values():
            self.run_worker(
                lambda s=session: s.update_oled("MANUAL", activity="STOPPED"),
                lambda _result: None,
                self.set_status,
            )

    def start_tracking(self, kind: str) -> None:
        if self.parking_active:
            self.status_var.set("Stop Park before tracking.")
            return
        if self.yfactor_active:
            self.status_var.set("Stop Y Factor before tracking.")
            return
        if self.tracking_active or (self.tracking_thread and self.tracking_thread.is_alive()):
            if not self.stop_tracking_for_restart(kind):
                return
        if not self.sessions:
            self.status_var.set("Connect antennas before tracking.")
            return
        try:
            self.validate_site(self.site)
            self.target_for_kind(kind)
        except RuntimeError as exc:
            self.status_var.set(str(exc))
            return
        self.tracking_stop_event.clear()
        self.tracking_active = True
        self.tracking_kind = kind
        self.tracking_last_update = time.monotonic()
        self.status_var.set(f"Slewing to {self.kind_label(kind)}.")
        self.event_log.info("TRACK_START", kind=kind)
        self.tracking_thread = threading.Thread(target=lambda: self.tracking_loop(kind), daemon=True)
        self.tracking_thread.start()

    def stop_tracking_for_restart(self, next_kind: str) -> bool:
        previous_kind = self.tracking_kind
        thread = self.tracking_thread
        self.tracking_stop_event.set()
        self.status_var.set(f"Switching to {self.kind_label(next_kind)}...")
        self.event_log.info("TRACK_SWITCH", from_kind=previous_kind, to_kind=next_kind)
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            timeout = min(10.0, max(2.0, self.site.track_interval_seconds + 1.0))
            thread.join(timeout=timeout)
            if thread.is_alive():
                self.status_var.set("Previous tracking is still stopping; try again in a moment.")
                self.event_log.warn("TRACK_SWITCH_DEFERRED", from_kind=previous_kind, to_kind=next_kind)
                return False
        self.tracking_active = False
        self.tracking_kind = ""
        return True

    def stop_tracking_for_operation(
        self,
        action_text: str,
        event_action: str,
        event_prefix: str,
        deferred_message: str,
    ) -> bool:
        previous_kind = self.tracking_kind
        thread = self.tracking_thread
        if not self.tracking_active and not (thread and thread.is_alive()):
            return True

        self.tracking_stop_event.set()
        self.status_var.set(f"Stopping tracking before {action_text}...")
        self.event_log.info(f"{event_prefix}_STOP_TRACKING", from_kind=previous_kind, action=event_action)
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            max_jog = max((session.config.limits.max_jog_seconds for session in self.sessions.values()), default=60.0)
            timeout = min(15.0, max(3.0, self.site.track_interval_seconds + max_jog * 0.1))
            thread.join(timeout=timeout)
            if thread.is_alive():
                self.status_var.set(deferred_message)
                self.event_log.warn(f"{event_prefix}_DEFERRED", reason="tracking_thread_alive", from_kind=previous_kind)
                return False
        self.tracking_active = False
        self.tracking_kind = ""
        return True

    def stop_tracking_before_park(self) -> bool:
        return self.stop_tracking_for_operation(
            "parking",
            "Parking",
            "PARK",
            "Tracking is still stopping; try Park again in a moment.",
        )

    def stop_sun_tracking(self) -> None:
        self.tracking_stop_event.set()
        self.tracking_active = False
        self.tracking_kind = ""
        self.stop_scan()
        self.stop_yfactor()
        self.target_ha_var.set("HA --")
        self.stop_all()
        self.status_var.set("Stopped.")
        self.event_log.info("TRACK_STOP")

    def validate_scan_config(self, config: ScanConfig) -> None:
        if config.antenna_name not in self.configs:
            raise RuntimeError("Select East or West antenna for the scan.")
        if config.antenna_name not in self.sessions:
            raise RuntimeError(f"{config.antenna_name} must be connected before scanning.")
        if not (0.1 <= config.span_degrees <= 30.0):
            raise RuntimeError("Scan span must be 0.1..30.0 degrees.")
        if not (0.01 <= config.increment_degrees <= config.span_degrees):
            raise RuntimeError("Scan increment must be 0.01 degrees up to the scan span.")
        if not (0.1 <= config.dwell_seconds <= 60.0):
            raise RuntimeError("Dwell must be 0.1..60.0 seconds.")
        if not (1 <= config.scan_count <= 20):
            raise RuntimeError("Scan count must be 1..20.")

    def start_calibration_scan(self, axis: Axis, config: ScanConfig, dialog: ScanCalibrationDialog) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            dialog.set_status("Scan already running.")
            return
        if not self.tracking_active or not self.tracking_kind:
            dialog.set_status("Start tracking Sun, Moon, or Source before scanning.")
            return
        if not self.sessions:
            dialog.set_status("Connect antennas before scanning.")
            return
        try:
            self.validate_scan_config(config)
        except RuntimeError as exc:
            dialog.set_status(str(exc))
            return
        pending_message = self.power_panel.pending_hardware_config_message()
        if pending_message:
            dialog.set_status(pending_message)
            return
        if self.power_panel.current_power_measurement(config.antenna_name) is None:
            channel = self.power_panel.power_channel_for_antenna(config.antenna_name)
            dialog.set_status(f"Start B210 power and wait for CH {channel} readings before scanning.")
            return
        self.scan_config = config
        save_scan_config(self.config_path, config)
        self.scan_stop_event.clear()
        self.scan_active = True
        dialog.set_status(f"{axis_label(axis)} scan starting on {config.antenna_name}...")
        self.status_var.set(f"{axis_label(axis)} scan starting on {config.antenna_name}.")
        self.event_log.info(
            "SCAN_START",
            antenna=config.antenna_name,
            axis=axis_label(axis),
            direction=(
                "high_to_low"
                if axis != Axis.AZIMUTH or config.az_scan_high_to_low
                else "low_to_high"
            ),
            span=config.span_degrees,
            increment=config.increment_degrees,
            dwell=config.dwell_seconds,
            count=config.scan_count,
        )
        self.scan_thread = threading.Thread(target=lambda: self.scan_worker(axis, config, dialog), daemon=True)
        self.scan_thread.start()

    def stop_scan(self) -> None:
        if not self.scan_active and not (self.scan_thread and self.scan_thread.is_alive()):
            self.set_scan_offset(None)
            if self.scan_dialog and self.scan_dialog.winfo_exists():
                self.scan_dialog.set_status("No scan is running.")
            return
        self.scan_stop_event.set()
        self.set_scan_offset(None)
        self.event_log.info("SCAN_STOP")
        if self.scan_dialog and self.scan_dialog.winfo_exists():
            self.scan_dialog.set_status("Scan stopping; returning to nominal target.")

    def scan_offsets(self, axis: Axis, config: ScanConfig) -> list[float]:
        offsets: list[float] = []
        value = config.span_degrees
        limit = -config.span_degrees - config.increment_degrees * 0.5
        while value >= limit:
            offsets.append(round(value, 6))
            value -= config.increment_degrees
        if offsets and offsets[-1] < -config.span_degrees:
            offsets[-1] = -config.span_degrees
        if axis == Axis.AZIMUTH and not config.az_scan_high_to_low:
            offsets.reverse()
        return offsets

    def scan_worker(self, axis: Axis, config: ScanConfig, dialog: ScanCalibrationDialog) -> None:
        rows: list[dict[str, object]] = []
        averaged_rows: list[dict[str, object]] = []
        scan_dir = Path(self.config_path).parent / "scan"
        scan_dir.mkdir(parents=True, exist_ok=True)
        csv_path = scan_dir / f"wt6_scan_{config.antenna_name.lower()}_{axis_label(axis).lower()}_{datetime.now():%Y%m%d-%H%M%S}.csv"
        try:
            offsets = self.scan_offsets(axis, config)
            total_points = len(offsets) * config.scan_count
            point_index = 0
            for scan_number in range(1, config.scan_count + 1):
                if self.scan_stop_event.is_set():
                    break
                self.move_scan_to_start(axis, config, offsets[0])
                for offset in offsets:
                    if self.scan_stop_event.is_set():
                        break
                    point_index += 1
                    nominal = self.current_tracking_target(self.tracking_kind)
                    self.set_scan_offset(config.antenna_name, axis, offset)
                    target = self.apply_scan_offset(nominal, config.antenna_name)
                    self.events.put(
                        (
                            "ok",
                            dialog.set_status,
                            f"{config.antenna_name} {axis_label(axis)} scan {scan_number}/{config.scan_count} "
                            f"point {point_index}/{total_points} offset {offset:+0.2f} deg",
                        )
                    )
                    self.events.put(("ok", self.set_status, f"{config.antenna_name} {axis_label(axis)} scan offset {offset:+0.2f} deg."))
                    self.event_log.debug(
                        "SCAN_POINT",
                        antenna=config.antenna_name,
                        axis=axis_label(axis),
                        scan_number=scan_number,
                        offset=offset,
                        nominal_az=nominal.azimuth,
                        nominal_el=nominal.elevation,
                        target_az=target.azimuth,
                        target_el=target.elevation,
                    )
                    self.slew_all_to_target(
                        nominal,
                        nominal.name[:8].upper(),
                        show_slewing=False,
                        stop_event=self.scan_stop_event,
                    )
                    if self.scan_stop_event.is_set():
                        break
                    row = self.collect_scan_point(axis, offset, config.dwell_seconds, nominal, target, config.antenna_name, scan_number)
                    rows.append(row)
            stopped = self.scan_stop_event.is_set()
            averaged_rows = self.average_scan_rows(rows, offsets)
            if not stopped:
                self.write_scan_csv(csv_path, rows, averaged_rows)
            self.set_scan_offset(None)
            if self.tracking_kind and not self.tracking_stop_event.is_set():
                target = self.current_tracking_target(self.tracking_kind)
                self.slew_all_to_target(target, target.name[:8].upper(), show_slewing=False)
            if stopped:
                self.event_log.info("SCAN_STOPPED", antenna=config.antenna_name, axis=axis_label(axis))
                self.events.put(("ok", dialog.set_status, "Scan stopped; tracking source."))
                self.events.put(("ok", self.set_status, "Scan stopped; tracking source."))
            elif averaged_rows:
                self.event_log.info("SCAN_COMPLETE", antenna=config.antenna_name, axis=axis_label(axis), csv=str(csv_path))
                self.events.put(("ok", lambda _unused: ScanGraphDialog(self, axis, averaged_rows, csv_path, config.antenna_name), None))
                self.events.put(("ok", dialog.set_status, f"Scan complete: {csv_path}"))
                self.events.put(("ok", self.set_status, f"Scan complete: {csv_path}"))
            else:
                self.events.put(("ok", dialog.set_status, "Scan stopped before measurements were taken."))
        except Exception as exc:
            self.scan_stop_event.set()
            self.set_scan_offset(None)
            self.event_log.error("SCAN_FAULT", antenna=config.antenna_name, axis=axis_label(axis), error=str(exc))
            self.events.put(("error", dialog.set_status, str(exc)))
            self.events.put(("error", self.set_status, f"Scan fault: {exc}"))
        finally:
            self.scan_active = False

    def collect_scan_point(
        self,
        axis: Axis,
        offset: float,
        dwell_seconds: float,
        nominal: TargetPosition,
        target: TargetPosition,
        antenna_name: str,
        scan_number: int,
    ) -> dict[str, object]:
        measurements: list[dict[str, object]] = []
        end_time = time.monotonic() + dwell_seconds
        while not self.scan_stop_event.is_set() and time.monotonic() < end_time:
            measurement = self.power_panel.current_power_measurement(antenna_name)
            if measurement is not None:
                measurements.append(measurement)
            time.sleep(0.1)
        power_unit = str(measurements[-1]["power_unit"]) if measurements else "dBFS"
        calibrated = bool(measurements[-1]["power_calibrated"]) if measurements else False
        now_local = datetime.now().astimezone()
        row: dict[str, object] = {
            "local_time": now_local.isoformat(timespec="milliseconds"),
            "antenna": antenna_name,
            "axis": axis_label(axis),
            "scan_number": scan_number,
            "offset_degrees": offset,
            "nominal_az": nominal.azimuth,
            "nominal_el": nominal.elevation,
            "target_az": target.azimuth,
            "target_el": target.elevation,
            "power_dbfs": (
                sum(float(measurement["power_dbfs"]) for measurement in measurements) / len(measurements)
                if measurements
                else None
            ),
            "power_value": (
                sum(float(measurement["power_value"]) for measurement in measurements) / len(measurements)
                if measurements
                else None
            ),
            "power_unit": power_unit,
            "power_channel": str(measurements[-1].get("power_channel", "")) if measurements else "",
            "power_calibrated": calibrated,
            "power_extrapolated": any(bool(measurement["power_extrapolated"]) for measurement in measurements),
            "sample_count": len(measurements),
        }
        for name, panel in self.panels.items():
            position = panel.session.last_position if panel.session else None
            row[f"{name}_az"] = position.azimuth if position else None
            row[f"{name}_el"] = position.elevation if position else None
            row[f"{name}_raw_az"] = position.raw_azimuth if position else None
            row[f"{name}_raw_el"] = position.raw_elevation if position else None
        return row

    def average_scan_rows(self, rows: list[dict[str, object]], offsets: list[float]) -> list[dict[str, object]]:
        averaged: list[dict[str, object]] = []
        for offset in offsets:
            matching = [row for row in rows if row.get("power_value") is not None and float(row["offset_degrees"]) == offset]
            if not matching:
                continue
            template = dict(matching[-1])
            template["power_dbfs"] = sum(float(row["power_dbfs"]) for row in matching) / len(matching)
            template["power_value"] = sum(float(row["power_value"]) for row in matching) / len(matching)
            template["power_calibrated"] = all(bool(row.get("power_calibrated")) for row in matching)
            template["power_extrapolated"] = any(bool(row.get("power_extrapolated")) for row in matching)
            template["sample_count"] = sum(int(row.get("sample_count", 0)) for row in matching)
            template["scan_number"] = "avg"
            averaged.append(template)
        return averaged

    def move_scan_to_start(self, axis: Axis, config: ScanConfig, start_offset: float) -> None:
        self.set_scan_offset(config.antenna_name, axis, start_offset)
        if self.tracking_kind and not self.scan_stop_event.is_set():
            target = self.current_tracking_target(self.tracking_kind)
            self.slew_all_to_target(target, target.name[:8].upper(), show_slewing=False, stop_event=self.scan_stop_event)

    def write_scan_csv(self, csv_path: Path, rows: list[dict[str, object]], averaged_rows: list[dict[str, object]]) -> None:
        if not rows:
            return
        fieldnames = list(rows[0])
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            if averaged_rows:
                writer.writerow({field: "" for field in fieldnames})
                for row in averaged_rows:
                    writer.writerow(row)

    def start_yfactor(
        self,
        dialog: YFactorDialog,
        antenna_name: str,
        target_label: str,
        cold_mode: str,
        cold_az: float,
        cold_el: float,
        cold_ra: float,
        cold_dec: float,
        count: int,
        dwell_seconds: float,
    ) -> None:
        if self.yfactor_thread and self.yfactor_thread.is_alive():
            dialog.set_status("Y Factor measurement already running.")
            return
        if self.scan_active or self.parking_active:
            dialog.set_status("Stop scan or park before Y Factor measurement.")
            return
        if antenna_name not in self.sessions:
            dialog.set_status("Connect and select an antenna before Y Factor measurement.")
            return
        pending_message = self.power_panel.pending_hardware_config_message()
        if pending_message:
            dialog.set_status(pending_message)
            return
        if self.power_panel.current_power_measurement(antenna_name) is None:
            channel = self.power_panel.power_channel_for_antenna(antenna_name)
            dialog.set_status(f"Start B210 power and wait for CH {channel} readings before Y Factor measurement.")
            return
        if not (1 <= count <= 50):
            dialog.set_status("Measurements must be 1..50.")
            return
        if not (0.5 <= dwell_seconds <= 120.0):
            dialog.set_status("Dwell must be 0.5..120.0 seconds.")
            return
        try:
            hot_target = self.yfactor_hot_target(target_label)
            cold_target = self.yfactor_cold_target(cold_mode, hot_target, cold_az, cold_el, cold_ra, cold_dec)
            session = self.sessions[antenna_name]
            session.config.limits.assert_position_allowed(hot_target.azimuth, hot_target.elevation)
            session.config.limits.assert_position_allowed(cold_target.azimuth, cold_target.elevation)
        except Exception as exc:
            dialog.set_status(str(exc))
            return

        self.tracking_stop_event.set()
        if self.tracking_thread and self.tracking_thread.is_alive():
            max_jog = max((session.config.limits.max_jog_seconds for session in self.sessions.values()), default=60.0)
            timeout = min(10.0, max(2.0, self.site.track_interval_seconds + max_jog * 0.1))
            self.tracking_thread.join(timeout=timeout)
            if self.tracking_thread.is_alive():
                message = "Tracking is still stopping; try Y Factor again in a moment."
                dialog.set_status(message)
                self.status_var.set(message)
                self.event_log.warn("YFACTOR_START_DEFERRED", reason="tracking_thread_alive")
                return
        self.tracking_active = False
        self.yfactor_stop_event.clear()
        self.yfactor_active = True
        self.yfactor_target_label = target_label
        self.events.put(("ok", self.apply_yfactor_target_position, target_label))
        dialog.set_status(f"Y Factor starting on {antenna_name}.")
        self.status_var.set(f"Y Factor starting on {antenna_name}.")
        self.event_log.info(
            "YFACTOR_START",
            antenna=antenna_name,
            hot=hot_target.name,
            cold=cold_target.name,
            channel=self.power_panel.power_channel_for_antenna(antenna_name),
            count=count,
            dwell=dwell_seconds,
        )
        self.yfactor_thread = threading.Thread(
            target=lambda: self.yfactor_worker(dialog, antenna_name, target_label, cold_mode, cold_az, cold_el, cold_ra, cold_dec, count, dwell_seconds),
            daemon=True,
        )
        self.yfactor_thread.start()

    def stop_yfactor(self) -> None:
        if not self.yfactor_active and not (self.yfactor_thread and self.yfactor_thread.is_alive()):
            if self.yfactor_dialog and self.yfactor_dialog.winfo_exists():
                self.yfactor_dialog.set_status("No Y Factor measurement is running.")
            return
        self.yfactor_stop_event.set()
        self.yfactor_active = False
        self.yfactor_target_label = ""
        self.event_log.info("YFACTOR_STOP")
        if self.yfactor_dialog and self.yfactor_dialog.winfo_exists():
            self.yfactor_dialog.set_status("Y Factor stopping.")

    def yfactor_worker(
        self,
        dialog: YFactorDialog,
        antenna_name: str,
        target_label: str,
        cold_mode: str,
        cold_az: float,
        cold_el: float,
        cold_ra: float,
        cold_dec: float,
        count: int,
        dwell_seconds: float,
    ) -> None:
        rows: list[dict[str, float]] = []
        completed_unit = "dB"
        yfactor_dir = Path(self.config_path).parent / "yfactor"
        yfactor_dir.mkdir(parents=True, exist_ok=True)
        log_path = yfactor_dir / f"wt6_yfactor_{antenna_name.lower()}_{datetime.now():%Y%m%d-%H%M%S}.csv"
        try:
            with log_path.open("w", newline="", encoding="utf-8") as handle:
                fieldnames = [
                    "local_time",
                    "utc_time",
                    "antenna",
                    "measurement",
                    "measurement_count",
                    "hot_source",
                    "hot_az",
                    "hot_el",
                    "cold_mode",
                    "cold_az",
                    "cold_el",
                    "hot_power",
                    "cold_power",
                    "power_unit",
                    "hot_channel",
                    "cold_channel",
                    "hot_dbfs",
                    "cold_dbfs",
                    "hot_samples",
                    "cold_samples",
                    "calibrated",
                    "extrapolated",
                    "y_factor_ratio",
                    "y_factor_db",
                    "dwell_seconds",
                ]
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                selected = self.sessions.get(antenna_name)
                if not selected:
                    raise RuntimeError(f"{antenna_name} is not connected.")
                selected_panel = self.panels.get(antenna_name)
                with self.motion_lock:
                    for other_name, other_session in list(self.sessions.items()):
                        if other_name == antenna_name:
                            continue
                        other_session.stop_all()
                        other_session.update_oled_activity("STOPPED")
                        other_panel = self.panels.get(other_name)
                        if other_panel:
                            self.events.put(("ok", other_panel.set_tracking_status, "STOPPED"))

                    for index in range(1, count + 1):
                        if self.yfactor_stop_event.is_set():
                            break
                        hot_provider = lambda label=target_label: self.yfactor_hot_target(label)
                        cold_provider = lambda mode=cold_mode, az=cold_az, el=cold_el, ra=cold_ra, dec=cold_dec: self.yfactor_cold_target(
                            mode,
                            self.yfactor_hot_target(target_label),
                            az,
                            el,
                            ra,
                            dec,
                        )
                        hot_target = hot_provider()
                        cold_target = cold_provider()
                        self.events.put(("ok", dialog.set_status, f"Measurement {index}/{count}: hot {hot_target.name}."))
                        self.events.put(("ok", self.apply_yfactor_target_position, target_label))
                        self.yfactor_slew_selected(antenna_name, selected, selected_panel, hot_provider, "HOT")
                        hot = self.collect_power_average(dwell_seconds, self.yfactor_stop_event, selected, selected_panel, target_label, antenna_name)
                        if self.yfactor_stop_event.is_set():
                            break
                        completed_unit = str(hot["power_unit"])
                        hot_target = hot_provider()
                        self.events.put(("ok", dialog.set_status, f"Measurement {index}/{count}: cold sky."))
                        self.events.put(("ok", self.apply_yfactor_target_position, target_label))
                        self.yfactor_slew_selected(antenna_name, selected, selected_panel, cold_provider, "COLD")
                        cold = self.collect_power_average(dwell_seconds, self.yfactor_stop_event, selected, selected_panel, target_label, antenna_name)
                        if self.yfactor_stop_event.is_set():
                            break
                        cold_target = cold_provider()
                        y_db = hot["power_value"] - cold["power_value"]
                        y_ratio = 10 ** (y_db / 10.0)
                        rows.append(
                            {
                                "hot": hot["power_value"],
                                "cold": cold["power_value"],
                                "y_db": y_db,
                                "y_ratio": y_ratio,
                            }
                        )
                        now_local = datetime.now().astimezone()
                        now_utc = now_local.astimezone(timezone.utc)
                        writer.writerow(
                            {
                                "local_time": now_local.isoformat(timespec="seconds"),
                                "utc_time": now_utc.isoformat(timespec="seconds"),
                                "antenna": antenna_name,
                                "measurement": index,
                                "measurement_count": count,
                                "hot_source": hot_target.name,
                                "hot_az": f"{hot_target.azimuth:0.3f}",
                                "hot_el": f"{hot_target.elevation:0.3f}",
                                "cold_mode": cold_mode,
                                "cold_az": f"{cold_target.azimuth:0.3f}",
                                "cold_el": f"{cold_target.elevation:0.3f}",
                                "hot_power": f"{float(hot['power_value']):0.3f}",
                                "cold_power": f"{float(cold['power_value']):0.3f}",
                                "power_unit": hot["power_unit"],
                                "hot_channel": hot.get("power_channel", ""),
                                "cold_channel": cold.get("power_channel", ""),
                                "hot_dbfs": f"{float(hot['power_dbfs']):0.3f}",
                                "cold_dbfs": f"{float(cold['power_dbfs']):0.3f}",
                                "hot_samples": int(hot["sample_count"]),
                                "cold_samples": int(cold["sample_count"]),
                                "calibrated": bool(hot.get("power_calibrated")) and bool(cold.get("power_calibrated")),
                                "extrapolated": bool(hot.get("power_extrapolated")) or bool(cold.get("power_extrapolated")),
                                "y_factor_ratio": f"{y_ratio:0.6f}",
                                "y_factor_db": f"{y_db:0.3f}",
                                "dwell_seconds": f"{dwell_seconds:0.3f}",
                            }
                        )
                        handle.flush()
                        self.event_log.debug(
                            "YFACTOR_POINT",
                            antenna=antenna_name,
                            index=index,
                            hot=hot["power_value"],
                            cold=cold["power_value"],
                            unit=hot["power_unit"],
                            y_db=y_db,
                            csv=str(log_path),
                        )
                    selected.stop_all()
                    selected.update_oled_activity("STOPPED")
                    if selected_panel:
                        self.events.put(("ok", selected_panel.set_tracking_status, "STOPPED"))
            if self.yfactor_stop_event.is_set():
                self.events.put(("ok", dialog.set_status, "Y Factor stopped."))
                self.events.put(("ok", self.set_status, "Y Factor stopped."))
                return
            if not rows:
                raise RuntimeError("No Y Factor measurements were completed.")
            avg_hot = sum(row["hot"] for row in rows) / len(rows)
            avg_cold = sum(row["cold"] for row in rows) / len(rows)
            avg_y_db = sum(row["y_db"] for row in rows) / len(rows)
            avg_y_ratio = sum(row["y_ratio"] for row in rows) / len(rows)
            summary = (
                f"Y Factor {avg_y_db:0.1f} dB, "
                f"hot {avg_hot:0.1f} {completed_unit}, cold {avg_cold:0.1f} {completed_unit}, n={len(rows)}"
            )
            self.event_log.info("YFACTOR_COMPLETE", antenna=antenna_name, y_ratio=avg_y_ratio, y_db=avg_y_db, count=len(rows), csv=str(log_path))
            self.events.put(("ok", dialog.set_status, summary))
            self.events.put(("ok", self.set_status, summary))
        except Exception as exc:
            self.yfactor_stop_event.set()
            self.event_log.error("YFACTOR_FAULT", antenna=antenna_name, error=str(exc))
            self.events.put(("error", dialog.set_status, str(exc)))
            self.events.put(("error", self.set_status, f"Y Factor fault: {exc}"))
        finally:
            self.yfactor_active = False
            self.yfactor_target_label = ""

    def yfactor_hot_target(self, target_label: str) -> TargetPosition:
        if target_label == "Sun":
            return self.target_for_kind("sun")
        if target_label == "Moon":
            return self.target_for_kind("moon")
        if target_label == "Source":
            return self.target_for_kind("source")
        raise RuntimeError("Unknown Y Factor target.")

    def yfactor_cold_target(
        self,
        cold_mode: str,
        hot_target: TargetPosition,
        cold_az: float,
        cold_el: float,
        cold_ra: float,
        cold_dec: float,
    ) -> TargetPosition:
        if cold_mode == "Sun AZ / EL 80":
            sun = self.target_for_kind("sun")
            return TargetPosition("Cold Sky", sun.azimuth, 80.0)
        if cold_mode == "Moon AZ / EL 80":
            moon = self.target_for_kind("moon")
            return TargetPosition("Cold Sky", moon.azimuth, 80.0)
        if cold_mode == "AZ/EL":
            return TargetPosition("Cold Sky", cold_az % 360.0, cold_el)
        if cold_mode == "RA/Dec":
            return source_position("Cold Sky", cold_ra, cold_dec, self.site.latitude, self.site.longitude)
        raise RuntimeError("Unknown cold sky mode.")

    def yfactor_slew_selected(
        self,
        antenna_name: str,
        session: SafeAntenna,
        panel: Optional[AntennaPanel],
        target_provider: Callable[[], TargetPosition],
        mode: str,
    ) -> Position:
        effective_target = self.apply_az_low_to_high_compensation(antenna_name, session, target_provider())
        current_target = {"target": effective_target}

        def live_target(position: Position) -> tuple[float, float]:
            target = target_provider()
            compensation = session.config.az_low_to_high_compensation
            effective = target
            if compensation != 0.0:
                az_delta = session.config.limits.azimuth_delta_to_target(position.azimuth, target.azimuth)
                if az_delta > self.az_tracking_start_tolerance():
                    effective = TargetPosition(target.name, (target.azimuth + compensation) % 360.0, target.elevation)
            current_target["target"] = effective
            return effective.azimuth, effective.elevation

        def progress(position: Position) -> None:
            if panel:
                self.events.put(("position", panel.update_position, position))
            target = current_target["target"]
            session.update_oled_position(target.azimuth, target.elevation, "YFACT")

        if panel:
            self.events.put(("ok", panel.set_tracking_status, "YFACTOR"))
        session.update_oled(mode, effective_target.azimuth, effective_target.elevation, "YFACT")
        position = session.guarded_slew_to(
            effective_target.azimuth,
            effective_target.elevation,
            session.config.az_track_speed,
            session.config.el_track_speed,
            self.yfactor_stop_event,
            self.az_tracking_start_tolerance(),
            self.el_tracking_start_tolerance(),
            self.az_tracking_stop_tolerance(),
            self.el_tracking_stop_tolerance(),
            self.site.az_slow_speed,
            self.site.el_slow_speed,
            self.site.az_slow_threshold_degrees,
            self.site.el_slow_threshold_degrees,
            progress,
            live_target,
        )
        if panel:
            self.events.put(("position", panel.update_position, position))
        return position

    def collect_power_average(
        self,
        dwell_seconds: float,
        stop_event: threading.Event,
        session: Optional[SafeAntenna] = None,
        panel: Optional[AntennaPanel] = None,
        target_label: str = "",
        antenna_name: str = "",
    ) -> dict[str, object]:
        measurements: list[dict[str, object]] = []
        end_time = time.monotonic() + dwell_seconds
        next_position_update = 0.0
        while not stop_event.is_set() and time.monotonic() < end_time:
            measurement = self.power_panel.current_power_measurement(antenna_name)
            if measurement is not None:
                measurements.append(measurement)
            if session and panel and time.monotonic() >= next_position_update:
                try:
                    position = session.read_position()
                    self.events.put(("position", panel.update_position, position))
                    if target_label:
                        self.events.put(("ok", self.apply_yfactor_target_position, target_label))
                except Exception as exc:
                    self.events.put(("ok", self.handle_controller_fault_event, (session.config.name, str(exc))))
                    raise
                next_position_update = time.monotonic() + 0.5
            time.sleep(0.1)
        if not measurements:
            raise RuntimeError("No B210 power measurements were available.")
        return {
            "power_value": sum(float(row["power_value"]) for row in measurements) / len(measurements),
            "power_dbfs": sum(float(row["power_dbfs"]) for row in measurements) / len(measurements),
            "power_unit": str(measurements[-1]["power_unit"]),
            "power_channel": str(measurements[-1].get("power_channel", "")),
            "power_calibrated": all(bool(row.get("power_calibrated")) for row in measurements),
            "power_extrapolated": any(bool(row.get("power_extrapolated")) for row in measurements),
            "sample_count": len(measurements),
        }

    def prepare_peak_calibration_owner(self) -> None:
        if self.parking_active:
            raise RuntimeError("Stop parking before using Peak Calibration.")
        thread = self.tracking_thread
        if not self.tracking_active and not (thread and thread.is_alive()):
            return

        self.tracking_stop_event.set()
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            max_jog = max((session.config.limits.max_jog_seconds for session in self.sessions.values()), default=60.0)
            timeout = min(10.0, max(2.0, self.site.track_interval_seconds + max_jog * 0.1))
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise RuntimeError("Main tracking is still stopping; try Peak Calibration again in a moment.")
        self.tracking_active = False
        self.tracking_kind = None
        self.status_var.set("Tracking stopped for Peak Calibration.")

    def park_all(self) -> None:
        if self.parking_active:
            self.status_var.set("Parking already in progress.")
            return
        if not self.sessions:
            self.status_var.set("Connect antennas before parking.")
            return
        try:
            for name, session in self.sessions.items():
                session.config.limits.assert_position_allowed(session.config.park_az, session.config.park_el)
        except Exception as exc:
            self.status_var.set(f"Park position invalid: {exc}")
            return

        if not self.stop_tracking_before_park():
            return
        self.park_stop_event.clear()
        self.parking_active = True
        self.status_var.set("Parking antennas...")
        self.event_log.info(
            "PARK_START",
            antennas=list(self.sessions),
            targets={name: {"az": session.config.park_az, "el": session.config.park_el} for name, session in self.sessions.items()},
        )
        threading.Thread(target=self.park_worker, daemon=True).start()

    def park_worker(self) -> None:
        sessions = list(self.sessions.items())
        errors: list[str] = []
        lock = threading.Lock()

        def make_worker(name: str, session: SafeAntenna):
            panel = self.panels.get(name)

            def progress(position: Position) -> None:
                if panel:
                    self.events.put(("position", panel.update_position, position))
                session.update_oled_position(session.config.park_az, session.config.park_el, "PARKING")

            def worker() -> None:
                try:
                    if panel:
                        self.events.put(("ok", panel.set_tracking_status, "PARKING"))
                    session.update_oled("PARK", session.config.park_az, session.config.park_el, "PARKING")
                    position = session.guarded_slew_to(
                        session.config.park_az,
                        session.config.park_el,
                        session.config.az_track_speed,
                        session.config.el_track_speed,
                        self.park_stop_event,
                        self.az_tracking_start_tolerance(),
                        self.el_tracking_start_tolerance(),
                        self.az_tracking_stop_tolerance(),
                        self.el_tracking_stop_tolerance(),
                        self.site.az_slow_speed,
                        self.site.el_slow_speed,
                        self.site.az_slow_threshold_degrees,
                        self.site.el_slow_threshold_degrees,
                        progress,
                    )
                    if self.park_stop_event.is_set():
                        raise RuntimeError("Park cancelled.")
                    session.update_oled("PARK", session.config.park_az, session.config.park_el, "PARKED")
                    if panel:
                        self.events.put(("position", panel.update_position, position))
                        self.events.put(("ok", panel.set_tracking_status, "PARKED"))
                except Exception as exc:
                    if panel:
                        self.events.put(("error", panel.set_fault, str(exc)))
                    with lock:
                        errors.append(f"{name}: {exc}")

            return worker

        with self.motion_lock:
            threads = [threading.Thread(target=make_worker(name, session), daemon=True) for name, session in sessions]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        if errors:
            for _name, session in sessions:
                try:
                    session.stop_all()
                except Exception:
                    pass
            self.events.put(("error", self.finish_park_fault, "; ".join(errors)))
            return

        closed_names: list[str] = []
        for name, session in sessions:
            try:
                session.close()
                closed_names.append(name)
            except Exception as exc:
                errors.append(f"{name}: disconnect failed after park: {exc}")
        if errors:
            self.events.put(("error", self.finish_park_fault, "; ".join(errors)))
            return
        self.events.put(("ok", self.finish_park_success, closed_names))

    def finish_park_success(self, names: list[str]) -> None:
        for name in names:
            self.detach_session(name)
        self.parking_active = False
        self.timeout_in_progress = False
        self.park_stop_event.clear()
        self.status_var.set("Parked and disconnected.")
        self.event_log.info("PARK_SUCCESS", antennas=names)

    def finish_park_fault(self, message: str) -> None:
        self.parking_active = False
        self.timeout_in_progress = False
        self.park_stop_event.clear()
        for panel in self.panels.values():
            if panel.session and panel.status_var.get() == "PARKING":
                panel.status_var.set("STOPPED")
        self.status_var.set(f"Park fault: {message}")
        self.event_log.error("PARK_FAULT", error=message)

    def tracking_loop(self, kind: str) -> None:
        acquired = False
        try:
            while not self.tracking_stop_event.is_set():
                target = self.current_tracking_target(kind)
                self.tracking_last_update = time.monotonic()
                self.events.put(("ok", self.apply_target_position, target))
                if not acquired:
                    self.events.put(("ok", self.set_status, f"Slewing to {target.name}."))
                self.slew_all_to_target(target, target.name[:8].upper(), show_slewing=not acquired)
                if self.tracking_stop_event.is_set():
                    break
                acquired = True
                self.tracking_last_update = time.monotonic()
                self.events.put(("ok", self.finish_target_slew, target))
                wait_until = time.monotonic() + max(0.1, self.site.track_interval_seconds)
                while not self.tracking_stop_event.is_set() and time.monotonic() < wait_until:
                    time.sleep(0.05)
        except Exception as exc:
            self.tracking_stop_event.set()
            self.events.put(("error", self.finish_tracking_fault, str(exc)))
        finally:
            self.tracking_active = False

    def target_for_kind(self, kind: str, when: Optional[datetime] = None) -> TargetPosition:
        if kind == "sun":
            sun = sun_position(self.site.latitude, self.site.longitude, when)
            return TargetPosition("Sun", sun.azimuth, sun.elevation)
        if kind == "moon":
            return moon_position(self.site.latitude, self.site.longitude, when)
        if kind == "source":
            source = self.selected_source()
            return source_position(
                source.name,
                source.ra_hours,
                source.dec_degrees,
                self.site.latitude,
                self.site.longitude,
                when,
            )
        raise RuntimeError(f"Unknown target type: {kind}")

    def selected_source(self) -> SourceConfig:
        if not self.site.selected_source:
            raise RuntimeError("Open Sources and select a source before source tracking.")
        source = self.sources.get(self.site.selected_source)
        if source is None:
            raise RuntimeError(f"Selected source {self.site.selected_source!r} was not found.")
        return source

    def current_tracking_target(self, kind: str, when: Optional[datetime] = None) -> TargetPosition:
        source = self.target_for_kind(kind, when)
        az_tolerance = self.site.az_track_tolerance_degrees
        el_tolerance = self.site.el_track_tolerance_degrees
        if az_tolerance >= 0 and el_tolerance >= 0:
            return source

        now = when or datetime.now(timezone.utc)
        future = self.target_for_kind(kind, now + timedelta(seconds=60))
        az_delta = shortest_angle_delta(source.azimuth, future.azimuth)
        el_delta = future.elevation - source.elevation
        azimuth = source.azimuth
        elevation = source.elevation
        if az_tolerance < 0 and az_delta != 0.0:
            azimuth = (azimuth + abs(az_tolerance) * (1.0 if az_delta > 0 else -1.0)) % 360.0
        if el_tolerance < 0 and el_delta != 0.0:
            elevation += abs(el_tolerance) * (1.0 if el_delta > 0 else -1.0)
        target = TargetPosition(
            name=source.name,
            azimuth=azimuth,
            elevation=elevation,
        )
        return target

    def tracking_target_is_low_to_high(
        self,
        kind: str,
        antenna_name: str,
        session: SafeAntenna,
        target: TargetPosition,
    ) -> bool:
        now = datetime.now(timezone.utc)
        future = self.current_tracking_target(kind, now + timedelta(seconds=60))
        future = self.apply_scan_offset(future, antenna_name)
        try:
            az_delta = session.config.limits.azimuth_delta_to_target(target.azimuth, future.azimuth)
        except Exception:
            az_delta = shortest_angle_delta(target.azimuth, future.azimuth)
        return az_delta > 0.001

    def apply_scan_offset(self, target: TargetPosition, antenna_name: str) -> TargetPosition:
        with self.scan_offset_lock:
            scan_antenna_name = self.scan_antenna_name
            axis = self.scan_axis
            offset = self.scan_offset_degrees
        if antenna_name != scan_antenna_name or axis is None or offset == 0.0:
            return target
        if axis == Axis.AZIMUTH:
            return TargetPosition(target.name, (target.azimuth + offset) % 360.0, target.elevation)
        return TargetPosition(target.name, target.azimuth, max(0.0, min(90.0, target.elevation + offset)))

    def set_scan_offset(self, antenna_name: Optional[str], axis: Optional[Axis] = None, offset: float = 0.0) -> None:
        with self.scan_offset_lock:
            self.scan_antenna_name = antenna_name or ""
            self.scan_axis = axis
            self.scan_offset_degrees = offset if antenna_name and axis else 0.0

    def validate_site(self, site: SiteConfig) -> None:
        self.validate_observer(site)
        if not (0.1 <= site.track_interval_seconds <= 10.0):
            raise RuntimeError("Tracking interval must be 0.1..10.0 seconds.")
        if not (1 <= site.log_retention_days <= 365):
            raise RuntimeError("Log retention must be 1..365 days.")
        if not (1.0 <= site.timeout_minutes <= 1440.0):
            raise RuntimeError("Timeout must be 1..1440 minutes.")
        if site.timeout_action not in ("disconnect", "park_disconnect"):
            raise RuntimeError("Timeout action must be disconnect or park_disconnect.")
        self._validate_axis_tracking(
            "AZ",
            site.az_track_tolerance_degrees,
            site.az_stop_tolerance_degrees,
            site.az_slow_speed,
            site.az_slow_threshold_degrees,
        )
        self._validate_axis_tracking(
            "EL",
            site.el_track_tolerance_degrees,
            site.el_stop_tolerance_degrees,
            site.el_slow_speed,
            site.el_slow_threshold_degrees,
        )

    def _validate_axis_tracking(
        self,
        axis: str,
        start_tolerance: float,
        stop_tolerance: float,
        slow_speed: int,
        slow_threshold: float,
    ) -> None:
        if not (-0.2 <= start_tolerance <= 0.2) or start_tolerance == 0.0:
            raise RuntimeError(f"{axis} start tolerance must be -0.20..-0.01 or 0.01..0.20 degrees.")
        if not (-abs(start_tolerance) <= stop_tolerance <= abs(start_tolerance)) or stop_tolerance == 0.0:
            raise RuntimeError(f"{axis} stop tolerance must be +/-0.01 degrees up to the start tolerance.")
        if not (1 <= slow_speed <= 100):
            raise RuntimeError(f"{axis} slow speed must be 1..100.")
        if not (abs(start_tolerance) <= slow_threshold <= 30.0):
            raise RuntimeError(f"{axis} slow deg must be at least start tolerance and no more than 30 degrees.")

    def validate_observer(self, site: SiteConfig) -> None:
        if not (-90.0 <= site.latitude <= 90.0):
            raise RuntimeError("Latitude must be -90..90 degrees.")
        if not (-180.0 <= site.longitude <= 180.0):
            raise RuntimeError("Longitude must be -180..180 degrees.")

    def apply_target_position(self, target: TargetPosition) -> None:
        self.current_target = target
        self.target_name_var.set(target.name)
        self.target_az_var.set(f"AZ {target.azimuth:0.2f}")
        self.target_el_var.set(f"EL {target.elevation:0.2f}")
        self.target_ha_var.set(self.current_hour_angle_text())
        self.state_store.set_target(target.name, target.azimuth, target.elevation, self.target_ha_var.get())

    def apply_yfactor_target_position(self, target_label: str) -> None:
        target = self.yfactor_hot_target(target_label)
        self.current_target = target
        self.target_name_var.set(target.name)
        self.target_az_var.set(f"AZ {target.azimuth:0.2f}")
        self.target_el_var.set(f"EL {target.elevation:0.2f}")
        self.target_ha_var.set(self.current_hour_angle_text(self.kind_for_yfactor_target(target_label)))
        self.state_store.set_target(target.name, target.azimuth, target.elevation, self.target_ha_var.get())

    def current_hour_angle_text(self, kind: Optional[str] = None) -> str:
        now = datetime.now(timezone.utc)
        kind = kind or self.tracking_kind
        try:
            if kind == "sun":
                ra_hours = sun_equatorial(now).ra_hours
            elif kind == "moon":
                ra_hours = moon_equatorial(now)[0].ra_hours
            elif kind == "source":
                ra_hours = self.selected_source().ra_hours
            else:
                return "HA --"
        except RuntimeError:
            return "HA --"
        hour_angle_degrees = local_sidereal_time(self.site.longitude, now) - ra_hours * 15.0
        hour_angle_degrees = self.wrap_signed_degrees(hour_angle_degrees)
        return f"HA {self.format_hour_angle(hour_angle_degrees)}"

    def wrap_signed_degrees(self, value: float) -> float:
        while value <= -180.0:
            value += 360.0
        while value > 180.0:
            value -= 360.0
        return value

    def format_hour_angle(self, hour_angle_degrees: float) -> str:
        sign = "+" if hour_angle_degrees >= 0.0 else "-"
        total_minutes = int(round(abs(hour_angle_degrees) / 15.0 * 60.0))
        hours, minutes = divmod(total_minutes, 60)
        return f"{sign}{hours:02d}:{minutes:02d}"

    def kind_for_yfactor_target(self, target_label: str) -> str:
        if target_label == "Sun":
            return "sun"
        if target_label == "Moon":
            return "moon"
        if target_label == "Source":
            return "source"
        return ""

    def slew_all_to_target(
        self,
        target: TargetPosition,
        mode: str,
        show_slewing: bool = True,
        stop_event: Optional[threading.Event] = None,
    ) -> TargetPosition:
        with self.motion_lock:
            return self._slew_all_to_target(target, mode, show_slewing, stop_event or self.tracking_stop_event)

    def _slew_all_to_target(
        self,
        target: TargetPosition,
        mode: str,
        show_slewing: bool = True,
        stop_event: Optional[threading.Event] = None,
    ) -> TargetPosition:
        errors: list[str] = []
        threads: list[threading.Thread] = []
        lock = threading.Lock()
        active_stop_event = stop_event or self.tracking_stop_event
        primary_slews = {"count": 0}

        def other_primary_slews_active() -> bool:
            with lock:
                return primary_slews["count"] > 0

        def mark_primary_slew_done() -> None:
            with lock:
                primary_slews["count"] = max(0, primary_slews["count"] - 1)

        def make_worker(name: str, session: SafeAntenna, panel: AntennaPanel):
            activity = self.oled_activity_for_antenna(name, "SLEWING" if show_slewing else "TRACKING")
            effective_target = self.apply_scan_offset(target, name)
            force_live_low_to_high = (
                bool(self.tracking_kind)
                and not show_slewing
                and self.tracking_target_is_low_to_high(self.tracking_kind, name, session, effective_target)
            )
            effective_target = self.apply_az_low_to_high_compensation(
                name,
                session,
                effective_target,
                force_low_to_high=force_live_low_to_high,
            )
            current_effective_target = {"target": effective_target}
            current_activity = {"activity": activity}

            def progress(position: Position) -> None:
                self.events.put(("position", panel.update_position, position))
                display_target = current_effective_target["target"]
                session.update_oled_position(
                    display_target.azimuth,
                    display_target.elevation,
                    current_activity["activity"],
                )

            def live_tracking_target(position: Position) -> tuple[float, float]:
                if not self.tracking_active or not self.tracking_kind:
                    return effective_target.azimuth, effective_target.elevation
                live_target = self.current_tracking_target(self.tracking_kind)
                live_target = self.apply_scan_offset(live_target, name)
                force_live_low_to_high = (
                    not show_slewing
                    and self.tracking_target_is_low_to_high(self.tracking_kind, name, session, live_target)
                )
                live_target = self.apply_az_low_to_high_compensation(
                    name,
                    session,
                    live_target,
                    force_low_to_high=force_live_low_to_high,
                )
                current_effective_target["target"] = live_target
                return live_target.azimuth, live_target.elevation

            def worker() -> None:
                primary_slew_done = False
                try:
                    self.events.put(("ok", panel.set_tracking_status, activity))
                    slew_log = self.event_log.info if show_slewing else self.event_log.debug
                    slew_log(
                        "SLEW_START",
                        antenna=name,
                        mode=mode,
                        activity=activity,
                        nominal_az=target.azimuth,
                        nominal_el=target.elevation,
                        effective_az=effective_target.azimuth,
                        effective_el=effective_target.elevation,
                    )
                    session.update_oled(mode, effective_target.azimuth, effective_target.elevation, activity)
                    position = session.guarded_slew_to(
                        effective_target.azimuth,
                        effective_target.elevation,
                        session.config.az_track_speed,
                        session.config.el_track_speed,
                        active_stop_event,
                        self.az_tracking_start_tolerance(),
                        self.el_tracking_start_tolerance(),
                        self.az_tracking_stop_tolerance(),
                        self.el_tracking_stop_tolerance(),
                        self.site.az_slow_speed,
                        self.site.el_slow_speed,
                        self.site.az_slow_threshold_degrees,
                        self.site.el_slow_threshold_degrees,
                        progress,
                        live_tracking_target if self.tracking_kind else None,
                    )
                    mark_primary_slew_done()
                    primary_slew_done = True
                    tracking_activity = self.oled_activity_for_antenna(name, "TRACKING")
                    current_activity["activity"] = tracking_activity
                    session.update_oled(
                        mode,
                        current_effective_target["target"].azimuth,
                        current_effective_target["target"].elevation,
                        tracking_activity,
                    )
                    self.events.put(("ok", panel.set_tracking_status, tracking_activity))
                    while (
                        live_tracking_target
                        and other_primary_slews_active()
                        and not active_stop_event.is_set()
                        and self.tracking_active
                        and self.tracking_kind
                    ):
                        position = session.read_position()
                        live_azimuth, live_elevation = live_tracking_target(position)
                        position = session.guarded_slew_to(
                            live_azimuth,
                            live_elevation,
                            session.config.az_track_speed,
                            session.config.el_track_speed,
                            active_stop_event,
                            self.az_tracking_start_tolerance(),
                            self.el_tracking_start_tolerance(),
                            self.az_tracking_stop_tolerance(),
                            self.el_tracking_stop_tolerance(),
                            self.site.az_slow_speed,
                            self.site.el_slow_speed,
                            self.site.az_slow_threshold_degrees,
                            self.site.el_slow_threshold_degrees,
                            progress,
                            live_tracking_target,
                        )
                        if active_stop_event.wait(max(0.1, min(1.0, self.site.track_interval_seconds))):
                            break
                    final_target = current_effective_target["target"]
                    if active_stop_event.is_set():
                        session.update_oled(mode, final_target.azimuth, final_target.elevation, "STOPPED")
                        self.events.put(("position", panel.update_position, position))
                        self.events.put(("ok", panel.set_tracking_status, "STOPPED"))
                        return
                    final_activity = self.oled_activity_for_antenna(name, "TRACKING")
                    session.update_oled(mode, final_target.azimuth, final_target.elevation, final_activity)
                    self.events.put(("position", panel.update_position, position))
                    self.events.put(("ok", panel.set_tracking_status, final_activity))
                    slew_log(
                        "SLEW_END",
                        antenna=name,
                        mode=mode,
                        activity=final_activity,
                        az=position.azimuth,
                        el=position.elevation,
                        target_az=final_target.azimuth,
                        target_el=final_target.elevation,
                    )
                except SafetyError as exc:
                    self.event_log.error("SLEW_SAFETY_STOP", antenna=name, mode=mode, error=str(exc))
                    try:
                        session.stop_all()
                        position = session.read_position()
                        fault_target = current_effective_target["target"]
                        session.update_oled(mode, fault_target.azimuth, fault_target.elevation, "STOPPED")
                        self.events.put(("position", panel.update_position, position))
                        self.events.put(("error", panel.set_fault, str(exc)))
                    except Exception as comm_exc:
                        self.event_log.error(
                            "SLEW_SAFETY_STOP_OFFLINE",
                            antenna=name,
                            mode=mode,
                            error=str(exc),
                            communication_error=str(comm_exc),
                        )
                        self.events.put(("ok", self.handle_controller_fault_event, (name, str(comm_exc))))
                    with lock:
                        errors.append(f"{name}: {exc}")
                except Exception as exc:
                    self.event_log.error("SLEW_FAULT", antenna=name, mode=mode, error=str(exc))
                    self.events.put(("ok", self.handle_controller_fault_event, (name, str(exc))))
                    with lock:
                        errors.append(f"{name}: {exc}")
                finally:
                    if not primary_slew_done:
                        mark_primary_slew_done()

            return worker

        workers: list[tuple[str, SafeAntenna, AntennaPanel]] = []
        for name, session in list(self.sessions.items()):
            panel = self.panels.get(name)
            if not panel:
                continue
            workers.append((name, session, panel))

        primary_slews["count"] = len(workers)
        for name, session, panel in workers:
            thread = threading.Thread(target=make_worker(name, session, panel), daemon=True)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()
        if errors:
            active_stop_event.set()
            raise RuntimeError("; ".join(errors))
        return target

    def apply_az_low_to_high_compensation(
        self,
        antenna_name: str,
        session: SafeAntenna,
        target: TargetPosition,
        force_low_to_high: bool = False,
    ) -> TargetPosition:
        compensation = session.config.az_low_to_high_compensation
        if compensation == 0.0:
            return target
        if self.scan_active and antenna_name == self.scan_antenna_name and self.scan_axis == Axis.AZIMUTH:
            self.event_log.debug(
                "AZ_HYSTERESIS_COMP_DISABLED_FOR_SCAN",
                antenna=antenna_name,
                target=target.name,
                nominal_az=target.azimuth,
                compensation=compensation,
            )
            return target
        try:
            current = session.last_position or session.read_position()
            az_delta = session.config.limits.azimuth_delta_to_target(current.azimuth, target.azimuth)
        except Exception as exc:
            self.event_log.warn(
                "AZ_HYSTERESIS_COMP_SKIPPED",
                antenna=antenna_name,
                target=target.name,
                error=str(exc),
            )
            return target
        if not force_low_to_high and az_delta <= self.az_tracking_start_tolerance():
            return target
        adjusted_az = (target.azimuth + compensation) % 360.0
        adjusted = TargetPosition(target.name, adjusted_az, target.elevation)
        self.event_log.info(
            "AZ_HYSTERESIS_COMP_APPLIED",
            antenna=antenna_name,
            target=target.name,
            current_az=current.azimuth,
            nominal_az=target.azimuth,
            effective_az=adjusted_az,
            az_delta=az_delta,
            compensation=compensation,
            force_low_to_high=force_low_to_high,
        )
        return adjusted

    def oled_activity_for_antenna(self, antenna_name: str, default_activity: str) -> str:
        if self.scan_active and antenna_name == self.scan_antenna_name:
            return "SCAN"
        return default_activity

    def az_tracking_start_tolerance(self) -> float:
        return abs(self.site.az_track_tolerance_degrees)

    def az_tracking_stop_tolerance(self) -> float:
        return self.site.az_stop_tolerance_degrees

    def el_tracking_start_tolerance(self) -> float:
        return abs(self.site.el_track_tolerance_degrees)

    def el_tracking_stop_tolerance(self) -> float:
        return self.site.el_stop_tolerance_degrees

    def finish_target_slew(self, target: TargetPosition) -> None:
        self.apply_target_position(target)
        if not self.tracking_stop_event.is_set():
            self.status_var.set(f"Tracking {target.name}.")

    def finish_tracking_fault(self, message: str) -> None:
        self.tracking_stop_event.set()
        self.tracking_active = False
        self.tracking_kind = ""
        self.target_ha_var.set("HA --")
        self.status_var.set(f"Tracking fault: {message}")
        self.event_log.error("TRACK_FAULT", error=message)
        for panel in self.panels.values():
            if panel.session and panel.status_var.get() in ("SLEWING", "TRACKING"):
                panel.status_var.set("STOPPED")

    def refresh_tracking_target_display(self) -> None:
        if not self.tracking_active or not self.tracking_kind:
            return
        try:
            self.apply_target_position(self.current_tracking_target(self.tracking_kind))
        except Exception as exc:
            self.finish_tracking_fault(str(exc))

    def check_tracking_watchdog(self) -> None:
        if not self.tracking_active:
            return
        if self.tracking_thread and not self.tracking_thread.is_alive():
            self.tracking_active = False
            self.finish_tracking_fault("Tracking worker stopped unexpectedly.")
            return
        max_jog = max((session.config.limits.max_jog_seconds for session in self.sessions.values()), default=60.0)
        timeout = max(15.0, max_jog + 5.0, self.site.track_interval_seconds * 3.0 + 5.0)
        if time.monotonic() - self.tracking_last_update > timeout:
            self.tracking_stop_event.set()
            self.tracking_active = False
            self.finish_tracking_fault(f"Tracking worker stalled for more than {timeout:0.1f}s.")

    def kind_label(self, kind: str) -> str:
        if kind == "sun":
            return "Sun"
        if kind == "moon":
            return "Moon"
        if kind == "source":
            return self.site.selected_source or "Source"
        return kind

    def default_peak_cal_source_label(self) -> str:
        if self.tracking_kind == "moon":
            return "Moon"
        if self.tracking_kind == "source":
            return "Selected Source"
        return "Sun"

    def open_limits(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        LimitsDialog(self)

    def open_observer(self) -> None:
        ObserverDialog(self)

    def open_tracking(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        TrackingDialog(self)

    def open_sources(self) -> None:
        SourcesDialog(self)

    def open_calibration(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        selected_name = (
            self.peak_calibration_dialog.antenna_var.get()
            if self.peak_calibration_dialog and self.peak_calibration_dialog.winfo_exists()
            else ""
        )
        if self.calibration_dialog and self.calibration_dialog.winfo_exists():
            self.calibration_dialog.refresh_offsets()
            self.calibration_dialog.refresh_live_positions()
            if selected_name:
                self.calibration_dialog.select_antenna(selected_name)
            self.calibration_dialog.lift()
            return
        self.calibration_dialog = CalibrationDialog(self)
        if selected_name:
            self.calibration_dialog.select_antenna(selected_name)

    def open_peak_calibration(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        source_label = self.default_peak_cal_source_label()
        if self.peak_calibration_dialog and self.peak_calibration_dialog.winfo_exists():
            self.peak_calibration_dialog.set_source_label(source_label)
            self.peak_calibration_dialog.lift()
            return
        self.peak_calibration_dialog = PeakCalibrationDialog(self)

    def open_scan_calibration(self) -> None:
        if self.scan_dialog and self.scan_dialog.winfo_exists():
            self.scan_dialog.lift()
            return
        self.scan_dialog = ScanCalibrationDialog(self)

    def open_yfactor(self) -> None:
        if self.yfactor_dialog and self.yfactor_dialog.winfo_exists():
            self.yfactor_dialog.lift()
            return
        self.yfactor_dialog = YFactorDialog(self)

    def open_rtl_calibration(self) -> None:
        if self.rtl_calibration_dialog and self.rtl_calibration_dialog.winfo_exists():
            self.rtl_calibration_dialog.lift()
            return
        self.rtl_calibration_dialog = RtlCalibrationDialog(self)

    def open_b210_calibration(self) -> None:
        if self.b210_calibration_dialog and self.b210_calibration_dialog.winfo_exists():
            self.b210_calibration_dialog.lift()
            return
        self.b210_calibration_dialog = B210CalibrationDialog(self)

    def refresh_calibration_views(self, name: Optional[str] = None, position: Optional[Position] = None) -> None:
        if self.calibration_dialog and self.calibration_dialog.winfo_exists():
            self.calibration_dialog.refresh_offsets(name, position)
        if self.peak_calibration_dialog and self.peak_calibration_dialog.winfo_exists():
            self.peak_calibration_dialog.refresh_offsets(name, position)

    def select_calibration_antenna(self, name: str) -> None:
        if self.calibration_dialog and self.calibration_dialog.winfo_exists():
            self.calibration_dialog.select_antenna(name)

    def open_encoders(self) -> None:
        if not self.configs:
            self.status_var.set("No antenna configs loaded.")
            return
        if not self.sessions:
            message = "Connect antennas before encoder scan."
            self.status_var.set(message)
            messagebox.showerror("Encoders", message, parent=self)
            return
        EncodersDialog(self)

    def update_reference_positions(self) -> None:
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc.astimezone()
        self.local_time_var.set(f"Local {local_now:%Y-%m-%d %H:%M:%S %Z}")
        self.utc_var.set(f"UTC {now_utc:%Y-%m-%d %H:%M:%S}")
        self.lmst_var.set(f"LMST {self.format_sidereal_time(local_sidereal_time(self.site.longitude, now_utc))}")
        try:
            sun = self.target_for_kind("sun", now_utc)
            moon = self.target_for_kind("moon", now_utc)
            self.sun_ref_var.set(f"Sun AZ {sun.azimuth:0.2f} EL {sun.elevation:0.2f}")
            self.moon_ref_var.set(f"Moon AZ {moon.azimuth:0.2f} EL {moon.elevation:0.2f}")
        except Exception as exc:
            self.sun_ref_var.set(f"Reference error: {exc}")
            self.moon_ref_var.set("")

    def format_sidereal_time(self, degrees: float) -> str:
        total_seconds = int(round((degrees / 15.0) * 3600.0)) % 86400
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def save_site_settings(self, message: str) -> None:
        save_site_config(self.config_path, self.site)
        self.status_var.set(message)

    def save_tracking_and_config(self, message: str) -> None:
        save_site_config(self.config_path, self.site)
        save_configs(self.config_path, self.configs)
        self.event_log.retention_days = max(1, int(self.site.log_retention_days))
        self.event_log.level = self.site.log_level.upper() if self.site.log_level.upper() in EventLogger.LEVELS else "INFO"
        self.event_log.cleanup_old_logs()
        self.event_log.info(
            "TRACKING_SETTINGS_SAVE",
            interval=self.site.track_interval_seconds,
            az_start_tolerance=self.site.az_track_tolerance_degrees,
            el_start_tolerance=self.site.el_track_tolerance_degrees,
            az_stop_tolerance=self.site.az_stop_tolerance_degrees,
            el_stop_tolerance=self.site.el_stop_tolerance_degrees,
            log_retention_days=self.site.log_retention_days,
            timeout_enabled=self.site.timeout_enabled,
            timeout_minutes=self.site.timeout_minutes,
            timeout_action=self.site.timeout_action,
            antenna_compensation={
                name: config.az_low_to_high_compensation for name, config in self.configs.items()
            },
        )
        self.status_var.set(message)

    def stop_all(self) -> None:
        self.tracking_stop_event.set()
        self.park_stop_event.set()
        self.scan_stop_event.set()
        self.yfactor_stop_event.set()
        self.scan_active = False
        self.yfactor_active = False
        self.set_scan_offset(None)
        self.event_log.warn("STOP_ALL")
        for panel in self.panels.values():
            panel.stop_event.set()
            if panel.session:
                panel.status_var.set("STOPPED")
        for session in self.sessions.values():
            self.run_worker(
                lambda s=session: (s.stop_all(), s.update_oled_activity("STOPPED")),
                lambda _result: None,
                self.set_status,
            )
        self.status_var.set("Stopped.")

    def check_app_timeout(self) -> None:
        if not self.site.timeout_enabled or self.timeout_in_progress or self.connecting_active or not self.sessions:
            return
        elapsed = time.monotonic() - self.last_user_activity
        if elapsed < max(60.0, self.site.timeout_minutes * 60.0):
            return
        self.timeout_in_progress = True
        action = self.site.timeout_action
        self.event_log.warn("APP_TIMEOUT", action=action, timeout_minutes=self.site.timeout_minutes)
        if action == "park_disconnect":
            self.timeout_park_disconnect()
        else:
            self.timeout_disconnect_only()

    def timeout_park_disconnect(self) -> None:
        self.scan_stop_event.set()
        self.yfactor_stop_event.set()
        self.scan_active = False
        self.yfactor_active = False
        self.status_var.set("Timeout: parking antennas.")
        if self.parking_active:
            return
        self.park_all()

    def timeout_disconnect_only(self) -> None:
        self.status_var.set("Timeout: disconnecting controllers.")
        self.tracking_stop_event.set()
        self.park_stop_event.set()
        self.scan_stop_event.set()
        self.yfactor_stop_event.set()
        self.tracking_active = False
        self.tracking_kind = ""
        self.scan_active = False
        self.yfactor_active = False
        self.parking_active = False
        self.set_scan_offset(None)
        self.run_worker(self.timeout_disconnect_work, self.finish_timeout_disconnect, self.finish_timeout_disconnect_fault)

    def timeout_disconnect_work(self) -> list[str]:
        closed: list[str] = []
        with self.motion_lock:
            for name, session in list(self.sessions.items()):
                session.stop_all()
                session.close()
                closed.append(name)
        return closed

    def finish_timeout_disconnect(self, names: list[str]) -> None:
        for name in names:
            self.detach_session(name)
        self.timeout_in_progress = False
        self.status_var.set("Timeout: controllers disconnected.")
        self.event_log.warn("APP_TIMEOUT_DISCONNECT", antennas=names)

    def finish_timeout_disconnect_fault(self, message: str) -> None:
        self.timeout_in_progress = False
        self.status_var.set(f"Timeout disconnect fault: {message}")
        self.event_log.error("APP_TIMEOUT_DISCONNECT_FAULT", error=message)

    def update_timeout_display(self) -> None:
        if not self.site.timeout_enabled:
            self.timeout_var.set("Timeout off")
            return
        if not self.sessions:
            self.timeout_var.set("Timeout stopped")
            return
        if self.connecting_active:
            self.timeout_var.set("Timeout starting")
            return
        timeout_seconds = max(60.0, self.site.timeout_minutes * 60.0)
        remaining = max(0.0, timeout_seconds - (time.monotonic() - self.last_user_activity))
        total_seconds = int(math.ceil(remaining))
        minutes, seconds = divmod(total_seconds, 60)
        action = "park" if self.site.timeout_action == "park_disconnect" else "disconnect"
        self.timeout_var.set(f"Timeout {minutes:02d}:{seconds:02d} to {action}")

    def periodic_refresh(self) -> None:
        self.update_timeout_display()
        self.check_app_timeout()
        if not self.tracking_active and not self.parking_active and not self.scan_active and not self.yfactor_active:
            self.refresh_all()
        else:
            self.check_controller_health()
        if self.tracking_active:
            self.refresh_tracking_target_display()
            self.check_tracking_watchdog()
        elif self.yfactor_active and self.yfactor_target_label:
            try:
                self.apply_yfactor_target_position(self.yfactor_target_label)
            except Exception as exc:
                self.status_var.set(f"Y Factor target refresh fault: {exc}")
        self.update_reference_positions()
        self.after(1500, self.periodic_refresh)

    def save_config(self, message: str = "Settings saved.") -> None:
        save_configs(self.config_path, self.configs)
        self.event_log.info("CONFIG_SAVE", message=message)
        self.status_var.set(message)

    def run_worker(self, work, on_success, on_error) -> None:
        def target() -> None:
            try:
                self.events.put(("ok", on_success, work()))
            except Exception as exc:
                self.events.put(("error", on_error, str(exc)))

        threading.Thread(target=target, daemon=True).start()

    def process_events(self) -> None:
        while True:
            try:
                kind, callback, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind in ("ok", "position"):
                callback(payload)
            else:
                callback(str(payload))
        self.after(100, self.process_events)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.state_store.set_status(text, self.system_state_from_flags(text))

    def system_state_from_flags(self, text: str = "") -> SystemRunState:
        lowered = text.lower()
        if "fault" in lowered or "error" in lowered:
            return SystemRunState.FAULT
        if self.parking_active:
            return SystemRunState.PARKING
        if self.scan_active:
            return SystemRunState.SCANNING
        if self.yfactor_active:
            return SystemRunState.YFACTOR
        if self.tracking_active:
            return SystemRunState.TRACKING
        if self.connecting_active:
            return SystemRunState.CONNECTING
        if "stopped" in lowered:
            return SystemRunState.STOPPED
        return SystemRunState.IDLE

    def on_close(self) -> None:
        try:
            self.event_log.info("APP_STOP")
            self.power_panel.save_settings()
            self.power_panel.stop_log()
            self.power_panel.stop()
            self.power_panel.wait_for_stop()
            self.stop_scan()
            self.stop_yfactor()
            self.stop_all()
            for session in self.sessions.values():
                session.close()
        finally:
            self.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch WT6 two-antenna GUI.")
    parser.add_argument("--config", default="wt6_ubuntu.ini", help="Config file. Default: wt6_ubuntu.ini")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = WT6App(args.config)
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())







