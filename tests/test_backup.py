import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.backup import (
    BackupVerificationError,
    create_sqlite_backup,
    restore_sqlite_backup,
    verify_sqlite_backup,
)


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "state.db"
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
            connection.execute("INSERT INTO messages(body) VALUES (?)", ("售后政策",))
            connection.commit()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_creates_atomic_verified_backup_with_checksum(self) -> None:
        destination = self.root / "backups" / "state.db"

        metadata = create_sqlite_backup(self.source, destination)

        self.assertEqual(metadata.path, destination.resolve())
        self.assertEqual(len(metadata.sha256), 64)
        self.assertGreater(metadata.size_bytes, 0)
        self.assertEqual(verify_sqlite_backup(destination), metadata)
        with closing(sqlite3.connect(destination)) as connection:
            body = connection.execute("SELECT body FROM messages").fetchone()[0]
        self.assertEqual(body, "售后政策")

    def test_rejects_missing_source_same_destination_and_corrupt_backup(self) -> None:
        with self.assertRaises(FileNotFoundError):
            create_sqlite_backup(self.root / "missing.db", self.root / "backup.db")
        with self.assertRaises(ValueError):
            create_sqlite_backup(self.source, self.source)

        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"not sqlite")
        with self.assertRaises(BackupVerificationError):
            verify_sqlite_backup(corrupt)

    def test_restore_requires_confirmation_and_never_overwrites_by_default(self) -> None:
        backup = self.root / "backup.db"
        create_sqlite_backup(self.source, backup)
        target = self.root / "restored.db"

        with self.assertRaises(PermissionError):
            restore_sqlite_backup(backup, target, confirmation="wrong")

        restore_sqlite_backup(backup, target, confirmation="RESTORE")
        with self.assertRaises(FileExistsError):
            restore_sqlite_backup(backup, target, confirmation="RESTORE")

        with closing(sqlite3.connect(target)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
