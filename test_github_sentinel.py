"""Deterministic unit tests for GitHub Sentinel (CI/CD sentinel & PR radar)."""

from __future__ import annotations

import base64
from io import BytesIO
import json
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

import github_sentinel as gs


class GitHubSentinelTests(unittest.TestCase):
    def test_ci_icons_are_valid_gifs_on_120ms_beat(self) -> None:
        draw_fns = [
            (gs.draw_ci_pass_icon, 1),
            (gs.draw_ci_fail_icon, 2),
            (gs.draw_ci_gear_icon, 4),
            (lambda f, p: gs.draw_octocat_pr_icon(f, p, oldest_waiting_seconds=0), 1),
            (lambda f, p: gs.draw_octocat_pr_icon(f, p, oldest_waiting_seconds=30 * 3600), 1),
            (lambda f, p: gs.draw_octocat_pr_icon(f, p, oldest_waiting_seconds=50 * 3600), 2),
        ]
        for fn, frames in draw_fns:
            for animated in (True, False):
                with self.subTest(fn=fn, animated=animated):
                    data = gs.build_icon_gif(fn, frame_count=frames, animated=animated)
                    self.assertLessEqual(len(data), gs.ICON_MAX_BYTES)
                    gs.validate_icon_gif(data)
                    with Image.open(BytesIO(data)) as img:
                        self.assertEqual(img.size, (8, 8))
                        self.assertNotIn("transparency", img.info)
                        for idx in range(img.n_frames):
                            img.seek(idx)
                            dur = int(img.info.get("duration", 0))
                            if dur:
                                self.assertEqual(dur % gs.FRAME_BEAT_MS, 0)

    def test_ci_lane_has_black_gutter_and_correct_dimensions(self) -> None:
        run = gs.WorkflowRun(
            repo="owner/awtrix",
            run_number=42,
            status="completed",
            conclusion="success",
            branch="master",
        )
        frame = gs.render_ci_lane_frame(run)
        for y in range(gs.HEIGHT):
            self.assertEqual(frame[y][gs.GUTTER_X], gs.BLACK)

        b64 = gs.lane_bitmap_b64(frame)
        raw = base64.b64decode(b64)
        self.assertEqual(len(raw), gs.LANE_WIDTH * gs.LANE_HEIGHT * 3)

    def test_pr_lane_has_black_gutter_and_correct_dimensions(self) -> None:
        report = gs.PRRadarReport(
            repo="owner/awtrix",
            open_prs=3,
            review_requested=1,
            oldest_waiting_seconds=36 * 3600,
        )
        frame = gs.render_pr_lane_frame(report)
        for y in range(gs.HEIGHT):
            self.assertEqual(frame[y][gs.GUTTER_X], gs.BLACK)

        b64 = gs.lane_bitmap_b64(frame)
        raw = base64.b64decode(b64)
        self.assertEqual(len(raw), gs.LANE_WIDTH * gs.LANE_HEIGHT * 3)

    def test_build_ci_payload_safety_budget(self) -> None:
        for status, conclusion in (
            ("completed", "success"),
            ("completed", "failure"),
            ("in_progress", None),
        ):
            run = gs.WorkflowRun(
                repo="myorg/super-long-repo-name",
                run_number=999,
                status=status,
                conclusion=conclusion,
            )
            payload = gs.build_ci_payload(run, animated=True)
            self.assertEqual(payload["iconMode"], "fixed")
            self.assertEqual(payload["backgroundColor"], "#000000")
            serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.assertLess(len(serialized), gs.PAYLOAD_MAX_BYTES)

    def test_build_pr_payload_safety_budget(self) -> None:
        report = gs.PRRadarReport(
            repo="myorg/repo",
            open_prs=12,
            review_requested=3,
            oldest_waiting_seconds=60 * 3600,
        )
        payload = gs.build_pr_payload(report, animated=True)
        serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(serialized), gs.PAYLOAD_MAX_BYTES)

    def test_github_client_fetch_run_and_prs(self) -> None:
        client = gs.GitHubClient(token="mock-token")
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "workflow_runs": [
                    {
                        "run_number": 88,
                        "status": "completed",
                        "conclusion": "success",
                        "head_branch": "main",
                    }
                ]
            }
            mock_get.return_value = mock_resp

            run = client.fetch_latest_run("test/repo")
            self.assertIsNotNone(run)
            self.assertEqual(run.run_number, 88)
            self.assertEqual(run.conclusion, "success")

            # Test PRs
            mock_resp.json.return_value = [
                {"created_at": "2026-09-10T12:00:00Z"},
                {"created_at": "2026-09-08T12:00:00Z"},
            ]
            radar = client.fetch_pr_radar("test/repo")
            self.assertIsNotNone(radar)
            self.assertEqual(radar.open_prs, 2)
            self.assertGreater(radar.oldest_waiting_seconds, 0)

    def test_controller_tick_pushes_and_activates_on_failure(self) -> None:
        panel = MagicMock()
        client = MagicMock()
        client.fetch_latest_run.return_value = gs.WorkflowRun(
            repo="owner/repo",
            run_number=10,
            status="completed",
            conclusion="failure",
        )
        client.fetch_pr_radar.return_value = None

        clock = MagicMock()
        clock.monotonic.return_value = 100.0

        config = gs.GitHubConfig(
            enabled=True,
            repos=("owner/repo",),
            poll_interval_seconds=30.0,
        )

        controller = gs.GitHubSentinelController(
            panel=panel,
            config=config,
            client=client,
            clock=clock,
        )

        tick = controller.tick()
        self.assertTrue(tick.pushed)
        self.assertTrue(tick.has_failure)
        panel.push_app.assert_called_once()
        panel.activate_app.assert_called_once_with(gs.APP_NAME)


if __name__ == "__main__":
    unittest.main()
