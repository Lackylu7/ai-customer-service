from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Mapping
from pathlib import Path


class ModelAssetsError(RuntimeError):
    pass


MODEL_ASSETS: dict[str, dict[str, object]] = {
    "docling-project--docling-layout-heron/config.json": {
        "size": 3268,
        "sha256": "fdea30805ce2f5666b147fca941dcdd27ad468e27d6ed21902207d3da056a97d",
    },
    "docling-project--docling-layout-heron/model.safetensors": {
        "size": 171658996,
        "sha256": "00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c",
    },
    "docling-project--docling-layout-heron/preprocessor_config.json": {
        "size": 444,
        "sha256": "cd38cd59999e7a95d68e487fbe5132df3d4e5c32a0836add57e6126ba0c4eaf1",
    },
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors": {
        "size": 212758388,
        "sha256": "2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9",
    },
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tm_config.json": {
        "size": 7060,
        "sha256": "984e122ceb8ccf84d84c9d2882f6f2302a44b4f1e577babd6289892c36f3cffd",
    },
    "RapidOcr/paddle/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile/ppocr_keys_v1.txt": {
        "size": 26249,
        "sha256": "28b2362ad4ab2dc38769aa72feb535e3a9ddb3fd2a7585a05920e6393b1dc7f7",
    },
    "RapidOcr/resources/fonts/FZYTK.TTF": {
        "size": 3241748,
        "sha256": "4065a23df6823c8e2b69a0e76d02f02a6470b8774a5e91086609701ad95cc33f",
    },
    "RapidOcr/torch/PP-OCRv4/cls/ch_ptocr_mobile_v2.0_cls_mobile.pth": {
        "size": 588638,
        "sha256": "bfe13860824b3365c0c7f7ccfcddc8ff11645c60051739ff18bc9913f60c98e1",
    },
    "RapidOcr/torch/PP-OCRv4/det/ch_PP-OCRv4_det_mobile.pth": {
        "size": 14506268,
        "sha256": "89622c3f3e76b3ac7d10d9434c1f117a7471dba44723885cc04b49932a740d5b",
    },
    "RapidOcr/torch/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile.pth": {
        "size": 26919769,
        "sha256": "cb4265bb4300a2487e93e82ccfa1924bf9cd1194c1a202ab17a96b4911c27e0b",
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_assets(
    root: Path,
    manifest: Mapping[str, Mapping[str, object]] = MODEL_ASSETS,
) -> None:
    resolved_root = root.resolve()
    failures: list[str] = []

    for relative_path, expected in manifest.items():
        candidate = (resolved_root / relative_path).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            failures.append(f"unsafe path: {relative_path}")
            continue

        if not candidate.is_file():
            failures.append(f"missing: {relative_path}")
            continue

        expected_size = int(expected["size"])
        actual_size = candidate.stat().st_size
        if actual_size != expected_size:
            failures.append(
                f"size mismatch: {relative_path} (expected {expected_size}, got {actual_size})"
            )
            continue

        expected_sha256 = str(expected["sha256"]).lower()
        actual_sha256 = _sha256(candidate)
        if actual_sha256 != expected_sha256:
            failures.append(
                f"sha256 mismatch: {relative_path} (expected {expected_sha256}, got {actual_sha256})"
            )

    if failures:
        raise ModelAssetsError("Docling model verification failed: " + "; ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify packaged Docling model assets.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.getenv("DOCLING_ARTIFACTS_PATH", "/opt/docling-models")),
    )
    args = parser.parse_args()
    verify_model_assets(args.root)
    print(f"Verified {len(MODEL_ASSETS)} Docling model assets in {args.root.resolve()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
