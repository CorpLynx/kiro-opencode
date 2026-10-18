"""File type detection, atomic output, and the plain-text handler."""

import contextlib
import os
import shutil
import sys
import tempfile

UTF16_BOMS = ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
PCAP_MAGICS = (
    b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4",  # pcap, microsecond
    b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d",  # pcap, nanosecond
    b"\x0a\x0d\x0d\x0a",                       # pcapng
)


class SkipFile(Exception):
    """This input can't be redacted safely; leave it out of the output."""


def detect(head):
    """Classify an input by its first bytes: 'capture', 'package', or 'text'."""
    if head[:4] in PCAP_MAGICS:
        return "capture"
    if head[:4] == b"PK\x03\x04":
        return "package"
    return "text"


def current_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


@contextlib.contextmanager
def open_output(path, mode=None):
    """Yield a binary file that replaces `path` only if the block succeeds.

    Files are written to a temp file next to the target and renamed into
    place, so a crash never leaves a half-written file. Existing permissions
    are kept unless `mode` is given. For "-" the output is buffered and only
    sent to stdout at the end, so a failed input emits nothing.
    """
    if path == "-":
        with tempfile.TemporaryFile() as tmp:
            yield tmp
            tmp.seek(0)
            shutil.copyfileobj(tmp, sys.stdout.buffer)
            sys.stdout.flush()
        return
    path = os.path.realpath(path)  # replace a symlink's target, not the link
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o7777
        except FileNotFoundError:
            mode = 0o666 & ~current_umask()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".ipredact-")
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_bytes(path, data, mode=None):
    with open_output(path, mode) as f:
        f.write(data)


def redact_bytes(redactor, data, reverse):
    """Redact a text file's bytes. UTF-16 (with BOM) is decoded; anything
    else is treated as ASCII-compatible and non-UTF-8 bytes pass through."""
    for bom, enc in UTF16_BOMS:
        if data.startswith(bom):
            text = data[len(bom):].decode(enc, "surrogatepass")
            return bom + redactor.redact_text(text, reverse).encode(enc, "surrogatepass")
    if b"\x00" in data[:8192]:
        raise SkipFile("looks binary and isn't a format ipredact understands")
    text = data.decode("utf-8", "surrogateescape")
    return redactor.redact_text(text, reverse).encode("utf-8", "surrogateescape")


def redact_text_file(redactor, infile, outfile, reverse, notes):
    outfile.write(redact_bytes(redactor, infile.read(), reverse))
