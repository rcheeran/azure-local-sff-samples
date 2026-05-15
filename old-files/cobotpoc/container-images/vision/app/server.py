"""FastAPI server -- vision-service.

Owns /dev/video0. The only process in the cluster that opens a V4L2 device.
Everything that needs pixels (browser previews, the orchestrator, future
debugging tools) consumes them over HTTP from this service. UVC cameras are
effectively single-streamer per node, so co-tenancy on /dev/videoN does not
work reliably -- centralizing the open here is the difference between
"sometimes works" and "always works".

Exposes:
  POST /v1/detect          multipart/form-data: image, target_type, target_text, annotate
  POST /v1/detect/live     same contract, but detect on the current captured frame
  GET  /v1/models          describe loaded models
  GET  /v1/camera/info     {device, name, width, height, frame_count, frame_age_s, ...}
  GET  /frame.jpg          latest captured frame as a single JPEG
  GET  /stream             multipart/x-mixed-replace MJPEG, for browsers
  GET  /                   tiny HTML wrapper that loads /stream in an <img>
  GET  /healthz            liveness
  GET  /readyz             readiness (200 once both models are loaded;
                           camera availability reported separately on /v1/camera/info)
"""

import base64
import glob
import logging
import os
import stat
import threading
import time
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from app.detectors.bowl import detectBowl
from app.detectors.cube import detectCube
from app.detectors.hand import detectHand
from app.detectors.misc import detectMisc


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
HAND_MODEL_PATH = os.environ.get("VISION_HAND_MODEL", "/app/models/hand_landmarker.task")
OWLVIT_MODEL_ID = os.environ.get("VISION_OWLVIT_MODEL_ID", "google/owlvit-base-patch32")
DEVICE = os.environ.get("VISION_DEVICE", "cpu")

# Camera config (was the camera-capture container; merged 2026-05-05 so vision
# is the only opener of /dev/video0). 0 = use camera default.
CAMERA_DEVICE_HINT = os.environ.get("VISION_CAMERA_DEVICE", "").strip()
CAMERA_WIDTH = int(os.environ.get("VISION_CAMERA_WIDTH", "0"))
CAMERA_HEIGHT = int(os.environ.get("VISION_CAMERA_HEIGHT", "0"))
CAMERA_FPS = float(os.environ.get("VISION_CAMERA_FPS", "30"))
CAMERA_JPEG_QUALITY = int(os.environ.get("VISION_CAMERA_JPEG_QUALITY", "75"))
CAMERA_STREAM_FPS = float(os.environ.get("VISION_CAMERA_STREAM_FPS", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vision-service")


# --------------------------------------------------------------------------- #
# Model state -- populated by warmup thread, read by request handlers.        #
# --------------------------------------------------------------------------- #
_HAND_LANDMARKER = None
_HAND_CONNECTIONS = None
_OWL_PROCESSOR = None
_OWL_MODEL = None
_READY = False
_WARMUP_ERROR: Optional[str] = None


def _warmup() -> None:
    """Load both models. Sets _READY=True when done. Runs in a daemon thread
    so the HTTP server starts immediately and /healthz responds even while
    OWL-ViT is still downloading."""
    global _HAND_LANDMARKER, _HAND_CONNECTIONS, _OWL_PROCESSOR, _OWL_MODEL
    global _READY, _WARMUP_ERROR
    try:
        log.info("Loading MediaPipe HandLandmarker from %s ...", HAND_MODEL_PATH)
        from mediapipe.tasks import python as _mpPython
        from mediapipe.tasks.python import vision as _mpVision

        baseOptions = _mpPython.BaseOptions(model_asset_path=HAND_MODEL_PATH)
        handOptions = _mpVision.HandLandmarkerOptions(
            base_options=baseOptions,
            num_hands=1,
            min_hand_detection_confidence=0.6,
            min_tracking_confidence=0.5,
            running_mode=_mpVision.RunningMode.IMAGE,
        )
        _HAND_LANDMARKER = _mpVision.HandLandmarker.create_from_options(handOptions)
        _HAND_CONNECTIONS = _mpVision.HandLandmarksConnections.HAND_CONNECTIONS
        log.info("HandLandmarker ready.")

        log.info("Loading OWL-ViT %s on %s ...", OWLVIT_MODEL_ID, DEVICE)
        from transformers import OwlViTForObjectDetection, OwlViTProcessor

        _OWL_PROCESSOR = OwlViTProcessor.from_pretrained(OWLVIT_MODEL_ID)
        owlModel = OwlViTForObjectDetection.from_pretrained(OWLVIT_MODEL_ID).to(DEVICE)
        owlModel.eval()
        _OWL_MODEL = owlModel
        log.info("OWL-ViT ready.")

        _READY = True
        log.info("vision-service warmup complete.")
    except Exception as exc:  # noqa: BLE001
        _WARMUP_ERROR = repr(exc)
        log.exception("warmup failed")


# --------------------------------------------------------------------------- #
# Camera capture state -- single-opener V4L2 loop. /v1/detect (multipart      #
# upload) does NOT depend on this; only /frame.jpg, /stream, /v1/detect/live  #
# do, so a missing camera does not break the upload-detect path.              #
# --------------------------------------------------------------------------- #
_FRAME_LOCK = threading.Lock()
_LATEST_BGR: Optional[np.ndarray] = None
_LATEST_JPEG: Optional[bytes] = None
_LATEST_FRAME_TS: float = 0.0
_FRAME_COUNT: int = 0

_CAMERA_DEVICE_PATH: Optional[str] = None
_CAMERA_NAME: Optional[str] = None
_CAMERA_OPEN_WIDTH: int = 0
_CAMERA_OPEN_HEIGHT: int = 0
_CAMERA_ERROR: Optional[str] = None
_CAMERA_RUNNING: bool = True


def _read_v4l2_name(device_path: str) -> Optional[str]:
    """Read the human-friendly V4L2 device name from sysfs.

    The Linux kernel exposes each `/dev/videoN` as `/sys/class/video4linux/videoN/name`,
    a single-line plain-text label like `"Logitech BRIO"` or `"usb camera: usb camera"`.
    Cleaner than parsing v4l2-ctl output and works in privileged containers because
    `/sys` is mounted read-only by default.
    """
    try:
        basename = os.path.basename(device_path)
        with open(f"/sys/class/video4linux/{basename}/name", "r") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _open_camera():
    """Find a /dev/video* node that actually returns frames.

    UVC cameras typically register a capture node + a metadata node; only
    the capture node yields frames. Probes each candidate, prefers the V4L2
    backend, falls back to the default (FFmpeg) backend, and accepts the
    first node from which cv2 can read a non-empty frame.
    """
    candidates = [CAMERA_DEVICE_HINT] if CAMERA_DEVICE_HINT else sorted(glob.glob("/dev/video*"))
    if not candidates:
        return None

    for dev in candidates:
        try:
            mode = os.stat(dev).st_mode
        except OSError as e:
            log.info("skip %s: stat failed (%s)", dev, e)
            continue
        if not stat.S_ISCHR(mode):
            log.info("skip %s: not a character device", dev)
            continue

        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(dev)
        if not cap.isOpened():
            log.info("skip %s: could not open", dev)
            cap.release()
            continue

        if CAMERA_WIDTH > 0:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        if CAMERA_HEIGHT > 0:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        if CAMERA_FPS > 0:
            cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        ok, frame = cap.read()
        if ok and frame is not None:
            log.info("using camera %s", dev)
            return dev, cap

        log.info("skip %s: opened but no frame (likely metadata node)", dev)
        cap.release()

    return None


def _capture_loop() -> None:
    """Own /dev/video* and continuously refresh _LATEST_BGR / _LATEST_JPEG.

    On open failure or repeated read failure the loop releases the device,
    sleeps, and reopens -- so a transient unplug/replug or a brief V4L2
    glitch does not kill the pod. /v1/detect (upload-based) is unaffected.
    """
    global _LATEST_BGR, _LATEST_JPEG, _LATEST_FRAME_TS, _FRAME_COUNT
    global _CAMERA_DEVICE_PATH, _CAMERA_NAME, _CAMERA_OPEN_WIDTH, _CAMERA_OPEN_HEIGHT, _CAMERA_ERROR

    while _CAMERA_RUNNING:
        result = _open_camera()
        if result is None:
            _CAMERA_ERROR = "no working /dev/video* found"
            log.error(_CAMERA_ERROR)
            time.sleep(2.0)
            continue

        dev, cap = result
        _CAMERA_DEVICE_PATH = dev
        _CAMERA_NAME = _read_v4l2_name(dev)
        _CAMERA_OPEN_WIDTH = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        _CAMERA_OPEN_HEIGHT = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        _CAMERA_ERROR = None
        log.info("camera %s (%s) opened: %dx%d", dev, _CAMERA_NAME or "unknown", _CAMERA_OPEN_WIDTH, _CAMERA_OPEN_HEIGHT)

        consecutive_failures = 0
        try:
            while _CAMERA_RUNNING:
                ok, frame = cap.read()
                if not ok or frame is None:
                    consecutive_failures += 1
                    if consecutive_failures >= 30:
                        log.error("camera read failing repeatedly, reopening")
                        break
                    time.sleep(0.01)
                    continue
                consecutive_failures = 0

                ok2, jpeg = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, CAMERA_JPEG_QUALITY]
                )
                if not ok2:
                    continue

                with _FRAME_LOCK:
                    _LATEST_BGR = frame
                    _LATEST_JPEG = jpeg.tobytes()
                    _LATEST_FRAME_TS = time.time()
                    _FRAME_COUNT += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("capture loop error: %s", exc)
            _CAMERA_ERROR = repr(exc)
        finally:
            try:
                cap.release()
            except Exception:
                pass

        time.sleep(1.0)  # backoff before reopen


# --------------------------------------------------------------------------- #
# FastAPI application                                                         #
# --------------------------------------------------------------------------- #
app = FastAPI(title="vision-service", version="1.0.0")


@app.on_event("startup")
def _on_startup() -> None:
    threading.Thread(target=_warmup, daemon=True, name="warmup").start()
    threading.Thread(target=_capture_loop, daemon=True, name="capture").start()


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/readyz")
def readyz():
    if _READY:
        return JSONResponse({"status": "ready"})
    return JSONResponse(
        {"status": "warming-up", "error": _WARMUP_ERROR},
        status_code=503,
    )


@app.get("/v1/models")
def models():
    return {
        "detectors": ["cube", "bowl", "hand", "misc"],
        "models": {
            "hand_landmarker": HAND_MODEL_PATH,
            "owlvit": OWLVIT_MODEL_ID,
        },
        "device": DEVICE,
        "ready": _READY,
    }


# --------------------------------------------------------------------------- #
# Camera routes                                                               #
# --------------------------------------------------------------------------- #
@app.get("/v1/camera/info")
def camera_info():
    age = time.time() - _LATEST_FRAME_TS if _LATEST_FRAME_TS > 0 else None
    return {
        "device": _CAMERA_DEVICE_PATH,
        "name": _CAMERA_NAME,
        "width": _CAMERA_OPEN_WIDTH,
        "height": _CAMERA_OPEN_HEIGHT,
        "frame_count": _FRAME_COUNT,
        "frame_age_s": age,
        "jpeg_quality": CAMERA_JPEG_QUALITY,
        "stream_fps": CAMERA_STREAM_FPS,
        "capture_error": _CAMERA_ERROR,
    }


@app.get("/frame.jpg")
def frame_jpg():
    with _FRAME_LOCK:
        jpeg = _LATEST_JPEG
    if jpeg is None:
        raise HTTPException(status_code=503, detail=_CAMERA_ERROR or "no frame yet")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


def _mjpeg_generator():
    """Yield each new frame exactly once as it arrives.

    Tracks the last published _FRAME_COUNT so identical frames aren't
    re-emitted; rate-limits to CAMERA_STREAM_FPS to bound CPU on slow viewers.
    """
    period = 1.0 / max(1.0, CAMERA_STREAM_FPS)
    last_count = -1
    while _CAMERA_RUNNING:
        with _FRAME_LOCK:
            jpeg = _LATEST_JPEG
            count = _FRAME_COUNT
        if jpeg is None or count == last_count:
            time.sleep(period / 2)
            continue
        last_count = count
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
            + jpeg + b"\r\n"
        )
        time.sleep(period)


@app.get("/stream")
def stream():
    if _LATEST_JPEG is None and _CAMERA_ERROR:
        raise HTTPException(status_code=503, detail=_CAMERA_ERROR)
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store",
            # X-Accel-Buffering: no asks nginx ingress to flush each chunk
            # immediately instead of buffering the whole response, which
            # would otherwise stall the live stream until EOF (never).
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/", response_class=HTMLResponse)
def root():
    return (
        "<html><body style='margin:0;background:#000'>"
        "<img src='/stream' style='width:100%;height:auto'>"
        "</body></html>"
    )


# --------------------------------------------------------------------------- #
# Detection                                                                   #
# --------------------------------------------------------------------------- #
def _decodeImage(payload: bytes) -> np.ndarray:
    arr = np.frombuffer(payload, dtype=np.uint8)
    if arr.size == 0:
        raise HTTPException(status_code=400, detail="empty image payload")
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise HTTPException(status_code=400, detail="cv2.imdecode failed; not a valid image")
    return bgr


def _encodeAnnotated(frame: np.ndarray) -> Optional[str]:
    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        return None
    return base64.b64encode(jpeg.tobytes()).decode("ascii")


def _runDetect(bgr: np.ndarray, target_type: str, target_text: str, annotate: bool):
    """Shared dispatch used by both /v1/detect (upload) and /v1/detect/live
    (current captured frame)."""
    if not _READY:
        raise HTTPException(status_code=503, detail="models not yet loaded; check /readyz")

    targetType = target_type.strip().lower()
    if targetType not in ("cube", "bowl", "hand", "misc"):
        raise HTTPException(status_code=400, detail=f"unknown target_type {target_type!r}")

    started = time.time()
    if targetType == "cube":
        pose, annotated, meta = detectCube(bgr, target_text)
    elif targetType == "bowl":
        pose, annotated, meta = detectBowl(bgr, target_text)
    elif targetType == "hand":
        pose, annotated, meta = detectHand(bgr, _HAND_LANDMARKER, _HAND_CONNECTIONS)
    else:  # misc
        pose, annotated, meta = detectMisc(bgr, target_text, _OWL_PROCESSOR, _OWL_MODEL)
    elapsedMs = int((time.time() - started) * 1000)

    return {
        "detected": pose is not None,
        "target_type": targetType,
        "target_text": target_text,
        "color_resolved": meta.get("color"),
        "samples": meta.get("samples", 1),
        "score": meta.get("score"),
        "latency_ms": elapsedMs,
        "pose": (
            None
            if pose is None
            else {"x": pose[0], "y": pose[1], "z": pose[2], "rz": pose[3]}
        ),
        "annotated_jpeg_b64": _encodeAnnotated(annotated) if annotate else None,
    }


@app.post("/v1/detect")
async def detect(
    image: UploadFile = File(...),
    target_type: str = Form(...),
    target_text: str = Form(""),
    annotate: bool = Form(True),
):
    payload = await image.read()
    bgr = _decodeImage(payload)
    return _runDetect(bgr, target_type, target_text, annotate)


@app.post("/v1/detect/live")
def detect_live(
    target_type: str = Form(...),
    target_text: str = Form(""),
    annotate: bool = Form(True),
):
    """Run detection on the most recently captured frame.

    Lets the orchestrator skip the round-trip of (GET /frame.jpg -> POST it
    back to /v1/detect). Returns 503 if the capture loop has not produced a
    frame yet.
    """
    with _FRAME_LOCK:
        bgr = None if _LATEST_BGR is None else _LATEST_BGR.copy()
    if bgr is None:
        raise HTTPException(status_code=503, detail=_CAMERA_ERROR or "no frame yet")
    return _runDetect(bgr, target_type, target_text, annotate)
