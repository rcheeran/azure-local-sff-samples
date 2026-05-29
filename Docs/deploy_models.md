# Deploy Models on Foundry Local

This guide walks through deploying a model on Foundry Local end-to-end:

1. **List** the models available in the operator's catalog.
2. **Apply** a `ModelDeployment` (using the Qwen2.5 0.5B CPU model as the example).
3. **Issue** a TLS certificate for the model's Ingress hostname.

> Prerequisites: the inference operator, `cert-manager`, `trust-manager`, and the community `ingress-nginx` controller are already installed (see [install_foundry_local.md](install_foundry_local.md)).

---

## 1. List available models in the catalog

The operator publishes the catalog as a `ConfigMap` named `foundry-local-catalog` in the `foundry-local-operator` namespace. Render it as a readable table:

> Requires `jq` and `column` (`util-linux`). On Azure Linux: `sudo dnf install -y jq util-linux`.

```bash
kubectl get cm foundry-local-catalog -n foundry-local-operator -o json \
  | jq -r '.data."catalog.json"' \
  | jq -r '["ALIAS", "DEVICE", "SIZE", "MODEL_ID"],
      (.models[] | [
        .alias,
        (.variants[0].compute | ascii_upcase),
        ((.variants[0].fileSizeBytes / 1073741824 * 100 | floor) / 100 | tostring + "GB"),
        .variants[0].id
      ]) | @tsv' \
  | column -t
```

## 2. Deploy the Qwen3 1.7B GPU model

Deploy Qwen3 1.7B on GPU. Review the /foundry_yamls/model_qwen3_gpu.yaml file. 

Field notes:

| Field | Why this value |
|-------|----------------|
| `metadata.name: qwen3-1-7b ` | Periods are **not** allowed in `ModelDeployment.metadata.name` (DNS-1035). Use hyphens. |
| `spec.model.catalog.{name,version}` | Pins the exact CPU variant (`qwen3-1.7b-cuda-gpu:2`) found in step 1. |
| `spec.workloadType: generative` | Enum: `generative` (LLM/chat) or `predictive` (classification/detection). |
| `spec.compute: gpu` | Must match the variant's `compute` field. |
| `spec.endpoint.host: qwen-gpu.local` | The hostname clients hit. Must be added to `/etc/hosts` on each client (see step 3). |
| `spec.endpoint.ingressClassName: nginx` | Required for the operator's NGINX-style annotations to take effect. |

### Apply

```bash
kubectl apply -f foundry_yamls/model_qwen3_gpu.yaml
```

### Watch the deployment come up

```bash
kubectl get modeldeployment -n foundry-local-operator -w
kubectl get pods -n foundry-local-operator -l app.kubernetes.io/name=qwen3-1-7b-cuda-gpu
kubectl get svc,ingress -n foundry-local-operator
```

The operator will create a Pod, Service (`qwen3-1-7b`), and Ingress (`qwen3-1-7b`). The Ingress has the NGINX annotations but **no TLS block** — that is added by the certificate step below.

---

### Issue a TLS certificate for the Ingress hostname

The operator-generated Ingress points at `qwen-gpu.local`, but the cluster's default certificate doesn't include that as a SAN. Create a dedicated `Certificate` (issued by `foundry-local-operator-ca-issuer`, installed with the operator) that covers both the public hostname and the in-cluster Service DNS names.

Save as [`foundry_yamls/model_qwen3_gpu_cert.yaml`](../foundry_yamls/model_qwen3_gpu_cert.yaml):


> The internal SANs (`qwen2-5-0-5b.*`) match the Service the operator creates for `metadata.name: qwen2-5-0-5b`. They allow backend TLS verification by NGINX (see [Nginx_setup.md](Nginx_setup.md) Step 6). If you only need the external Ingress to work, just `qwen-cpu.local` is enough.

#### Apply and verify

```bash
kubectl apply -f foundry_yamls/model_qwen3_gpu_cert.yaml

kubectl get certificate qwen3-1-7b-cuda-gpu-local-cert -n foundry-local-operator
kubectl describe certificate qwen3-1-7b-cuda-gpu-local-cert -n foundry-local-operator | tail -20
```

Wait for `READY: True`. The corresponding TLS secret will be:

```bash
kubectl get secret qwen3-1-7b-cuda-gpu-local-tls -n foundry-local-operator
```

#### Patch the Ingress to use the new cert

```bash
kubectl patch ingress qwen3-1-7b-cuda-gpu -n foundry-local-operator --type merge -p '{
  "spec": {
    "tls": [{
      "hosts": ["qwen-gpu.local"],
      "secretName": "qwen3-1-7b-cuda-gpu-local-tls"
    }]
  }
}'
```

#### Remove the operator's broken `rewrite-target` annotation

The operator generates the Ingress with:

```yaml
nginx.ingress.kubernetes.io/rewrite-target: /$2
```

`/$2` is a regex backreference, but the rule's `path: /` (with `pathType: Prefix`) has **no capture groups**, so NGINX rewrites every request to empty/`/` and the backend returns `404` for `/v1/models`, `/v1/chat/completions`, etc. (You can confirm this in the model pod's nginx access log: `GET / HTTP/1.1 404`.)

The model server already serves directly at `/v1/...`, so no rewrite is needed. Remove the annotation:

```bash
kubectl annotate ingress qwen3-1-7b-cuda-gpu -n foundry-local-operator \
  nginx.ingress.kubernetes.io/rewrite-target-
```

Verify the annotation is gone:

```bash
kubectl get ingress qwen3-1-7b-cuda-gpu -n foundry-local-operator \
  -o jsonpath='{.metadata.annotations}{"\n"}' | tr ',' '\n' | grep -i rewrite || echo "rewrite-target removed"
```

#### Add the host to your client's `/etc/hosts`

```bash
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "$NODE_IP qwen-gpu.local" | sudo tee -a /etc/hosts
```

#### Smoke test

```bash
API_KEY=$(kubectl get secret qwen3-1-7b-cuda-gpu-api-keys -n foundry-local-operator \
  -o jsonpath='{.data.primary-key}' | base64 -d)

# List models
curl -k "https://qwen-gpu.local/v1/models" \
  -H "Authorization: Bearer $API_KEY"

# Chat completion
curl -k -X POST "https://qwen-gpu.local/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-1.7b-cuda-gpu:2",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 20
  }'
```

Expected: HTTP 200 with a JSON list containing `qwen3-1.7b-cuda-gpu:2` for `/v1/models`, and a chat completion JSON response for `/v1/chat/completions`.

The model server is OpenAI-API compatible and exposes `/v1/models`, `/v1/chat/completions`, `/v1/completions`, and `/v1/embeddings` (where supported).

---

### List live endpoints on a model deployment

The model server doesn't ship an OpenAPI spec, so the easiest way to enumerate what's actually responding is to probe a known set of paths from inside the cluster (so you bypass any Ingress/rewrite issues).

```bash
DEPLOY=qwen3-1-7b-cuda-gpu
NS=foundry-local-operator
API_KEY=$(kubectl get secret ${DEPLOY}-api-keys -n $NS \
  -o jsonpath='{.data.primary-key}' | base64 -d)

kubectl run -n $NS probe --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 --command -- sh -c "
    for p in /v1/models /v1/chat/completions /v1/completions /v1/embeddings \
             /healthz /readyz /openai/status /metrics; do
      code=\$(curl -sk -o /dev/null -w '%{http_code}' \
        https://${DEPLOY}:5000\$p -H 'Authorization: Bearer $API_KEY')
      echo \"\$code  \$p\"
    done"
```

Observed results on a Qwen2.5 CPU deployment:

| Status | Path | Notes |
|--------|------|-------|
| `200` | `/v1/models` | Lists the loaded variant. |
| `405` | `/v1/chat/completions` | `GET` not allowed; works with `POST` + JSON body. |
| `405` | `/v1/embeddings` | `GET` not allowed; works with `POST` (model-dependent). |
| `404` | `/v1/completions` | Not implemented on this model — use `/v1/chat/completions`. |
| `200` | `/healthz` | Liveness probe target. |
| `200` | `/readyz` | Readiness probe target. |
| `200` | `/openai/status` | Internal status endpoint. |
| `404` | `/metrics` | No Prometheus endpoint exposed. |

A `405` is good news — it means the route exists but only accepts `POST`.

#### Probe via the Ingress (external view)

After the Ingress and `/etc/hosts` are configured (see step 3), the same probe works externally:

```bash
HOST=qwen-gpu.local
API_KEY=$(kubectl get secret qwen3-1-7b-cuda-gpu-api-keys -n $NS \
  -o jsonpath='{.data.primary-key}' | base64 -d)

for p in /v1/models /v1/chat/completions /v1/completions /v1/embeddings \
         /healthz /readyz /openai/status; do
  code=$(curl -sk -o /dev/null -w '%{http_code}' \
    "https://${HOST}${p}" -H "Authorization: Bearer $API_KEY")
  echo "$code  $p"
done
```

If the external probe returns `404` everywhere while the in-cluster probe is fine, the Ingress `rewrite-target: /$2` annotation is back — re-run the [annotation removal](#remove-the-operators-broken-rewrite-target-annotation) step.

---

For the full per-model TLS / CA-bundle wiring (including the `foundry-local-operator-ca-bundle` secret used by the Ingress's `proxy-ssl-secret` annotation), see [Nginx_setup.md](Nginx_setup.md) Steps 4–7.

---

## 3. Deploy the Nemotron CPU streaming ASR model

Nemotron speech-streaming-en (0.6B) is an English streaming automatic-speech-recognition model that runs CPU-only. It is in the `foundry-local-catalog` (alias `nemotron-speech-streaming-en-0.6b`), so deployment uses the same `ModelDeployment` CR pattern as Qwen3.

Review the [`foundry_yamls/model_nemotron_cpu.yaml`](../foundry_yamls/model_nemotron_cpu.yaml) file.

### Field notes

| Field | Why this value |
|-------|----------------|
| `metadata.name: nemotron` | DNS-1035 compliant (no periods); also used as the generated Deployment / Service / Ingress name. |
| `spec.model.catalog.name: nemotron-speech-streaming-en-0.6b` | Catalog alias from step 1. Without a `version`, the operator resolves the newest variant (`nemotron-speech-streaming-en-0.6b-generic-cpu:3` at time of writing). |
| `spec.runtime: onnx-genai` | ONNX Runtime GenAI runtime; required for the streaming-ASR variants in the catalog. |
| `spec.compute: cpu` | Matches the variant's `compute` field; no GPU is requested. |
| `spec.endpoint.host: nemotron-cpu.local` | Hostname clients hit. Add to `/etc/hosts` on each client (see below). |
| `spec.endpoint.path: /(.*)` + `pathType: ImplementationSpecific` + `rewritePath: /$1` + `annotations."nginx.ingress.kubernetes.io/use-regex": "true"` | Together they enable NGINX regex matching and rewrite captures back to `/$1`, so the upstream OpenAI server still sees the original `/v1/...` path. Without `use-regex: "true"` the regex path matches nothing and the Ingress returns 404. |

### Apply

```bash
kubectl apply -f foundry_yamls/model_nemotron_cpu.yaml
```

### Watch the deployment come up

```bash
kubectl get modeldeployment nemotron -n foundry-local-operator -w
kubectl get pods -n foundry-local-operator -l app.kubernetes.io/name=nemotron
kubectl get svc,ingress -n foundry-local-operator | grep nemotron
```

On a fresh cluster the operator first runs a one-shot `cache-foundry-local-nemotron-speech-streaming-en-0-6b-*` Job that downloads the model into the shared `inference-operator-model-store` PVC; then it creates the Deployment, Service (`nemotron`, port 5000), and Ingress (`nemotron`). End-to-end rollout typically takes 2–5 minutes on first deploy and seconds on subsequent re-rollouts.

### Issue a TLS certificate for the Ingress hostname

Like the Qwen3 case, the operator-generated Ingress has no `tls` block. Apply [`foundry_yamls/model_nemotron_cpu_cert.yaml`](../foundry_yamls/model_nemotron_cpu_cert.yaml) to issue `nemotron-cpu-local-tls`, covering both the public hostname and the in-cluster Service DNS names:

```bash
kubectl apply -f foundry_yamls/model_nemotron_cpu_cert.yaml

kubectl get certificate nemotron-cpu-local-cert -n foundry-local-operator
kubectl describe certificate nemotron-cpu-local-cert -n foundry-local-operator | tail -20
```

Wait for `READY: True`. The corresponding TLS secret will be:

```bash
kubectl get secret nemotron-cpu-local-tls -n foundry-local-operator
```

#### Patch the Ingress to use the new cert

```bash
kubectl patch ingress nemotron -n foundry-local-operator --type merge -p '{
  "spec": {
    "tls": [{
      "hosts": ["nemotron-cpu.local"],
      "secretName": "nemotron-cpu-local-tls"
    }]
  }
}'
```

> Unlike the Qwen3 deployment, **no annotation removal is needed**. The CR's `rewritePath: /$1` is paired with the single-capture regex path `/(.*)`, so the operator-emitted `nginx.ingress.kubernetes.io/rewrite-target: /$1` is already correct.

#### Add the host to your client's `/etc/hosts`

```bash
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "$NODE_IP nemotron-cpu.local" | sudo tee -a /etc/hosts
```

#### Smoke test

The Nemotron server is OpenAI-compatible but exposes the **audio** route, not chat-completions:

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/v1/models` | Lists the loaded variant. |
| `POST` | `/v1/audio/transcriptions` | OpenAI-style multipart upload (`file=@<audio>`). |
| `GET`  | `/healthz`, `/readyz` | Probe targets. |

```bash
API_KEY=$(kubectl get secret nemotron-api-keys -n foundry-local-operator \
  -o jsonpath='{.data.primary-key}' | base64 -d)

# List the loaded model
curl -k "https://nemotron-cpu.local/v1/models" \
  -H "Authorization: Bearer $API_KEY"

# Transcribe an audio file (16 kHz mono WAV works best)
curl -k -X POST "https://nemotron-cpu.local/v1/audio/transcriptions" \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@some.wav" \
  -F "model=nemotron-speech-streaming-en-0.6b-generic-cpu:3"
```

Expected: HTTP 200 with a JSON list containing `nemotron-speech-streaming-en-0.6b-generic-cpu:3` from `/v1/models`, and a transcription JSON (`{"text": "..."}`) from `/v1/audio/transcriptions`. If `/v1/models` returns `404` from the Ingress while it succeeds via a `kubectl port-forward svc/nemotron 15000:5000`, the `nginx.ingress.kubernetes.io/use-regex: "true"` annotation didn't make it onto the Ingress — re-apply the CR and confirm with `kubectl get ingress nemotron -n foundry-local-operator -o jsonpath='{.metadata.annotations}'`.

---

## 4. Deploy the Parakeet ASR GPU model

Parakeet (NVIDIA NeMo `parakeet-tdt-0.6b-v2`) is a speech‑to‑text model exposed via a small FastAPI wrapper. Unlike Qwen3, **it is not in the `foundry-local-catalog`** — there is no `ModelDeployment` CR. Instead you build a container image locally, import it into k3s containerd, and apply a plain `Deployment` + `Service` + `Ingress` (see [`foundry_yamls/parakeet_gpu.yaml`](../cobotpoc/yamls/foundry_yamls/parakeet_gpu.yaml)).

Field notes:

| Field | Why this value |
| ----- | -------------- |
| `metadata.namespace: parakeet` | Dedicated namespace; the operator's `foundry-local-operator` namespace is not used. |
| `image: parakeet-server:local` + `imagePullPolicy: Never` | The image is loaded directly into k3s containerd; no registry is involved. |
| `runtimeClassName: nvidia` (no `nvidia.com/gpu` request) | Uses the legacy `NVIDIA_VISIBLE_DEVICES` path. The GPU‑operator device plugin would inject CDI hooks that call `nvidia-ctk` subcommands not present in the host's `nvidia-container-toolkit 1.17.8-2.azl3`, producing `createContainer hook #4: exit status 3`. |
| `NVIDIA_VISIBLE_DEVICES=all` / `NVIDIA_DRIVER_CAPABILITIES=compute,utility` | Tells `nvidia-container-runtime` to mount the GPU devices and driver libs the legacy way. |
| `PARAKEET_MODEL=nvidia/parakeet-tdt-0.6b-v2` | Model is downloaded from HuggingFace on first request (~2.4 GB) and cached in the PVC. |
| `PersistentVolumeClaim parakeet-model-cache` (10 Gi) | Keeps the HF/NeMo cache so pods don't re‑download the model on restart. |
| `strategy.type: Recreate` | Only one GPU on the node; prevents two pods racing for it during a rollout. |
| `startupProbe.failureThreshold: 60` (10 min) | Cold start has to download and load the model before `/health` returns 200. |
| `spec.host: parakeet-gpu.local` | The hostname clients hit. Add it to `/etc/hosts` on each client. |
| Ingress annotations `proxy-body-size: 100m`, `proxy-read-timeout: 300` | Audio uploads need a generous body size and read timeout. |
| **No `auth-snippet` / no API key** | The NeMo wrapper has no built‑in auth, and ingress‑nginx 4.x's annotation‑risk rules block `auth-snippet`. Rely on TLS + the private network — **do not expose publicly**. |

### Parakeet prerequisites

1. **Build the image** on the node and import it into k3s containerd. The Dockerfile and `server.py` live under [`/container-images/parakeet/`](../container-images/parakeet/):

    > **Before building, move Docker's data-root off `/`.** The Parakeet build pulls `nvcr.io/nvidia/pytorch:24.07-py3` (~20 GB) and produces overlay layers larger than that. On this node `/` (`/dev/sda6`) only has ~14 GB free, while `/acsa` (`/dev/sda11`) has ~259 GB free, so Docker must store its data on `/acsa`. Do this **once**, before the first `docker build`:
    >
    > ```bash
    > # 1. Confirm free space
    > df -h /                 # source ('Avail' on /var/lib/docker)
    > df -h /acsa             # destination
    >
    > # 2. Stop docker so the data dir is quiescent
    > sudo systemctl stop docker.socket
    > sudo systemctl stop docker
    > systemctl is-active docker     # should print 'inactive'
    >
    > # 3. Prepare destination and copy the existing data dir
    > #    -H preserves overlay2 hardlinks (critical); -X preserves xattrs.
    > #    Do NOT pass -A: rsync 3.4.1 on Azure Linux has no ACL support.
    > sudo mkdir -p /acsa/docker
    > sudo chown root:root /acsa/docker
    > sudo chmod 0710 /acsa/docker
    > sudo rsync -aHX --numeric-ids --info=progress2 \
    >   /var/lib/docker/ /acsa/docker/
    >
    > # 4. Point dockerd at the new location (preserve existing daemon.json keys)
    > sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak
    > sudo jq '. + {"data-root": "/acsa/docker"}' /etc/docker/daemon.json.bak \
    >   | sudo tee /etc/docker/daemon.json
    >
    > # 5. Restart and verify
    > sudo systemctl start docker
    > sudo docker info | grep 'Docker Root Dir'   # must print /acsa/docker
    > sudo docker images                           # pre-existing images still listed
    >
    > # 6. Park the old dir; delete after the Parakeet build succeeds
    > sudo mv /var/lib/docker /var/lib/docker.old
    > ```
    >
    > If `/etc/docker/daemon.json` does not exist yet, create it with `echo '{"data-root": "/acsa/docker"}' | sudo tee /etc/docker/daemon.json` instead of the `jq` line. Skip steps 3 and 6 on a host where Docker has never been started (the dir is empty).

    > **If `docker build` fails with `permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock`**, your user is not in the `docker` group. Add it once and start a new shell so the group takes effect (membership in `docker` is effectively root on the host — only do this on a dev node):
    >
    > ```bash
    > sudo usermod -aG docker $USER
    > newgrp docker     # or log out + log back in
    > id | tr ',' '\n' | grep docker   # confirm 'docker' is in your groups
    > ```
    >
    > For a one-off, you can instead prefix each command with `sudo` (`sudo docker build ...`, `sudo docker save ...`).

    > **`--network host` is required during `docker build`.** This node's `/etc/docker/daemon.json` sets `"bridge": "none"` and `"iptables": false` so it doesn't conflict with k3s/CNI iptables rules. As a side effect, build containers have no default network and steps like `apt-get update` fail with `Temporary failure resolving 'archive.ubuntu.com'`. Build with `--network host` so the build container uses the host's network stack for DNS and outbound HTTP. This affects build-time only; the resulting image's runtime networking is unaffected.

    > **The Dockerfile pins `pytorch-lightning>=2.2.1,<2.5`.** `nemo_toolkit==2.0.0` declares `pytorch-lightning>2.2.1` with no upper bound, so pip resolves the latest 2.6.x. That release removed `NeptuneLogger` from `pytorch_lightning.loggers`, which `nemo.utils.exp_manager` imports unconditionally — the container crashes at startup with `ImportError: cannot import name 'NeptuneLogger' from 'pytorch_lightning.loggers'`. The Dockerfile installs a 2.4.x build in a follow-on `pip install` layer so this resolves itself; do not remove that line.

    ```bash
    cd /container-images/parakeet
    docker build --network host -t parakeet-server:local .
    # The resulting image is ~21 GB. Stream-piping `docker save` straight into
    # `k3s ctr images import -` is unreliable at this size (the server EOFs
    # after ~30 s). Land the tar on /acsa first, then import from the file.
    sudo docker save -o /acsa/parakeet.tar parakeet-server:local
    sudo /usr/local/bin/k3s ctr -n k8s.io images import /acsa/parakeet.tar
    sudo rm -f /acsa/parakeet.tar
    ```

    Verify the image is present in k3s containerd:

    ```bash
    sudo /usr/local/bin/k3s ctr -n k8s.io images ls | grep parakeet-server
    ```

2. **Foundry CA issuer is Ready.** The Certificate is issued by the same `foundry-local-operator-ca-issuer` ClusterIssuer used by Qwen3:

    ```bash
    kubectl get clusterissuer foundry-local-operator-ca-issuer
    ```

### Apply Parakeet

```bash
kubectl apply -f foundry_yamls/parakeet_gpu.yaml
kubectl apply -f foundry_yamls/parakeet_gpu_cert.yaml
```

### Watch the Parakeet deployment come up

```bash
kubectl -n parakeet get pods,svc,ingress
kubectl -n parakeet logs -l app.kubernetes.io/name=parakeet-gpu -f
kubectl -n parakeet get certificate parakeet-gpu-local-cert
```

The first start downloads the model from HuggingFace (~2.4 GB) into the PVC, so the pod can take several minutes to become `Ready`. Subsequent restarts hit the cache and start in seconds.

### Parakeet TLS certificate

[`foundry_yamls/parakeet_gpu_cert.yaml`](../cobotpoc/yamls/foundry_yamls/parakeet_gpu_cert.yaml) creates a `Certificate` named `parakeet-gpu-local-cert` in the `parakeet` namespace. SANs cover both the public hostname (`parakeet-gpu.local`) and the in‑cluster Service DNS names. cert‑manager populates the `parakeet-gpu-local-tls` Secret, which the Ingress in `parakeet_gpu.yaml` already references — **no Ingress patching needed** (unlike Qwen3, the Ingress is written by hand here, not by the operator, so the `tls` block and the host SAN are correct from the start).

Wait for `READY: True`:

```bash
kubectl -n parakeet get certificate parakeet-gpu-local-cert
kubectl -n parakeet describe certificate parakeet-gpu-local-cert | tail -20
```

### Add `parakeet-gpu.local` to your client's `/etc/hosts`

```bash
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "$NODE_IP parakeet-gpu.local" | sudo tee -a /etc/hosts
```

### Parakeet smoke test

Parakeet exposes three REST endpoints (no API key):

| Method | Path | Notes |
| ------ | ---- | ----- |
| `GET` | `/health` | Liveness/readiness probe target. |
| `GET` | `/v1/models` | Lists the loaded model id. |
| `POST` | `/v1/audio/transcriptions` | OpenAI‑compatible multipart upload: `file=@<audio>`. |

```bash
# Health and model listing
curl -k https://parakeet-gpu.local/health
curl -k https://parakeet-gpu.local/v1/models

# Transcribe an audio file (16 kHz mono WAV works best)
curl -k -X POST https://parakeet-gpu.local/v1/audio/transcriptions \
  -F "file=@some.wav"
```

Expected: HTTP 200 with `{"status":"ok"}` from `/health`, a JSON list containing `nvidia/parakeet-tdt-0.6b-v2` from `/v1/models`, and a transcription JSON (`{"text": "..."}`) from `/v1/audio/transcriptions`.

If the first transcription request times out, check the pod logs — it is still downloading the model from HuggingFace into the PVC. Once cached, requests complete in well under a second on GPU.

---

## 5. Deploy the Vision detector service (CPU)

`vision-service` is a stateless FastAPI wrapper around four detectors extracted from `cobotpoc.py` (`lookForCube`, `lookForBowl`, `lookForHand`, `lookForMisc`). Like Parakeet it is **not** in the `foundry-local-catalog` — you build the image locally, import it into k3s containerd, and apply a plain `Namespace` + `PVC` + `Deployment` + `Service` + `Certificate` + `Ingress` (all in one file: [`yamls/vision_yamls/vision_deployment.yaml`](../yamls/vision_yamls/vision_deployment.yaml)).

It runs **CPU-only on purpose** to side-step the same `nvidia-container-toolkit 1.17` / CDI hook bug that the Qwen3 / Parakeet GPU pods work around with `runtimeClassName: nvidia` + legacy `NVIDIA_VISIBLE_DEVICES`. CPU latency for OWL-ViT base + MediaPipe HandLandmarker is well under 200 ms per call.

Field notes:

| Field | Why this value |
| ----- | -------------- |
| `metadata.namespace: vision` | Dedicated namespace; not under `foundry-local-operator`. |
| `image: vision-service:local` + `imagePullPolicy: Never` | Loaded directly into k3s containerd; no registry. |
| `nodeSelector.kubernetes.io/hostname: <node>` | Built-in label every node carries automatically. Pin to the host that owns `/dev/video0`. Change this value when deploying on a different machine — no custom labeling needed. |
| `securityContext.privileged: true` | Required for `/dev/video0`: the device cgroup whitelist denies V4L2 I/O on non-privileged containers, even with a `hostPath` device mount. |
| `volumes.dev-video0 (hostPath, type: CharDevice)` | The kubelet validates the host path exists and is a char device before starting the container. Make sure `/dev/video0` is present on the target node (`ls -l /dev/video*`). |
| `PersistentVolumeClaim vision-model-cache` (5 Gi, default `local-path`) | Caches OWL-ViT (~580 MB) so the pod doesn't re-download from HuggingFace on every restart. Provisioned dynamically under `/var/lib/rancher/k3s/storage` (now symlinked to `/acsa/k3s/storage`). |
| `strategy.type: Recreate` | Only one webcam on the node — a rolling update would race the new pod against the old one for `/dev/video0`. |
| `startupProbe.failureThreshold: 60` (10 min) | Cold start downloads OWL-ViT before `/readyz` returns 200. |
| Ingress `proxy-buffering: "off"` + `proxy-read-timeout: 86400` | The MJPEG stream is `multipart/x-mixed-replace` — buffering would hold every frame until a flush that never comes. |
| Ingress `proxy-body-size: 16m` | `/v1/detect` responses include the annotated frame as base64 JPEG. |
| **No `auth-snippet` / no API key** | The FastAPI wrapper has no built-in auth. Rely on TLS + the private network — **do not expose publicly**. |

### Vision prerequisites

1. **Build the image** on the node and import it into k3s containerd. The Dockerfile, `app/`, and `hand_landmarker.task` live under [`/container_images/vision/`](../container_images/vision/):

    > **Same build environment as Parakeet.** Docker's data-root is already on `/acsa/docker` (see [Parakeet step 1](#parakeet-prerequisites)), so the ~2.6 GB image fits comfortably. `--network host` is still required because `/etc/docker/daemon.json` sets `"bridge": "none"`.

    ```bash
    cd /home/clouduser/container_images/vision
    sudo docker build --network host -t vision-service:local .
    # The image is ~2.6 GB. Use the same "save to /acsa, then import from file"
    # pattern used for Parakeet — even at this size, streaming `docker save`
    # straight into `k3s ctr images import -` is the brittle path.
    sudo docker save -o /acsa/vision.tar vision-service:local
    sudo /usr/local/bin/k3s ctr -n k8s.io images import /acsa/vision.tar
    sudo rm -f /acsa/vision.tar
    ```

    Verify the image is present in k3s containerd:

    ```bash
    sudo /usr/local/bin/k3s ctr -n k8s.io images ls | grep vision-service
    ```

    Expected: one line ending in `docker.io/library/vision-service:local ... 2.5 GiB ...`.

2. **Confirm `/dev/video0` exists on the target node.** The Deployment hostPath-mounts it with `type: CharDevice`, so the pod won't start if the device is absent.

    ```bash
    ls -l /dev/video*
    ```

3. **Set `nodeSelector.kubernetes.io/hostname` to the target node.** Edit `vision_deployment.yaml` so the value matches `kubectl get nodes -o name`:

    ```yaml
    nodeSelector:
      kubernetes.io/hostname: <your-node-name>
    ```

4. **Foundry CA issuer is Ready** (same ClusterIssuer used by Qwen3 / Parakeet):

    ```bash
    kubectl get clusterissuer foundry-local-operator-ca-issuer
    ```

### Apply Vision

```bash
kubectl apply -f /home/clouduser/yamls/vision_yamls/vision_deployment.yaml
```

This creates the `vision` namespace, the PVC, Deployment, Service, Certificate, and Ingress in one shot.

### Watch the Vision deployment come up

`kubectl get -w` accepts only a single resource type, so use one watch and snapshot the rest:

```bash
kubectl -n vision get pods,svc,ingress,certificate,pvc
kubectl -n vision get pods -w        # ctrl-C when Ready=1/1
kubectl -n vision logs -l app.kubernetes.io/name=vision-service -f
```

On first start the pod stays in `ContainerCreating` for ~10–30 s (local-path PV provisioning), then `Running` but `0/1 Ready` while it downloads OWL-ViT (~580 MB) from HuggingFace into the PVC. Once cached, subsequent restarts hit Ready in seconds.

### Vision TLS certificate

The `Certificate vision-local-cert` (issued by `foundry-local-operator-ca-issuer`) is in the same YAML; cert-manager populates the `vision-local-tls` Secret, which the Ingress already references — **no patching required**.

Wait for `READY: True`:

```bash
kubectl -n vision get certificate vision-local-cert
```

### Add `vision.local` to your client's `/etc/hosts`

```bash
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "$NODE_IP vision.local" | sudo tee -a /etc/hosts
```

### Vision smoke test

Vision exposes the following endpoints (no API key):

| Method | Path | Notes |
| ------ | ---- | ----- |
| `GET` | `/healthz` | Liveness probe; returns immediately once the process is up. |
| `GET` | `/readyz` | Readiness probe; returns 200 once HandLandmarker **and** OWL-ViT are loaded (503 while warming up). |
| `GET` | `/v1/models` | Lists detectors and the loaded model ids. |
| `POST` | `/v1/detect` | `multipart/form-data`: `image=@<jpeg>`, `target_type=cube\|bowl\|hand\|misc`, optional `target_text`, optional `annotate=false`. |
| `GET` | `/stream` | Live MJPEG (`multipart/x-mixed-replace`) preview of `/dev/video0`. |

```bash
# Health and models
curl -k https://vision.local/healthz
curl -k https://vision.local/readyz
curl -k https://vision.local/v1/models


# Detect a pair of scissors with the open-vocabulary misc detector (OWL-ViT)
curl -k -X POST https://vision.local/v1/detect \
  -F "image=@/home/clouduser/prereq/test.jpg" \
  -F "target_type=misc" \
  -F "target_text=scissors"
```

Expected: `{"status":"ok"}` from `/healthz`, a JSON object listing `cube/bowl/hand/misc` from `/v1/models`, and a JSON detection from `/v1/detect` (`detected: true|false`, `pose: {x, y, z, rz}`, optional `annotated_jpeg_b64`).

If `/readyz` returns 503 for a few minutes after the first start, the pod is still downloading OWL-ViT into the PVC — watch the logs.

## Next steps

- [Run mycobot robot](run_cobotpoc.md)


