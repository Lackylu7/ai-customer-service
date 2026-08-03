import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path


class BackupVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupMetadata:
    path: Path
    size_bytes: int
    sha256: str


def create_sqlite_backup(source: Path | str, destination: Path | str) -> BackupMetadata:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source_path}")
    if source_path == destination_path:
        raise ValueError("SQLite backup destination must differ from the source.")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(
        f".{destination_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        _copy_sqlite_database(source_path, temporary_path)
        verify_sqlite_backup(temporary_path)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination_path)
        return _metadata(destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def verify_sqlite_backup(path: Path | str) -> BackupMetadata:
    backup_path = Path(path).resolve()
    if not backup_path.is_file():
        raise FileNotFoundError(f"SQLite backup does not exist: {backup_path}")
    try:
        with closing(sqlite3.connect(backup_path)) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupVerificationError("SQLite backup integrity check failed.") from exc
    if result is None or result[0] != "ok":
        raise BackupVerificationError("SQLite backup integrity check failed.")
    return _metadata(backup_path)


def restore_sqlite_backup(
    backup: Path | str,
    target: Path | str,
    *,
    confirmation: str,
) -> BackupMetadata:
    if not hmac.compare_digest(confirmation, "RESTORE"):
        raise PermissionError("SQLite restore requires confirmation.")
    backup_path = Path(backup).resolve()
    target_path = Path(target).resolve()
    verify_sqlite_backup(backup_path)
    if target_path.exists():
        raise FileExistsError(f"SQLite restore target already exists: {target_path}")
    return create_sqlite_backup(backup_path, target_path)


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    try:
        with closing(sqlite3.connect(source)) as source_connection, closing(
            sqlite3.connect(destination)
        ) as destination_connection:
            source_connection.execute("PRAGMA busy_timeout=5000")
            source_connection.backup(destination_connection)
    except sqlite3.Error as exc:
        raise BackupVerificationError("SQLite backup could not be created.") from exc


def _metadata(path: Path) -> BackupMetadata:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return BackupMetadata(
        path=path.resolve(),
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Back up or restore orchestration SQLite state.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument(
        "--source",
        type=Path,
        default=Path(
            os.getenv(
                "CUSTOMER_SERVICE_DB_PATH",
                ".runtime/customer-service/state.db",
            )
        ),
    )
    backup_parser.add_argument("--destination", type=Path, required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--backup", type=Path, required=True)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--backup", type=Path, required=True)
    restore_parser.add_argument("--target", type=Path, required=True)
    restore_parser.add_argument("--confirm", required=True)

    args = parser.parse_args()
    if args.command == "backup":
        metadata = create_sqlite_backup(args.source, args.destination)
    elif args.command == "verify":
        metadata = verify_sqlite_backup(args.backup)
    else:
        metadata = restore_sqlite_backup(
            args.backup,
            args.target,
            confirmation=args.confirm,
        )
    payload = asdict(metadata)
    payload["path"] = str(metadata.path)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    _main()
