# azure-local-sff-samples — Simple Collaborative Robot

> A working sample of Physical AI operations on **Azure Local (Linux, small form factor)** using **Foundry Local** and **Azure IoT Operations**.

[![Status](https://img.shields.io/badge/status-proof--of--concept-orange)](#status)
[![Platform](https://img.shields.io/badge/platform-Azure%20Local%20(Linux)-blue)](https://learn.microsoft.com/azure/azure-local/small-form-factor/)
[![Runtime](https://img.shields.io/badge/runtime-k3s%20%2B%20Foundry%20Local-2496ED)](https://learn.microsoft.com/azure/azure-sovereign-clouds/private/foundry-local/)
[![Robot](https://img.shields.io/badge/robot-MyCobot%20280%20M5-7B68EE)](https://shop.elephantrobotics.com/collections/mycobot-280)

> **Repo status — early scaffold.** This is the first commit. Only the
> orchestrator (`cobotpoc/cobotpoc_sff.py`) and three setup docs are in place
> so far. Container images, Kubernetes manifests, and utilities will land in
> follow-up commits; the legacy monolithic prototype is preserved under
> [`old-files/`](old-files/) for reference.

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
azure-local-sff-samples/
├── README.md
├── LICENSE
├── Docs/
│   ├── setup.md                  # Connect to the Azure Local machine
│   ├── install_k3s.md            # Install + configure k3s (Traefik disabled)
│   └── install_foundry_local.md  # Install cert-manager + Foundry Local operator
├── cobotpoc/
│   ├── cobotpoc_sff.py           # Orchestrator (mic → ASR → LLM → vision → robot)
│   ├── container-images/         # (placeholder) per-service Dockerfiles
│   ├── kubernetes-yamls/         # (placeholder) cluster manifests
│   └── utilities/                # (placeholder) calibration & hardware scripts
└── old-files/                    # Legacy monolithic prototype, kept for reference
```

The `(placeholder)` directories are scaffolded for upcoming commits. Until
they're populated, refer to [`old-files/cobotpoc/`](old-files/cobotpoc/) for
the equivalent assets from the previous prototype.

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

1. **Connect to the machine** — [Docs/setup.md](Docs/setup.md)
2. **Install k3s** — [Docs/install_k3s.md](Docs/install_k3s.md)
3. **Install Foundry Local** — [Docs/install_foundry_local.md](Docs/install_foundry_local.md)

Additional guides (GPU enablement on k3s, NGINX ingress, container registry,
model deployment, troubleshooting) will be added in upcoming commits.

---

## Run the orchestrator

Once the cluster, broker, and model services are up, run the orchestrator on
the host that has the peripherals attached:

```bash
python3 cobotpoc/cobotpoc_sff.py
```

Then speak into the mic: *"pick up the red cube"*, *"put the blue cube in the
bowl"*, *"wave"*.

---

## Status

This is a **proof of concept**, not a production deployment. Expect rough
edges, hardcoded paths, and a single-tenant assumption.

---

## Acknowledgements

- [Elephant Robotics](https://www.elephantrobotics.com/) — MyCobot 280 M5
- [Google MediaPipe](https://developers.google.com/mediapipe) — Hand Landmarker
- [Google Research](https://huggingface.co/google/owlvit-base-patch32) — OWL-ViT
- [NVIDIA NeMo](https://github.com/NVIDIA/NeMo) — Parakeet TDT
- [Alibaba Cloud Qwen](https://huggingface.co/Qwen) — Qwen 2.5 Instruct
- Microsoft [Azure Local](https://learn.microsoft.com/azure/azure-local/small-form-factor/), [Foundry Local](https://learn.microsoft.com/azure/azure-sovereign-clouds/private/foundry-local/), and [Azure IoT Operations](https://learn.microsoft.com/azure/iot-operations/overview-iot-operations)

---

## License

Licensed under the [MIT License](LICENSE).
