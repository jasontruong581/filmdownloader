"""API behavior, with the engines mocked and a temporary database.

Two properties get particular attention:

* a refusal is machine-readable, never a stack trace, and
* a crafted library id cannot read a file outside the output directory.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from videotrack.core.models import BatchItem, BatchProbe
from videotrack.core.resolvers import Resolution, ResolvedMedia
from videotrack.jobs.manager import JobManager
from videotrack.server.app import create_app
from videotrack.server.settings import Settings


def _resolution(engine: str = "ytdlp", title: str = "Example Clip") -> Resolution:
    from videotrack.core.models import MediaFormat

    return Resolution(
        resolver=engine,
        page_url="https://page.example.test/w/1",
        final_url="https://page.example.test/w/1",
        title=title,
        media=(ResolvedMedia("https://cdn.example.test/a.mp4", "https://page.example.test/w/1", "mp4"),),
        engine=engine,
        duration=128.0,
        thumbnail="https://img.example.test/t.jpg",
        formats=(
            MediaFormat("22", "mp4", height=720, vcodec="avc1", acodec="mp4a", tbr=1200.0),
            MediaFormat("140", "m4a", vcodec="none", acodec="mp4a", tbr=128.0),
        ),
    )


class _ApiFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.output_dir = Path(self._temp.name) / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # The worker runs on a background thread, so a transient `with patch(...)`
        # around submit() is already gone by the time it executes and the real
        # runner reaches the network. The stub is installed for the whole test.
        self.runner_calls: list[str] = []
        runner_patch = patch.object(
            JobManager, "_default_runner", autospec=True, side_effect=self._fake_runner
        )
        runner_patch.start()
        self.addCleanup(runner_patch.stop)

        # The settings endpoint persists, so the destination is redirected for
        # the same reason the database is: a test run must not rewrite the
        # operator's real configuration file.
        self.settings_file = Path(self._temp.name) / "settings.json"
        self.settings = Settings(host="127.0.0.1", output_dir=str(self.output_dir))
        self.client = TestClient(
            create_app(self.settings, db_path=":memory:", settings_path=self.settings_file),
            base_url="http://127.0.0.1:8756",
        )
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))

    def _fake_runner(self, manager, job, resolution, cancel, on_event) -> Path:
        self.runner_calls.append(job.url)
        target = self.output_dir / f"{job.id}.mp4"
        target.write_bytes(b"stub")
        return target


class HealthTests(_ApiFixture):
    def test_health_reports_tools_and_the_output_directory(self) -> None:
        body = self.client.get("/api/health").json()

        self.assertIn("tools", body)
        self.assertEqual(body["output_dir"], str(self.output_dir))
        self.assertIn("ffmpeg", [tool["name"] for tool in body["tools"]])

    def test_health_is_not_ok_when_a_required_tool_is_missing(self) -> None:
        with patch("videotrack.server.routes.check_tools") as check:
            from videotrack.core.preflight import ToolStatus

            check.return_value = (ToolStatus("ffmpeg", True, None, None),)
            body = self.client.get("/api/health").json()

        self.assertFalse(body["ok"])

    def test_health_reports_free_space(self) -> None:
        body = self.client.get("/api/health").json()

        self.assertIsNotNone(body["free_bytes"])


    def test_health_honors_the_configured_ffmpeg_location(self) -> None:
        # Regression: health read the environment variable directly, so the
        # Settings field the README points operators at changed nothing.
        configured = Path(self._temp.name) / "ffmpeg-bin"
        configured.mkdir()
        self.settings.ffmpeg_location = str(configured)

        with patch("videotrack.server.routes.check_tools") as check:
            check.return_value = ()
            self.client.get("/api/health")

        self.assertEqual(check.call_args.args[0], configured)

    def test_health_falls_back_to_discovery_when_no_location_is_set(self) -> None:
        self.settings.ffmpeg_location = ""

        with patch("videotrack.server.routes.check_tools") as check:
            check.return_value = ()
            self.client.get("/api/health")

        self.assertIsNone(check.call_args.args[0])


class ResolveTests(_ApiFixture):
    def test_a_successful_resolve_returns_formats_and_a_reusable_id(self) -> None:
        with patch("videotrack.server.routes.chain_resolve", return_value=[_resolution()]):
            body = self.client.post("/api/resolve", json={"url": "https://page.example.test/w/1"}).json()

        self.assertEqual(body["title"], "Example Clip")
        self.assertEqual(body["engine"], "ytdlp")
        self.assertEqual([f["format_id"] for f in body["formats"]], ["22", "140"])
        self.assertTrue(body["resolution_id"])

    def test_format_labels_reach_the_client(self) -> None:
        with patch("videotrack.server.routes.chain_resolve", return_value=[_resolution()]):
            body = self.client.post("/api/resolve", json={"url": "https://page.example.test/w/1"}).json()

        self.assertIn("720p", body["formats"][0]["label"])

    def test_nothing_resolved_is_a_structured_refusal(self) -> None:
        with patch("videotrack.server.routes.chain_resolve", return_value=[]):
            response = self.client.post("/api/resolve", json={"url": "https://page.example.test/x"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["reason"], "not_resolved")

    def test_a_sign_in_wall_is_a_structured_refusal(self) -> None:
        # Regression: this answered 200, and the UI then queued a job whose URL
        # was the login page.
        wall = Resolution(
            resolver="browser",
            page_url="https://page.example.test/w/1",
            final_url="https://page.example.test/auth/login?currentUrl=%2Fw%2F1",
            title="Sign in",
            media=(),
            engine="browser",
        )

        with patch("videotrack.server.routes.chain_resolve", return_value=[wall]):
            response = self.client.post("/api/resolve", json={"url": "https://page.example.test/w/1"})

        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["reason"], "login_required")
        self.assertIn("cookies_from_browser", detail["message"])

    def test_a_page_whose_media_lives_in_an_embed_still_resolves(self) -> None:
        # The guard for the refusal above: no direct media is the normal shape
        # for an embed-hosted page, and it must still resolve.
        embedded = Resolution(
            resolver="browser",
            page_url="https://page.example.test/w/1",
            final_url="https://page.example.test/w/1",
            title="Embedded Clip",
            media=(),
            engine="browser",
        )

        with patch("videotrack.server.routes.chain_resolve", return_value=[embedded]):
            response = self.client.post("/api/resolve", json={"url": "https://page.example.test/w/1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Embedded Clip")

    def test_the_requested_url_is_returned_alongside_the_final_one(self) -> None:
        # What the client should record for the job, so a redirect target is not
        # mistaken for the thing that was asked for.
        redirected = Resolution(
            resolver="browser",
            page_url="https://page.example.test/w/1",
            final_url="https://cdn.example.test/canonical/1",
            title="Example Clip",
            media=(),
            engine="browser",
        )

        with patch("videotrack.server.routes.chain_resolve", return_value=[redirected]):
            body = self.client.post(
                "/api/resolve", json={"url": "https://page.example.test/w/1"}
            ).json()

        self.assertEqual(body["url"], "https://page.example.test/w/1")
        self.assertEqual(body["final_url"], "https://cdn.example.test/canonical/1")

    def test_an_enumerating_url_reports_its_item_count(self) -> None:
        with patch(
            "videotrack.server.routes.chain_resolve",
            return_value=[_resolution(title="a"), _resolution(title="b")],
        ):
            body = self.client.post("/api/resolve", json={"url": "https://page.example.test/list"}).json()

        self.assertEqual(body["item_count"], 2)


class JobTests(_ApiFixture):
    def _queue(self, url: str = "https://page.example.test/w/1") -> dict:
        return self.client.post("/api/jobs", json={"url": url}).json()

    def test_a_job_can_be_queued_and_read_back(self) -> None:
        created = self._queue()

        fetched = self.client.get(f"/api/jobs/{created['id']}").json()

        self.assertEqual(fetched["url"], "https://page.example.test/w/1")

    def test_queueing_without_a_url_or_resolution_is_refused(self) -> None:
        response = self.client.post("/api/jobs", json={})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["reason"], "no_url")

    def test_an_expired_resolution_id_is_refused_clearly(self) -> None:
        response = self.client.post("/api/jobs", json={"resolution_id": "gone"})

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["detail"]["reason"], "resolution_expired")

    def test_a_cached_resolution_is_reused_without_re_resolving(self) -> None:
        with patch("videotrack.server.routes.chain_resolve", return_value=[_resolution()]) as resolve:
            resolved = self.client.post("/api/resolve", json={"url": "https://page.example.test/w/1"}).json()
            created = self.client.post(
                "/api/jobs", json={"resolution_id": resolved["resolution_id"], "format_id": "22"}
            ).json()

        self.assertEqual(created["engine"], "ytdlp")
        self.assertEqual(created["format_id"], "22")
        # Exactly the one resolve the operator asked for.
        self.assertEqual(resolve.call_count, 1)

    def test_an_unknown_job_is_a_404(self) -> None:
        response = self.client.get("/api/jobs/nope")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["reason"], "unknown_job")

    def test_jobs_can_be_listed_and_filtered(self) -> None:
        self._queue("https://page.example.test/w/1")
        self._queue("https://page.example.test/w/2")

        listed = self.client.get("/api/jobs").json()
        filtered = self.client.get("/api/jobs", params={"status": "nonsense"}).json()

        self.assertGreaterEqual(len(listed), 2)
        self.assertEqual(filtered, [])

    def test_retrying_an_unknown_job_is_a_404(self) -> None:
        response = self.client.post("/api/jobs/nope/retry")

        self.assertEqual(response.status_code, 404)

    def test_cancelling_an_unknown_job_is_a_conflict(self) -> None:
        response = self.client.delete("/api/jobs/nope")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["reason"], "not_cancellable")


class BatchTests(_ApiFixture):
    def test_a_proven_probe_returns_its_items(self) -> None:
        probe = BatchProbe(
            capability="playlist",
            confidence="proven",
            items=(BatchItem("https://page.example.test/w/1", "One"), BatchItem("https://page.example.test/w/2", "Two")),
            total_estimate=2,
        )
        with patch("videotrack.server.routes.batch_probe", return_value=probe):
            body = self.client.post("/api/batch/probe", json={"url": "https://page.example.test/list"}).json()

        self.assertTrue(body["batchable"])
        self.assertEqual(body["capability"], "playlist")
        self.assertEqual([item["title"] for item in body["items"]], ["One", "Two"])

    def test_a_negative_probe_returns_the_reason_verbatim(self) -> None:
        probe = BatchProbe(reason="yt-dlp resolved a single video, not a playlist")
        with patch("videotrack.server.routes.batch_probe", return_value=probe):
            body = self.client.post("/api/batch/probe", json={"url": "https://page.example.test/w/1"}).json()

        self.assertFalse(body["batchable"])
        self.assertEqual(body["reason"], "yt-dlp resolved a single video, not a playlist")

    def test_a_possible_probe_is_flagged_so_the_ui_can_gate_it(self) -> None:
        probe = BatchProbe(
            capability="crawl",
            confidence="possible",
            items=(BatchItem("https://page.example.test/a"), BatchItem("https://page.example.test/b")),
        )
        with patch("videotrack.server.routes.batch_probe", return_value=probe):
            body = self.client.post("/api/batch/probe", json={"url": "https://page.example.test/"}).json()

        self.assertEqual(body["confidence"], "possible")
        self.assertTrue(body["batchable"])

    def test_queueing_a_batch_creates_one_job_per_item(self) -> None:
        items = [{"url": f"https://page.example.test/w/{i}", "title": f"Clip {i}"} for i in range(5)]
        body = self.client.post(
            "/api/batch/jobs", json={"items": items, "source_url": "https://page.example.test/list"}
        ).json()

        self.assertEqual(len(body["jobs"]), 5)
        self.assertTrue(all(job["batch_id"] == body["id"] for job in body["jobs"]))

    def test_queueing_an_empty_batch_is_refused(self) -> None:
        response = self.client.post("/api/batch/jobs", json={"items": []})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["reason"], "no_items")

    def test_a_batch_can_be_read_back_with_its_jobs(self) -> None:
        items = [{"url": "https://page.example.test/w/1"}]
        created = self.client.post("/api/batch/jobs", json={"items": items}).json()

        fetched = self.client.get(f"/api/batches/{created['id']}").json()

        self.assertEqual(len(fetched["jobs"]), 1)

    def test_an_unknown_batch_is_a_404(self) -> None:
        self.assertEqual(self.client.get("/api/batches/nope").status_code, 404)

    def test_sample_verification_reports_what_it_checked(self) -> None:
        with patch("videotrack.server.routes.sample_verify", return_value=(2, 2)):
            body = self.client.post(
                "/api/batch/verify",
                json={"items": [{"url": "https://page.example.test/w/1"}], "count": 2},
            ).json()

        self.assertEqual(body, {"verified": 2, "attempted": 2})


class LibraryPathTests(_ApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.media = self.output_dir / "clip.mp4"
        self.media.write_bytes(b"0123456789")
        self.secret = self.output_dir.parent / "secret.txt"
        self.secret.write_text("do not serve me", encoding="utf-8")

    def test_media_files_are_listed(self) -> None:
        body = self.client.get("/api/library").json()

        self.assertEqual([item["name"] for item in body], ["clip.mp4"])
        self.assertEqual(body[0]["size_bytes"], 10)

    def test_a_sidecar_is_not_listed(self) -> None:
        (self.output_dir / "clip description.txt").write_text("x", encoding="utf-8")

        names = [item["name"] for item in self.client.get("/api/library").json()]

        self.assertNotIn("clip description.txt", names)

    def test_a_legitimate_file_is_served_whole(self) -> None:
        response = self.client.get("/api/library/clip.mp4/file")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"0123456789")

    def test_a_ranged_request_is_honored(self) -> None:
        response = self.client.get("/api/library/clip.mp4/file", headers={"range": "bytes=2-5"})

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")

    def test_a_suffix_range_is_honored(self) -> None:
        response = self.client.get("/api/library/clip.mp4/file", headers={"range": "bytes=-3"})

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"789")

    def test_an_unsatisfiable_range_is_rejected(self) -> None:
        response = self.client.get("/api/library/clip.mp4/file", headers={"range": "bytes=999-"})

        self.assertEqual(response.status_code, 416)

    def test_a_traversal_id_cannot_escape_the_output_directory(self) -> None:
        for crafted in ("../secret.txt", "..%2Fsecret.txt", "a/../../secret.txt"):
            with self.subTest(id=crafted):
                response = self.client.get(f"/api/library/{crafted}/file")

                self.assertEqual(response.status_code, 404)
                self.assertNotIn(b"do not serve me", response.content)

    def test_an_absolute_path_is_rejected(self) -> None:
        response = self.client.get(f"/api/library/{self.secret.as_posix()}/file")

        self.assertEqual(response.status_code, 404)

    def test_a_windows_drive_prefix_is_rejected(self) -> None:
        response = self.client.get("/api/library/C:/Windows/win.ini/file")

        self.assertEqual(response.status_code, 404)

    def test_no_capture_artifact_is_ever_listed(self) -> None:
        # Captures can hold session cookies and must never be served.
        logs = self.output_dir / "capture.json"
        logs.write_text("{}", encoding="utf-8")

        names = [item["name"] for item in self.client.get("/api/library").json()]

        self.assertNotIn("capture.json", names)


class SettingsTests(_ApiFixture):
    def test_settings_are_readable(self) -> None:
        body = self.client.get("/api/settings").json()

        self.assertEqual(body["output_dir"], str(self.output_dir))
        self.assertGreaterEqual(body["concurrency"], 1)

    def test_concurrency_can_be_changed(self) -> None:
        body = self.client.put("/api/settings", json={"concurrency": 4}).json()

        self.assertEqual(body["concurrency"], 4)

    def test_a_nonsensical_concurrency_is_refused(self) -> None:
        response = self.client.put("/api/settings", json={"concurrency": 0})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["reason"], "bad_concurrency")

    def test_the_bind_host_is_not_editable_at_runtime(self) -> None:
        self.client.put("/api/settings", json={"host": "0.0.0.0"})

        self.assertEqual(self.client.get("/api/settings").json()["host"], "127.0.0.1")

    def test_a_change_is_written_to_the_configured_destination(self) -> None:
        self.client.put("/api/settings", json={"concurrency": 3})

        self.assertTrue(self.settings_file.exists())
        self.assertEqual(json.loads(self.settings_file.read_text())["concurrency"], 3)

    def test_the_state_directory_is_left_alone(self) -> None:
        # Regression: every run of this suite rewrote the real settings file with
        # the test's temporary output directory, so an operator was left pointing
        # at a path that no longer existed.
        #
        # The state directory is redirected rather than compared in place: reading
        # the developer's own file would make this pass or fail on whatever else
        # happens to be running, such as a server the operator left open.
        state = Path(self._temp.name) / "state"
        state.mkdir()

        with patch.dict(os.environ, {"FILMDOWNLOADER_STATE": str(state)}):
            self.client.put("/api/settings", json={"concurrency": 3})

            self.assertEqual(list(state.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
