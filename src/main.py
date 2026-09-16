"""
ClickUp evidence-attachment MCP tool.

Exposes one tool, `attach_evidence_to_clickup_task`, that:
  1. Decodes the incoming base64 file data.
  2. If it's a compressible image over the target size, downscales /
     re-encodes it (same algorithm as the local compress_for_clickup.py
     script) until it's under the target, or gives up after a bounded
     number of attempts and uploads the original instead.
  3. Uploads the (possibly compressed) file directly to the ClickUp task
     via ClickUp's real REST API, using a token read from this server's
     own environment -- never passed in by the caller, never hardcoded.

Run this on a machine/VM that:
  - Anthropic's servers can reach over the public internet (remote MCP
    connectors are called FROM Anthropic's infrastructure, not from the
    caller's own sandbox -- that's what lets this succeed in places where
    a direct call from a restricted sandbox shell would be blocked).
  - Has CLICKUP_API_TOKEN set in its environment.

Requires: `pip install "mcp[cli]" pillow requests`
"""

import base64
import io
import os

import requests
from mcp.server.fastmcp import FastMCP
from PIL import Image

mcp = FastMCP("clickup-evidence")

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN")
CLICKUP_ATTACHMENT_URL = "https://api.clickup.com/api/v2/task/{task_id}/attachment"

# Same ladder as the local compress_for_clickup.py script, so behavior is
# consistent regardless of which one ends up doing the compressing.
QUALITIES = [85, 70, 55, 40, 25]
MAX_DIMS = [None, 1600, 1200, 800, 500]
MAX_ATTEMPTS = 25


def _compress_image_bytes(raw_bytes: bytes, target_bytes: int) -> dict:
    """
    Try to get `raw_bytes` (an image) under `target_bytes` by iteratively
    re-encoding as JPEG at decreasing quality and, if needed, downscaling
    dimensions. Returns a dict describing the outcome; on success includes
    the compressed bytes under "data".
    """
    try:
        im = Image.open(io.BytesIO(raw_bytes))
        im.load()
    except Exception as e:
        return {"ok": False, "error": f"could not open as image: {e}"}

    if im.mode in ("RGBA", "P", "LA"):
        im = im.convert("RGB")

    best = None
    attempts = 0
    for max_dim in MAX_DIMS:
        work = im
        if max_dim is not None:
            work = im.copy()
            work.thumbnail((max_dim, max_dim))
        for quality in QUALITIES:
            attempts += 1
            buf = io.BytesIO()
            work.save(buf, "JPEG", quality=quality, optimize=True)
            size = buf.tell()
            if best is None or size < best[0]:
                best = (size, buf.getvalue(), max_dim, quality)
            if size <= target_bytes:
                return {
                    "ok": True,
                    "bytes": size,
                    "max_dim": max_dim or "original",
                    "quality": quality,
                    "attempts": attempts,
                    "data": buf.getvalue(),
                }
            if attempts >= MAX_ATTEMPTS:
                break
        if attempts >= MAX_ATTEMPTS:
            break

    if best is not None:
        return {
            "ok": False,
            "error": "could not reach target size",
            "best_bytes": best[0],
            "best_data": best[1],
            "best_max_dim": best[2] or "original",
            "best_quality": best[3],
            "attempts": attempts,
        }
    return {"ok": False, "error": "no attempts succeeded"}


@mcp.tool()
def attach_evidence_to_clickup_task(
    task_id: str,
    file_name: str,
    file_data_b64: str,
    target_bytes: int = 40000,
) -> dict:
    """
    Compress (if it's an image over the target size) and upload a piece of
    evidence to a ClickUp task's attachments, using this server's own
    ClickUp API token.

    Args:
        task_id: ClickUp task ID (e.g. "868m3rehv").
        file_name: Name for the attached file, including extension.
        file_data_b64: Base64-encoded file content.
        target_bytes: Only relevant for images -- try to compress under
            this size before uploading. Non-image files, or images that
            can't be compressed enough, are uploaded as-is.

    Returns:
        A dict with "ok" (bool) and either attachment details or an error.
        Never raises for expected failure modes (missing token, ClickUp
        API error, bad input) -- always returns a dict so the caller can
        decide how to relay the outcome to the requester.
    """
    if not CLICKUP_API_TOKEN:
        return {
            "ok": False,
            "error": "CLICKUP_API_TOKEN is not set in this server's environment",
        }

    try:
        raw_bytes = base64.b64decode(file_data_b64)
    except Exception as e:
        return {"ok": False, "error": f"invalid base64 input: {e}"}

    upload_bytes = raw_bytes
    upload_name = file_name
    compression_note = None

    if len(raw_bytes) > target_bytes:
        result = _compress_image_bytes(raw_bytes, target_bytes)
        if result.get("ok"):
            upload_bytes = result["data"]
            # Re-encoded as JPEG regardless of original format.
            base_name = file_name.rsplit(".", 1)[0]
            upload_name = f"{base_name}.jpg"
            compression_note = (
                f"compressed {len(raw_bytes)} -> {result['bytes']} bytes "
                f"(max_dim={result['max_dim']}, quality={result['quality']}, "
                f"attempts={result['attempts']})"
            )
        elif "best_data" in result:
            # Couldn't hit the target, but got some reduction -- still
            # better than uploading the raw original.
            upload_bytes = result["best_data"]
            base_name = file_name.rsplit(".", 1)[0]
            upload_name = f"{base_name}.jpg"
            compression_note = (
                f"could not reach {target_bytes} bytes, best effort "
                f"{result['best_bytes']} bytes "
                f"(max_dim={result['best_max_dim']}, quality={result['best_quality']}) "
                f"-- uploading original file instead"
                if result["best_bytes"] > len(raw_bytes)
                else f"partially compressed to {result['best_bytes']} bytes "
                f"(target was {target_bytes})"
            )
            # If "best effort" is somehow not actually smaller, fall back
            # to the raw original rather than uploading something bigger.
            if result["best_bytes"] > len(raw_bytes):
                upload_bytes = raw_bytes
                upload_name = file_name
        else:
            # Not an image (or unreadable) -- upload the original as-is.
            compression_note = f"not compressed ({result.get('error')}) -- uploading original"

    try:
        response = requests.post(
            CLICKUP_ATTACHMENT_URL.format(task_id=task_id),
            headers={"Authorization": CLICKUP_API_TOKEN},
            files={"attachment": (upload_name, upload_bytes)},
            timeout=60,
        )
    except requests.RequestException as e:
        return {"ok": False, "error": f"upload request failed: {e}", "compression": compression_note}

    if response.status_code >= 300:
        return {
            "ok": False,
            "error": f"ClickUp API returned {response.status_code}: {response.text[:500]}",
            "compression": compression_note,
        }

    body = response.json()
    return {
        "ok": True,
        "attachment_id": body.get("id"),
        "url": body.get("url"),
        "uploaded_bytes": len(upload_bytes),
        "compression": compression_note,
    }


if __name__ == "__main__":
    mcp.run()