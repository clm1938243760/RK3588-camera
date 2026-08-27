from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_parallel_recognizer_uses_three_distinct_npu_cores() -> None:
    source = (ROOT / "scripts" / "ppocr_system_parallel.cc").read_text(encoding="utf-8")

    assert "RKNN_NPU_CORE_0" in source
    assert "RKNN_NPU_CORE_1" in source
    assert "RKNN_NPU_CORE_2" in source
    assert "rknn_dup_context" in source
    assert "std::vector<std::thread>" in source


def test_worker_releases_optional_parallel_contexts() -> None:
    source = (ROOT / "scripts" / "ppocr_rknn_worker.cc").read_text(encoding="utf-8")

    assert "__attribute__((weak))" in source
    assert "release_ppocr_rec_parallel_pool != NULL" in source


def test_builder_refuses_to_replace_active_worker_directly() -> None:
    source = (ROOT / "scripts" / "build_ppocr_parallel_worker.sh").read_text(
        encoding="utf-8"
    )

    assert "Refusing to overwrite the active production worker" in source
    assert "rknn_ppocr_system_parallel_worker" in source
