# cobotpoc — Simple Collaborative Robot

> A working sample of Physical AI operations on **Azure Local (Linux, small form factor)** using **Foundry Local** and **Azure IoT Operations**.

[![Status](https://img.shields.io/badge/status-proof--of--concept-orange)](#status)
[![Platform](https://img.shields.io/badge/platform-Azure%20Local%20(Linux)-blue)](https://learn.microsoft.com/azure/azure-local/small-form-factor/)
[![Runtime](https://img.shields.io/badge/runtime-k3s%20%2B%20Foundry%20Local-2496ED)](https://learn.microsoft.com/azure/azure-sovereign-clouds/private/foundry-local/)
[![Robot](https://img.shields.io/badge/robot-MyCobot%20280%20M5-7B68EE)](https://shop.elephantrobotics.com/collections/mycobot-280)

---

## Overview

`cobotpoc` shows how to combine [Azure Local](https://learn.microsoft.com/azure/azure-local/small-form-factor/), [Foundry Local](https://learn.microsoft.com/azure/azure-sovereign-clouds/private/foundry-local/), and [Azure IoT Operations](https://learn.microsoft.com/azure/iot-operations/overview-iot-operations) to build an inexpensive **voice-driven collaborative robot**. The user speaks; a 6-DoF arm picks and places objects on a table based on what it hears and sees.

All AI inference, robot control, and message bus traffic runs **on a single Linux machine** (Azure Local SFF) — no cloud round-trips at runtime.

```
                           ┌──────────────────────────────────────────────┐
   USB mic     ─►  mic ──► │  Parakeet (ASR)                              │
                           │      ▼                                       │
   USB camera  ─►  vision  │  Qwen 2.5 (LLM, tool-calling, on Foundry)    │ ─► MQTT (AIO broker)
                           │      ▼                                       │
                           │  Hand Landmarker / OWL-ViT (vision)          │
                           └──────────────────────────────────────────────┘
                                              │
                                              ▼
                                MyCobot 280 M5  (USB serial)
```

See [Docs/cobotpoc_architecture.md](../Docs/cobotpoc_architecture.md) for the full Kubernetes-native, MQTT-based architecture, topic plan, and payload schemas.

---

## Features

The project invokes these AI models:

| Capability | Model | Hosted by |
|---|---|---|
| Speech (transcription) | NVIDIA Parakeet TDT 0.6b v2 | k3s GPU pod |
| Language, reasoning, tool calling | Qwen 2.5 1.5b Instruct | Foundry Local |
| Vision — hands | Google [Hand Landmarker](https://developers.google.com/mediapipe/solutions/vision/hand_landmarker) | k3s pod |
| Vision — objects | [OWL-ViT Base Patch32](https://huggingface.co/google/owlvit-base-patch32) | k3s pod |

The robot arm executes coordinate-based movements (e.g. `go to x, y, z`, `gripper open/close`) based on what it hears and sees.

---

## Repository layout

```text
cobotpoc/
├── cobotpoc_sff.py          # Orceshtrator program
├── container-images/        # Per-service Dockerfiles + code
│   ├── parakeet/            # ASR (speech-to-text) service
│   ├── vision/              # Detectors (cube / bowl / hand / misc) — see sub-README
│
├── kubernetes_yamls/        # Cluster manifests
│   ├── foundry_yamls/       # Qwen 2.5, Parakeet, Whisper ModelDeployments
│   ├── vision_yamls/        # vision-service Deployment
│
└── utilities/               # Calibration & hardware sanity scripts
    ├── calibrate.py         # Camera ↔ robot homography
    ├── eyedropper.py        # HSV picker for vision tuning
    └── moverobot.py         # Two-pose motion sanity check
```

Top-level [`Docs/`](../Docs/) contains the setup and ops guides referenced below.

---

## Prerequisites

### Hardware

| Item | Notes |
|---|---|
| Computer running [Azure Local (Linux SFF)](https://learn.microsoft.com/azure/azure-local/small-form-factor/) | Provisioned and connected to Azure Arc |
| NVIDIA GPU | Developed/tested on RTX 3050 (8 GB) and RTX 2000E Ada (16 GB VRAM, CC 8.9, driver 580.105.08), single GPU |
| [MyCobot 280 M5](https://shop.elephantrobotics.com/collections/mycobot-280/products/mycobot-worlds-smallest-and-lightest-six-axis-collaborative-robot) | 6-DoF collaborative arm |
| UAC-compliant USB microphone | Any USB mic that Linux enumerates as a capture device |
| UVC-compliant USB camera | Any USB webcam at `/dev/video0` |

All peripherals connect over USB to the Linux host.

### Software

- k3s (Kubernetes 1.29+)
- NVIDIA container runtime
- `cert-manager`, `trust-manager`, community `ingress-nginx`
- Foundry Local inference operator
- Azure IoT Operations (provides the MQTT broker on `:11000`)

---

## Setup

Follow the docs in order. Each one is self-contained.

1. **Connect to the machine** — [Docs/setup.md](../Docs/setup.md)
2. **Configure VS Code remote** *(optional)* — [Docs/vs_code_config.md](../Docs/vs_code_config.md)
3. **Install k3s** — [Docs/install_k3s.md](../Docs/install_k3s.md)
4. **Enable GPU on k3s** — [Docs/Configure_GPU_k3s.md](../Docs/Configure_GPU_k3s.md)
5. **Set up a container registry** — [Docs/Container_registry_setup.md](../Docs/Container_registry_setup.md)
6. **Install Foundry Local** — [Docs/install_foundry_local.md](../Docs/install_foundry_local.md)
7. **Configure NGINX ingress for Foundry** — [Docs/Nginx_setup.md](../Docs/Nginx_setup.md)
8. **Deploy models (Qwen, Parakeet, Whisper)** — [Docs/deploy_models.md](../Docs/deploy_models.md)
9. *(Troubleshooting)* **Free up disk space** — [Docs/Space_issue.md](../Docs/Space_issue.md)

---

## Run the demo

After all services are deployed and the MQTT broker is reachable:

```bash
# (one-time) install python deps not pinned by k3s images
python3 install_missing_packages.py

# (one-time) download the MediaPipe Hand Landmarker model
#   already vendored at: cobotpoc/hand_landmarker.task

# Run the main PoC against your hardware
python3 cobotpoc.py
```

Then speak into the mic: *"pick up the red cube"*, *"put the blue cube in the bowl"*, *"wave"*.

A live annotated camera feed is served at **<http://localhost:8090>** (MJPEG).

---

## Services & endpoints

| Service | Image | Endpoint(s) | Notes |
|---|---|---|---|
| `vision-service` | [`container-images/vision/`](container-images/vision/) | `POST /v1/detect`, `GET /healthz`, `GET /readyz`, `GET /v1/models` | Stateless detector for `cube` / `bowl` / `hand` / `misc`. See [container-images/vision/README.md](container-images/vision/README.md). |
| `parakeet` (ASR) | [`container-images/parakeet/`](container-images/parakeet/) | OpenAI-compatible `/v1/audio/transcriptions` | GPU-pinned. |
| Qwen 2.5 (LLM) | Foundry Local `ModelDeployment` | OpenAI-compatible `/v1/chat/completions` via NGINX | See [kubernetes_yamls/foundry_yamls/](kubernetes_yamls/foundry_yamls/). |
| `mic` | [`container-images/mic/`](container-images/mic/) | Publishes audio chunks | Host audio passthrough. |
| `robot` | [`container-images/robot/`](container-images/robot/) | Consumes `robot/cmd`, publishes `robot/state` | Runs as a `Job`, privileged, `/dev/ttyACM*`. |
| `robot-metrics` | [`container-images/robot-metrics/`](container-images/robot-metrics/) | Publishes telemetry to MQTT | Optional. |
| `mqtt-publisher` | [`container-images/mqtt-publisher/`](container-images/mqtt-publisher/) | Sample / smoke-test publisher | Not a runtime component. |
| MQTT broker | `aio-lb-broker` (Azure IoT Operations) | Plaintext on `:11000` | Already deployed by AIO. |

MQTT topic plan and payload schemas: [Docs/cobotpoc_architecture.md §3–6](../Docs/cobotpoc_architecture.md).

---

## Calibration & tuning

The robot has to know *where* on the table the camera sees an object. The helpers in [`utilities/`](utilities/) take care of this:

| Script | Purpose |
|---|---|
| [`utilities/calibrate.py`](utilities/calibrate.py) | Capture matched robot-frame (mm) and camera-frame (px) points → compute homography. |
| [`utilities/eyedropper.py`](utilities/eyedropper.py) | Click pixels in a live feed to read HSV values; use these to tune color thresholds in [`container-images/vision/app/detectors/`](container-images/vision/app/detectors/). |
| [`utilities/moverobot.py`](utilities/moverobot.py) | Two-pose motion test — verifies the MyCobot is connected and reachable. |

See [utilities/README.md](utilities/README.md).

---

## Configuration

Common environment variables consumed by the components:

| Variable | Default | Purpose |
|---|---|---|
| `MQTT_BROKER` | *(required)* | Hostname/IP of the AIO MQTT broker. |
| `MQTT_PORT` | `11000` | Plaintext MQTT port exposed by `aio-lb-broker`. |
| `TENANT` | `default` | Topic prefix segment (`cobotpoc/{tenant}/...`). |
| `COMPONENT` | *(required)* | Set per Deployment; used in exception telemetry. |
| `CAMERA_DEVICE` | `/dev/video0` | UVC camera index. |
| `ROBOT_SERIAL` | `/dev/ttyACM0` | MyCobot serial device (varies by host). |

---

## Troubleshooting

- **`hand_landmarker.task` missing** — the file is vendored at `cobotpoc/hand_landmarker.task`. If your container image strips it, copy it back in or download from MediaPipe.
- **Python deps missing on the host** — run `python3 install_missing_packages.py`.
- **Out of disk on the SFF box** — [Docs/Space_issue.md](../Docs/Space_issue.md).
- **NGINX ingress / TLS issues with Foundry models** — [Docs/Nginx_setup.md](../Docs/Nginx_setup.md).
- **GPU not visible inside pods** — [Docs/Configure_GPU_k3s.md](../Docs/Configure_GPU_k3s.md).
- **Robot not moving / silent on serial** — run `python3 utilities/moverobot.py`.

---

## Status

This is a **proof of concept**, not a production deployment. Expect rough edges, hardcoded paths, and a single-tenant assumption. The Kubernetes-native redesign in [Docs/cobotpoc_architecture.md](../Docs/cobotpoc_architecture.md) is the intended target shape.

---

## Acknowledgements

- [Elephant Robotics](https://www.elephantrobotics.com/) — MyCobot 280 M5
- [Google MediaPipe](https://developers.google.com/mediapipe) — Hand Landmarker
- [Google Research](https://huggingface.co/google/owlvit-base-patch32) — OWL-ViT
- [NVIDIA NeMo](https://github.com/NVIDIA/NeMo) — Parakeet TDT
- [Alibaba Cloud Qwen](https://huggingface.co/Qwen) — Qwen 2.5 Instruct


---

## License

See `LICENSE` at the repository root.
