"""
FastAPI server wrapping NVIDIA's Parakeet ASR model from HuggingFace.

Exposes an OpenAI-compatible REST surface so clients can switch between
Whisper-style transcription endpoints and this one with minimal changes:

    GET  /health                       -> liveness/readiness
    GET  /v1/models                    -> {"data":[{"id": "<model name>", ...}]}
    POST /v1/audio/transcriptions      -> multipart with `file=<audio>`,
                                          returns {"text": "..."}

Configuration via environment variables:
    PARAKEET_MODEL    HuggingFace model id (default: nvidia/parakeet-tdt-0.6b-v2)
    PARAKEET_DEVICE   "cuda" or "cpu" (default: cuda if available, else cpu)
    PARAKEET_PORT     uvicorn port (default: 8000)

Model loading happens once at startup; subsequent requests reuse the
in-memory model.
"""
import io
import os
import time
import logging
import numpy as np
import soundfile as sf
import torch

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("parakeet")

MODEL_NAME = os.environ.get("PARAKEET_MODEL", "nvidia/parakeet-tdt-0.6b-v2")
PARAKEET_DEVICE = os.environ.get(
    "PARAKEET_DEVICE",
    "cuda" if torch.cuda.is_available() else "cpu",
)
TARGET_SR = 16_000  # Parakeet operates on 16 kHz mono audio


def _load_model():
    """Load the NeMo ASR model once. Heavy: pulls weights from HF on first run."""
    log.info("Loading %s on %s ...", MODEL_NAME, PARAKEET_DEVICE)
    t0 = time.time()
    import nemo.collections.asr as nemo_asr  # imported here so import failures surface in logs

    model = nemo_asr.models.ASRModel.from_pretrained(MODEL_NAME)
    model = model.to(PARAKEET_DEVICE)
    model.eval()
    log.info("Model loaded in %.1fs", time.time() - t0)
    return model


MODEL = _load_model()
app = FastAPI(title="Parakeet ASR", version="1.0")


def _decode_audio(raw_bytes: bytes) -> np.ndarray:
    """Decode arbitrary audio bytes into a 16 kHz mono float32 numpy array."""
    try:
        audio, sr = sf.read(io.BytesIO(raw_bytes))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not decode audio: {e}")

    if audio.ndim > 1:
        # Mix down to mono by averaging channels.
        audio = audio.mean(axis=1)

    audio = audio.astype(np.float32, copy=False)

    if sr != TARGET_SR:
        # NeMo expects 16 kHz. Resample to 16 kHz with polyphase filtering.
        from scipy.signal import resample_poly
        audio = resample_poly(audio, TARGET_SR, sr).astype(np.float32, copy=False)

    return audio


def _extract_text(transcription_result) -> str:
    """NeMo's transcribe() return shape varies by version. Normalize to a string."""
    if isinstance(transcription_result, str):
        return transcription_result.strip()
    if hasattr(transcription_result, "text"):
        return str(transcription_result.text).strip()
    if isinstance(transcription_result, (list, tuple)) and transcription_result:
        return _extract_text(transcription_result[0])
    return str(transcription_result).strip()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "device": PARAKEET_DEVICE,
    }


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": 0,
                "owned_by": "nvidia",
            }
        ],
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    # `model` is accepted for OpenAI-API parity but ignored: this server only
    # serves the model named by PARAKEET_MODEL.
    model: str = Form(default=None),
    response_format: str = Form(default="json"),
    language: str = Form(default=None),
    temperature: float = Form(default=0.0),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file upload")

    audio = _decode_audio(raw)

    t0 = time.time()
    with torch.inference_mode():
        result = MODEL.transcribe([audio], verbose=False, batch_size=1)
    text = _extract_text(result)
    log.info("transcribed %d samples in %.2fs", len(audio), time.time() - t0)

    if response_format == "text":
        # OpenAI's `text` response_format returns plain text rather than JSON.
        return JSONResponse(content=text, media_type="text/plain")
    return {"text": text}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PARAKEET_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
