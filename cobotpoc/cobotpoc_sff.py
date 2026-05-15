from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import io
import os
import socket
import subprocess
import sys
import time
import threading
import json
import cv2
import numpy as np
import scipy as sp
import glob
import sounddevice as sd
import soundfile as sf
import requests
import urllib3
import paho.mqtt.client as mqtt
from urllib.parse import urlparse, urlunparse
from rich.console import Console
from rich.theme import Theme


# Foundry Local Qwen LLM:
LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "https://qwen-gpu.local/v1/chat/completions")
LLM_MODEL_ID = os.environ.get("LLM_MODEL_ID", "qwen2.5-1.5b-instruct-cuda-gpu:4")
LLM_VERIFY_TLS = os.environ.get("LLM_VERIFY_TLS", "false").lower() == "true"
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "15"))

# K8s lookup details for fetching the LLM API key automatically.
LLM_API_KEY_SECRET = os.environ.get("LLM_API_KEY_SECRET", "qwen2-5-1-5b-gpu-api-keys")
LLM_API_KEY_NAMESPACE = os.environ.get("LLM_API_KEY_NAMESPACE", "foundry-local-operator")
LLM_API_KEY_FIELD = os.environ.get("LLM_API_KEY_FIELD", "primary-key")

# Resolved bearer token after initialize-time probe (see end of file). Promoted
# to module scope so the LLM call doesn't NameError when LLM_API_KEY isn't in
# the environment.
LLM_API_KEY = None

# Remote ASR (speech-to-text) endpoint. The default points at the in-cluster
# Parakeet REST wrapper (nvidia/parakeet-tdt-0.6b-v2 -- see
# kubernetes_yamls/foundry_yamls/parakeet_gpu.yaml), but any OpenAI-compatible
# `/v1/audio/transcriptions` server (Whisper, Canary, Nemotron-Speech, ...)
# can be plugged in by overriding ASR_ENDPOINT.
ASR_ENDPOINT = os.environ.get(
    "ASR_ENDPOINT",
    "https://parakeet-gpu.local/v1/audio/transcriptions",
)
ASR_VERIFY_TLS = os.environ.get("ASR_VERIFY_TLS", "false").lower() == "true"
ASR_TIMEOUT = float(os.environ.get("ASR_TIMEOUT", "30"))
# Sample rate the server expects. 16 kHz is the de-facto standard for ASR
# models (Parakeet, Whisper, Canary all train at this rate).
# `makeSpeechCallback` already resamples mic audio to this rate.
ASR_SR = 16_000

# Remote vision-service endpoint. Owns /dev/video0 in the cluster, runs the
# four detectors (cube, bowl, hand, misc) extracted from this file, and
# exposes a live MJPEG preview at the same host. See
# `container-images/vision/` and `kubernetes_yamls/vision_yamls/`. The
# /v1/detect/live endpoint runs detection on the most-recently-captured
# frame -- no upload, no local camera open.
VISION_ENDPOINT = os.environ.get(
    "VISION_ENDPOINT",
    "https://vision.local/v1/detect/live",
)
VISION_VERIFY_TLS = os.environ.get("VISION_VERIFY_TLS", "false").lower() == "true"
VISION_TIMEOUT = float(os.environ.get("VISION_TIMEOUT", "10"))

# AIO configurations for MQTT broker connection. 
# AIO MQTT broker (`aio-lb-broker` in the cluster's `azure-iot-operations`
# namespace). Plain TCP, no TLS, no auth. Same defaults the robot-metrics
# sample publisher uses.
MQTT_BROKER  = os.environ.get("MQTT_BROKER", "192.168.1.197")
MQTT_PORT    = int(os.environ.get("MQTT_PORT", "11000"))
MQTT_QOS     = int(os.environ.get("MQTT_QOS", "0"))
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", f"cobotpoc-{os.getpid()}")

# Lazy-initialised module-level client + lock so concurrent callers don't
# race to connect. publishMqttMessage() is the only public surface.
_MQTT_CLIENT = None
_MQTT_LOCK = threading.Lock()
SEQUENCE = 0


def _getMqttClient():
    """Return a connected paho-mqtt client, opening the connection lazily.

    Reuses a single TCP session for the lifetime of the process; the client's
    background network loop (`loop_start`) handles reconnects automatically
    if the broker drops the link.
    """
    global _MQTT_CLIENT
    with _MQTT_LOCK:
        if _MQTT_CLIENT is not None:
            return _MQTT_CLIENT
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            protocol=mqtt.MQTTv5,
        )
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=30)
        client.loop_start()
        _MQTT_CLIENT = client
        return _MQTT_CLIENT


def publishMqttMessage(topic, messageBody):
    """Publish one MQTT message to the AIO broker.

    Args:
        topic: MQTT topic string, e.g. "robot/coords".
        messageBody: payload to publish. Dicts/lists are JSON-encoded
            automatically; strings and bytes are sent as-is.

    Returns the paho-mqtt MQTTMessageInfo on success, or None on error
    (so the caller can ignore the result if they just want fire-and-forget).
    """
    global SEQUENCE
    SEQUENCE += 1
    if isinstance(messageBody, (dict, list)):
        messageBody = json.dumps(messageBody, separators=(",", ":"))
    try:
        client = _getMqttClient()
        info = client.publish(
            topic,
            messageBody,
            qos=MQTT_QOS,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"MQTT publish to {topic} failed: rc={info.rc}", file=sys.stderr)
            return None
        return info
    except (socket.gaierror, ConnectionRefusedError, OSError) as e:
        print(
            f"MQTT publish to {topic} failed: could not reach "
            f"{MQTT_BROKER}:{MQTT_PORT}: {e}",
            file=sys.stderr,
        )
        return None


def fetchLlmApiKey():
    """Resolve the LLM bearer token.

    Order of precedence:
      1. LLM_API_KEY env var (if set and non-empty).
      2. `kubectl get secret <LLM_API_KEY_SECRET> -n <LLM_API_KEY_NAMESPACE>
            -o jsonpath='{.data.<LLM_API_KEY_FIELD>}' | base64 -d`
    """
    key = os.environ.get("LLM_API_KEY", "").strip()
    if key:
        return key

    cmd = [
        "kubectl", "get", "secret", LLM_API_KEY_SECRET,
        "-n", LLM_API_KEY_NAMESPACE,
        "-o", f"jsonpath={{.data.{LLM_API_KEY_FIELD}}}",
    ]
    try:
        b64 = subprocess.check_output(cmd, stderr=subprocess.PIPE, text=True).strip()
    except FileNotFoundError as e:
        raise RuntimeError("kubectl not found on PATH; cannot fetch LLM API key") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to read secret {LLM_API_KEY_NAMESPACE}/{LLM_API_KEY_SECRET}: "
            f"{e.stderr.strip() or e}"
        ) from e

    if not b64:
        raise RuntimeError(
            f"Secret {LLM_API_KEY_NAMESPACE}/{LLM_API_KEY_SECRET} has no field "
            f"'{LLM_API_KEY_FIELD}'"
        )
    return base64.b64decode(b64).decode().strip()


PURPLE = "#D388FF"
BLUE = "#52BBFF"
RICH = Console(
    theme = Theme(
        {
            # JSON pretty-printer (used by RICH.print_json).
            "json.brace": f"bold {BLUE}",
            "json.key": f"bold {PURPLE}",
            "json.str": f"bold white",
            # repr-highlighter styles (applied automatically by RICH.print to
            # any plain string that looks like Python repr output: numbers,
            # quoted strings, booleans, None, tuples, paths, URLs, etc.).
            # We mute most of them to plain white so colored fields are
            # intentional, and keep True / False highlighted green / red so
            # boolean status flags pop visually.
            "repr.bool_true": "bold green",
            "repr.bool_false": "bold red",
            "repr.none": "white",
            "repr.number": "white",
            "repr.str": "white",
            "repr.path": "white",
            "repr.filename": "white",
            "repr.url": "white",
            "repr.uuid": "white",
            "repr.brace": "white",
            "repr.comma": "white",
            "repr.attrib_name": "white",
            "repr.attrib_value": "white",
            "repr.attrib_equal": "white",
            "repr.tag_name": "white",
            "repr.tag_contents": "white",
            "repr.ipv4": "white",
            "repr.ipv6": "white",
            "repr.call": "white",
        }
    )
)

# ---------- Vision preview ----------
# The vision-service container owns /dev/video0 and serves a live MJPEG
# preview at https://vision.local/ (or http://localhost:8090/ via
# `kubectl -n vision port-forward svc/vision-service 8090:80`). No local
# preview server is run here; this process is a pure HTTP client of the
# vision-service contract.

@contextmanager
def printVerbosely(codeBlockDescription):
    """Print to console before/after block, report outcome and duration"""
    RICH.print(f"[bold white]{codeBlockDescription}...[/bold white]")
    start = time.time()
    success = True

    try:
        yield
    except Exception:
        success = False
        raise

    finally:
        elapsed = time.time() - start
        if success:
            RICH.print(f"[bold green]✔ {codeBlockDescription} succeeded in {elapsed:.2f}s[/bold green]")
        else:
            RICH.print(f"[bold red]✗ {codeBlockDescription} failed after {elapsed:.2f}s[/bold red]")

###########################
### MICROPHONE & SPEECH ###
###########################

def listMicrophones():
    """Return available microphones as dictionaries of index, name, channels, default_samplerate"""
    devices = sd.query_devices()

    if not devices:
        return None

    allMicrophones = []

    for index, device in enumerate(devices):
        if device["max_input_channels"] <= 0:
            continue
        if device["max_input_channels"] > 8:
            continue
        if not device.get("default_samplerate"):
            continue
        allMicrophones.append({
            "Name": device["name"],
            "Index": index,
            "Channels": device["max_input_channels"],
            "SampleRate": device["default_samplerate"]
        })

    return allMicrophones or None

def selectBestMicrophone(allMicrophones):
    """Given list of microphones, select one, preferring USB and/or single-channel"""

    if not allMicrophones:
        return None

    usbMics = [mic for mic in allMicrophones if "usb" in mic["Name"].lower()]
    if usbMics:
        return usbMics[0]

    monoMics = [mic for mic in allMicrophones if mic["Channels"] == 1]
    if monoMics:
        return monoMics[0]

    return allMicrophones[0]

def makeSpeechCallback(inputSampleRate, targetSampleRate=16000, minTalkingThreshold=5, maxSilenceThreshold=5, quietThreshold=0.01):
    """Build a non-blocking microphone callback that segments utterances by speech followed by silence."""
    audioBuffer = []
    silenceCount = 0
    talkingCount = 0

    def callback(indata, frames, callback_time, status):
        global newestUtterance
        nonlocal audioBuffer, silenceCount, talkingCount

        def isTooQuiet(chunk, threshold=quietThreshold):
            rms = np.sqrt(np.mean(chunk ** 2))
            return rms < threshold

        if status:
            return

        audioChunk = indata[:, 0].astype(np.float32)
        audioChunkResampled = sp.signal.resample_poly(audioChunk, up=targetSampleRate, down=inputSampleRate).astype(np.float32)
        audioBuffer.append(audioChunkResampled)

        if isTooQuiet(audioChunkResampled):
            silenceCount += 1
        else:
            talkingCount += 1
            silenceCount = 0

        if silenceCount < maxSilenceThreshold:
            return

        if talkingCount >= minTalkingThreshold and len(audioBuffer) >= (minTalkingThreshold + maxSilenceThreshold):
            newestUtterance = np.concatenate(audioBuffer, axis=0)

        audioBuffer = []
        silenceCount = 0
        talkingCount = 0

    return callback

def transcribeSpeech(audio):
    """Transcribe one utterance audio buffer into text via the remote ASR endpoint.

    `audio` is a 1-D float32 numpy array sampled at ASR_SR (16 kHz). It's
    encoded in-memory as a 16-bit PCM WAV and uploaded as multipart form data.

    Prints timing for the encode step and the network round-trip so it's easy
    to see whether latency is on the client (encoding) or the server (model).
    """
    if audio is None or len(audio) == 0:
        return ""

    encodeStart = time.time()
    buf = io.BytesIO()
    sf.write(buf, audio, ASR_SR, format="WAV", subtype="PCM_16")
    buf.seek(0)
    wavBytes = buf.getbuffer().nbytes
    encodeMs = (time.time() - encodeStart) * 1000.0
    audioSeconds = len(audio) / float(ASR_SR)

    files = {"file": ("utterance.wav", buf, "audio/wav")}
    netStart = time.time()
    try:
        resp = requests.post(
            ASR_ENDPOINT,
            files=files,
            verify=ASR_VERIFY_TLS,
            timeout=ASR_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:
        netMs = (time.time() - netStart) * 1000.0
        print(
            f"ASR call failed after {netMs:.0f}ms "
            f"(audio={audioSeconds:.2f}s, encode={encodeMs:.0f}ms): {e}"
        )
        return ""
    netMs = (time.time() - netStart) * 1000.0
    text = (resp.json().get("text", "") or "").strip()
    print(
        f"ASR timing: audio={audioSeconds:.2f}s "
        f"wav={wavBytes/1024:.1f}KiB encode={encodeMs:.0f}ms "
        f"net+infer={netMs:.0f}ms"
    )
    return text

def startSpeechPipelineWorker():
    """Start a background worker that transcribes utterances and parses them into robot commands."""
    stopEvent = threading.Event()

    def worker():
        global newestUtterance
        global newestToolCall

        while not stopEvent.is_set():
            utteranceAudio = newestUtterance
            newestUtterance = None

            if utteranceAudio is None:
                time.sleep(0.05)
                continue

            userText = transcribeSpeech(utteranceAudio)
            if not userText:
                continue

            toolCallJSON = interpretWithLLM(userText)
            RICH.print() # Empty line
            RICH.print(f'[bold {PURPLE}]HEARD:[/bold {PURPLE}] [bold white]"{userText}"[/bold white]')

            newestToolCall = {
                "userText": userText,
                "toolCallJSON": toolCallJSON,
            }

    workerThread = threading.Thread(target=worker, name="speech-pipeline", daemon=True)
    workerThread.start()
    return stopEvent, workerThread

#######################
### CAMERA & VISION ###
#######################
#
# This file no longer opens /dev/video0 or runs detectors locally. The
# vision-service container (see container-images/vision/, namespace `vision`)
# owns the V4L2 device, runs MediaPipe HandLandmarker + OWL-ViT + the HSV
# detectors, and serves a live MJPEG preview on the same host.
#
# Robot-specific coordinate logic (camera->robot homography, safe-zone gate)
# stays here -- those numbers are calibrated per physical setup, not per
# detector.

def getTargetCoordsFromVision(target):
    """Given {targetText, targetType}, call vision-service and return safe
    robot-frame coords, or None.

    Pipeline:
      1. POST {target_type, target_text} to VISION_ENDPOINT (default
         https://vision.local/v1/detect/live), which runs detection on the
         most recently captured frame in the cluster.
      2. Map the camera-frame pixel coords back to robot-frame mm via the
         calibrated homography below.
      3. Reject anything outside the safe reachable workspace.
    """

    def transformCoordsFromCameraToRobot(coords):
        """Transform coordinates from camera frame to robot base frame using homography and various offsets."""
        if coords is None:
            return None
        inputX, inputY, inputZ, inputRz = coords
        # Position: compute homography using values from calibrate.py
        robotCorners = np.array(
            [
                [-50, 200],
                [200, 200],
                [200, -200],
                [-50, -200],
            ],
            dtype=np.float32,
        )
        cameraCorners = np.array(
            [
                [486, 62],
                [485, 298],
                [116, 306],
                [104, 73],
            ],
            dtype=np.float32,
        )
        homography = cv2.getPerspectiveTransform(cameraCorners, robotCorners)
        point = np.array([[[float(inputX), float(inputY)]]], dtype=np.float32)
        mapped = cv2.perspectiveTransform(point, homography)
        outputX, outputY = float(mapped[0, 0, 0]), float(mapped[0, 0, 1])
        # Angle: shift 45 degrees, measured empirically
        outputRz = -(inputRz - 45)
        # Offset: necessary because gripper head is about 1cm ahead of the wrist rotation axis (ugh!)
        outputX += -10 * np.sin(np.deg2rad(outputRz + 45))
        outputY += 10 * np.cos(np.deg2rad(outputRz + 45))
        # Do nothing with Z, just leave 0
        outputZ = inputZ
        # Return
        return outputX, outputY, outputZ, outputRz

    def verifyCoordsAreSafe(coords):
        """Verify coordinates are within safe reachable area: between 100 mm and 280 mm distance from base, and x >= -100 mm."""
        if coords is None:
            return False
        inputX, inputY = coords[0], coords[1]
        if inputX < -100:
            return False
        distance = (inputX ** 2 + inputY ** 2) ** 0.5
        if distance < 100 or distance > 280:
            return False
        return True

    if not isinstance(target, dict):
        return None

    targetText = target.get("targetText", "")
    targetType = (target.get("targetType") or "misc").lower()
    # The /v1/detect/live route accepts cube|bowl|hand|misc.
    if targetType not in ("cube", "bowl", "hand", "misc"):
        targetType = "misc"

    # 1. Ask vision-service to run the relevant detector on the most recent
    #    captured frame. annotate=false skips the base64 JPEG round-trip --
    #    the live MJPEG preview is on the same service for human viewing.
    cameraCoords = None
    netStart = time.time()
    try:
        response = requests.post(
            VISION_ENDPOINT,
            data={
                "target_type": targetType,
                "target_text": targetText,
                "annotate": "false",
            },
            verify=VISION_VERIFY_TLS,
            timeout=VISION_TIMEOUT,
        )
        response.raise_for_status()
        netMs = (time.time() - netStart) * 1000.0
        body = response.json()
        if body.get("detected") and body.get("pose"):
            pose = body["pose"]
            cameraCoords = (
                int(pose["x"]),
                int(pose["y"]),
                int(pose.get("z", 0)),
                int(pose["rz"]),
            )
            RICH.print(
                f"[bold {BLUE}]vision[/bold {BLUE}] "
                f"type={targetType} text={targetText!r} "
                f"detected={body.get('detected')} "
                f"server_latency_ms={body.get('latency_ms')} "
                f"net+infer_ms={netMs:.0f} "
                f"pose={cameraCoords}"
        )
        else:
            RICH.print(
                f"[bold {BLUE}]vision[/bold {BLUE}] "
                f"type={targetType} text={targetText!r} "
                f"detected={body.get('detected')} "
                f"server_latency_ms={body.get('latency_ms')} "
                f"net+infer_ms={netMs:.0f}"
            )
    except requests.RequestException as exc:
        netMs = (time.time() - netStart) * 1000.0
        RICH.print(
            f"[bold red]vision-service request failed after {netMs:.0f}ms: {exc}[/bold red]"
        )
        return None

    # 2. Camera -> robot.
    robotCoords = transformCoordsFromCameraToRobot(cameraCoords)
    safe = verifyCoordsAreSafe(robotCoords)
    # send a message to AIO
    if cameraCoords is not None and robotCoords is not None:
        publishMqttMessage("robot/coords", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "leader_id": "mycobot280",
            "sequence":  SEQUENCE,
            "coords": {
                "inputX":  round(float(robotCoords[0]), 2),
                "inputY":  round(float(robotCoords[1]), 2),
                "inputZ":  round(float(robotCoords[2]), 2),
                "inputRz": round(float(robotCoords[3]), 2),
                "safe":    safe,
            },
            "period_s":  300.0,
        })
        RICH.print(
            f"[bold {BLUE}]vision[/bold {BLUE}] robot(mm): "
            f"x={robotCoords[0]:.1f} y={robotCoords[1]:.1f} rz={robotCoords[3]:.1f} "
            f"safe={'YES' if safe else 'NO'}"
        )

    if not safe:
        return None
    return robotCoords

############################
### LANGUAGE & REASONING ###
############################

def interpretWithLLM(userText):
    EXTRACT_TOOL_CALL_SYSTEM_TEXT = (
        "Classify the command into one robot toolCall. "
        "Output JSON only with exactly one field: {\"tool\":\"<TOOL>\"}. "
        "Allowed tool values are exactly: pick, place, pickAndPlace, stop, none. "
        "Ignore filler words like hey, oh, uh, please, can you, and would you. "
        "Definitions: pick=pick object only. place=place object already held. pickAndPlace=pick object and place it in one command. stop=wait/stop/pause/hold. none=not a robot manipulation request. "
        "Decision rule 1: if command has both a concrete object phrase and a concrete destination phrase, output pickAndPlace. "
        "Decision rule 2: if object is only a pronoun (it/that/this) or explicitly already held, output place. "
        "Decision rule 3: person handoff phrases (give it to me, hand it to me, gimme, pass it to me, to me, my hand) are place. "
        "Decision rule 4: revision words (instead, actually, no wait, I changed my mind) do not change the action class by themselves; classify by the final requested action. "
        "Use none only when there is no meaningful robot manipulation action. "
        "Example: 'pick up the red cube' -> {\"tool\":\"pick\"}. "
        "Example: 'put it in the bowl' -> {\"tool\":\"place\"}. "
        "Example: 'give it to me' -> {\"tool\":\"place\"}. "
        "Example: 'give it to me instead' -> {\"tool\":\"place\"}. "
        "Example: 'ok please put the green cube in the green bowl' -> {\"tool\":\"pickAndPlace\"}. "
        "Example: 'please put the blue block in the red bowl' -> {\"tool\":\"pickAndPlace\"}. "
        "Example: 'move the cube to the plate' -> {\"tool\":\"pickAndPlace\"}. "
        "Example: 'actually wait' -> {\"tool\":\"stop\"}. "
        "Example: 'what time is it' -> {\"tool\":\"none\"}."
    )
    EXTRACT_PICK_SYSTEM_TEXT = (
        "The user's sentence indicates a PICK action. "
        "Your task is ONLY to extract WHAT OBJECT the robot should pick up. "
        "Do not extract the destination. Extract only the object being moved. "
        "Output JSON only with exactly two fields: {\"targetText\":\"<TEXT>\",\"targetType\":\"<TYPE>\"}. "
        "targetText must be the object words from the user sentence, including color and descriptors when present. "
        "targetType must be exactly one of: cube, bowl, hand, misc. "
        "Use cube for pickable objects like cube, block, brick, square, box, brick. "
        "Use bowl for pickable containers like bowl, plate, cup, dish, container. "
        "Use hand only if the object to pick is a hand (rare, for hand detection). "
        "Use misc only if none of cube, bowl, or hand apply to the object phrase. "
        "Critical rule: when both source object and destination appear, return the source object only. "
        "Example: 'pick up the red cube' -> {\"targetText\":\"red cube\",\"targetType\":\"cube\"}. "
        "Example: 'grab the yellow block and move it to the table' -> {\"targetText\":\"yellow block\",\"targetType\":\"cube\"}. "
        "Example: 'pick up the blue bowl' -> {\"targetText\":\"blue bowl\",\"targetType\":\"bowl\"}. "
        "Example: 'get the green container' -> {\"targetText\":\"green container\",\"targetType\":\"bowl\"}."
    )
    EXTRACT_PLACE_SYSTEM_TEXT = (
        "The user's sentence indicates a PLACE action. "
        "Your task is ONLY to extract WHERE the object should be dropped into or onto. "
        "Do not extract the object being moved. Extract only the destination phrase. "
        "Output JSON only with exactly two fields: {\"targetText\":\"<TEXT>\",\"targetType\":\"<TYPE>\"}. "
        "targetText must be the destination words from the user sentence, including color and descriptors when present. "
        "targetType must be exactly one of: cube, bowl, hand, misc. "
        "Use bowl for destination containers/surfaces like bowl, plate, cup, container, dish, bin, tray. "
        "Use hand only when destination is a person handoff (me/my hand/hand it to me/give it to me/pass it to me). "
        "Use cube only if the destination itself is another cube/block/box object. "
        "Use misc only if none of cube, bowl, or hand apply to the destination phrase. "
        "Critical rule: when both source object and destination appear, return the destination only. "
        "Example: 'put the yellow cube in the green bowl' -> {\"targetText\":\"green bowl\",\"targetType\":\"bowl\"}. "
        "Example: 'drop the red block onto the blue box' -> {\"targetText\":\"blue box\",\"targetType\":\"cube\"}. "
        "Example: 'place it in the tray' -> {\"targetText\":\"tray\",\"targetType\":\"bowl\"}. "
        "Example: 'give it to me' -> {\"targetText\":\"me\",\"targetType\":\"hand\"}. "
        "Example: 'hand that to my colleague' -> {\"targetText\":\"my colleague\",\"targetType\":\"hand\"}."
    )

    def invokeLLM(systemText, maxNewTokens=500):
        """Send one chat prompt to the remote LLM endpoint and return its text response.

        Uses the OpenAI-compatible `/v1/chat/completions` API exposed by the
        Foundry Local Qwen deployment. On any error (HTTP, JSON, missing
        field), returns an empty string so the calling JSON extractor falls
        back to the default 'none' tool call instead of crashing the worker.
        """
        payload = {
            "model": LLM_MODEL_ID,
            "messages": [
                {"role": "system", "content": systemText},
                {"role": "user", "content": f"User command: {userText}"},
            ],
            "max_tokens": maxNewTokens,
            "temperature": 0.3,
        }
        netStart = time.time()
        try:
            resp = requests.post(
                LLM_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                verify=LLM_VERIFY_TLS,
                timeout=LLM_TIMEOUT,
            )
            resp.raise_for_status()
            netMs = (time.time() - netStart) * 1000.0
            body = resp.json()
            usage = body.get("usage") or {}
            content = body["choices"][0]["message"]["content"].strip()
            print(
                f"LLM timing: max_tokens={maxNewTokens} "
                f"prompt_tokens={usage.get('prompt_tokens')} "
                f"completion_tokens={usage.get('completion_tokens')} "
                f"net+infer_ms={netMs:.0f}"
            )
            return content
        except Exception as e:
            netMs = (time.time() - netStart) * 1000.0
            print(f"LLM call failed after {netMs:.0f}ms ({e}); returning empty response.")
            return ""

    def extractJson(rawResponse):
        """Extract and parse the first JSON object span from LLM text output, trimming any surrounding fluff before and after."""
        start = rawResponse.find("{")
        end = rawResponse.rfind("}")
        if start == -1 or end == -1 or end < start:
            return {}
        try:
            parsedResponse = json.loads(rawResponse[start:end + 1])
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsedResponse, dict):
            return {}

        return parsedResponse

    # Note: possible toolCalls are 'pick', 'place', 'pickAndPlace', 'stop', 'none'
    toolCall = extractJson(invokeLLM(EXTRACT_TOOL_CALL_SYSTEM_TEXT, maxNewTokens=20)).get("tool", "none")

    if toolCall == "none":
        return {
            "toolCall": "none"
        }

    if toolCall == "stop":
        return {
            "toolCall": "stop"
        }

    if toolCall == "pick":
        return {
            "toolCall": "pick",
            "pickTarget": extractJson(invokeLLM(EXTRACT_PICK_SYSTEM_TEXT)),
        }

    if toolCall == "place":
        return {
            "toolCall": "place",
            "placeTarget": extractJson(invokeLLM(EXTRACT_PLACE_SYSTEM_TEXT)),
        }

    if toolCall == "pickAndPlace":
        return {
            "toolCall": "pickAndPlace",
            "pickTarget": extractJson(invokeLLM(EXTRACT_PICK_SYSTEM_TEXT)),
            "placeTarget": extractJson(invokeLLM(EXTRACT_PLACE_SYSTEM_TEXT)),
        }

    return {
        "toolCall": "none"
    }

############################
### ROBOT MOTION CONTROL ###
############################

def robotPlanMovementQueue(toolCallJSON):
    """Convert parsed tool call into a fresh ordered queue of robot movements"""
    global robotMovementQueue
    global robotCurrentMovement

    toolCall = toolCallJSON.get("toolCall", "none")

    # For 'none', keep current execution state untouched
    if toolCall == "none":
        return

    # For actionable calls, reset current movement and rebuild queue
    robotCurrentMovement = None
    robotMovementQueue = []

    if toolCall == "stop":
        robotMovementQueue.append({"movement": "stop"})
        return

    if toolCall == "pick":
        pickTarget = toolCallJSON.get("pickTarget")
        robotMovementQueue.extend(
            [
                {"movement": "moveGripperToValue", "value": 100}, # Ensure gripper is open
                {"movement": "moveToReadyPosition"}, # Lean back to maximize camera view of the work area
                {"movement": "movetoAboveTarget", "target": pickTarget}, # Move above the pick target
                {"movement": "moveToHeight", "height": 105}, # Descend to pick height
                {"movement": "moveGripperToValue", "value": 40}, # Grasp object
                {"movement": "moveToHeight", "height": 225}, # Ascend back to safe height
            ]
        )
        return

    if toolCall == "place":
        placeTarget = toolCallJSON.get("placeTarget")
        robotMovementQueue.extend(
            [
                {"movement": "movetoAboveTarget", "target": placeTarget}, # Move above the place target
                {"movement": "moveToHeight", "height": 165}, # Descend about halfway
                {"movement": "moveGripperToValue", "value": 100}, # Release object
                {"movement": "moveToReadyPosition"}, # Get ready to start over
            ]
        )
        return

    if toolCall == "pickAndPlace":
        pickTarget = toolCallJSON.get("pickTarget")
        placeTarget = toolCallJSON.get("placeTarget")
        robotMovementQueue.extend(
            [
                {"movement": "moveGripperToValue", "value": 100}, # Ensure gripper is open
                {"movement": "moveToReadyPosition"}, # Lean back to maximize camera view of the work area
                {"movement": "movetoAboveTarget", "target": pickTarget}, # Move above the pick target
                {"movement": "moveToHeight", "height": 105}, # Descend to pick height
                {"movement": "moveGripperToValue", "value": 40}, # Grasp object
                {"movement": "moveToHeight", "height": 225}, # Ascend back to safe height
                {"movement": "movetoAboveTarget", "target": placeTarget}, # Move above the place target
                {"movement": "moveToHeight", "height": 165}, # Descend about halfway
                {"movement": "moveGripperToValue", "value": 100}, # Release object
                {"movement": "moveToReadyPosition"}, # Get ready to start over
            ]
        )
        return

def robotControllerTick():
    """Proceed one tick of robot-control cycle: either do current movement, start next movement, or just wait to avoid overwhelming robot firmware"""
    global robotMovementQueue
    global robotCurrentMovement
    global robotLastCommandSentTime
    now = time.time()
    # Compute error magnitude between coordinates for completion check
    def positionDifferenceMm(coordsA, coordsB):
        if coordsA is None or coordsB is None:
            return None
        ax, ay, az = coordsA[0], coordsA[1], coordsA[2]
        bx, by, bz = coordsB[0], coordsB[1], coordsB[2]
        return ((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) ** 0.5

    # Compute error magnitude between angles for completion check
    def angleDifferenceDeg(anglesA, anglesB):
        if anglesA is None or anglesB is None:
            return None
        return max(abs(anglesA[i] - anglesB[i]) for i in range(len(anglesB)))

    # Limit commands to robot to prevent overwhelming its firmware
    if robotLastCommandSentTime is not None and (now - robotLastCommandSentTime) < 0.1:
        return

    # Pop the next movement
    if robotCurrentMovement is None:
        if not robotMovementQueue:
            return
        robotCurrentMovement = robotMovementQueue.pop(0)
        RICH.print(f"[bold {PURPLE}]MOVEMENT:[/bold {PURPLE}] [{BLUE}]{robotCurrentMovement}[/{BLUE}]")

    movementType = robotCurrentMovement.get("movement")

    # Stop immediately and clear queued work
    if movementType == "stop":
        ROBOT.stop()
        robotLastCommandSentTime = now
        robotMovementQueue = []
        robotCurrentMovement = None
        return

    # Drive to ready pose and wait until within joint-angle tolerance
    if movementType == "moveToReadyPosition":
        readyAngles = [-30, 45, -45, 0, 30, -45]
        currentAngles = ROBOT.get_angles()
        angleDiff = angleDifferenceDeg(currentAngles, readyAngles)
        if angleDiff is not None and angleDiff <= 2:
            robotCurrentMovement = None
            return
        if ROBOT.is_moving() == 1:
            return
        ROBOT.send_angles(readyAngles, ROBOT_SPEED)
        robotLastCommandSentTime = now
        return

    if movementType == "movetoAboveTarget":
        target = robotCurrentMovement.get("target")
        targetCoords = robotCurrentMovement.get("targetCoords")
        if targetCoords is None:
            targetCoords = getTargetCoordsFromVision(target)
            if targetCoords is None:
                return
            robotCurrentMovement["targetCoords"] = targetCoords

        targetX, targetY, _, targetRz = targetCoords
        desiredCoords = [targetX, targetY, 200, targetRz]

        # Phase 1: Send move command to get above target
        if not robotCurrentMovement.get("aboveTargetCommandSent", False):
            ROBOT.send_coords([targetX, targetY, 200, 180, 0, targetRz], ROBOT_SPEED)
            robotCurrentMovement["aboveTargetCommandSent"] = True
            robotLastCommandSentTime = now
            return

        # Phase 2: Wait until motion ends
        if ROBOT.is_moving() == 1:
            return

        # Verify that robot is actually near desired position before considering the move complete
        currentCoords = ROBOT.get_coords()
        if isinstance(currentCoords, (list, tuple)) and len(currentCoords) >= 3:
            currentXyz = [currentCoords[0], currentCoords[1], currentCoords[2], 0]
            posErrorMm = positionDifferenceMm(currentXyz, desiredCoords)
            if posErrorMm is not None and posErrorMm > 20:
                ROBOT.send_coords([targetX, targetY, 200, 180, 0, targetRz], ROBOT_SPEED)
                robotLastCommandSentTime = now
                return

        # Phase 3: Confirm that target hasn't moved
        refreshedTargetCoords = getTargetCoordsFromVision(target)
        if refreshedTargetCoords is not None:
            driftMm = positionDifferenceMm(targetCoords, refreshedTargetCoords)
            if driftMm is not None and driftMm > 2:
                robotCurrentMovement["targetCoords"] = refreshedTargetCoords
                robotCurrentMovement["aboveTargetCommandSent"] = False
                return

        # Stable enough: proceed to next movement
        robotCurrentMovement = None
        return

    if movementType == "moveGripperToValue":
        targetValue = robotCurrentMovement.get("value", 100)
        gripperValue = ROBOT.get_gripper_value()
        if gripperValue is not None and abs(gripperValue - targetValue) <= 15:
            robotCurrentMovement = None
            return
        ROBOT.set_gripper_value(targetValue, ROBOT_SPEED)
        robotLastCommandSentTime = now
        return

    if movementType == "moveToHeight":
        currentCoords = ROBOT.get_coords()
        if not (isinstance(currentCoords, (list, tuple)) and len(currentCoords) >= 6):
            return

        movement = robotCurrentMovement

        # Build a one-axis Cartesian path so all joints move together
        if "path" not in movement:
            startX, startY, startZ, fixedRx, fixedRy, fixedRz = currentCoords[:6]
            deltaZ = movement["height"] - startZ
            if abs(deltaZ) <= 2:
                robotCurrentMovement = None
                return

            stepCount = max(1, int(np.ceil(abs(deltaZ) / 10.0))) # divide the total Z distance into 10mm increments
            movement["path"] = (startX, startY, startZ, deltaZ, fixedRx, fixedRy, fixedRz, stepCount, 0)

        startX, startY, startZ, deltaZ, fixedRx, fixedRy, fixedRz, stepCount, stepIndex = movement["path"]
        if stepIndex >= stepCount:
            robotCurrentMovement = None
            return

        stepIndex += 1
        nextZ = startZ + deltaZ * (stepIndex / stepCount)
        ROBOT.send_coords([startX, startY, nextZ, fixedRx, fixedRy, fixedRz], 100)
        movement["path"] = (startX, startY, startZ, deltaZ, fixedRx, fixedRy, fixedRz, stepCount, stepIndex)
        robotLastCommandSentTime = now
        return

######################
### INITIALIZATION ###
######################

# Microphone
MICROPHONE_INDEX = None
MICROPHONE_SAMPLERATE = None
MICROPHONE_BLOCK_SIZE = None # aka "chunk" size for audio processing

# Robot
ROBOT = None
ROBOT_SPEED = 50
robotMovementQueue = []
robotCurrentMovement = None
robotLastCommandSentTime = None

# Speech, language, and vision models are now served REMOTELY (see
# env-driven endpoints at the top of this file). No local model objects
# or GPU are required here.

# Runtime state
newestUtterance = None
newestToolCall = None

with printVerbosely("Prepare microphone"):
    allMicrophones = listMicrophones()
    if not allMicrophones:
        raise RuntimeError("No microphone found")
    bestMicrophone = selectBestMicrophone(allMicrophones)
    MICROPHONE_INDEX = int(bestMicrophone["Index"])
    MICROPHONE_SAMPLERATE = int(bestMicrophone["SampleRate"])
    MICROPHONE_BLOCK_SIZE = int(MICROPHONE_SAMPLERATE / 10) # Blocks of 1/10th of a second of audio

with printVerbosely("Prepare robot"):
    from pymycobot.mycobot280 import MyCobot280
    # Prefer the stable /dev/serial/by-id symlink.Falls back to /dev/ttyACM0 if no by-id symlink exists.
    # Override with ROBOT_PORT env var when needed (e.g. inside containers
    # that mount the device at a fixed path).
    robotPort = os.environ.get("ROBOT_PORT", "").strip()
    if not robotPort:
        # MyCobot 280 uses the WCH CH343 USB-serial chip (vid:pid 1a86:55d4),
        # which udev names usb-1a86_USB_Single_Serial_<serial>-if00.
        byIdCandidates = sorted(glob.glob("/dev/serial/by-id/usb-1a86_USB_Single_Serial_*-if00"))
        if byIdCandidates:
            robotPort = byIdCandidates[0]
        elif os.path.exists("/dev/ttyACM0"):
            robotPort = "/dev/ttyACM0"
        else:
            raise RuntimeError(
                "No robot serial port found: looked for "
                "/dev/serial/by-id/usb-1a86_USB_Single_Serial_*-if00 and /dev/ttyACM0"
            )
    print(f"Robot port: {robotPort}")
    robotBaud = 115200
    ROBOT = MyCobot280(robotPort, robotBaud)

# A single warning-suppression call covers all three insecure-TLS clients
# below (LLM, ASR, vision-service). Done once so urllib3 doesn't emit one
# warning per request when *_VERIFY_TLS is false (the dev default).
if not (LLM_VERIFY_TLS and ASR_VERIFY_TLS and VISION_VERIFY_TLS):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

with printVerbosely(f"Probe language model endpoint: {LLM_ENDPOINT}"):
    LLM_API_KEY = fetchLlmApiKey()
    if not LLM_API_KEY:
        raise RuntimeError("Failed to fetch LLM API key")
    # Derive /v1/models from the chat-completions URL for a quick reachability check.
    modelsUrl = LLM_ENDPOINT.replace("/chat/completions", "/models")
    resp = requests.get(
        modelsUrl,
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        verify=LLM_VERIFY_TLS,
        timeout=10,
    )
    resp.raise_for_status()
    modelIds = [m.get("id") for m in resp.json().get("data", [])]
    if LLM_MODEL_ID not in modelIds:
        raise RuntimeError(
            f"Model '{LLM_MODEL_ID}' not served at {modelsUrl}. Available: {modelIds}"
        )

with printVerbosely(f"Probe speech model endpoint: {ASR_ENDPOINT}"):
    # Derive /v1/models from /v1/audio/transcriptions for the reachability check.
    asrModelsUrl = ASR_ENDPOINT.replace("/audio/transcriptions", "/models")
    resp = requests.get(asrModelsUrl, verify=ASR_VERIFY_TLS, timeout=10)
    resp.raise_for_status()
    asrModelIds = [m.get("id") for m in resp.json().get("data", [])]
    if not asrModelIds:
        raise RuntimeError(f"ASR endpoint returned no models at {asrModelsUrl}")
    print(f"ASR models available: {asrModelIds}")

with printVerbosely(f"Probe vision-service endpoint: {VISION_ENDPOINT}"):
    # The /v1/detect/live route is POST-only; we readiness-check via /readyz
    # on the same host. /readyz is at the service root (not under /v1) and
    # returns 200 once both detector models are warmed up inside the
    # vision-service pod.
    _visionParts = urlparse(VISION_ENDPOINT)
    visionReadyzUrl = urlunparse(_visionParts._replace(path="/readyz", params="", query="", fragment=""))
    resp = requests.get(visionReadyzUrl, verify=VISION_VERIFY_TLS, timeout=10)
    resp.raise_for_status()
    visionStatus = resp.json().get("status", "unknown")
    if visionStatus != "ready":
        raise RuntimeError(
            f"vision-service at {visionReadyzUrl} reports status={visionStatus!r}; "
            "wait for the pod's models to finish loading"
        )
    print(f"vision-service ready at {visionReadyzUrl}")

    # Show which physical camera the in-cluster service has bound to, plus
    # the MJPEG preview URL for the human in the loop. /v1/camera/info
    # returns {device, width, height, frame_count, frame_age_s,
    # jpeg_quality, stream_fps, capture_error}.
    visionCameraUrl = urlunparse(_visionParts._replace(path="/v1/camera/info", params="", query="", fragment=""))
    visionStreamUrl = urlunparse(_visionParts._replace(path="/stream", params="", query="", fragment=""))
    try:
        camResp = requests.get(visionCameraUrl, verify=VISION_VERIFY_TLS, timeout=10)
        camResp.raise_for_status()
        camInfo = camResp.json()
        RICH.print(
            f"[bold {BLUE}]Vision[/bold {BLUE}] camera "
            f"name={camInfo.get('name')!r} "
            f"device={camInfo.get('device')} "
            f"{camInfo.get('width')}x{camInfo.get('height')} "
            f"frames={camInfo.get('frame_count')} "
            f"age={camInfo.get('frame_age_s'):.2f}s "
            f"err={camInfo.get('capture_error')}"
        )
    except (requests.RequestException, ValueError, TypeError) as exc:
        RICH.print(f"[bold yellow]vision-service /v1/camera/info unavailable: {exc}[/bold yellow]")

    # Reachability probe for the MJPEG preview URL. A streaming GET with a
    # short timeout opens the connection just long enough to confirm the
    # server is responding 200, then closes it without consuming any frames.
    # If the canonical https://vision.local/stream URL is unreachable from
    # this host, fall back to suggesting the kubectl port-forward.
    try:
        streamResp = requests.get(
            visionStreamUrl, stream=True, verify=VISION_VERIFY_TLS, timeout=2
        )
        streamResp.close()
        streamResp.raise_for_status()
        RICH.print(
            f"[bold {BLUE}]MJPEG preview:[/bold {BLUE}] {visionStreamUrl} "
            f"[bold green](reachable)[/bold green]"
        )
    except requests.RequestException as exc:
        RICH.print(
            f"[bold yellow]MJPEG preview at {visionStreamUrl} not reachable from this host: "
            f"{exc}[/bold yellow]"
        )
        RICH.print(
            f"[bold yellow]Fallback: run in another terminal[/bold yellow] "
            f"[white]kubectl -n vision port-forward svc/vision-service 8090:80[/white] "
            f"[bold yellow]then open[/bold yellow] [white]http://localhost:8090/stream[/white]"
        )

#################
### MAIN LOOP ###
#################

if __name__ == "__main__":
    speechCallback = makeSpeechCallback(inputSampleRate=MICROPHONE_SAMPLERATE)
    speechStopEvent, speechWorkerThread = startSpeechPipelineWorker()
    RICH.print(
        f"[bold {BLUE}]Live preview:[/bold {BLUE}] open the same host as VISION_ENDPOINT in a browser "
        f"(e.g. https://vision.local/ or http://localhost:8090/ via "
        f"`kubectl -n vision port-forward svc/vision-service 8090:80`)."
    )

    with sd.InputStream(device=MICROPHONE_INDEX, channels=1, samplerate=MICROPHONE_SAMPLERATE, blocksize=MICROPHONE_BLOCK_SIZE, callback=speechCallback):
        print("Listening... Press Ctrl+C to stop.")
        try:
            while True:
                # If the speech pipeline has produced a new tool call, re-plan immediately
                currentToolCall = newestToolCall
                newestToolCall = None
                if currentToolCall is not None:
                    toolCallJSON = currentToolCall["toolCallJSON"]
                    RICH.print(f"[bold {PURPLE}]TOOL CALL:[/bold {PURPLE}]")
                    RICH.print_json(json=json.dumps(toolCallJSON))
                    robotPlanMovementQueue(toolCallJSON)
                # Regardless, keep on executing!
                robotControllerTick()
                sd.sleep(50)

        except KeyboardInterrupt:
            print("Stopping...")

        finally:
            speechStopEvent.set()
            speechWorkerThread.join(timeout=1.0)