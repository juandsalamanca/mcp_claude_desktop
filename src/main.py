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


from fastmcp import FastMCP
from tools.upload_image_to_clickup import upload_to_clickup

mcp = FastMCP("clickup-evidence")

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
    upload_response  = upload_to_clickup(task_id, file_name, file_data_b64, target_bytes)
    return upload_response

if __name__ == "__main__":
     mcp.run(transport="http", host="0.0.0.0", port=9000)