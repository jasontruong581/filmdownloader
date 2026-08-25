"""The pipeline's event sequence is a contract.

The job layer persists these events and the API streams them, so the sequence and
payload shapes are pinned here. The pipeline must also never write to stdout: a
server calling it cannot have output land in its console.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from videotrack.core import events, pipeline
from videotrack.core.models import CaptureResult, NetworkRequest, StreamCandidate
from videotrack.core.options import PipelineOptions


def _capture(*urls: str) -> CaptureResult:
    return CaptureResult(
        page_url="https://page.example.test/watch",
        final_url="https://page.example.test/watch",
        title="Example",
        user_agent="test-agent",
        cookies={},
        requests=[
            NetworkRequest(url=url, method="GET", headers={}, resource_type="Media", status=200)
            for url in urls
        ],
    )


def _options(**overrides) -> PipelineOptions:
    defaults = dict(
        probe=False,
        precheck_hls=False,
        rank_with_ffprobe=False,
        min_duration=0,
        output_dir=Path("output"),
    )
    defaults.update(overrides)
    return PipelineOptions(**defaults)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[events.PipelineEvent] = []

    def __call__(self, event: events.PipelineEvent) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def payload_for(self, kind: str) -> dict:
        for event in self.events:
            if event.kind == kind:
                return event.payload
        raise AssertionError(f"no {kind} event was emitted: {self.kinds}")


class SuccessSequenceTests(unittest.TestCase):
    def test_a_successful_run_reports_candidates_then_attempt_then_completion(self) -> None:
        recorder = _Recorder()
        with TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "clip.mp4"
            out_file.write_bytes(b"x")
            with (
                patch.object(pipeline, "download_with_ffmpeg", return_value=out_file),
                patch.object(pipeline, "probe_duration_seconds", return_value=600.0),
            ):
                code, candidates, _, _ = pipeline.run(
                    _capture("https://cdn.example.test/a.mp4"),
                    _options(output_dir=Path(temp_dir)),
                    recorder,
                )

        self.assertEqual(code, 0)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            recorder.kinds,
            [events.CANDIDATES_FOUND, events.CANDIDATE_ATTEMPT, events.DOWNLOAD_COMPLETED],
        )

    def test_the_candidates_found_payload_carries_the_count_and_stages(self) -> None:
        recorder = _Recorder()
        with TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "clip.mp4"
            out_file.write_bytes(b"x")
            with (
                patch.object(pipeline, "download_with_ffmpeg", return_value=out_file),
                patch.object(pipeline, "probe_duration_seconds", return_value=600.0),
            ):
                pipeline.run(
                    _capture("https://cdn.example.test/a.mp4", "https://cdn.example.test/b.m3u8"),
                    _options(output_dir=Path(temp_dir)),
                    recorder,
                )

        payload = recorder.payload_for(events.CANDIDATES_FOUND)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["stage_counts"]["main"], 2)

    def test_the_completion_payload_names_the_output_path(self) -> None:
        recorder = _Recorder()
        with TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "clip.mp4"
            out_file.write_bytes(b"x")
            with (
                patch.object(pipeline, "download_with_ffmpeg", return_value=out_file),
                patch.object(pipeline, "probe_duration_seconds", return_value=600.0),
            ):
                pipeline.run(_capture("https://cdn.example.test/a.mp4"), _options(output_dir=Path(temp_dir)), recorder)

        self.assertEqual(recorder.payload_for(events.DOWNLOAD_COMPLETED)["path"], str(out_file))


class FailureSequenceTests(unittest.TestCase):
    def test_no_candidates_fails_without_attempting_a_download(self) -> None:
        recorder = _Recorder()
        with patch.object(pipeline, "download_with_ffmpeg") as download:
            code, _, _, _ = pipeline.run(_capture(), _options(), recorder)

        self.assertEqual(code, 2)
        self.assertEqual(recorder.payload_for(events.FAILED)["reason"], "no_candidates")
        download.assert_not_called()

    def test_every_candidate_failing_reports_the_last_error(self) -> None:
        recorder = _Recorder()
        with patch.object(pipeline, "download_with_ffmpeg", side_effect=RuntimeError("ffmpeg exploded")):
            code, _, _, _ = pipeline.run(
                _capture("https://cdn.example.test/a.mp4", "https://cdn.example.test/b.mp4"),
                _options(),
                recorder,
            )

        self.assertEqual(code, 3)
        failed = recorder.payload_for(events.FAILED)
        self.assertEqual(failed["reason"], "all_candidates_failed")
        self.assertIn("ffmpeg exploded", failed["error"])

    def test_a_rejected_candidate_reports_the_error_it_raised(self) -> None:
        recorder = _Recorder()
        with patch.object(pipeline, "download_with_ffmpeg", side_effect=RuntimeError("boom")):
            pipeline.run(_capture("https://cdn.example.test/a.mp4"), _options(), recorder)

        payload = recorder.payload_for(events.CANDIDATE_REJECTED)
        self.assertEqual(payload["reason"], "error")
        self.assertIn("boom", payload["error"])


class ShortOutputRejectionTests(unittest.TestCase):
    def test_a_short_output_is_rejected_with_its_measurements(self) -> None:
        recorder = _Recorder()
        with TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "clip.mp4"
            out_file.write_bytes(b"x")
            with (
                patch.object(pipeline, "download_with_ffmpeg", return_value=out_file),
                patch.object(pipeline, "probe_duration_seconds", return_value=12.0),
            ):
                code, _, _, _ = pipeline.run(
                    _capture("https://cdn.example.test/a.mp4"),
                    _options(min_duration=120, output_dir=Path(temp_dir)),
                    recorder,
                )

        self.assertEqual(code, 3)
        payload = recorder.payload_for(events.CANDIDATE_REJECTED)
        self.assertEqual(payload["reason"], "too_short")
        self.assertEqual(payload["duration"], 12.0)
        self.assertEqual(payload["minimum"], 120)

    def test_a_rejected_short_output_is_deleted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "clip.mp4"
            out_file.write_bytes(b"x")
            with (
                patch.object(pipeline, "download_with_ffmpeg", return_value=out_file),
                patch.object(pipeline, "probe_duration_seconds", return_value=1.0),
            ):
                pipeline.run(
                    _capture("https://cdn.example.test/a.mp4"),
                    _options(min_duration=120, output_dir=Path(temp_dir)),
                    None,
                )

            self.assertFalse(out_file.exists())


class AttemptOrderTests(unittest.TestCase):
    def test_attempts_are_capped_by_max_attempts(self) -> None:
        recorder = _Recorder()
        urls = [f"https://cdn.example.test/{i}.mp4" for i in range(6)]
        with patch.object(pipeline, "download_with_ffmpeg", side_effect=RuntimeError("boom")):
            pipeline.run(_capture(*urls), _options(max_attempts=2), recorder)

        attempts = [e for e in recorder.events if e.kind == events.CANDIDATE_ATTEMPT]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].payload["total"], 2)

    def test_pick_rotates_the_candidate_order(self) -> None:
        recorder = _Recorder()
        urls = ["https://cdn.example.test/a.m3u8", "https://cdn.example.test/b.mp4"]
        with patch.object(pipeline, "download_with_ffmpeg", side_effect=RuntimeError("boom")):
            pipeline.run(_capture(*urls), _options(pick=2), recorder)

        first_attempt = recorder.payload_for(events.CANDIDATE_ATTEMPT)
        # Default ranking puts the HLS candidate first; pick=2 starts on the mp4.
        self.assertEqual(first_attempt["url"], "https://cdn.example.test/b.mp4")

    def test_an_out_of_range_pick_falls_back_to_the_first_candidate(self) -> None:
        recorder = _Recorder()
        with patch.object(pipeline, "download_with_ffmpeg", side_effect=RuntimeError("boom")):
            pipeline.run(_capture("https://cdn.example.test/a.mp4"), _options(pick=99), recorder)

        self.assertEqual(recorder.payload_for(events.CANDIDATE_ATTEMPT)["url"], "https://cdn.example.test/a.mp4")

    def test_a_reorder_hook_can_change_the_attempt_order(self) -> None:
        recorder = _Recorder()
        urls = ["https://cdn.example.test/a.m3u8", "https://cdn.example.test/b.mp4"]

        def reversed_order(candidates: list[StreamCandidate]) -> list[StreamCandidate]:
            return list(reversed(candidates))

        with patch.object(pipeline, "download_with_ffmpeg", side_effect=RuntimeError("boom")):
            pipeline.run(_capture(*urls), _options(), recorder, reorder=reversed_order)

        self.assertEqual(recorder.payload_for(events.CANDIDATE_ATTEMPT)["url"], "https://cdn.example.test/b.mp4")


class SilenceTests(unittest.TestCase):
    def test_the_pipeline_writes_nothing_to_stdout(self) -> None:
        # A server calling the pipeline cannot have output land in its console.
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with patch.object(pipeline, "download_with_ffmpeg", side_effect=RuntimeError("boom")):
                pipeline.run(_capture("https://cdn.example.test/a.mp4"), _options(), None)

        self.assertEqual(buffer.getvalue(), "")

    def test_a_none_sink_is_accepted_everywhere(self) -> None:
        code, _, _, _ = pipeline.run(_capture(), _options(), None)

        self.assertEqual(code, 2)


class ConsoleRendererTests(unittest.TestCase):
    def test_every_event_kind_renders_without_raising(self) -> None:
        from videotrack.console import print_event

        import contextlib
        import io

        samples = [
            events.PipelineEvent(events.INFO, {"message": "hello"}),
            events.PipelineEvent(events.STAGE_STARTED, {"stage": "embed1_phase1", "message": "phase"}),
            events.PipelineEvent(events.CANDIDATES_FOUND, {"count": 2, "stage_counts": {}}),
            events.PipelineEvent(
                events.CANDIDATE_ATTEMPT, {"index": 1, "total": 2, "kind": "mp4", "url": "u"}
            ),
            events.PipelineEvent(
                events.CANDIDATE_REJECTED, {"reason": "too_short", "duration": 1.0, "minimum": 120}
            ),
            events.PipelineEvent(events.CANDIDATE_REJECTED, {"reason": "error", "error": "boom"}),
            events.PipelineEvent(events.DOWNLOAD_COMPLETED, {"path": "out.mp4", "url": "u"}),
            events.PipelineEvent(events.FAILED, {"reason": "no_candidates"}),
            events.PipelineEvent(events.FAILED, {"reason": "all_candidates_failed", "error": "boom"}),
            events.PipelineEvent(events.PROGRESS, {"phase": "downloading", "percent": 42.0}),
            events.PipelineEvent(events.PROGRESS, {"phase": "downloading", "percent": None}),
        ]

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            for event in samples:
                print_event(event)

        rendered = buffer.getvalue()
        self.assertIn("Try 1/2", rendered)
        self.assertIn("Reject short output", rendered)
        self.assertIn("Downloaded: out.mp4", rendered)
        self.assertIn("All candidate attempts failed.", rendered)

    def test_every_declared_event_kind_is_handled(self) -> None:
        from videotrack.console import print_event

        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            for kind in events.EVENT_KINDS:
                print_event(events.PipelineEvent(kind, {}))


if __name__ == "__main__":
    unittest.main()
