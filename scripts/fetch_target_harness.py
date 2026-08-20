"""
Fetch the target-harness bundle (targets/target-app/'s content) from S3 and
extract it before the app starts.

Why this exists: targets/target-app/ contains real, load-bearing per-brand
Mongoose model names that can't be genericized in place (see
docs/PLAN_TARGET_HARNESS_SPLIT.md) — it now lives in a private repo
(sidreddy88/agent-platform-target-harness) and is synced to S3 on every push.
This script is run once, before uvicorn starts, to pull that content down.

Fail-loud by design, deliberately NOT matching the rest of this codebase's
usual fail-open convention for missing harness content
(BaseAgent._load_harness_docs() already degrades silently on its own, and
that's fine as a last-resort default — but the fetch step that runs before it
is the only place a real failure will ever be visible at all). Two production
incidents already found in this project's own history — a Langfuse tracing
outage that went undetected for months (a placeholder secret + a missing env
var, both silently defaulting to "nothing works"), and FIX_TARGET_REPO
silently reverting to a placeholder default after an org-name-scrub PR — are
the exact failure shape this script is designed not to repeat: a config value
quietly degrades, nothing crashes, nothing pages anyone, and the gap is found
by a human noticing an absence, much later. Any failure here exits non-zero
and takes the container startup down with it, on purpose.

Usage (also the Docker CMD's first step — see Dockerfile):
    python scripts/fetch_target_harness.py

Exits 0 if:
  - settings.target_harness_bucket is unset (explicit opt-out, e.g. local dev
    without AWS credentials — harness_docs_path is expected to already exist
    locally in that case, e.g. from cloning the private harness repo directly)
  - the fetch and extraction both succeed

Exits 1 (and logs at ERROR) on any other failure: S3 read error, empty/missing
object, corrupt archive, or a write failure extracting it.
"""
from __future__ import annotations

import logging
import os
import sys
import tarfile
from io import BytesIO
from pathlib import Path

# Running as `python scripts/fetch_target_harness.py` puts scripts/ on
# sys.path, not the project root — app/ is a sibling, not importable without
# this. Same pattern as scripts/triage_replay.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="[fetch-target-harness] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    from app.core.config import settings

    if not settings.target_harness_bucket:
        logger.info("target_harness_bucket is unset — skipping fetch (expects harness_docs_path to already exist locally).")
        return 0

    dest = Path(settings.harness_docs_path)
    if not dest.is_absolute():
        dest = Path(__file__).parent.parent / dest

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        s3 = boto3.client("s3", region_name=settings.aws_region)
        logger.info(
            "Fetching s3://%s/%s ...", settings.target_harness_bucket, settings.target_harness_key,
        )
        obj = s3.get_object(Bucket=settings.target_harness_bucket, Key=settings.target_harness_key)
        body = obj["Body"].read()
    except (BotoCoreError, ClientError, Exception) as exc:  # noqa: BLE001 — this MUST fail loud, catch everything
        logger.error("Fetch failed: %s", exc)
        return 1

    if not body:
        logger.error("Fetch returned an empty object — refusing to extract nothing over a real harness dir.")
        return 1

    try:
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=BytesIO(body), mode="r:gz") as tar:
            members = tar.getmembers()
            if not members:
                logger.error("Archive has zero members — refusing to extract an empty bundle.")
                return 1
            tar.extractall(path=dest, filter="data")  # filter="data": reject path traversal / absolute paths
    except Exception as exc:  # noqa: BLE001 — same fail-loud requirement as the fetch above
        logger.error("Extraction failed: %s", exc)
        return 1

    file_count = sum(1 for m in members if m.isfile())
    logger.info("Extracted %d file(s) to %s", file_count, dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
