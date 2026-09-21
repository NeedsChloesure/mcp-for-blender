"""
Persist image results to files so the model can read them.

Why this exists: some MCP hosts (notably the Freebuff desktop harness) cannot
render an `image` content block. The base64 arrives as JSON text the model never
gets to look at, and results above the host's size cap are dropped outright.
Writing the bytes to a file and returning the path sidesteps both problems: the
model opens the path with its own file-reading tool, which does work there.

Environment:
- ``BLENDER_MCP_IMAGE_DIR``     where to write the files
                                (default: ``<tmp>/mcp-for-blender-images``)
- ``BLENDER_MCP_IMAGE_OUTPUT``  ``file`` (default) or ``inline`` to return the
                                bytes inline as before

No third-party dependencies, so this module can be imported and tested on its
own, without the MCP runtime.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

IMAGE_DIR_ENV = "BLENDER_MCP_IMAGE_DIR"
IMAGE_OUTPUT_ENV = "BLENDER_MCP_IMAGE_OUTPUT"

# Readers of this kind (read_files in the Freebuff/Codebuff clients) refuse
# images above ~768 KiB, so keep the file comfortably under that.
MAX_IMAGE_BYTES = 700 * 1024

# Retain the newest screenshots so the directory cannot grow without bound.
KEEP_NEWEST = 40

_EXTENSIONS = {
    "png": "png",
    "jpeg": "jpg",
    "jpg": "jpg",
    "webp": "webp",
    "gif": "gif",
    "bmp": "bmp",
    "tiff": "tiff",
}

# (max side, jpeg quality) attempts, largest/best first.
_SHRINK_ATTEMPTS = ((1600, 85), (1200, 75), (900, 65), (700, 55))


def image_output_mode() -> str:
    """``"file"`` (default) or ``"inline"``."""
    return os.environ.get(IMAGE_OUTPUT_ENV, "file").strip().lower()


def image_dir() -> str:
    """Directory the images are written to, created on demand."""
    configured = os.environ.get(IMAGE_DIR_ENV)
    base = configured or os.path.join(
        tempfile.gettempdir(), "mcp-for-blender-images"
    )
    return os.path.expanduser(base)


def _human_size(size_bytes: int) -> str:
    """``237 bytes`` / ``252 KB`` — never a misleading ``0 KB``."""
    if size_bytes < 1024:
        return f"{size_bytes} bytes"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _unique_path(directory: str, stem: str, extension: str) -> str:
    """``<directory>/<stem>.<extension>``, never colliding with a sibling."""
    path = os.path.join(directory, f"{stem}.{extension}")
    counter = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{stem}-{counter}.{extension}")
        counter += 1
    return path


def _prune(directory: str) -> None:
    """Keep the newest ``KEEP_NEWEST`` files in ``directory``."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    files = [os.path.join(directory, name) for name in names]
    files = [path for path in files if os.path.isfile(path)]
    if len(files) <= KEEP_NEWEST:
        return
    files.sort(key=os.path.getmtime, reverse=True)
    for stale in files[KEEP_NEWEST:]:
        try:
            os.remove(stale)
        except OSError:
            pass


def _shrink_with_imagemagick(path: str) -> str | None:
    """Downscale ``path`` with ImageMagick; return the smaller file, or None."""
    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        return None
    stem = os.path.splitext(path)[0]
    for max_side, quality in _SHRINK_ATTEMPTS:
        candidate = f"{stem}-{max_side}.jpg"
        command = [
            magick,
            path,
            "-resize",
            f"{max_side}x{max_side}>",
            "-quality",
            str(quality),
            candidate,
        ]
        try:
            subprocess.run(
                command, check=True, capture_output=True, timeout=60
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if os.path.exists(candidate) and os.path.getsize(candidate) <= MAX_IMAGE_BYTES:
            try:
                os.remove(path)
            except OSError:
                pass
            return candidate
    return None


def save_image(data: bytes, image_format: str, tool: str) -> str:
    """Write ``data`` into the image directory and return the absolute path.

    Downscales with ImageMagick when the file would exceed the size a
    file-reading host will accept.
    """
    directory = image_dir()
    os.makedirs(directory, exist_ok=True)

    fmt = (image_format or "png").lower().split("/")[-1]
    extension = _EXTENSIONS.get(fmt, "png")
    safe_tool = "".join(
        char if char.isalnum() or char in "-_" else "-" for char in tool
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = _unique_path(
        directory, f"{safe_tool}-{stamp}-{os.getpid()}", extension
    )

    with open(path, "wb") as handle:
        handle.write(data)

    if os.path.getsize(path) > MAX_IMAGE_BYTES:
        smaller = _shrink_with_imagemagick(path)
        if smaller:
            path = smaller

    _prune(directory)
    return path


def deliver_image(
    data: bytes, image_format: str, tool: str, *, detail: str = ""
) -> str:
    """Save ``data`` and return the note (with path) to hand back to the model."""
    path = save_image(data, image_format, tool)
    size_bytes = os.path.getsize(path)

    note = f"{tool} saved the image to {path} ({_human_size(size_bytes)})."
    if detail:
        note += " " + detail
    note += (
        " Open that path with your file-reading tool (for example read_files) to "
        "see it; this host does not render inline image content."
    )
    if size_bytes > MAX_IMAGE_BYTES:
        note += (
            " If your reader refuses it for size, downscale first: "
            f"`magick {path} -resize 1200x1200 "
            f"{os.path.splitext(path)[0]}-small.jpg`."
        )
    return note
