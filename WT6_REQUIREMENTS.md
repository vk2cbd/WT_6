# WT6 Requirements

WT6 is a clean rebuild of the WT4 prototype antenna controller. WT4 should remain
available as the reference implementation and field-tested prototype. WT6 should
reuse the proven protocol knowledge, safety rules, calibration workflows, and
lessons learned, but should be built with clearer architecture, better
diagnostics, and a more usable GUI.

## Project Goals

- Control two independent antenna systems, normally labelled East and West.
- Replace WinTrak for normal antenna operation.
- Keep safety logic central, explicit, and testable.
- Make long-drive behaviour explainable, especially East/West differences.
- Provide a cleaner, instrument-like GUI with clear normal/abnormal colour
  states.
- Support tracking, calibration, RTL power measurement, scan calibration, and
  Y factor measurement.
- Preserve all field-proven behaviours from WT4 unless deliberately changed.

## Hardware Context

- Host platform: Raspberry Pi running Linux.
- Each antenna controller is Arduino based and connected by USB serial.
- Each Arduino controls one SVH3 slew-drive antenna system.
- Each Arduino also drives a local OLED display.
- Encoder feedback is quadrature pulse based; absolute position is maintained
  by the Arduino/controller software rather than written into the encoder itself.
- There are currently no physical azimuth/elevation limit switches, so software
  safety limits are mandatory.
- Two antennas are used together for interferometry, but many calibration and
  measurement operations act on one antenna at a time.

## Architectural Requirements

WT6 should be split into clear modules. Suggested structure:

- `wt6_protocol.py`: serial protocol only.
- `wt6_safety.py`: limits, azimuth dead-zone logic, path choice, safety checks.
- `wt6_antenna.py`: one antenna session, position reads, OLED writes, guarded
  moves.
- `wt6_tracking.py`: Sun, Moon, and source tracking logic.
- `wt6_power.py`: RTL-SDR power meter and RTL calibration.
- `wt6_calibration.py`: manual calibration, peak calibration, scan calibration,
  Gaussian fitting, Y factor.
- `wt6_config.py`: INI configuration load/save and defaults.
- `wt6_logging.py`: event and measurement logging.
- `wt6_gui.py`: GUI presentation and user interaction only.
- `tests/`: unit and simulation tests.

The GUI should call into service/controller objects rather than containing most
of the business logic itself.

## Safety Requirements

Safety requirements are mandatory and should be implemented before advanced GUI
features.

### Limits

- Elevation must always be limited by configured `el_min` and `el_max`.
- For the current systems, elevation should always be treated as constrained to
  a physically sensible 0 to 90 degree range, further restricted by configured
  limits.
- Azimuth must support wrap-around limits and a forbidden dead-zone. Current
  example: `az_min = 270`, `az_max = 265`, meaning allowed travel is 270 through
  360 and 0 through 265; 265 through 270 is forbidden.
- The antenna must never be commanded through the forbidden azimuth dead-zone.
- Park positions must be validated against all configured limits before motion.
- Source targets outside limits must be rejected before motion.

### Motion Guarding

- Every motion command must be guarded by:
  - current position read
  - software limit check
  - direction-specific margin check
  - timeout
  - stop-event handling
  - post-move stop command
- A failure to reach the target before timeout is a safety stop, not
  automatically a controller-disconnect fault.
- A controller should only be marked offline after a communication failure or
  inability to read/respond after a safety stop.
- Stop commands must take priority over tracking, scanning, Y factor, parking,
  or calibration routines.

### Long Slews

Long slews should not be opaque single calls. WT6 should prefer a segmented
long-slew state machine:

- Break long moves into controlled segments.
- Re-read position between segments.
- Recompute source position between segments if tracking a moving target.
- Recheck limits and dead-zone path between segments.
- Log why each segment stopped.
- Continue until target is reached, stopped by user, limited, timed out, or
  faulted.

This is important because WT4 field testing showed inconsistent long-drive
behaviour between East and West, with West apparently running normally to the
jog timer while East sometimes stops early.

### Motion Anomaly Detection

WT6 should detect and log:

- Position changing when no command is active.
- Command active but position not changing.
- Position jump larger than a configurable sanity threshold.
- Position moving in the opposite direction to the command.
- Serial replies missing, malformed, or delayed.
- East/West behaviour diverging under similar command conditions.

The app may not be able to automatically correct all erratic movement, but it
must flag it clearly and log enough detail for the operator to decide whether to
remove power.

## East/West Diagnostic Requirements

WT6 must make East/West differences visible.

At connect time, log per antenna:

- port
- baud
- open delay
- az/el track speeds
- slow speeds and slow thresholds
- start and stop tolerances
- max jog seconds
- poll interval
- azimuth/elevation limits and margins
- calibration offsets
- hysteresis compensation
- park position

For every move, log per antenna:

- command id
- operation type: manual, track, park, scan, Y factor, calibration
- axis or axes commanded
- direction
- speed
- slow-speed transition if used
- start time
- stop time
- elapsed time
- start raw/calibrated position
- target position
- final raw/calibrated position
- stop reason
- any exception/fault

Stop reasons should be explicit, for example:

- target reached
- user stop
- scan stop
- tracking handoff
- timeout
- limit margin reached
- target outside limits
- no encoder motion
- unexpected encoder jump
- communication fault
- app shutdown

## Tracking Requirements

- Track Sun.
- Track Moon.
- Track a selected user source.
- User sources should include name, RA, Dec, and 4800 MHz flux.
- Source menu should display current AZ/EL for each source.
- Sun and Moon current AZ/EL should be continuously displayed even when not
  tracking.
- Source hour angle should be shown for Sun, Moon, and user sources.
- Tracking must support separate AZ and EL:
  - start tolerance
  - stop tolerance
  - tracking speed
  - slow speed
  - slow threshold
- Negative stop tolerance / lead behaviour from WT4 should be reviewed and
  either preserved or replaced with an explicit lead/lag concept.
- Gross movements should display as slewing.
- Once on source, small tracking corrections should display as tracking, not
  gross slewing.

## Azimuth Hysteresis Compensation

- Support per-antenna low-to-high azimuth compensation.
- Apply compensation when the commanded AZ motion is from lower AZ to higher AZ.
- Compensation must be logged whenever applied.
- The GUI should make it clear when compensation is being applied, or at least
  expose it in the event log.
- Future improvement: consider direction-aware calibration or backlash take-up
  moves, but only if safe and operationally acceptable.

## Parking Requirements

- Park button drives antennas to configured park positions.
- Park positions should be editable from the GUI.
- Park must validate positions before motion.
- Park should stop and disconnect after successful completion.
- Parking should be logged per antenna with the same detailed motion records as
  other long slews.
- If one antenna faults during park, both antennas should be stopped and the
  cause logged clearly.

## Calibration Requirements

### Manual Calibration

- Show calibrated antenna AZ/EL on the main GUI.
- Show raw AZ/EL and offsets in the calibration menu.
- Calibration offsets must be visible and directly editable.
- Calibration can be performed per axis.
- Calibration can use current source AZ/EL as the actual pointing reference.
- It must be possible to track one axis while manually peaking the other axis.

### Peak Calibration

- Support Sun, Moon, and selected user source as the calibration source.
- Operator should be able to manually jog one axis to maximum power and lock
  that axis calibration immediately with one button.
- Offset values shown in peak calibration and standard calibration menus must be
  the same live values.
- Raw values must be truly raw, not calibrated values.

### Scan Calibration

- Select antenna to scan.
- Scan AZ or EL.
- Specify span, increment, dwell time, and number of scans.
- Repeated scans should always run in the same direction for hysteresis
  consistency.
- Current WT4 direction is `+span` to `-span`.
- Save scan CSV files under `scan/`.
- Show scan graph with graticule and boresight line.
- Fit Gaussian plus sloped baseline.
- Plot and fit scan calibration data using clearly labelled axis coordinates.
  The high-elevation azimuth/cross-elevation convention needs further review
  before implementation.
- Report peak centre, FWHM, fitted peak, and residual.
- Continue tracking the selected source while scanning, with only the selected
  antenna offset.
- Stop Scan must interrupt the scan, clear offsets, return to nominal tracking,
  and leave the app ready for another action.

## RTL Power Meter Requirements

- Use RTL-SDR as a power meter.
- Explicitly disable RTL AGC where supported.
- Use manual tuner gain when gain is numeric.
- Flag automatic gain as uncalibrated.
- Support:
  - frequency in MHz
  - sample rate in ksps
  - gain
  - PPM correction
  - samples in kilosamples
  - GUI refresh rate
  - averaging
  - warm-up seconds
- Power display should use one decimal place.
- On stop, stale power should not remain as if live.
- Settings should persist across app restarts.
- Calibration table should support signal generator calibration from -40 dBm to
  -110 dBm.
- Calibrated readings should display in dBm when frequency, sample rate, and
  gain match the stored calibration.
- If calibration does not match, readings should be flagged as uncalibrated.

## Y Factor Requirements

- Y Factor is a hot/cold power measurement using RTL power.
- Select one antenna for the measurement.
- The non-selected antenna must be stopped.
- Hot target options:
  - Sun
  - Moon
  - selected user source
- Cold-sky options:
  - Sun AZ / EL 80
  - Moon AZ / EL 80
  - manual AZ/EL
  - manual RA/Dec
- User can specify number of measurements.
- User can specify dwell time.
- Dwell time is used to collect and average power after each hot/cold slew.
- Final GUI result should display Y factor in dB only, to one decimal place.
- Save per-measurement CSV logs under `yfactor/`.
- Log each measurement cycle with:
  - local and UTC timestamp
  - antenna
  - hot source
  - hot AZ/EL
  - cold mode
  - cold AZ/EL
  - hot/cold power
  - dBFS values
  - calibrated/extrapolated flags
  - Y factor ratio
  - Y factor dB
  - dwell time
- During Y Factor slews, the hot/cold target should be continuously refreshed so
  AZ can keep tracking while EL is moving.
- During Y Factor dwell, the selected antenna position should continue updating.

## OLED Display Requirements

- OLED should be updated immediately after connect.
- OLED labels should use configured antenna names, such as East and West.
- OLED should show AZ and EL, not raw/cal distinction.
- OLED status should match app state:
  - stopped
  - tracking
  - slewing
  - scan
  - Y factor
  - park
  - fault/offline
- Non-selected antenna OLED should not be made to look like it is performing a
  scan or Y factor measurement.
- If the app stops tracking, OLED should show stopped.

## GUI Requirements

The WT6 GUI should be more aesthetically pleasing while remaining practical and
field-friendly.

Suggested design:

- Instrument-panel style, not decorative.
- Clear top toolbar grouped by task.
- Per-antenna cards for East and West.
- Larger calibrated AZ/EL values.
- Normal state colours:
  - green for connected/tracking/ready
  - amber for slewing/warming/calibration in progress
  - red for fault/offline/limit/timeout
  - neutral grey for stopped/disconnected
- Keep manual controls compact and intuitive.
- Display current Sun and Moon positions at all times.
- Keep source target line live during tracking and measurement operations.
- Avoid stale status text.
- Status line should show the most important current state.
- Event log should carry the deeper detail.

## Logging Requirements

WT6 needs both event logging and measurement logging.

### Event Log

- Always-on event log.
- Prefer a single rolling event log file or a clearly managed log strategy.
- Retention days should be configurable in the GUI.
- Event log must include:
  - app start/stop
  - connect/disconnect
  - config changes
  - limits changes
  - tracking start/stop/fault
  - slew start/stop/fault
  - park start/stop/fault
  - scan start/stop/fault
  - Y factor start/stop/fault
  - RTL start/stop/fault
  - calibration changes
  - safety stops
  - controller offline events
  - anomalous motion detection

### Measurement Logs

- Scan logs under `scan/`.
- Y factor logs under `yfactor/`.
- Power logs under an agreed directory, not cluttering the app root.
- CSV files should include enough metadata to be useful later.

## Simulation And Testing Requirements

WT6 should include a simulation backend before hardware tests.

Simulation should support:

- two antennas
- configurable motion rates
- encoder position changes
- dead-zone limits
- no-motion fault injection
- position jump injection
- serial fault injection
- East/West asymmetric behaviour
- long slew testing

Unit tests should cover:

- azimuth wrap/dead-zone path selection
- target rejection outside limits
- elevation limits
- hysteresis compensation
- timeout classification
- no-motion detection
- position jump detection
- stop-priority handling
- scan-stop recovery
- Y factor hot/cold target calculations

## Migration Plan

1. Create WT6 skeleton and this requirements document.
2. Build config and safety modules.
3. Add unit tests for safety and azimuth pathing.
4. Add serial protocol module based on WT4.
5. Add antenna session and simulator backend.
6. Build minimal GUI shell with connect/read position/stop all.
7. Add manual jog with safety.
8. Add tracking Sun/Moon/source.
9. Add event logging.
10. Add park.
11. Add calibration workflows.
12. Add RTL power meter.
13. Add scan calibration.
14. Add Y factor.
15. Polish GUI colours and layout.
16. Field-test against WT4 behaviours before relying on WT6 operationally.

## Non-Goals For Initial WT6

- Do not rewrite Arduino firmware initially.
- Do not depend on internet access for ephemeris.
- Do not assume physical limit switches exist.
- Do not remove WT4 until WT6 has proven field behaviour.

## Open Questions

- Should WT6 support both RTL-SDR and SDRplay in the first clean rebuild, or
  defer SDRplay until WT6 is stable?
- Should Y factor cold-sky presets be persisted in the INI?
- Should long-slew segmentation size be configurable globally or per antenna?
- Should hysteresis compensation eventually be direction-dependent calibration
  tables rather than a single low-to-high parameter?
- Should the event log be JSONL, CSV, or both?
- Should WT6 store measurement metadata in a small SQLite database later, while
  still exporting CSV?



