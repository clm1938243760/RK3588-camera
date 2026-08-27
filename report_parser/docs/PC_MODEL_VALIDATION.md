# Desktop Qwen Validation Record

## Scope

This record is for the desktop semantic validation stage only. It is not an
RK3588 deployment approval and it does not contain any clinical report image or
patient OCR fixture.

## Verified desktop environment

- OS: Windows desktop workstation
- Python: 3.14
- PyTorch: 2.11.0+cu128
- GPU visibility: two NVIDIA GeForce RTX 3060 devices, 12 GB each
- Test device: cuda:0
- Desktop service binding: 127.0.0.1:8010 only

## Local model provenance

- Model family: Qwen2.5-1.5B-Instruct
- Hugging Face revision: 989aa7980e4cf806f80c7fef2b1adb7bc71aa306
- Download mirror: ModelScope Qwen/Qwen2.5-1.5B-Instruct
- model.safetensors SHA-256:
  dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee

The model directory is ignored by Git. The server loads it with
local_files_only=True, so report OCR data never leaves the workstation during
inference.

## Synthetic protocol result

The checked-in synthetic fixture was used only to validate plumbing:

- Free-form JSON model-only baseline: rejected. Qwen2.5-1.5B returned value
  text where the span-ID-only contract required numeric IDs; the latest run
  took about 4.32 seconds for the synthetic fixture.
- Hybrid run: accepted, with all final values reconstructed from OCR span IDs.
  The generic label/value geometry layer selected eight unambiguous fields.
- Observed end-to-end hybrid latency: about 4.1 seconds for the synthetic OCR
  fixture, including local Qwen inference.
- A later desktop-only constrained-choice run forced Qwen to select one of the
  existing OCR span IDs for each field. Generic labels remained visible as OCR
  context but could not be selected as values. After selecting required report
  fields before the optional HIS/check identifier, the one synthetic fixture
  was accepted with all expected fields exact in about 2.60 seconds.

This is not an accuracy claim. The free-form JSON baseline still rejects, and
the constrained-choice result is only one synthetic fixture. Neither mode is
approved for RKLLM conversion or board deployment yet.

## Next gate

Before RKLLM conversion or board deployment:

1. Prepare at least 50 manually deidentified OCR fixtures from varied report
   layouts, with canonical reviewed field labels.
2. Run the free-form JSON baseline, constrained-choice semantic benchmark, and
   hybrid benchmark as separate measurements.
3. Require no false accepts and the deployment thresholds documented in README.
4. Inspect all rejected samples and all accepted mismatches before freezing the
   prompt, candidate policy, and validation rules.
5. Only then convert the exact recorded Qwen revision and validate the matching
   RKLLM runtime on RK3588.
