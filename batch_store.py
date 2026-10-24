"""Durable, local staging for user-selected recipe source files."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import threading
import time
import uuid

import fitz
from PIL import Image, UnidentifiedImageError


_IMAGE_TYPES = {
    ".jpg": ("jpeg", "image/jpeg"),
    ".jpeg": ("jpeg", "image/jpeg"),
    ".png": ("png", "image/png"),
    ".webp": ("webp", "image/webp"),
}
_PARENT_COPY_CHUNK_BYTES = 1024 * 1024
_PARENT_DISK_CHECK_BYTES = 16 * 1024 * 1024
_DEFAULT_PARENT_FILE_BYTES = 256 * 1024 * 1024
_DEFAULT_PARENT_PDF_PAGES = 2_000
_DEFAULT_PARENT_DISK_RESERVE_BYTES = 64 * 1024 * 1024
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


class _LockBusy(RuntimeError):
    pass


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve()).casefold()
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _file_lock(path: Path, *, blocking: bool = True):
    """Cross-thread and cross-process byte-range lock for small manifests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    local = _thread_lock(path)
    if not local.acquire(blocking=blocking):
        raise _LockBusy(str(path))
    handle = None
    locked = False
    try:
        handle = path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        while not locked:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                if not blocking:
                    raise _LockBusy(str(path))
                time.sleep(0.05)
        yield
    finally:
        if handle is not None:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        local.release()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class BatchStore:
    """Content-addressed source staging beneath an isolated UI batch directory."""

    def __init__(self, output_dir: Path | str, *, max_file_bytes: int = 50 * 1024 * 1024,
                 max_pdf_pages: int = 100, max_image_pixels: int = 100_000_000,
                 max_parent_file_bytes: int = _DEFAULT_PARENT_FILE_BYTES,
                 max_parent_pdf_pages: int = _DEFAULT_PARENT_PDF_PAGES,
                 parent_disk_reserve_bytes: int = _DEFAULT_PARENT_DISK_RESERVE_BYTES):
        if (max_file_bytes <= 0 or max_pdf_pages <= 0 or max_image_pixels <= 0
                or max_parent_file_bytes <= 0 or max_parent_pdf_pages <= 0
                or parent_disk_reserve_bytes < 0):
            raise ValueError("file, page, and image limits must be positive; parent disk reserve cannot be negative")
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.root = self.output_dir / "ui_batches"
        self.manifest_path = self.root / "batches.json"
        self.lock_path = self.root / "batches.lock"
        self.max_file_bytes = int(max_file_bytes)
        self.max_pdf_pages = int(max_pdf_pages)
        self.max_image_pixels = int(max_image_pixels)
        self.max_parent_file_bytes = int(max_parent_file_bytes)
        self.max_parent_pdf_pages = int(max_parent_pdf_pages)
        self.parent_disk_reserve_bytes = int(parent_disk_reserve_bytes)

    def create(self, name: str, tag: str = "") -> dict:
        clean_name = str(name or "").strip()
        clean_tag = str(tag or "").strip()
        if not clean_name:
            raise ValueError("Batch name is required")
        if len(clean_name) > 120:
            raise ValueError("Batch name must be 120 characters or fewer")
        if len(clean_tag) > 120:
            raise ValueError("Batch tag must be 120 characters or fewer")
        batch_id = str(uuid.uuid4())
        batch = {
            "batch_id": batch_id,
            "name": clean_name,
            "tag": clean_tag,
            "created_at": _now(),
            "updated_at": _now(),
            "status": "staging",
            "sources": [],
        }
        with _file_lock(self.lock_path):
            manifest = self._read_manifest()
            manifest["batches"][batch_id] = batch
            self._write_manifest(manifest)
        return batch

    def stage(self, batch_id: str, filename: str, data: bytes) -> dict:
        """Validate and durably stage one file; rejected inputs do not poison accepted work."""
        basename = _safe_basename(filename)
        if not basename:
            return _rejected(str(filename or ""), "A filename is required.")
        if not isinstance(data, bytes):
            return _rejected(basename, "File content must be bytes.")
        if not data:
            return _rejected(basename, "The selected file is empty.")
        if len(data) > self.max_file_bytes:
            return _rejected(basename, f"File exceeds the {self.max_file_bytes}-byte size limit.")

        ext = Path(basename).suffix.lower()
        inspected = self._inspect(data, ext)
        if isinstance(inspected, str):
            return _rejected(basename, inspected)
        file_format, page_count, mime_type = inspected
        digest = sha256(data).hexdigest()

        with _file_lock(self.lock_path):
            manifest = self._read_manifest()
            batch = manifest["batches"].get(str(batch_id))
            if batch is None:
                return _rejected(basename, "Batch was not found.")
            for other_batch in manifest["batches"].values():
                for existing in other_batch["sources"]:
                    if existing["digest"] == digest:
                        existing_path = Path(str(existing.get("staged_path", ""))).expanduser()
                        try:
                            existing_path.resolve().relative_to(self.root.resolve())
                            path_is_owned = True
                        except (OSError, ValueError):
                            path_is_owned = False
                        if path_is_owned:
                            try:
                                already_present = existing_path.is_file() and _digest_file(existing_path) == digest
                            except OSError:
                                already_present = False
                            if not already_present:
                                existing_path.parent.mkdir(parents=True, exist_ok=True)
                                temporary = existing_path.with_name(
                                    f".{existing_path.name}.{uuid.uuid4().hex}.restore"
                                )
                                try:
                                    with temporary.open("xb") as handle:
                                        handle.write(data)
                                        handle.flush()
                                        os.fsync(handle.fileno())
                                    os.replace(temporary, existing_path)
                                except Exception as exc:
                                    try:
                                        temporary.unlink()
                                    except FileNotFoundError:
                                        pass
                                    return _rejected(
                                        basename,
                                        f"The original scan could not be restored: {type(exc).__name__}.",
                                    )
                                return {
                                    "status": "relinked",
                                    "accepted": True,
                                    "duplicate": True,
                                    "linked_to_batch": False,
                                    "batch_id": str(batch_id),
                                    "filename": basename,
                                    "digest": digest,
                                    "existing_source_id": existing["source_id"],
                                    "existing_batch_id": other_batch["batch_id"],
                                    "existing_path": str(existing_path),
                                    "message": "The exact original scan was restored for its existing recipe work.",
                                }
                        return {
                            "status": "duplicate",
                            "accepted": True,
                            "duplicate": True,
                            "linked_to_batch": False,
                            "batch_id": str(batch_id),
                            "filename": basename,
                            "digest": digest,
                            "existing_source_id": existing["source_id"],
                            "existing_batch_id": other_batch["batch_id"],
                            "existing_path": existing["staged_path"],
                            "message": "This exact file is already in the library; open its existing work or add an explicit batch reference.",
                        }

            source_id = str(uuid.uuid4())
            source_dir = self.root / str(batch_id) / "sources"
            source_dir.mkdir(parents=True, exist_ok=True)
            staged_path = source_dir / f"{digest}{ext}"
            if staged_path.exists():
                if _digest_file(staged_path) != digest:
                    staged_path = source_dir / f"{digest}-{source_id}{ext}"
            if not staged_path.exists():
                temporary = source_dir / f".{source_id}.tmp"
                try:
                    with temporary.open("xb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, staged_path)
                except Exception as exc:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                    return _rejected(basename, f"Could not save this file: {type(exc).__name__}.")

            source = {
                "source_id": source_id,
                "batch_id": str(batch_id),
                "digest": digest,
                "original_filename": basename,
                "staged_path": str(staged_path),
                "format": file_format,
                "mime_type": mime_type,
                "size_bytes": len(data),
                "page_count": page_count,
                "status": "staged",
                "created_at": _now(),
                "recipe_ids": [],
            }
            batch["sources"].append(source)
            batch["status"] = "staged"
            batch["updated_at"] = _now()
            try:
                self._write_manifest(manifest)
            except Exception as exc:
                # Keep the atomic content-addressed file for a safe retry after a manifest error.
                return _rejected(basename, f"File was saved but batch metadata could not be committed: {type(exc).__name__}.")
            return dict(source)

    def stage_parent_pdf(self, batch_id: str, source_path: Path | str) -> dict:
        """Stage a whole cookbook PDF separately from recipe-sized job sources."""
        try:
            source = Path(source_path).expanduser().resolve(strict=True)
            filename = _safe_basename(source.name)
            if not filename:
                return _rejected(str(source_path or ""), "A filename is required.")
            if source.suffix.casefold() != ".pdf":
                return _rejected(filename, "Unsupported file type. Choose a PDF.")
            source_stat = source.stat()
            if not stat.S_ISREG(source_stat.st_mode):
                return _rejected(filename, "The selected path is not a regular file.")
            if source_stat.st_size <= 0:
                return _rejected(filename, "The selected PDF is empty.")
            if source_stat.st_size > self.max_parent_file_bytes:
                return _rejected(
                    filename,
                    f"PDF exceeds the {self.max_parent_file_bytes}-byte parent-document limit.",
                )
        except (OSError, RuntimeError, ValueError):
            return _rejected(Path(str(source_path or "")).name, "The selected PDF path is unavailable.")

        batch_key = str(batch_id)
        with _file_lock(self.lock_path):
            manifest = self._read_manifest()
            if batch_key not in manifest["batches"]:
                return _rejected(filename, "Batch was not found.")

        staging_dir = self.root / ".parent_staging"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            staging_dir.mkdir(parents=True, exist_ok=True)
            free_bytes = shutil.disk_usage(staging_dir).free
        except OSError:
            return _rejected(filename, "Could not check free disk space for the parent PDF.")
        required_bytes = source_stat.st_size + self.parent_disk_reserve_bytes
        if free_bytes < required_bytes:
            return _rejected(filename, "Insufficient free disk space to stage this PDF safely.")

        temporary = staging_dir / f".{uuid.uuid4().hex}.parent.tmp"
        try:
            # On Windows, opening a file can refresh the filesystem's ctime value.
            # Device/inode, size, and mtime are stable for an unchanged file and
            # still detect path replacement or content edits around the copy.
            source_identity = (source_stat.st_dev, source_stat.st_ino, source_stat.st_size,
                               source_stat.st_mtime_ns)
            digest, size_bytes = self._copy_parent_pdf(
                source, temporary, source_stat.st_size, source_identity,
            )
            inspected = self._inspect_parent_pdf(temporary)
            if isinstance(inspected, str):
                return _rejected(filename, inspected)
            page_count = inspected

            with _file_lock(self.lock_path):
                manifest = self._read_manifest()
                batch = manifest["batches"].get(batch_key)
                if batch is None:
                    return _rejected(filename, "Batch was not found.")
                duplicate = self._existing_parent_pdf_result(
                    manifest, digest, size_bytes, filename, temporary,
                )
                if duplicate is not None:
                    return duplicate

                parent_dir = self.root / batch_key / "parent_documents"
                parent_dir.mkdir(parents=True, exist_ok=True)
                staged_path = parent_dir / f"{digest}.pdf"
                if staged_path.is_symlink():
                    return _rejected(filename, "A conflicting file already occupies this document identity.")
                if staged_path.exists():
                    if (not staged_path.is_file() or staged_path.stat().st_size != size_bytes
                            or _digest_file(staged_path) != digest):
                        return _rejected(filename, "A conflicting file already occupies this document identity.")
                    temporary.unlink()
                else:
                    os.replace(temporary, staged_path)

                record = {
                    "record_version": 1,
                    "kind": "parent_pdf",
                    "document_id": digest,
                    "batch_id": batch_key,
                    "sha256": digest,
                    "original_filename": filename,
                    "staged_path": str(staged_path),
                    "size_bytes": size_bytes,
                    "page_count": page_count,
                    "created_at": _now(),
                    "status": "staged",
                }
                batch.setdefault("parent_documents", []).append(record)
                batch["updated_at"] = _now()
                try:
                    self._write_manifest(manifest)
                except Exception as exc:
                    # Keep the content-addressed PDF so an identical retry can commit metadata.
                    return _rejected(
                        filename,
                        f"PDF was saved but batch metadata could not be committed: {type(exc).__name__}.",
                    )
                return dict(record)
        except ValueError as exc:
            return _rejected(filename, str(exc))
        except Exception as exc:
            return _rejected(filename, f"Could not stage this PDF: {type(exc).__name__}.")
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _existing_parent_pdf_result(self, manifest: dict, digest: str, size_bytes: int,
                                    filename: str, temporary: Path) -> dict | None:
        """Return a duplicate/relink result, restoring only its owned content path."""
        for other_batch in manifest["batches"].values():
            for existing in other_batch.get("parent_documents", []):
                if existing.get("sha256") != digest:
                    continue
                existing_path = Path(str(existing.get("staged_path", ""))).expanduser()
                if existing_path.is_symlink():
                    return _rejected(filename, "The existing parent document path is unsafe and cannot be restored.")
                try:
                    batch_key = str(other_batch.get("batch_id", ""))
                    expected_parent = self.root / batch_key / "parent_documents"
                    resolved_parent = existing_path.parent.resolve()
                    resolved_parent.relative_to(self.root.resolve())
                    path_is_owned = (
                        existing_path.name == f"{digest}.pdf"
                        and resolved_parent == expected_parent.resolve()
                    )
                except (OSError, ValueError):
                    path_is_owned = False
                try:
                    existing_is_valid = (
                        path_is_owned
                        and existing_path.is_file()
                        and existing_path.stat().st_size == size_bytes
                        and _digest_file(existing_path) == digest
                    )
                except OSError:
                    existing_is_valid = False
                if existing_is_valid:
                    return {
                        "status": "duplicate", "accepted": True, "duplicate": True,
                        "document_id": existing.get("document_id", digest), "sha256": digest,
                        "filename": filename, "existing_batch_id": other_batch.get("batch_id"),
                        "existing_path": str(existing_path),
                    }
                if not path_is_owned or (existing_path.exists() and not existing_path.is_file()):
                    return _rejected(filename, "The existing parent document path is unsafe and cannot be restored.")
                try:
                    existing_path.parent.mkdir(parents=True, exist_ok=True)
                    if existing_path.is_symlink():
                        return _rejected(filename, "The existing parent document path is unsafe and cannot be restored.")
                    resolved_parent = existing_path.parent.resolve()
                    resolved_parent.relative_to(self.root.resolve())
                    if resolved_parent != expected_parent.resolve():
                        return _rejected(filename, "The existing parent document path is unsafe and cannot be restored.")
                    os.replace(temporary, existing_path)
                except (OSError, ValueError):
                    return _rejected(filename, "The existing parent PDF could not be restored safely.")
                return {
                    "status": "relinked", "accepted": True, "duplicate": True,
                    "document_id": existing.get("document_id", digest), "sha256": digest,
                    "filename": filename, "existing_batch_id": other_batch.get("batch_id"),
                    "existing_path": str(existing_path),
                }
        return None

    def _copy_parent_pdf(self, source: Path, temporary: Path, expected_size: int,
                         expected_identity: tuple[int, int, int, int]) -> tuple[str, int]:
        """Copy a stable source in fixed chunks while hashing and checking remaining disk."""
        digest = sha256()
        copied = 0
        next_disk_check = _PARENT_DISK_CHECK_BYTES
        with source.open("rb") as source_handle:
            before = os.fstat(source_handle.fileno())
            identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            if not stat.S_ISREG(before.st_mode) or identity_before != expected_identity:
                raise ValueError("The selected PDF changed before copying began.")
            with temporary.open("xb") as target_handle:
                while True:
                    chunk = source_handle.read(_PARENT_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > self.max_parent_file_bytes:
                        raise ValueError("PDF grew beyond the configured parent-document size limit.")
                    target_handle.write(chunk)
                    digest.update(chunk)
                    if copied >= next_disk_check:
                        remaining = max(0, expected_size - copied)
                        if shutil.disk_usage(temporary.parent).free < remaining + self.parent_disk_reserve_bytes:
                            raise ValueError("Insufficient free disk space while staging this PDF.")
                        next_disk_check = copied + _PARENT_DISK_CHECK_BYTES
                after = os.fstat(source_handle.fileno())
                identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                if copied != expected_size or identity_before != identity_after:
                    raise ValueError("The selected PDF changed while it was being copied.")
                target_handle.flush()
                os.fsync(target_handle.fileno())
        if copied < 5:
            raise ValueError("The selected file is not a PDF.")
        with temporary.open("rb") as staged_handle:
            if staged_handle.read(5) != b"%PDF-":
                raise ValueError("File extension does not match a PDF signature.")
        return digest.hexdigest(), copied

    def _inspect_parent_pdf(self, path: Path) -> int | str:
        try:
            document = fitz.open(path)
            try:
                if document.needs_pass or document.is_encrypted:
                    return "Password-protected or encrypted PDFs cannot be staged."
                page_count = len(document)
                if page_count < 1:
                    return "PDF contains no pages."
                if page_count > self.max_parent_pdf_pages:
                    return f"PDF exceeds the {self.max_parent_pdf_pages}-page parent-document limit."
                return page_count
            finally:
                document.close()
        except Exception:
            return "PDF file is corrupt or cannot be opened."

    def list_batches(self) -> list[dict]:
        with _file_lock(self.lock_path):
            batches = list(self._read_manifest()["batches"].values())
        return sorted(batches, key=lambda item: (item.get("created_at", ""), item["batch_id"]), reverse=True)

    def get(self, batch_id: str) -> dict | None:
        with _file_lock(self.lock_path):
            batch = self._read_manifest()["batches"].get(str(batch_id))
            return json.loads(json.dumps(batch)) if batch is not None else None

    def _inspect(self, data: bytes, extension: str) -> tuple[str, int, str] | str:
        expected = _IMAGE_TYPES.get(extension)
        if extension == ".pdf":
            if not data.startswith(b"%PDF-"):
                return "File extension does not match a PDF signature."
            try:
                document = fitz.open(stream=data, filetype="pdf")
                try:
                    if document.needs_pass:
                        return "Password-protected PDFs cannot be staged."
                    pages = len(document)
                    if pages < 1:
                        return "PDF contains no pages."
                    if pages > self.max_pdf_pages:
                        return f"PDF exceeds the {self.max_pdf_pages}-page limit."
                    return "pdf", pages, "application/pdf"
                finally:
                    document.close()
            except Exception:
                return "PDF file is corrupt or cannot be opened."
        if expected is None:
            return "Unsupported file type. Choose PDF, JPG, JPEG, PNG, or WEBP."
        kind, mime_type = expected
        if not _matches_image_signature(data, kind):
            return f"File extension does not match a valid {kind.upper()} image."
        try:
            with Image.open(BytesIO(data)) as image:
                actual_format = str(image.format or "").lower()
                if actual_format != kind:
                    return f"File extension does not match the decoded {actual_format.upper()} image."
                if image.width * image.height > self.max_image_pixels:
                    return f"Image exceeds the {self.max_image_pixels}-pixel limit."
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            return f"Image is corrupt or cannot be opened as {kind.upper()}."
        except Exception:
            return f"Image is corrupt or cannot be opened as {kind.upper()}."
        return kind, 1, mime_type

    def _read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"schema_version": 1, "batches": {}}
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Batch metadata is unavailable; accepted source files were preserved.") from exc
        if not isinstance(value, dict) or not isinstance(value.get("batches"), dict):
            raise RuntimeError("Batch metadata has an unsupported shape.")
        return value

    def _write_manifest(self, value: dict) -> None:
        _atomic_json(self.manifest_path, value)


def _safe_basename(filename: str) -> str:
    value = str(filename or "").replace("\x00", "").strip()
    basename = PureWindowsPath(value).name
    basename = Path(basename).name
    basename = re.sub(r"[\r\n\t]", " ", basename).strip(" .")
    if basename in {"", ".", ".."}:
        return ""
    return basename[:255]


def _matches_image_signature(data: bytes, kind: str) -> bool:
    if kind == "jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if kind == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    return kind == "webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP"


def _rejected(filename: str, reason: str) -> dict:
    return {"status": "rejected", "accepted": False, "filename": filename, "reason": reason}


def _digest_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
