"""One camera process shared by previews and inference snapshots."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time


class CameraHub:
    def __init__(self):
        self.condition = threading.Condition()
        self.frame = None
        self.sequence = 0
        self.updated = 0.0
        self.error = None
        self.thread = None
        self.process = None
        self.stopped = False
        self.last_start = 0.0

    def start(self):
        with self.condition:
            if self.thread and self.thread.is_alive():
                return
            if time.monotonic() - self.last_start < 3:
                return
            self.last_start = time.monotonic()
            self.stopped = False
            self.frame = None
            self.error = None
            self.thread = threading.Thread(target=self._read, name="camera-reader", daemon=True)
            self.thread.start()

    @staticmethod
    def command():
        if os.getenv("CAMERA_KIND", "usb") == "csi":
            return ["rpicam-vid", "--nopreview", "--timeout", "0", "--width", "640",
                    "--height", "480", "--framerate", "8", "--codec", "mjpeg", "--output", "-"]
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2",
                "-video_size", "640x480", "-i", os.getenv("CAMERA_DEVICE", "/dev/video0"),
                "-an", "-vf", "fps=8", "-threads", "1", "-c:v", "mjpeg", "-q:v", "6",
                "-flush_packets", "1", "-f", "image2pipe", "pipe:1"]

    def _publish(self, frame):
        with self.condition:
            self.frame = frame
            self.sequence += 1
            self.updated = time.monotonic()
            self.error = None
            self.condition.notify_all()

    def _read(self):
        process = None
        try:
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=errors)
                with self.condition:
                    self.process = process
                    should_stop = self.stopped
                if should_stop:
                    return
                buffer = bytearray()
                while not self.stopped:
                    chunk = process.stdout.read1(65536)
                    if not chunk:
                        errors.seek(0)
                        raise RuntimeError(errors.read()[-1200:].decode(errors="replace") or
                                           "카메라 영상이 종료됐습니다.")
                    buffer.extend(chunk)
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        if start < 0:
                            buffer[:] = buffer[-1:]
                            break
                        end = buffer.find(b"\xff\xd9", start + 2)
                        if end < 0:
                            del buffer[:start]
                            break
                        self._publish(bytes(buffer[start:end + 2]))
                        del buffer[:end + 2]
                    if len(buffer) > 4 * 1024 * 1024:
                        raise RuntimeError("카메라 JPEG 프레임이 너무 큽니다.")
        except (OSError, RuntimeError) as exc:
            with self.condition:
                self.error = str(exc)
                self.condition.notify_all()
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
            with self.condition:
                self.process = None
                self.condition.notify_all()

    def latest(self):
        with self.condition:
            if self.frame is None or time.monotonic() - self.updated > 2:
                return self.sequence, None
            return self.sequence, self.frame

    def snapshot(self, path: Path, timeout=10):
        self.start()
        deadline = time.monotonic() + timeout
        with self.condition:
            sequence = self.sequence
            # Wait for a NEW frame, so inference never uses an old preview frame.
            while self.sequence <= sequence or self.frame is None:
                if self.stopped:
                    raise RuntimeError("카메라가 종료됐습니다.")
                if self.error:
                    raise RuntimeError(self.error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("카메라 응답 시간 초과. /camera/status를 확인하세요.")
                self.condition.wait(remaining)
            frame = self.frame
        path.write_bytes(frame)

    def status(self):
        with self.condition:
            age = time.monotonic() - self.updated if self.frame is not None else None
            return {"ready": age is not None and age < 2 and not self.error,
                    "error": self.error, "frame_age_seconds": round(age, 2) if age is not None else None,
                    "kind": os.getenv("CAMERA_KIND", "usb"),
                    "device": os.getenv("CAMERA_DEVICE", "/dev/video0")}

    def close(self):
        with self.condition:
            self.stopped = True
            process = self.process
            self.condition.notify_all()
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        if self.thread:
            self.thread.join(timeout=4)
