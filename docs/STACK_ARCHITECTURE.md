# Stack Architecture

## Runtime Topology

```text
/dev/video23 -> camera_stream_mpp.sh -> MediaMTX -> WebRTC :8891

/dev/video22 -> camera_ocr_snapshots.sh -> 4K JPEG ring
  -> camera_paper_trigger.py
     -> DocAligner detection and stability tracking
     -> two-frame quality selection and perspective correction
     -> rk3588-ppocr.service :5002
     -> /run/rk3588-report-parser/verified-full-text.json
  -> report_center_forwarder
     -> report center internal camera API :8443
     -> configured field extraction and intake decision
     -> HID workflow and immutable entry log

printer gadget or MSC image
  -> report collector
  -> immutable PDF archive
  -> upload queue

camera/report state -> fb_status.py -> ILI9488 SPI display
```

## Component Boundaries

### Camera

The repository root deploys to `/opt/rk3588_kvm`. It owns camera devices,
the low-latency WebRTC stream, the independent 4K snapshot stream and the 8893
monitor. The monitor reads parser state files; it does not perform OCR itself.

### Report Parser

`report_parser/` deploys to `/opt/rk3588_report_parser`. It owns paper
detection, stability, perspective correction, frame selection, calls to the
local PP-OCR API and schema-v2 OCR evidence. The production camera profile uses
0.5 seconds of stability and exactly two candidate frames.

The forwarder is the sole camera-to-report-center bridge. It submits a capture
ID and immutable OCR evidence to the loopback HTTPS endpoint. Repeated capture
IDs are idempotent.

### Gateway

`gateway/` deploys to `/opt/rk3588_gateway`. The report center owns patient
sessions, structured fields, profile revisions, HID execution, entry logs,
captured source images, report archival and upload retries. SQLite and archived
files under `/var/lib/rk3588-report-center` are the source of truth.

The SPI display reads both the report-center display state and the live camera
status. Its production device is `/dev/spidev3.0`; the panel is driven in
userspace as ILI9488 RGB666.

## State Sequence

```text
wait_scan
  -> detecting: paper box is visible; keep the application stable
  -> collecting_frames / ocr_running
  -> structured fields available
     -> entering -> entry_completed: remove the application
     -> report_uploading -> report_upload_succeeded | report_upload_failed
  -> required fields missing or confidence rejected
     -> remove_and_retry
     -> wait_scan only after the paper box disappears
```

The browser and SPI display consume the same parser/report-center state. Fast
camera states come directly from the parser status file or the 8893 status API;
business states come from the report-center display-state file.

## USB and Kernel Boundary

The active RK3588 C0 configfs gadget uses this function order:

```text
HID keyboard -> HID absolute mouse -> USB printer
```

Required kernel options include
`CONFIG_USB_CONFIGFS_F_HID=y`,
`CONFIG_USB_CONFIGFS_F_PRINTER=y` and
`CONFIG_USB_CONFIGFS_MASS_STORAGE=y`.
`CONFIG_USB_CONFIGFS_UEVENT` remains disabled for the deployed gadget flow.

USB gadget setup is intentionally outside the unified installer's default
actions. Updating camera/OCR/business code must not tear down C0, reorder
`/dev/hidg0` and `/dev/hidg1`, or interrupt a connected host.

## Persistent Data

| Path | Owner |
| --- | --- |
| `/var/lib/rk3588-report-center` | database, archive, entry images, TLS and audit data |
| `/var/lib/rk3588-gateway` | device metadata, report upload and legacy gateway state |
| `/var/lib/rk3588-report-parser` | editable capture settings and parser rules |
| `/run/rk3588-report-parser` | current camera/OCR state and transient images |
| `/run/rk3588-gateway/display-state.json` | current business/display state |

No persistent or patient-derived data belongs in Git.

## Recovery

At report-center startup, interrupted `entering` sessions, running workflows
and running entry logs are marked failed, and a stale HID-active marker is
removed. This prevents one interrupted HID operation from blocking every later
session. Recovery does not replay HID actions automatically.
