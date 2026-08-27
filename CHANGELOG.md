# Changelog

## 0.2.0 - 2026-08-27

- Consolidated the camera, report parser and gateway/report-center sources in
  one repository while preserving their three production deployment paths.
- Added `install_stack.sh` for state-preserving deployment of the active stack
  without reconfiguring or restarting the C0 USB gadget.
- Added unified architecture, deployment and data-boundary documentation.
- Added startup recovery for interrupted HID sessions, workflow runs, entry
  logs and stale HID-active markers.
- Included the administration portal, entry logs with source-image access,
  SPI workflow display and report archive/upload implementation.
- Switched the deployed monitor to explicit full-text `text-only` mode.
- Added two-frame 4K quality selection with one full-page OCR pass and
  bounded regional refinement.
- Added schema-v2 OCR export with raw polygons, normalized geometry, sources,
  alternatives, quality, and timings.
- Disabled and hid identifier verification, patient query, and HID auto-entry
  for the first full-text stage.
- Added dynamic burst-count display and current-capture result isolation.
- Split OCR capture from the 1080p WebRTC pipeline onto `/dev/video22`.
- Added independent 3840x2160, 5 FPS, JPEG quality 95 OCR snapshots.
- Added the `rk3588-camera-ocr-snapshots.service` boot service.
- Fixed the browser preview using a 1920x1080 sensor crop instead of the full
  3840x2160 field seen by OCR.
- Kept the monitor web service online across camera stream restarts and made
  OCR busy states explicit.

## v0.1 - 2026-08-19

- Added CSI camera WebRTC preview with 1080p30 H.264 and low-latency MediaMTX.
- Added DocAligner-compatible camera monitor integration through the trigger status file.
- Added full-page OCR display, OCR boxes, copy, and JSON export.
- Added A/B identifier verification display.
- Added independent patient JSON query and HID auto-entry switches.
- Added loopback-only gateway patient query integration.
- Added capture-ID guarded patient JSON output and `0600` result files.
- Added camera services, watchdog scripts, deployment documentation, and automated tests.
