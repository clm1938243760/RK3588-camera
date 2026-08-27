# Deployment

## Prerequisites

- ATK-DLRK3588 Debian image with the board camera and SPI device tree enabled.
- `/dev/video22`, `/dev/video23` and `/dev/spidev3.0`.
- C0 gadget already configured by the board's gadget-mode service.
- Vendor RKNN runtime and PP-OCR worker under
  `/userdata/aidemo/rknn_PPOCR-System_demo_native`.
- MediaMTX at `/usr/local/bin/mediamtx`.
- Python 3.9, `venv`, `rsync` and systemd.

Do not place a real `ReportInfo.xml`, hospital credentials, TLS private keys,
patient captures, databases or logs in this repository.

## Copy From Windows

Run the PowerShell command on one line:

```powershell
scp -r "D:\path\to\rk3588_camera" root@BOARD_IP:/tmp/
```

PowerShell uses a backtick for line continuation, not a trailing backslash.
For repeated deployment, archive the source first so virtual environments and
other ignored local directories are not traversed:

```powershell
tar -czf rk3588-camera-stack.tar.gz --exclude=.git --exclude=.venv --exclude=node_modules -C "D:\path\to" rk3588_camera
scp .\rk3588-camera-stack.tar.gz root@BOARD_IP:/tmp/
```

## Install

On the board:

```bash
cd /tmp/rk3588_camera
sudo bash install_stack.sh
```

The default run:

- copies all three components to their established `/opt` paths;
- preserves existing virtual environments and `gateway/config.yaml`;
- preserves everything under `/var/lib`;
- installs and enables only the active stack service units;
- runs `systemctl daemon-reload`;
- does not restart any service;
- does not modify the USB gadget.

For a new installation with no Python environments:

```bash
sudo bash install_stack.sh --bootstrap-python
```

Review and edit:

```text
/opt/rk3588_gateway/config.yaml
/var/lib/rk3588-gateway/device/ReportInfo.xml
/var/lib/rk3588-report-parser/camera-capture.env
```

The example ReportInfo XML documents element names only. Replace every value
with the deployment values required by the report server.

## Activate

```bash
sudo bash install_stack.sh --restart
```

This restarts only camera, parser, OCR, report-center and SPI display services.
It does not restart gadget-mode or any USB gadget unit.

Check:

```bash
systemctl status \
  rk3588-camera-mediamtx.service \
  rk3588-camera-stream.service \
  rk3588-camera-ocr-snapshots.service \
  rk3588-ppocr.service \
  rk3588-report-camera-trigger.service \
  rk3588-report-center.service \
  rk3588-camera-report-center-forwarder.service \
  rk3588-camera-ocr-overlay.service \
  rk3588-fb-status.service \
  --no-pager -l

curl -sS http://127.0.0.1:8893/api/status
curl -ks https://127.0.0.1:8443/health
cat /run/rk3588-gateway/display-state.json
```

## First-Run Notes

The generated `gateway/config.yaml` starts report center in shadow mode.
Switch to active mode only after profile, scanner, HID devices, report endpoint
and camera field mapping have been reviewed. Never run the legacy gateway and
the report center simultaneously with both owning scanner/HID/report inputs.

The installer does not download MediaMTX automatically. If it is absent and
network access is available:

```bash
sudo /opt/rk3588_kvm/install_mediamtx.sh
```

## Rollback

Program files are additive updates, while databases and archives are untouched.
Before a production update, back up the three `/opt` directories and
`/var/lib/rk3588-report-center`. To stop the new stack without touching USB:

```bash
sudo systemctl stop \
  rk3588-fb-status.service \
  rk3588-camera-ocr-overlay.service \
  rk3588-camera-report-center-forwarder.service \
  rk3588-report-camera-trigger.service \
  rk3588-report-center.service
```

Restore the backed-up application directories, run
`systemctl daemon-reload`, then restart the prior service set.
