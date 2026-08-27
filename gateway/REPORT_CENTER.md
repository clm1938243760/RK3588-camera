# RK3588 Patient Intake and Report Center

The report center is implemented inside `rk3588_gateway_rk3588` and uses one
SQLite database as the source of truth for patient intake, HID entry, immutable
PDF archival, upload retries, configuration revisions and audit history.

## Runtime Boundaries

- `rk3588-report-center.service`: business state, HTTPS portal and API.
- `rk3588-ppocr.service`: unchanged PP-OCR runtime.
- `rk3588-camera`: unchanged camera, DocAligner and schema v2 OCR producer.
- C0 HID/mouse/printer and C1 MSC configfs ownership: unchanged.
- `rk3588-gateway.service`: remains available during shadow verification.

The new service starts with `report_center.shadow_mode: true`. In shadow mode it
does not open the scanner, HID, Printer gadget or MSC image and it does not run
uploads. New observations are stored as `review_required`; they can never begin
HID automatically after a later restart.

## Data

```text
/var/lib/rk3588-report-center/
├── db/report-center.sqlite3
├── incoming/
├── archive/YYYY/MM/DD/
├── template-images/
├── tls/report-center.crt
├── tls/report-center.key
└── bootstrap-admin-password
```

SQLite enables WAL, foreign keys and a 30 second busy timeout. The database and
PDF files use mode `0600`; archive directories use `0700`. A self-signed HTTPS
certificate is generated on first start unless certificate paths are explicitly
empty or point to an existing hospital certificate.

## Initial Deployment

```bash
sudo bash install_debian.sh
sudo systemctl status rk3588-gateway.service rk3588-report-center.service
sudo journalctl -u rk3588-report-center.service -n 100 --no-pager
sudo cat /var/lib/rk3588-report-center/bootstrap-admin-password
curl -k https://127.0.0.1:8443/health
```

Open `https://BOARD_IP:8443/`, log in as `admin`, and change the initial password.
The installer enables the report center in shadow mode beside the old gateway.

Before changing to active mode, verify new scanner, camera and report
observations in the portal, cancel any test observations, and make a database
backup. Then perform the controlled handover:

```bash
sudo systemctl stop rk3588-gateway.service
sudo sed -i '/^report_center:/,/^[^ ]/ s/^  shadow_mode: true/  shadow_mode: false/' \
  /opt/rk3588_gateway/config.yaml
sudo systemctl restart rk3588-report-center.service
sudo journalctl -u rk3588-report-center.service -f
```

Do not run both services with `shadow_mode: false`; they would compete for the
scanner, HID, Printer and MSC devices.

## Legacy Import

Dry run is read-only:

```bash
sudo /opt/rk3588_gateway/.venv/bin/python \
  /opt/rk3588_gateway/scripts/migrate_report_center.py \
  --config /opt/rk3588_gateway/config.yaml
```

Execute after reviewing the count:

```bash
sudo /opt/rk3588_gateway/.venv/bin/python \
  /opt/rk3588_gateway/scripts/migrate_report_center.py \
  --config /opt/rk3588_gateway/config.yaml --execute
```

Historical PDFs are copied into the immutable archive as `legacy_orphan` and
are not guessed against patients or automatically re-uploaded. Existing JSONL
and `events.db` remain read-only.

## API Boundaries

- Portal API: `/api/v1/*`, cookie session + CSRF.
- Camera callback: `POST /internal/v1/camera-captures`, loopback only.
- Camera observations: `GET /api/v1/camera-captures` and
  `GET /api/v1/camera-captures/{capture_id}`, authenticated portal users only.
- Camera patient preview:
  `POST /api/v1/camera-captures/{capture_id}/resolve-patient`.
- Arm one full-page configuration capture:
  `POST /api/v1/camera/configuration-capture`.
- Read the exact perspective-corrected configuration image:
  `GET /api/v1/camera-captures/{capture_id}/image`.
- Generated patient JSON: `GET /api/v1/camera-captures/{capture_id}/patient`
  or `GET /api/v1/camera-patient` for the latest result.
- Patient entry history: `GET /api/v1/entry-logs` and
  `GET /api/v1/entry-logs/{id}`. The authenticated portal exposes the same
  records on the **录入日志** page.
- Entry capture image: `GET /api/v1/entry-logs/{id}/image`; append
  `?download=1` to download the original JPEG.
- Report callback: `POST /internal/v1/reports`, loopback only.
- Archive API: `/archive/v1/*`, scoped Bearer tokens.
- Compatibility: `/scan`, `/patient/query`, `/health`, `/status`.

Archive token scopes are `reports:read` and `reports:download`. Archive JSON
never exposes local filesystem paths. PDF responses include `Content-Length`,
`Content-Disposition` and the PDF SHA-256 as `ETag`.

The camera callback always stores immutable OCR schema v2 evidence first. It
does not create a patient session unless the published profile explicitly sets
`camera_intake_enabled: true` and uses `camera_query` or `camera_direct`.
Repeated identical capture IDs are idempotent; a changed payload under an
existing capture ID is rejected to prevent cross-patient evidence replacement.

Camera patient processing is independent from the patient connector. The
portal's **OCR患者字段** page uses the exact perspective-corrected OCR image as a
standard 0..1000 coordinate canvas. An administrator arms one configuration
capture, removes and presents the sample form, then assigns OCR boxes or drawn
regions to patient fields. Unassigned fields are disabled and cannot be filled
by legacy label matching. Saving a draft retains the selected reference image;
publishing writes `/run/rk3588-report-parser/active-camera-template.json` for
the camera process.

After publishing, normal captures OCR only the enabled field regions. A region
whose result is empty or below its configured confidence can expand once and
retry. Arming another configuration capture bypasses the active regions exactly
once and marks its result `configuration_full_page`; it does not permanently
change the published runtime mode. Any stored camera OCR capture can be
resolved and persisted immediately from the page. Set
`camera_patient_enabled: true` to process later captures automatically.
Generated JSON uses the same
`code/data/msg/success` envelope and the same 14 patient fields as the patient
query response. It does not create a patient session or execute HID.

When HID entry starts, the report center writes an `entry_logs` row containing
the patient snapshot, all non-empty fields, action count and final status. The
corresponding perspective-corrected camera JPEG is copied from the runtime
camera directory into `/var/lib/rk3588-report-center/entry-captures` before the
runtime copy can be cleaned. A missing image is recorded as an image error but
does not block HID entry.

## Rollback

```bash
sudo systemctl stop rk3588-report-center.service
sudo systemctl enable --now rk3588-gateway.service
```

The report-center database and archive are preserved for diagnosis. Rollback
does not modify configfs, USB drivers, PP-OCR models, camera settings or KVM
encoding.
