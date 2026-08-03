import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_docling_models import prepare_model_assets
from scripts.verify_docling_models import ModelAssetsError, verify_model_assets


def _manifest_for(path: str, content: bytes) -> dict[str, dict[str, object]]:
    return {
        path: {
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    }


class DoclingModelAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_accepts_matching_file(self) -> None:
        content = b"verified-model"
        model_path = self.root / "models" / "model.bin"
        model_path.parent.mkdir(parents=True)
        model_path.write_bytes(content)

        verify_model_assets(self.root, _manifest_for("models/model.bin", content))

    def test_rejects_missing_file(self) -> None:
        with self.assertRaisesRegex(ModelAssetsError, "missing"):
            verify_model_assets(
                self.root,
                _manifest_for("models/model.bin", b"expected"),
            )

    def test_rejects_wrong_hash(self) -> None:
        model_path = self.root / "models" / "model.bin"
        model_path.parent.mkdir(parents=True)
        model_path.write_bytes(b"tampered")

        with self.assertRaisesRegex(ModelAssetsError, "sha256"):
            verify_model_assets(
                self.root,
                _manifest_for("models/model.bin", b"expected"),
            )

    def test_prepare_skips_download_when_assets_are_valid(self) -> None:
        content = b"ready"
        model_path = self.root / "models" / "model.bin"
        model_path.parent.mkdir(parents=True)
        model_path.write_bytes(content)
        calls: list[Path] = []

        prepare_model_assets(
            self.root,
            calls.append,
            _manifest_for("models/model.bin", content),
        )

        self.assertEqual(calls, [])

    def test_prepare_downloads_then_verifies_missing_assets(self) -> None:
        content = b"downloaded"

        def downloader(root: Path) -> None:
            model_path = root / "models" / "model.bin"
            model_path.parent.mkdir(parents=True)
            model_path.write_bytes(content)

        prepare_model_assets(
            self.root,
            downloader,
            _manifest_for("models/model.bin", content),
        )

        self.assertEqual((self.root / "models" / "model.bin").read_bytes(), content)

    def test_prepare_rejects_incomplete_download(self) -> None:
        with self.assertRaises(ModelAssetsError):
            prepare_model_assets(
                self.root,
                lambda _root: None,
                _manifest_for("models/model.bin", b"expected"),
            )
