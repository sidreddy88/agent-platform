"""
Tests for scripts/fetch_target_harness.py — the pre-startup S3 fetch for
targets/target-app/'s content (see docs/PLAN_TARGET_HARNESS_SPLIT.md).

This script is deliberately fail-loud: any failure must return a non-zero
exit code so the container startup fails outright, rather than silently
degrading like BaseAgent._load_harness_docs() already does further downstream.
That's the entire point of this script's existence, so every test here is
really testing "does a failure here actually propagate as a failure."
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "fetch_target_harness.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fetch_target_harness", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_target_harness = _load_module()


def _make_tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeSettings:
    target_harness_bucket = "test-bucket"
    target_harness_key = "latest.tar.gz"
    harness_docs_path = ""  # set per-test to a tmp_path
    aws_region = "us-east-1"
    skip_target_harness_fetch = False


@pytest.fixture
def fake_settings(tmp_path):
    s = _FakeSettings()
    s.harness_docs_path = str(tmp_path / "target-app")
    with patch("app.core.config.settings", s):
        yield s


def test_unset_bucket_fails_loud_not_skips(tmp_path):
    """The actual gap this closes: an empty bucket setting must never be
    silently treated as 'nothing to do' -- that's exactly how a forgotten
    env var in a task definition turns into a silent outage, the same
    failure shape as the Langfuse-tracing and FIX_TARGET_REPO incidents this
    script's docstring cites as precedent."""
    s = _FakeSettings()
    s.target_harness_bucket = ""
    s.harness_docs_path = str(tmp_path / "target-app")
    with patch("app.core.config.settings", s):
        assert fetch_target_harness.main() == 1
    assert not (tmp_path / "target-app").exists()


def test_skip_flag_skips_cleanly(tmp_path):
    """The actual, deliberate opt-out for local dev -- must be an explicit
    flag a developer sets on purpose, not an accidental default."""
    s = _FakeSettings()
    s.target_harness_bucket = ""
    s.skip_target_harness_fetch = True
    s.harness_docs_path = str(tmp_path / "target-app")
    with patch("app.core.config.settings", s):
        assert fetch_target_harness.main() == 0
    assert not (tmp_path / "target-app").exists()


def test_skip_flag_wins_even_if_bucket_is_set(tmp_path):
    """skip_target_harness_fetch short-circuits before any S3 call is made,
    regardless of what target_harness_bucket holds."""
    s = _FakeSettings()
    s.skip_target_harness_fetch = True
    s.harness_docs_path = str(tmp_path / "target-app")
    mock_boto3 = MagicMock()
    with patch("app.core.config.settings", s), patch.dict(sys.modules, {"boto3": mock_boto3}):
        assert fetch_target_harness.main() == 0
    mock_boto3.client.assert_not_called()


def test_successful_fetch_extracts_files(fake_settings):
    tarball = _make_tarball({"AGENTS.md": "# agents", "DECISIONS.md": "# decisions"})
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": io.BytesIO(tarball)}
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        assert fetch_target_harness.main() == 0

    dest = Path(fake_settings.harness_docs_path)
    assert (dest / "AGENTS.md").read_text() == "# agents"
    assert (dest / "DECISIONS.md").read_text() == "# decisions"


def test_s3_error_fails_loud(fake_settings):
    from botocore.exceptions import ClientError

    mock_s3 = MagicMock()
    mock_s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject",
    )
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        assert fetch_target_harness.main() == 1


def test_empty_object_fails_loud(fake_settings):
    """A real production risk: a sync that uploads a zero-byte object should
    never be treated the same as 'nothing to do' — it must fail, not silently
    extract nothing over a real harness dir."""
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": io.BytesIO(b"")}
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        assert fetch_target_harness.main() == 1


def test_empty_archive_fails_loud(fake_settings):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass  # zero members
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": io.BytesIO(buf.getvalue())}
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        assert fetch_target_harness.main() == 1


def test_corrupt_archive_fails_loud(fake_settings):
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": io.BytesIO(b"not a real tarball")}
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        assert fetch_target_harness.main() == 1


def test_unexpected_exception_fails_loud_not_raises(fake_settings):
    """Whatever goes wrong, main() must return 1 -- never let an unhandled
    exception escape and produce a Python traceback exit code instead of the
    explicit, intentional non-zero this script is designed around."""
    mock_boto3 = MagicMock()
    mock_boto3.client.side_effect = RuntimeError("totally unexpected")

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        assert fetch_target_harness.main() == 1
