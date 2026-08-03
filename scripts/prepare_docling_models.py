from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from pathlib import Path

try:
    from scripts.verify_docling_models import (
        MODEL_ASSETS,
        ModelAssetsError,
        verify_model_assets,
    )
except ModuleNotFoundError:
    from verify_docling_models import (  # type: ignore[no-redef]
        MODEL_ASSETS,
        ModelAssetsError,
        verify_model_assets,
    )


ModelDownloader = Callable[[Path], None]


def prepare_model_assets(
    root: Path,
    downloader: ModelDownloader,
    manifest: Mapping[str, Mapping[str, object]] = MODEL_ASSETS,
    *,
    force: bool = False,
) -> None:
    if not force:
        try:
            verify_model_assets(root, manifest)
            return
        except ModelAssetsError:
            pass

    root.mkdir(parents=True, exist_ok=True)
    downloader(root)
    verify_model_assets(root, manifest)


def download_required_models(root: Path) -> None:
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "300")

    from docling.datamodel.pipeline_options import LayoutOptions
    from docling.models.stages.layout.layout_model import LayoutModel
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel
    from docling.models.stages.table_structure.table_structure_model import (
        TableStructureModel,
    )

    layout_folder = LayoutOptions().model_spec.model_repo_folder
    LayoutModel.download_models(
        local_dir=root / layout_folder,
        force=True,
        progress=True,
    )
    TableStructureModel.download_models(
        local_dir=root / TableStructureModel._model_repo_folder,
        force=True,
        progress=True,
    )
    RapidOcrModel.download_models(
        backend="torch",
        lang="chinese",
        local_dir=root / RapidOcrModel._model_repo_folder,
        force=True,
        progress=True,
    )


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Download only the Docling models required by this service."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=project_root / ".runtime" / "docling-models",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    prepare_model_assets(
        args.root,
        download_required_models,
        force=args.force,
    )
    print(f"Docling model assets are ready in {args.root.resolve()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
