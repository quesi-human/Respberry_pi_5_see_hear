import unittest
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from fastapi import HTTPException
from pydantic import ValidationError

import app
from camera import CameraHub


class PipelineTests(unittest.TestCase):
    def test_camera_snapshot_waits_for_next_frame_after_command(self):
        hub = CameraHub()
        hub._publish(b"old-preview")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(hub, "start"), \
             patch.object(hub.condition, "wait", side_effect=lambda _: hub._publish(b"new-frame")):
            path = Path(directory) / "snapshot.jpg"
            hub.snapshot(path)
            self.assertEqual(path.read_bytes(), b"new-frame")

    def test_camera_status_does_not_open_device(self):
        with patch.object(app.camera, "start") as start:
            result = asyncio.run(app.camera_status())
        self.assertIn("ready", result)
        start.assert_not_called()

    def test_homepage_and_static_files_are_present(self):
        response = app.index()
        self.assertEqual(response.path, app.ROOT / "static/index.html")
        for file in ["index.html", "app.js", "style.css"]:
            self.assertTrue((app.ROOT / "static" / file).is_file())

    def test_result_url_cannot_return_a_different_commands_photo(self):
        with patch.object(app, "last_result_image", b"jpeg"), \
             patch.object(app, "last_result_id", "new-command"):
            self.assertEqual(app.result_image("new-command").body, b"jpeg")
            with self.assertRaises(HTTPException) as result:
                app.result_image("old-command")
            self.assertEqual(result.exception.status_code, 404)

    def test_camera_reads_jpegs_across_chunk_boundaries(self):
        hub = CameraHub()
        process = MagicMock()
        process.stdout.read1.side_effect = [b"noise\xff", b"\xd8first\xff", b"\xd9\xff\xd8second\xff\xd9", b""]
        process.poll.return_value = 0
        with patch("camera.subprocess.Popen", return_value=process) as popen, \
             patch.object(hub, "_publish") as publish:
            hub._read()
        self.assertEqual(popen.call_count, 1)
        self.assertEqual([call.args[0] for call in publish.call_args_list],
                         [b"\xff\xd8first\xff\xd9", b"\xff\xd8second\xff\xd9"])
        self.assertIsNotNone(hub.error)

    def test_camera_does_not_start_second_process_while_running(self):
        hub = CameraHub()
        hub.thread = MagicMock()
        hub.thread.is_alive.return_value = True
        with patch("camera.threading.Thread") as thread:
            hub.start()
        thread.assert_not_called()

    def test_preview_does_not_return_a_stale_frame(self):
        hub = CameraHub()
        hub._publish(b"jpeg")
        hub.updated -= 3
        self.assertIsNone(hub.latest()[1])
        self.assertFalse(hub.status()["ready"])

    def test_sph0645_records_native_pcm_then_converts_for_whisper(self):
        with patch.dict("os.environ", {"MIC_PROFILE": "sph0645", "MIC_DEVICE": "plughw:1,0"}), \
             patch.object(app, "run") as run:
            app.record_audio(Path("/tmp/speech.wav"), 5)
        record, convert = run.call_args_list
        self.assertIn("S32_LE", record.args[0])
        self.assertIn("48000", record.args[0])
        self.assertIn("plughw:1,0", record.args[0])
        self.assertIn("16000", convert.args[0])
        self.assertIn("pcm_s16le", convert.args[0])

    def test_target_and_region_are_both_applied(self):
        items = [{"label": "cup", "region": "left"},
                 {"label": "cup", "region": "right"},
                 {"label": "person", "region": "left"}]
        intent = app.Intent(action="count", target="cup", region="left")
        self.assertEqual(app.select_detections(items, intent), [items[0]])

    def test_detection_absence_does_not_assert_object_absence(self):
        intent = app.Intent(action="find", target="cup", region="all")
        self.assertEqual(app.answer(intent, []), "조건에 맞는 사물이 검출되지 않았습니다.")

    def test_failure_releases_command_lock(self):
        def fail():
            raise RuntimeError("microphone unavailable")
        with self.assertRaises(HTTPException) as result:
            app.guarded(fail)
        self.assertEqual(result.exception.status_code, 503)
        self.assertFalse(app.job_lock.locked())
        self.assertEqual(app.job_state["phase"], "idle")

    def test_overlapping_command_is_rejected(self):
        app.job_lock.acquire()
        try:
            with self.assertRaises(HTTPException) as result:
                app.guarded(lambda: None)
            self.assertEqual(result.exception.status_code, 409)
        finally:
            app.job_lock.release()

    def test_unsupported_command_does_not_capture_camera(self):
        model = type("Detector", (), {"names": {0: "cup"}})()
        intent = app.Intent(action="unsupported", target="all", region="all")
        with patch.object(app, "get_detector", return_value=model), \
             patch.object(app, "parse_intent", return_value=intent), \
             patch.object(app, "capture") as capture:
            response = app.execute("모터를 켜줘")
            self.assertEqual(response["intent"]["action"], "unsupported")
            capture.assert_not_called()

    def test_long_recording_rejected(self):
        with self.assertRaises(ValidationError):
            app.ListenCommand(seconds=60)


if __name__ == "__main__":
    unittest.main()
