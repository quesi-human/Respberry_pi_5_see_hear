"""On-demand voice and vision API for Raspberry Pi 5. No cloud services."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from camera import CameraHub

ROOT = Path(__file__).resolve().parent
camera = CameraHub()


@asynccontextmanager
async def lifespan(app):
    yield
    camera.close()


app = FastAPI(title="Pi voice vision", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
job_lock = threading.Lock()
detector = None
state_lock = threading.Lock()
job_state = {"phase": "idle", "started_at": None}
last_result_image = None
last_result_id = None


def set_phase(phase):
    with state_lock:
        job_state["phase"] = phase


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "static/index.html", headers={"Cache-Control": "no-cache"})


@app.get("/camera/status")
async def camera_status():
    return camera.status()


@app.get("/status")
async def status():
    with state_lock:
        job = dict(job_state)
    return {"job": job, "busy": job_lock.locked(), "camera": camera.status()}


@app.get("/video_feed", include_in_schema=False)
async def video_feed(request: Request):
    camera.start()

    async def frames():
        sequence = -1
        last_frame = time.monotonic()
        while not await request.is_disconnected():
            current, frame = camera.latest()
            if frame is not None and current != sequence:
                sequence = current
                last_frame = time.monotonic()
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                       str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
            if camera.status()["error"] or time.monotonic() - last_frame > 15:
                break
            await asyncio.sleep(0.1)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/result.jpg", include_in_schema=False)
def result_image(id: str):
    with state_lock:
        if last_result_image is None or id != last_result_id:
            raise HTTPException(404, "분석 이미지가 없거나 새 결과로 교체됐습니다.")
        image = last_result_image
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["count", "find", "describe", "unsupported"]
    target: str = Field(max_length=60, description="Exact English YOLO class or all")
    region: Literal["all", "left", "center", "right"]


class TextCommand(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ListenCommand(BaseModel):
    seconds: int = Field(default=5, ge=1, le=15)


def run(args, timeout=30):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"실행 파일이 없습니다: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{Path(str(args[0])).name}: {exc.stderr[-1500:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"실행 시간 초과: {args[0]}") from exc


def get_detector():
    global detector
    if detector is None:
        path = Path(os.getenv("YOLO_MODEL", str(ROOT / "models/yolo11n_ncnn_model")))
        if not path.exists():
            raise RuntimeError(f"YOLO 모델이 없습니다: {path}. scripts/setup.sh를 실행하세요.")
        # Lazy import: health and command validation do not load torch/YOLO.
        import torch
        from ultralytics import YOLO
        torch.set_num_threads(2)
        detector = YOLO(str(path), task="detect")
    return detector


def parse_intent(text, labels):
    prompt = (
        "한국어 카메라 명령을 JSON으로 변환하라. "
        "action: count(개수), find(위치), describe(보이는 사물), unsupported(기타). "
        "target은 다음 목록의 정확한 영어 클래스명 또는 all. "
        "지원하지 않는 사물, 색상/크기 조건, 장치 제어는 unsupported. "
        "지역은 region: all, left, center, right. 설명이나 추론 없이 JSON만 출력. "
        "클래스: " + ", ".join(labels)
    )
    with httpx.Client(timeout=180, trust_env=False) as client:
        response = client.post(os.getenv("QWEN_URL", "http://127.0.0.1:8081") +
                               "/v1/chat/completions", json={
            "model": "qwen", "temperature": 0, "max_tokens": 120,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": text + " /no_think"}],
            "response_format": {"type": "json_object", "schema": Intent.model_json_schema()},
        })
        response.raise_for_status()
    intent = Intent.model_validate_json(response.json()["choices"][0]["message"]["content"])
    if intent.target not in ["all", *labels]:
        intent.action = "unsupported"
    return intent


def capture(path):
    camera.snapshot(path)


def select_detections(items, intent):
    return [item for item in items
            if (intent.target == "all" or item["label"] == intent.target)
            and (intent.region == "all" or item["region"] == intent.region)]


def answer(intent, items):
    if not items:
        return "조건에 맞는 사물이 검출되지 않았습니다."
    if intent.action == "count":
        return f"조건에 맞는 사물이 {len(items)}개 검출되었습니다."
    counts = {}
    for item in items:
        key = item["label"] if intent.action == "describe" else item["label"] + " " + {
            "left": "왼쪽", "center": "가운데", "right": "오른쪽"}[item["region"]]
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{key} {count}개" for key, count in counts.items()) + " 검출되었습니다."


def execute(text):
    global last_result_image, last_result_id
    set_phase("loading")
    model = get_detector()
    names = model.names
    labels = list(names.values()) if isinstance(names, dict) else list(names)
    start = time.monotonic()
    set_phase("interpreting")
    intent = parse_intent(text, labels)
    parse_time = time.monotonic() - start
    if intent.action == "unsupported":
        return {"transcript": text, "intent": intent.model_dump(), "detections": [],
                "answer": "현재는 지원 사물의 개수·위치·목록을 확인할 수 있습니다.",
                "timings": {"intent_seconds": round(parse_time, 3)}}
    # Capture AFTER command parsing, never answer from a cached frame.
    with tempfile.TemporaryDirectory(prefix="pi-vision-") as directory:
        image = Path(directory) / "frame.jpg"
        start = time.monotonic()
        set_phase("detecting")
        capture(image)
        result = model.predict(source=str(image), imgsz=320, conf=0.4,
                               device="cpu", verbose=False)[0]
        items = []
        for box in result.boxes:
            xyxy = box.xyxy[0].tolist()
            center = (xyxy[0] + xyxy[2]) / (2 * result.orig_shape[1])
            items.append({"label": result.names[int(box.cls[0])],
                          "confidence": round(float(box.conf[0]), 3), "xyxy": xyxy,
                          "region": "left" if center < 1/3 else "right" if center > 2/3 else "center"})
        vision_time = time.monotonic() - start
        import cv2
        success, encoded = cv2.imencode(".jpg", result.plot(), [cv2.IMWRITE_JPEG_QUALITY, 80])
        image_id = uuid.uuid4().hex if success else None
        if success:
            with state_lock:
                last_result_image = encoded.tobytes()
                last_result_id = image_id
    selected = select_detections(items, intent)
    return {"transcript": text, "intent": intent.model_dump(), "detections": selected,
            "answer": answer(intent, selected),
            "snapshot_url": f"/result.jpg?id={image_id}" if image_id else None,
            "timings": {"intent_seconds": round(parse_time, 3),
                        "capture_detect_seconds": round(vision_time, 3)}}


def guarded(fn):
    if not job_lock.acquire(blocking=False):
        raise HTTPException(409, "다른 명령을 처리 중입니다.")
    try:
        with state_lock:
            job_state.update(phase="starting", started_at=time.time())
        return fn()
    except (RuntimeError, ValueError, KeyError, httpx.HTTPError) as exc:
        raise HTTPException(503, str(exc)) from exc
    finally:
        set_phase("idle")
        job_lock.release()


@app.get("/health")
def health():
    return {"status": "api_running", "busy": job_lock.locked(),
            "whisper_model": (ROOT / "models/ggml-base.bin").is_file(),
            "qwen_model": (ROOT / "models/Qwen3-1.7B-Q4_K_M.gguf").is_file(),
            "yolo_model": Path(os.getenv("YOLO_MODEL", str(ROOT / "models/yolo11n_ncnn_model"))).exists(),
            "note": "모델 파일 존재 여부입니다. 장치와 실제 추론 상태는 명령 실행으로 확인하세요."}


@app.post("/command")
def command(request: TextCommand):
    return guarded(lambda: execute(request.text))


def record_audio(path, seconds):
    """Capture native I2S PCM, then convert explicitly for Whisper."""
    sph0645 = os.getenv("MIC_PROFILE", "usb") == "sph0645"
    native = path.with_name("native.wav")
    run(["arecord", "-q", "-D", os.getenv("MIC_DEVICE", "default"), "-f",
         "S32_LE" if sph0645 else "S16_LE", "-r", "48000" if sph0645 else "16000",
         "-c", "1", "-d", str(seconds), str(native)], timeout=seconds + 10)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(native),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(path)])


def listen(seconds):
    with tempfile.TemporaryDirectory(prefix="pi-audio-") as directory:
        path = Path(directory) / "speech.wav"
        set_phase("recording")
        record_audio(path, seconds)
        start = time.monotonic()
        set_phase("transcribing")
        output = Path(directory) / "transcript"
        run([str(ROOT / "runtime/whisper.cpp/build/bin/whisper-cli"),
             "-m", str(ROOT / "models/ggml-base.bin"), "-f", str(path), "-l", "ko",
             "-t", "3", "-otxt", "-of", str(output), "-nt"], timeout=180)
        elapsed = time.monotonic() - start
        text = output.with_suffix(".txt").read_text().strip()
        if not text:
            raise ValueError("음성이 인식되지 않았습니다.")
        result = execute(text)
        result["timings"]["asr_seconds"] = round(elapsed, 3)
        result["timings"]["record_seconds"] = seconds
        return result


@app.post("/listen")
def listen_command(request: ListenCommand):
    return guarded(lambda: listen(request.seconds))
