# WT6

Ubuntu alpha port of the two-antenna radio astronomy controller, now renamed and packaged as WT6. The code is intended to run on an Ubuntu desktop or small-form-factor PC
with two Arduino antenna controllers and an optional Ettus B210 dual-channel power meter.

## Status

This is an alpha port. It keeps the existing control logic and safety rules, but renames the modules and adds Ubuntu-first setup notes.

## Ubuntu Setup

Install system packages:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-tk python3-numpy uhd-host soapysdr-tools soapysdr-module-uhd python3-soapysdr
```

Allow the user to access serial and USB devices:

```bash
sudo usermod -aG dialout,plugdev $USER
```

Log out and back in after changing groups.

Verify the B210 is visible before starting WT6 power measurement:

```bash
uhd_find_devices
SoapySDRUtil --find=driver=uhd
```

Install Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

## First Run

Copy the example config:

```bash
cp wt6_ubuntu.ini.example wt6_ubuntu.ini
```

Edit the antenna serial ports in `wt6_ubuntu.ini`. On Ubuntu these will usually
look like `/dev/ttyUSB0`, `/dev/ttyUSB1`, `/dev/ttyACM0`, or `/dev/ttyACM1`.

Run the GUI:

```bash
python3 wt6_ubuntu_gui.py --config wt6_ubuntu.ini
```

## Migrating From Earlier WT Versions

An existing earlier WT `.ini` can usually be copied to `wt6_ubuntu.ini`; then check:

- antenna serial ports
- observer location
- antenna limits and park positions
- tracking speeds, tolerances, and hysteresis compensation
- B210 frequency, sample rate, bandwidth, clock source, and channel gains

## Safety Rules

- Elevation is constrained to 0..90 degrees, then further constrained by the
  configured antenna limits.
- Azimuth moves use the configured allowed arc and avoid the configured
  dead-zone.
- Long slews are guarded by repeated position polling.
- No-progress, offline, stale-position, protocol, timeout, and limit faults
  stop the affected axes and are written to the event log.
- Manual jog hover events do not stop automatic tracking, slewing, or parking.

## Output Directories

- `logs/` - compact JSON-lines event logs with retention control
- `scan/` - scan calibration CSV files
- `yfactor/` - Y factor measurement CSV files
- `power/` - B210 power meter logs when logging is enabled

## Main Files

- `wt6_ubuntu_gui.py` - Tkinter operator interface and orchestration
- `wt6_antenna.py` - Arduino protocol, controller session, and guarded motion
- `wt6_config.py` - `.ini` loading/saving
- `wt6_b210_power.py` - B210 dual-channel power meter primitives
- `wt6_astro.py` / `wt6_solar.py` - source, Sun, and Moon position calculations
- `wt6_logging.py` - JSON-lines event log


