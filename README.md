# azure-local-sff-samples — Simple Collaborative Robot

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
                           │  Qwen 3 1.7B (LLM, tool calling, on Foundry) │ ─► MQTT (AIO broker)
   USB camera  ─►  vision  │      ▼                                       │
                           │  Hand Landmarker / OWL-ViT (vision-service)  │
                           └──────────────────────────────────────────────┘
                                              │
                                              ▼
                                MyCobot 280 M5  (USB serial)
```

---

## Features

The project invokes these AI models:

| Capability | Model | Hosted by | Ingress host |
|---|---|---|---|
| Speech (transcription) | NVIDIA Parakeet TDT 0.6B v2 | k3s GPU pod | `parakeet-gpu.local` |
| Language, reasoning, tool calling | Qwen 3 1.7B (CUDA) | Foundry Local | `qwen-gpu.local` |
| Language (CPU fallback) | NVIDIA Nemotron Nano (CPU) | Foundry Local | (per-deploy) |
| Vision — hands | Google [Hand Landmarker](https://developers.google.com/mediapipe/solutions/vision/hand_landmarker) | k3s pod (`vision-service`) | `vision.local` |
| Vision — objects | [OWL-ViT Base Patch32](https://huggingface.co/google/owlvit-base-patch32) | k3s pod (`vision-service`) | `vision.local` |

The robot arm executes coordinate-based movements (e.g. `go to x, y, z`, `gripper open/close`) based on what it hears and sees.

---

## Repository layout

```text
azure-local-sff-samples/
├── README.md
├── LICENSE
├── Docs/                            # Step-by-step setup and runbook
│   ├── setup.md                     # Hardware checklist + Azure Local provisioning
│   ├── connect.md                   # SSH + VS Code Remote-SSH to the appliance
│   ├── install_k3s.md               # Install k3s (Traefik disabled)
│   ├── check_gpu.md                 # Verify NVIDIA GPU is usable from k3s
│   ├── install_nginx.md             # Community ingress-nginx (NOT NGINX Inc)
│   ├── install_foundry_local.md     # cert-manager + Foundry Local operator
│   ├── deploy_models.md             # Deploy Qwen3 + Parakeet ModelDeployments
│   └── run_cobotpoc.md              # Run the orchestrator on the host
├── container_images/                # In-cluster service images (built on the node)
│   ├── parakeet/                    # NeMo ASR FastAPI wrapper
│   │   ├── Dockerfile
│   │   └── server.py
│   └── vision/                      # OWL-ViT + MediaPipe Hand Landmarker
│       ├── Dockerfile
│       ├── README.md
│       ├── hand_landmarker.task     # Bundled MediaPipe model
│       ├── requirements.txt
│       └── app/
│           ├── server.py            # FastAPI entrypoint + /v1/detect, /readyz
│           ├── hud.py               # Annotated-frame overlay
│           └── detectors/           # cube / bowl / hand / misc
├── yamls/                           # All Kubernetes manifests
│   ├── foundry_yamls/               # Foundry Local ModelDeployments + certs
│   │   ├── model_qwen3_gpu.yaml
│   │   ├── model_qwen3_gpu_cert.yaml
│   │   ├── model_nemotron_cpu.yaml
│   │   └── model_nemotron_cpu_cert.yaml
│   ├── parakeet_yamls/              # Hand-written Deployment/Service/Ingress + cert
│   │   ├── parakeet_gpu.yaml
│   │   └── parakeet_gpu_cert.yaml
│   └── vision_yamls/
│       └── vision_deployment.yaml
├── prereq/                          # Cluster sanity fixtures
│   ├── runtimeclass-nvidia.yaml     # RuntimeClass `nvidia` for legacy GPU pods
│   ├── cuda-vectoradd.yaml          # CUDA smoke-test pod
│   ├── test.jpg                     # Sample frame for /v1/detect
│   └── test_audio.wav               # Sample clip for /v1/audio/transcriptions
└── utilities/                       # Host-side calibration / hardware scripts
    ├── README.md
    ├── calibrate.py                 # Camera→robot homography sampler
    ├── eyedropper.py                # HSV picker for vision color thresholds
    └── moverobot.py                 # MyCobot two-pose connectivity test
```

The host-side orchestrator script (`cobotpoc_sff.py`) is described in
[Docs/run_cobotpoc.md](Docs/run_cobotpoc.md). It lives on the appliance host
(typically `/home/clouduser/cobotpoc_sff.py`) and is distributed separately
from this repo — this repo only ships the cluster-side pieces that the
orchestrator talks to.

---

## Prerequisites

### Hardware

| Item | Notes |
|---|---|
| Computer running [Azure Local (Linux SFF)](https://learn.microsoft.com/azure/azure-local/small-form-factor/) | Provisioned and connected to Azure Arc |
| NVIDIA GPU | Developed/tested on RTX 3050 (8 GB) and RTX 2000E Ada (16 GB VRAM, CC 8.9, driver 580.105.08), single GPU |
| [MyCobot 280 M5](https://shop.elephantrobotics.com/collections/mycobot-280/products/mycobot-worlds-smallest-and-lightest-six-axis-collaborative-robot) | 6-DoF collaborative arm (USB-serial via WCH CH343, `1a86:55d4`) |
| UAC-compliant USB microphone | Any USB mic that Linux enumerates as a capture device |
| UVC-compliant USB camera | Any USB webcam at `/dev/video0` |

All peripherals connect over USB to the Linux host.

### Software

- k3s (Kubernetes 1.29+) with Traefik disabled
- NVIDIA driver + `nvidia-container-toolkit` (legacy `NVIDIA_VISIBLE_DEVICES` path on Azure Linux 3)
- `cert-manager`, `trust-manager`, community `ingress-nginx` (annotation prefix `nginx.ingress.kubernetes.io/*`)
- Foundry Local inference operator
- Azure IoT Operations (provides the MQTT broker, optional but recommended)

---

## Setup

Follow the docs in order. Each one is self-contained.

1. **Hardware + Azure Local** — [Docs/setup.md](Docs/setup.md)
2. **Connect over SSH / VS Code Remote** — [Docs/connect.md](Docs/connect.md)
3. **Install k3s** — [Docs/install_k3s.md](Docs/install_k3s.md)
4. **Verify GPU in k3s** — [Docs/check_gpu.md](Docs/check_gpu.md)
5. **Install community ingress-nginx** — [Docs/install_nginx.md](Docs/install_nginx.md)
6. **Install Foundry Local** — [Docs/install_foundry_local.md](Docs/install_foundry_local.md)
7. **Deploy the models** — [Docs/deploy_models.md](Docs/deploy_models.md)
   - Qwen 3 1.7B GPU (Foundry catalog `ModelDeployment`)
   - Parakeet ASR GPU (hand-written manifests in [yamls/parakeet_yamls/](yamls/parakeet_yamls/))
   - Vision service ([yamls/vision_yamls/vision_deployment.yaml](yamls/vision_yamls/vision_deployment.yaml))
8. **Run the orchestrator** — [Docs/run_cobotpoc.md](Docs/run_cobotpoc.md)

---

## Run the orchestrator

Once the cluster, ingress, and three model services are up and resolvable on
the host (`qwen-gpu.local`, `parakeet-gpu.local`, `vision.local` in
`/etc/hosts`), run the orchestrator on the host that has the peripherals
attached:

```bash
python3 /home/clouduser/cobotpoc_sff.py
```

Then speak into the mic: *"pick up the red cube"*, *"put the blue cube in the
bowl"*, *"wave"*.

See [Docs/run_cobotpoc.md](Docs/run_cobotpoc.md) for the full host-side setup
(serial port + dialout group, audio + PortAudio install, dry-run probes for
all three services).

---

## Status

This is a **proof of concept**, not a production deployment. Expect rough
edges, hardcoded paths (e.g. `clouduser`, `/dev/video0`, `/dev/ttyACM0`),
and a single-tenant assumption.

---

## Acknowledgements

- [Elephant Robotics](https://www.elephantrobotics.com/) — MyCobot 280 M5
- [Google MediaPipe](https://developers.google.com/mediapipe) — Hand Landmarker
- [Google Research](https://huggingface.co/google/owlvit-base-patch32) — OWL-ViT
- [NVIDIA NeMo](https://github.com/NVIDIA/NeMo) — Parakeet TDT
- [Alibaba Cloud Qwen](https://huggingface.co/Qwen) — Qwen 3 Instruct
- Microsoft [Azure Local](https://learn.microsoft.com/azure/azure-local/small-form-factor/), [Foundry Local](https://learn.microsoft.com/azure/azure-sovereign-clouds/private/foundry-local/), and [Azure IoT Operations](https://learn.microsoft.com/azure/iot-operations/overview-iot-operations)

---

## License

Licensed under the [MIT License](LICENSE).
