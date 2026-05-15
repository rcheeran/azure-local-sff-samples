# vision-service

A stateless HTTP service that runs the four detectors currently embedded in
[`cobotpoc.py`](../../cobotpoc.py) (`lookForCube`, `lookForBowl`, `lookForHand`,
`lookForMisc`).

The service does **camera-pixel-space detection only**. Camera-to-robot
homography and reachability ("safe-zone") checks intentionally stay in
`cobotpoc.py` because they depend on the specific MyCobot mounting and the
empirical calibration in `getTargetCoordsFromVision`.

| Layer | Stays in `cobotpoc.py` | Moves to `vision-service` |
|---|---|---|
| Camera ownership (`/dev/video0`) | yes (becomes `camera-capture` later) | no |
| HSV thresholding, contouring, scoring | no | **yes** |
| MediaPipe HandLandmarker | no | **yes** |
| OWL-ViT open-vocabulary detection | no | **yes** |
| Annotated frame (HUD overlay) | no | **yes** (returned in response) |
| Camera→robot homography (`transformCoordsFromCameraToRobot`) | **yes** | no |
| Reachability check (`verifyCoordsAreSafe`) | **yes** | no |
| MJPEG live preview at `:8090` | **yes for now** (becomes `mjpeg-preview` later) | no |

---

## API

### `POST /v1/detect`

`multipart/form-data` with fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `image` | file | yes | JPEG/PNG of the scene. The server decodes via `cv2.imdecode`. |
| `target_type` | string | yes | One of `cube` \| `bowl` \| `hand` \| `misc`. |
| `target_text` | string | no | Free text. For `cube`/`bowl` the server greps for a known color word (`red`, `green`, `blue`, `yellow`, plus `white` for bowls). For `misc` the whole text is the OWL-ViT query. Ignored for `hand`. |
| `annotate` | bool | no | Default `true`. If false, `annotated_jpeg_b64` is omitted. |

Response (JSON):

```json
{
  "detected": true,
  "target_type": "cube",
  "target_text": "red cube",
  "color_resolved": "red",
  "samples": 1,
  "pose": { "x": 286, "y": 124, "z": 0, "rz": 12 },
  "score": null,
  "annotated_jpeg_b64": "/9j/4AAQ..."
}
```

The `pose` is in camera-pixel space. `z` is always `0` (the table plane) and
`rz` is the grasp angle that `cobotpoc.py`'s
`transformCoordsFromCameraToRobot` expects.

### `GET /healthz`

Liveness probe. `{"status":"ok"}` when the process is up.

### `GET /readyz`

Readiness probe. `200` once both `HandLandmarker` and OWL-ViT are loaded.
Returns `503` while warming up.

### `GET /v1/models`

```json
{
  "detectors": ["cube", "bowl", "hand", "misc"],
  "models": {
    "hand_landmarker": "google/mediapipe hand_landmarker.task (float16)",
    "owlvit":          "google/owlvit-base-patch32"
  },
  "device": "cpu"
}
```

---

## Architecture decisions

- **Single-frame in, single-detection out.** The original `lookForCube` /
  `lookForBowl` average across 5 frames. That logic was tightly coupled to
  the camera object. In the service we drop the multi-frame loop — callers
  who need stability should send N requests in a burst and average client-side.
  This keeps the service stateless and trivially horizontally scalable.
- **CPU-only.** Both MediaPipe HandLandmarker (`RunningMode.IMAGE`) and OWL-ViT
  (`base-patch32`) run fast enough on CPU that the latency of one inference
  call is well under 200 ms. We deliberately avoid GPU on the first cut so
  this service is not blocked on the host's `nvidia-container-toolkit 1.17.x`
  / CDI hook bug (the same one that bites Qwen-GPU and Parakeet on this
  cluster).
- **No model download at request time.** OWL-ViT is pulled from HuggingFace
  at pod startup and cached at `/cache/huggingface` — mount a PVC there to
  avoid re-downloading on every restart (mirrors the Parakeet pattern).

---

## Build & deploy on the k3s host

```bash
cd /home/clouduser/demo/cobotpoc/container-images/vision

# hand_landmarker.task is ~7.8 MB and is checked in next to cobotpoc.py.
# Copy it into the build context so the Dockerfile can COPY it.
cp ../../hand_landmarker.task ./hand_landmarker.task

sudo docker build --network=host -t vision-service:local .

# Import into k3s containerd (no registry needed):
sudo docker save vision-service:local \
    | sudo /usr/local/bin/k3s ctr -n k8s.io images import -

# Apply the manifests:
kubectl apply -f ../../kubernetes_yamls/vision_yamls/vision_deployment.yaml
```

Then add `192.168.1.197 vision.local` to `/etc/hosts` and try:

```bash
curl -k https://vision.local/healthz
curl -k -F image=@/some/jpeg.jpg \
        -F target_type=cube \
        -F target_text="red cube" \
        https://vision.local/v1/detect
```

---

## What's next

- `cobotpoc.py` will get a `VISION_ENDPOINT` env var (default
  `https://vision.local/v1/detect`) and `getTargetCoordsFromVision` will POST
  the latest camera frame instead of grabbing it locally.
- Once that wires up, `cobotpoc.py` no longer needs `mediapipe`, `transformers`,
  `torch`, or `hand_landmarker.task` on the robot host — those dependencies
  move entirely into this image.
