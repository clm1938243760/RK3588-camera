# Identifier System Design

## Evidence contract

OCR provides atomic text spans with original boxes, normalized boxes, reading
order, and confidence. The candidate graph creates only these relations:

```text
same_span > same_line_right > next_line_aligned > nearby
```

The model receives candidate IDs, label text, OCR value text, relation, and
source span IDs. Classification returns candidate ID and type. Verification
returns only `confirmed_candidate_ids`; omitted candidates are treated as not
confirmed. Free text, unknown IDs, unknown types, and duplicate IDs are
rejected. Explicit generic labels constrain their type before verification;
this is a field-semantic rule, not a hospital layout or coordinate template.

## Single-length selection

The active web profile contains one `selected_identifier` rule with one exact
character count. OCR-only rule mode extracts ASCII alphanumeric runs and keeps
only values matching that count. It does not infer medical field types and
never calls the language model.

One distinct match is accepted. Zero matches are rejected. Multiple distinct
matches are all exposed as alternatives and require review; the program never
selects the first value arbitrarily. Leading zeroes are preserved and OCR
characters are never corrected. Web changes are validated, written atomically
to `runtime/active_identifier_rules.json`, and loaded on the next start.

Image-quality metrics and OCR confidence remain in the response for diagnosis,
but they do not block single-length selection. The final status depends only on
the number of distinct ASCII alphanumeric values whose character count matches
the configured value. Legacy semantic exclusions such as phone-number patterns
are not applied in this mode.

Image decode applies EXIF orientation. PaddleOCR performs local line-angle
classification. Optional OpenCV perspective correction is fail-closed: only a
large convex four-corner document with sufficient rectangularity and confidence
is warped. The inverse homography restores OCR boxes to original-image
coordinates before evidence candidates are built.

## Result contract

`identifier` is the only business output. `primary_identifier` and evidence
arrays remain for API compatibility and audit display.

```text
accepted         exactly one configured-length value
review_required  more than one distinct configured-length value
rejected         no configured-length value
error            local service or runtime failure
```

No OCR character is corrected. QR and barcode content are not decoded. Any
printed ASCII alphanumeric value participates when its character count matches.

## Web and privacy

The service accepts one JPEG/PNG under 20 MB. Requests are serialized through
one inference lock with four waiting slots. The browser keeps its own preview;
the server releases image bytes after the response. Logs must not include OCR
text, values, filenames, request bodies, or model prompts.

## Deployment order

1. Run the desktop development model and annotate controlled samples.
2. Benchmark the 8 GB and 12 GB profiles with the same frozen evidence schema.
3. Freeze prompt, thresholds, runtime versions, and artifact hashes.
4. Convert the best qualifying 1.5B/1.7B candidate to RKLLM W8A8.
5. On a 4 GB RK3588, validate peak RSS, 100 sequential parses, and P95 latency.
6. Run at least 100 shadow-mode cases before connecting results to a workflow.

Release requires accepted identifier precision and primary accuracy of at least
99.5%, core identifier recall of at least 95%, and no false accept in the blind
release set.

The identifier benchmark dataset contains only deidentified OCR fixtures,
`image_size`, expected status, exact typed identifiers, and expected primary.
Image bytes and image paths are rejected by the loader. Use
`rk3588-report-evaluate-identifiers` for this protocol; the older
`rk3588-report-evaluate` command remains for the legacy nine-field parser.
