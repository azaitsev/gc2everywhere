"""Validate Garmin's ZIP envelope without trusting archive filenames or declared sizes."""

import hashlib
import io
import stat
import struct
import zipfile
import zlib
from datetime import date
from pathlib import PurePosixPath

from .common import MAX_FILE_BYTES, SyncError, activity_id


class UnsupportedFile(SyncError):
    """A summary/manual activity can exist without an original FIT recording."""


def original_record(row, content, max_bytes=16 * 1024 * 1024):
    # Both Worker modes forward the compressed original; decoding belongs to the receiver.
    if len(content) > max_bytes:
        raise SyncError("original exceeds the Worker size limit; use the local CLI")
    if content.startswith(b"PK\x03\x04"):
        extension = "zip"
    elif len(content) >= 14 and content[8:12] == b".FIT":
        extension = "fit"
    else:
        raise SyncError("Garmin did not return a ZIP or FIT original")
    day = date.fromisoformat(row["day"])
    return {
        "id": row["id"],
        "day": row["day"],
        "revision": row["revision"],
        "filename": f"{day.year:04d}/{day.month:02d}/{activity_id(row['id'])}.{extension}",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def validate_fit(data):
    # Check actual content rather than treating an HTML error or a renamed TCX as a FIT file.
    if len(data) < 14 or data[0] not in (12, 14) or data[8:12] != b".FIT":
        raise UnsupportedFile("original file is not a FIT recording")
    expected = data[0] + struct.unpack_from("<I", data, 4)[0] + 2
    if expected != len(data) or len(data) > MAX_FILE_BYTES:
        raise SyncError("FIT file is truncated or exceeds the 64 MiB limit")
    return data


def extract_fit(data):
    if not data.startswith(b"PK"):
        return validate_fit(data)
    # Bound the central directory before ZipFile allocates one Python object per entry.
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if end < 0 or len(data) - end < 22:
        raise SyncError("ZIP central directory is missing")
    _, disk, directory_disk, entries_on_disk, entries, size, offset, comment = struct.unpack_from(
        "<4s4H2IH", data, end
    )
    if (
        disk
        or directory_disk
        or entries_on_disk != entries
        or entries > 32
        or size > 1024 * 1024
        or offset + size != end
        or end + 22 + comment != len(data)
    ):
        raise SyncError(
            "ZIP directory exceeds limits or uses an unsupported multipart/ZIP64 format"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set()
            fits = []
            for entry in archive.infolist():
                name = PurePosixPath(entry.filename)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or "\\" in entry.filename
                    or ":" in entry.filename
                    or entry.filename in names
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise SyncError("ZIP contains an unsafe or duplicate filename")
                names.add(entry.filename)
                if entry.flag_bits & 1 or entry.compress_type not in (0, 8):
                    raise SyncError("encrypted ZIP or unsupported compression method")
                if not entry.is_dir():
                    if name.suffix.lower() != ".fit":
                        raise UnsupportedFile("ZIP must contain one FIT and no other files")
                    fits.append(entry)
            if len(fits) != 1:
                raise UnsupportedFile("ZIP must contain exactly one FIT recording")
            if fits[0].file_size > MAX_FILE_BYTES:
                raise SyncError("unpacked FIT exceeds 64 MiB")
            with archive.open(fits[0]) as source:
                # Enforce the limit while reading; never trust the ZIP's file_size alone.
                result = source.read(MAX_FILE_BYTES + 1)
                if len(result) > MAX_FILE_BYTES or source.read(1):
                    raise SyncError("unpacked FIT exceeds 64 MiB")
            return validate_fit(result)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as error:
        raise SyncError("ZIP is damaged or cannot be decoded") from error
