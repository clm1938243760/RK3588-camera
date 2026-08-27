# RK3588 Camera OCR Gateway

Complete ATK-DLRK3588 edge stack for camera capture, paper detection, medical
form OCR, structured patient intake, USB HID entry, report collection/upload,
the 480x320 SPI status display, and the browser administration portal.

## End-to-End Flow

```text
IMX415 camera
  -> 1080p30 MPP H.264 -> MediaMTX WebRTC :8891
  -> 4K JPEG snapshots -> DocAligner paper detection
  -> stable 0.5 s -> two-frame quality selection
  -> RKNN PP-OCR -> schema-v2 text and configured patient fields
  -> report center -> HID entry, entry history and capture image
  -> printer/MSC report collection -> PDF archive -> upload queue

Browser monitor :8893       Administration portal :8443
SPI ILI9488 /dev/spidev3.0  Workflow state and board IP
```

The repository keeps the three runtime components together while preserving
their established board paths:

| Repository path | Board path | Responsibility |
| --- | --- | --- |
| repository root | `/opt/rk3588_kvm` | CSI capture, WebRTC and the 8893 OCR monitor |
| `report_parser/` | `/opt/rk3588_report_parser` | DocAligner, frame selection, OCR orchestration and forwarding |
| `gateway/` | `/opt/rk3588_gateway` | report center, HID workflow, SPI display, report archive and upload |

See [stack architecture](docs/STACK_ARCHITECTURE.md) for service ownership and
runtime contracts.

## Deploy

Copy the repository to the board and run:

```bash
sudo bash install_stack.sh
```

This updates source files and systemd units while preserving existing virtual
environments, `config.yaml`, TLS material, databases, captures and archives.
It enables the active stack for the next boot but does not restart services or
touch USB configfs gadget state.

After reviewing `/opt/rk3588_gateway/config.yaml`, apply the update:

```bash
sudo bash install_stack.sh --restart
```

On a new board, add `--bootstrap-python` to create missing parser and gateway
virtual environments. MediaMTX, the vendor RKNN runtime and board device-tree
support remain board prerequisites. Full commands and rollback guidance are in
[deployment](docs/DEPLOYMENT.md).

## Active Services

- `rk3588-camera-mediamtx.service`
- `rk3588-camera-stream.service`
- `rk3588-camera-ocr-snapshots.service`
- `rk3588-ppocr.service`
- `rk3588-report-camera-trigger.service`
- `rk3588-report-center.service`
- `rk3588-camera-report-center-forwarder.service`
- `rk3588-camera-ocr-overlay.service`
- `rk3588-fb-status.service`

The unified installer deliberately does not install, enable, disable or restart
USB gadget units. The deployed C0 gadget remains owned by the board's current
gadget-mode service, with function order keyboard, mouse, printer.

## Interfaces

```text
http://BOARD_IP:8891/camera   WebRTC camera preview
http://BOARD_IP:8893/          paper/OCR monitor and capture settings
https://BOARD_IP:8443/         patient intake, profiles, entry logs and reports
http://127.0.0.1:5002/ocr      loopback RKNN OCR API
```

Sensitive runtime files are intentionally excluded from Git. Configure
`gateway/config.yaml` and install a real
`/var/lib/rk3588-gateway/device/ReportInfo.xml` on the board. The checked-in
`gateway/ReportInfo.example.xml` is schema documentation only.

## Verification

```powershell
python -m py_compile camera_ocr_overlay.py tests/test_camera_ocr_overlay.py
python -m unittest discover -s tests -v

$env:PYTHONPATH = "report_parser/src"
python -m unittest discover -s report_parser/tests -v

$env:PYTHONPATH = "gateway/src"
python -m pytest gateway/tests -q
```

Repository release: `0.2.0`.
