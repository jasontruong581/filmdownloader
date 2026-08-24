"""Characterization tests for candidate merging across capture stages.

`merge_candidates` folds candidates found on the main page and on each embed
phase into one ordered map. Its field-promotion rules decide which stream a
download attempts first, so they are pinned here now that the function has
moved out of `cli.py` into the pipeline module.
"""

from __future__ import annotations

import unittest
from collections import OrderedDict

from videotrack.core.pipeline import merge_candidates
from videotrack.core.models import StreamCandidate


def _candidate(
    url: str = "https://cdn.example.test/a.mp4",
    *,
    kind: str = "mp4",
    score: int = 80,
    status_code: int | None = None,
    content_type: str | None = None,
    validation_note: str | None = None,
    referer: str | None = None,
) -> StreamCandidate:
    return StreamCandidate(
        url=url,
        kind=kind,
        score=score,
        source="ignored",
        status_code=status_code,
        content_type=content_type,
        validation_note=validation_note,
        referer=referer,
    )


class MergeInsertTests(unittest.TestCase):
    def test_new_url_is_inserted_with_the_stage_as_its_source(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()

        merge_candidates(merged, [_candidate()], "main")

        self.assertEqual(list(merged), ["https://cdn.example.test/a.mp4"])
        self.assertEqual(merged["https://cdn.example.test/a.mp4"].source, "main")

    def test_insertion_order_is_preserved(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()

        merge_candidates(
            merged,
            [_candidate("https://cdn.example.test/b.mp4"), _candidate("https://cdn.example.test/a.mp4")],
            "main",
        )

        self.assertEqual(list(merged), ["https://cdn.example.test/b.mp4", "https://cdn.example.test/a.mp4"])

    def test_the_incoming_candidate_is_cloned_not_aliased(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()
        incoming = _candidate()

        merge_candidates(merged, [incoming], "main")
        merged["https://cdn.example.test/a.mp4"].score = 1

        self.assertEqual(incoming.score, 80)


class MergeSourceTrackingTests(unittest.TestCase):
    def test_second_stage_appends_its_source(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()
        merge_candidates(merged, [_candidate()], "main")

        merge_candidates(merged, [_candidate()], "embed1_phase1")

        self.assertEqual(merged["https://cdn.example.test/a.mp4"].source, "main,embed1_phase1")

    def test_repeated_stage_is_not_appended_twice(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()
        merge_candidates(merged, [_candidate()], "main")

        merge_candidates(merged, [_candidate()], "main")

        self.assertEqual(merged["https://cdn.example.test/a.mp4"].source, "main")


class MergeFieldPromotionTests(unittest.TestCase):
    def test_higher_score_promotes_score_kind_and_referer(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()
        merge_candidates(merged, [_candidate(kind="mp4", score=80, referer="https://old.example.test/")], "main")

        merge_candidates(
            merged,
            [_candidate(kind="hls", score=100, referer="https://new.example.test/")],
            "embed1_phase1",
        )

        result = merged["https://cdn.example.test/a.mp4"]
        self.assertEqual((result.score, result.kind, result.referer), (100, "hls", "https://new.example.test/"))

    def test_lower_score_does_not_change_score_kind_or_referer(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()
        merge_candidates(merged, [_candidate(kind="hls", score=100, referer="https://old.example.test/")], "main")

        merge_candidates(
            merged,
            [_candidate(kind="mp4", score=80, referer="https://new.example.test/")],
            "embed1_phase1",
        )

        result = merged["https://cdn.example.test/a.mp4"]
        self.assertEqual((result.score, result.kind, result.referer), (100, "hls", "https://old.example.test/"))

    def test_equal_score_does_not_promote(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()
        merge_candidates(merged, [_candidate(kind="hls", score=80)], "main")

        merge_candidates(merged, [_candidate(kind="mp4", score=80)], "embed1_phase1")

        self.assertEqual(merged["https://cdn.example.test/a.mp4"].kind, "hls")

    def test_diagnostic_fields_are_promoted_regardless_of_score(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()
        merge_candidates(merged, [_candidate(score=100)], "main")

        merge_candidates(
            merged,
            [
                _candidate(
                    score=10,
                    status_code=200,
                    content_type="video/mp4",
                    validation_note="hls_precheck_ok",
                )
            ],
            "embed1_phase1",
        )

        result = merged["https://cdn.example.test/a.mp4"]
        self.assertEqual(result.score, 100)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content_type, "video/mp4")
        self.assertEqual(result.validation_note, "hls_precheck_ok")

    def test_absent_diagnostic_fields_do_not_clear_existing_values(self) -> None:
        merged: OrderedDict[str, StreamCandidate] = OrderedDict()
        merge_candidates(
            merged,
            [_candidate(status_code=200, content_type="video/mp4", validation_note="hls_precheck_ok")],
            "main",
        )

        merge_candidates(merged, [_candidate()], "embed1_phase1")

        result = merged["https://cdn.example.test/a.mp4"]
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content_type, "video/mp4")
        self.assertEqual(result.validation_note, "hls_precheck_ok")


if __name__ == "__main__":
    unittest.main()
