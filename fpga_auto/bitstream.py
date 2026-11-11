"""Per-job bitstream publication and validation, after design acceptance."""
import hashlib
from pathlib import Path

from .jobs import atomic_json
from .results import FPGAError


def reserve_output(job):
    directory = job.directory / "artifacts"
    try:
        directory.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise FPGAError("bitstream_output_exists", "Artifact directory already exists; refusing to reuse prior output", "bitstream") from exc
    output = directory / "design.bit"
    job.request["bitstream_output"] = str(output)
    job.save_request()
    return output


def inspect_output(job, bridge):
    expected = Path(job.request["bitstream_output"]).resolve()
    reported = bridge.get("bitstream_path")
    if bridge.get("bitstream_status") != "passed" or not reported or Path(reported).resolve() != expected:
        raise FPGAError("bitstream_result_invalid", "No matching bitstream completion record for this job", "bitstream")
    if not expected.is_file() or expected.stat().st_size == 0:
        raise FPGAError("bitstream_output_missing", "Completed bitstream is missing or empty", "bitstream")
    digest = hashlib.sha256()
    with expected.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    artifact = dict(status="passed", path=str(expected), size_bytes=expected.stat().st_size, sha256=digest.hexdigest())
    atomic_json(expected.parent / "bitstream.json", dict(job_id=job.id, **artifact))
    return artifact
