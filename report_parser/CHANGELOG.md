# Changelog

## Unreleased

- Reduced the production camera quality burst from three frames to two while
  retaining best-frame scoring, perspective correction, cropping, and OCR
  preprocessing.
- Reduced the RK3588 paper stability gate from 0.8 seconds to 0.5 seconds
  while retaining the three-observation and geometry checks.
- Added UIE patient-field type validation so incompatible high-confidence
  predictions cannot enter the patient JSON.
- Added OCR-backed identifier range tightening, label-neighbor field recovery,
  and conservative unique sex/age/birthday recovery.
- Added UIE web review reasons, resolution-method labels, and explicit
  confirmation of the current OCR-backed candidate.
- Added the RK3588 camera `text-only` runtime for full-page OCR.
- Replaced five-frame A/B identifier capture with one three-frame quality burst.
- Added one primary OCR pass and at most three second-frame regional refinements.
- Added adaptive overlapping near-square OCR tiles for long RK3588 camera
  documents, with white padding, core ownership, coordinate restoration, and
  overlap deduplication.
- Added schema-v2 full-text output with lines, blocks, rectangles, polygons,
  normalized coordinates, recognition sources, and alternatives.
- Added a single-worker latest-only OCR queue and stale `capture_id` rejection.
- Disabled identifier output, patient lookup, and HID auto-entry in text-only mode.
- Locked the deployed PP-OCR worker, detection/recognition models, dictionary,
  RKNN runtime, and DocAligner artifacts in the runtime manifest.
- Parallelized PP-OCR text-line recognition across three duplicated RKNN
  contexts pinned to NPU cores 0, 1, and 2 while retaining the batch-1 model.
- Reduced the measured primary OCR stage from 2.335 seconds to 1.340 seconds
  and total OCR time from 4.229 seconds to 2.688 seconds for the 39-block board
  sample, with exact text, box, and score parity against the serial worker.
- Rejected the experimental batch-4 recognition model after it dropped text
  blocks and changed recognition outputs despite its small speed improvement.
- Added a single rectified-document OCR region for the current report type:
  OCR receives 8%-68% of page height and retains block centers in 10%-66%.
- Preserved full-page OCR coordinates after region cropping and added a live
  perspective-mapped translucent recognition-region overlay to the 8893 page.
- Narrowed the current report-type OCR region to 13%-60%, disabled primary
  tiling for this fixed region, and disabled low-confidence regional refinement.
- Added explicit primary, secondary-full, refinement, and total OCR call counts
  while retaining only the empty-primary second-frame retry.
- Board-verified five consecutive single-pass captures at 0.666-0.728 seconds,
  with all configured core identifiers and exam-item evidence present.
