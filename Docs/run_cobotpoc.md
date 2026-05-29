# Run the cobot demo (`cobotpoc_sff.py`)

This guide walks through getting the MyCobot280 demo client running end-to-end
on the appliance host.

## What this script does

`cobotpoc_sff.py` is a single-file demo client that wires together:

- **MyCobot280 robot arm** — connected over USB serial (WCH CH343 chip)
- **Local microphone** — captured via `sounddevice` / PortAudio / ALSA
- **Qwen3 1.7B LLM** — served by the `qwen3-1-7b-cuda-gpu` pod at `https://qwen-gpu.local`
- **Parakeet ASR** — served by the `parakeet-gpu` pod at `https://parakeet-gpu.local`
- **Vision-service** (OWL-ViT + cube/bowl/hand detectors) — served by the
  `vision-service` pod at `https://vision.local`
- **MQTT publish** — optional, to an Azure IoT Operations broker if present

The script does not run any ML locally; all inference is pushed to the
cluster's GPU-sharing time-sliced GPU.

## Prerequisites

These should already be in place from the cluster setup work:

- k3s up and healthy
- Three model pods Running with `nvidia.com/gpu: 1` each:
  - `foundry-local-operator/qwen3-1-7b-cuda-gpu`
  - `parakeet/parakeet-gpu`
  - `vision/vision-service`
- Ingress hostnames resolvable on the host:
  - `qwen-gpu.local`, `parakeet-gpu.local`, `vision.local` → host IP in `/etc/hosts`
- USB camera plugged in (used by the vision-service pod, not by this script)

Verify quickly:

```bash
kubectl get pods -A | grep -E 'qwen3|parakeet|vision-service'
curl -sk https://vision.local/readyz
curl -sk https://parakeet-gpu.local/v1/models | jq -r '.data[].id'
```

Expected: all three pods Running, vision `/readyz` returns
`{"status":"ready"}`, and ASR reports `nvidia/parakeet-tdt-0.6b-v2`.

## Step 1 — Connect the MyCobot280 robot

1. Power the robot on (the base controller LED should turn solid).
2. Plug the USB-C cable from the robot base into a USB-A port on the host.
3. Verify the host sees it:

```bash
ls -la /dev/serial/by-id/ | grep usb-1a86_USB_Single_Serial
ls -la /dev/ttyACM*
```

Expected: a `usb-1a86_USB_Single_Serial_<serial>-if00` symlink and a
`/dev/ttyACM0` device. The MyCobot280 uses the WCH CH343 USB-serial chip
(vid:pid `1a86:55d4`).

If neither path exists, `cobotpoc_sff.py` will fail at startup with:

```
RuntimeError: No robot serial port found: looked for
/dev/serial/by-id/usb-1a86_USB_Single_Serial_*-if00 and /dev/ttyACM0
```

In that case re-seat the cable, try a different USB port, and confirm the
robot is powered.

The serial port is `crw-rw---- root:dialout` on Azure Linux 3, and on
a clean AzL3 install the `dialout` group is **empty**. You must add
your user to it (this is a required step, not a fallback):

```bash
sudo usermod -aG dialout clouduser
getent group dialout   # should show: dialout:x:10:clouduser
```

If you skip this, `cobotpoc_sff.py` will fail with:

```
serial.serialutil.SerialException: [Errno 13] could not open port
/dev/ttyACM0: [Errno 13] Permission denied: '/dev/ttyACM0'
```

Group membership only takes effect in **new** shells — see the
"Group caveat" note in Step 2.

## Step 2 — Install the host Python dependencies

`install_missing_packages.py` ensures the host has everything the script
needs *outside* the cluster: ALSA kernel sound module, PortAudio system
library, audio group access, and the right pip packages.

Run it once with sudo:

```bash
sudo python3 install_missing_packages.py
```

What it does:

1. Installs the kernel ALSA driver (`kernel-drivers-sound-$(uname -r)`)
   if `/proc/asound/cards` is missing, then `modprobe snd-usb-audio`.
2. Adds `clouduser` to the `audio` group so `/dev/snd/*` (mode `660`,
   group `audio`) is accessible.
3. Installs PortAudio via `dnf` (`portaudio` + `portaudio-devel`); falls
   back to a source build if no package manager is available.
4. Pip-installs the runtime packages: `numpy`, `scipy`, `opencv-python`,
   `sounddevice`, `soundfile`, `requests`, `paho-mqtt`, `rich`,
   `pymycobot`.

Expected last line:

```
Done.
```

### Group caveat (important)

Linux evaluates group membership at process-spawn time. Your current SSH
shell was started **before** `usermod -aG audio clouduser` (Step 2) and
`usermod -aG dialout clouduser` (Step 1) ran, so it does not yet have
either `audio` or `dialout`. Until you start a fresh shell:

- `sounddevice` silently returns zero devices (→ `RuntimeError: No microphone found`)
- opening `/dev/ttyACM0` raises `PermissionError: [Errno 13]`

Verify with `id` — if the output does **not** include both `audio` and
`dialout`, the current shell is stale.

Pick one to refresh:

- **Best:** log out and log back in (or reconnect SSH). All new shells
  inherit both groups automatically.
- **VS Code Remote-SSH users:** opening a new integrated terminal is
  **not enough** — the VS Code remote-server agent caches group
  membership at its own spawn time, and every terminal you open in the
  window inherits from it. Use the command palette →
  **Remote-SSH: Reload Window** (or disconnect and reconnect the SSH
  host) to restart the agent.
- **One-off, no restart:** nested `sg` for both groups in the current
  terminal:

  ```bash
  sg dialout -c "sg audio -c 'python3 cobotpoc_sff.py'"
  ```

- **One-off for a single command:** `sg audio -c '<command>'` or
  `sg dialout -c '<command>'` when you only need one of the two.

Verify after refreshing:

```bash
id | grep -oE 'audio|dialout'         # both lines should appear
python3 -c "import sounddevice as sd; print(sd.query_devices())"
python3 -c "import serial; serial.Serial('/dev/ttyACM0', 115200).close(); print('serial ok')"
```

Expected: at least one input device labeled `USB PnP Audio Device` with
`(1 in, 0 out)`, and `serial ok`.

## Step 3 — (Optional) Dry-run the model probes

Before running the full script, you can confirm all three remote services
respond correctly. This does not require the robot or the microphone:

```bash
# 1. LLM
LLM_KEY=$(kubectl -n foundry-local-operator get secret \
  qwen3-1-7b-cuda-gpu-api-keys -o jsonpath='{.data.primary-key}' | base64 -d)
curl -sk -H "Authorization: Bearer $LLM_KEY" https://qwen-gpu.local/v1/models | jq

# 2. ASR
curl -sk https://parakeet-gpu.local/v1/models | jq

# 3. Vision
curl -sk https://vision.local/readyz
curl -sk https://vision.local/v1/camera/info | jq
```

Expected:

- LLM: `data[0].id == "qwen3-1.7b-cuda-gpu:2"`
- ASR: `data[0].id == "nvidia/parakeet-tdt-0.6b-v2"`
- Vision: `status: "ready"`, `frame_age_s < 1.0`, `capture_error: null`

## Step 4 — Run `cobotpoc_sff.py`

In a fresh shell (so the `audio` group is active):

```bash
cd /home/clouduser
python3 cobotpoc_sff.py
```

Startup sequence the script prints:

1. `Robot port: /dev/serial/by-id/usb-1a86_USB_Single_Serial_<serial>-if00`
2. `Probe language model endpoint: https://qwen-gpu.local/v1/chat/completions` → ✓
3. `Probe speech model endpoint: https://parakeet-gpu.local/v1/audio/transcriptions` → ✓
4. `Probe vision-service endpoint: https://vision.local/v1/detect/live` → ✓
5. `vision-service camera name='usb camera: usb camera' device=/dev/video0 640x480 frames=… age=…s err=None`
6. `MJPEG preview: https://vision.local/stream (reachable)`
7. Falls into the main loop and starts listening on the mic.

Once you see the listen prompt, speak a command like:
*"Pick up the red cube and put it in the bowl."*

The script will:

- Stream the audio chunk to parakeet → text
- Send the text + system prompt to qwen → JSON action plan
- For each action, call `https://vision.local/v1/detect/live` with the
  target (`red cube`, `bowl`, etc.) to get camera-frame pixel coords
- Apply the homography to convert camera pixels → robot mm
- Send the move commands to the MyCobot280 over USB serial

To stop, press `Ctrl-C`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'sounddevice'` | pip packages not installed | Re-run `sudo python3 install_missing_packages.py` |
| `OSError: PortAudio library not found` | `libportaudio.so` missing | `sudo dnf install -y portaudio portaudio-devel` |
| `sd.query_devices()` returns nothing | shell predates `usermod -aG audio` | Log out / log back in, or use `newgrp audio` |
| `RuntimeError: No robot serial port found` | robot not plugged in / not powered | Re-seat USB-C cable, verify `/dev/ttyACM0` exists |
| `Permission denied: '/dev/ttyACM0'` | user not in `dialout` group, or shell predates the `usermod` | See Step 1 + Group caveat in Step 2 |
| `RuntimeError: Model 'qwen3-1.7b-cuda-gpu:2' not served` | model pod restarted with different id | `curl -sk https://qwen-gpu.local/v1/models` and update `LLM_MODEL_ID` env var |
| `Failed to fetch LLM API key` | secret renamed or namespace changed | `kubectl -n foundry-local-operator get secrets`, update `LLM_API_KEY_SECRET` env var |
| vision `/readyz` returns `status: starting` | detectors still warming up | Wait ~30s on first start of vision-service pod, retry |
| MQTT `ConnectionRefusedError` spam in stderr | no AIO broker deployed | Harmless — `publishMqttMessage()` catches it; ignore, or `export MQTT_BROKER=` to point at a real broker |

## Overrideable environment variables

All the constants at the top of `cobotpoc_sff.py` can be overridden without
editing the file:

```bash
export LLM_ENDPOINT=https://qwen-gpu.local/v1/chat/completions
export LLM_MODEL_ID=qwen3-1.7b-cuda-gpu:2
export LLM_API_KEY_SECRET=qwen3-1-7b-cuda-gpu-api-keys
export LLM_API_KEY_NAMESPACE=foundry-local-operator
export LLM_API_KEY_FIELD=primary-key
export LLM_VERIFY_TLS=false
export ASR_ENDPOINT=https://parakeet-gpu.local/v1/audio/transcriptions
export ASR_VERIFY_TLS=false
export VISION_ENDPOINT=https://vision.local/v1/detect/live
export VISION_VERIFY_TLS=false
export MQTT_BROKER=192.168.1.196
python3 cobotpoc_sff.py
```
