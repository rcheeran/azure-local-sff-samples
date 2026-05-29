# Configure GPU in k3s and verify NVIDIA GPU access from Kubernetes

This guide shows how to confirm that an NVIDIA GPU is usable from a k3s cluster.

## Goal

At the end, the node should advertise:
- `nvidia.com/gpu` in node Capacity
- `nvidia.com/gpu` in node Allocatable

And a GPU test pod should run `nvidia-smi` successfully.

## Verify GPU on the host first

Kubernetes cannot expose GPU resources if the host driver stack is broken.

Run:

```bash
nvidia-smi
```

Expected:
- Driver version and CUDA version are shown.
- One or more GPUs are listed.

If this fails, also check:

```bash
lsmod | grep -E '^nvidia|nvidia_'
lspci | grep -Ei 'vga|3d|nvidia'
ls -l /dev/nvidia*
```

Interpretation:
- `lspci` shows hardware visibility at PCI level.
- `lsmod` confirms kernel modules are loaded.
- `/dev/nvidia*` confirms device nodes exist.

## Verify container runtime has NVIDIA support

k3s uses containerd. NVIDIA runtime/toolkit must be configured correctly.

Check for runtime classes:

```bash
kubectl get runtimeclass
```

You should see entries such as `nvidia`, `nvidia-cdi`, or similar.

If runtime is not configured, configure NVIDIA runtime for containerd:

```bash
sudo nvidia-ctk runtime configure --runtime=containerd --set-as-default
sudo systemctl restart containerd
sudo systemctl restart k3s
```

## Install the GPU device plugin

Follow the steps mentioned [here](https://github.com/kianaharris4/AKS-Arc-Private-Previews/blob/kibarilar-updates/docs/how-to/05A-deploy-GPU-workload.md)

## Check if node advertises GPU resources

Default `kubectl get nodes` does not show a GPU column. Use custom columns:

```bash
kubectl get nodes -o custom-columns="NAME:.metadata.name,READY:.status.conditions[?(@.type=='Ready')].status,GPU_CAPACITY:.status.capacity.nvidia\\.com/gpu,GPU_ALLOCATABLE:.status.allocatable.nvidia\\.com/gpu"
```

Expected example:
- `GPU_CAPACITY: 1`
- `GPU_ALLOCATABLE: 1`

If values are `<none>`, device plugin/runtime path is still not healthy.


## Make nvidia the default containerd runtime in k3s
This is the standard k3s approach when an operator generates pods that don't set runtimeClassName. k3s supports overlaying its containerd config via a .tmpl file. Drop in a containerd config template that defaults to nvidia

```bash
sudo tee /var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl >/dev/null <<'EOF'
{{ template "base" . }}

[plugins."io.containerd.cri.v1.runtime".containerd]
  default_runtime_name = "nvidia"
EOF
```

Restart k3s so the new config is rendered & loaded
```bash
sudo systemctl restart k3s
```


## Share one physical GPU across multiple pods (time-slicing)

By default the `nvidia-device-plugin` advertises `nvidia.com/gpu: 1` for one
physical GPU, so only **one** pod with `resources.limits.nvidia.com/gpu: 1` can
be `Running` on the node at a time. Additional GPU pods will stay `Pending`.

Time-slicing makes the device plugin advertise `nvidia.com/gpu: N` "virtual"
slots backed by the same physical GPU, so N pods can hold a slot simultaneously.

What it does **not** do:
- It does not partition the GPU. Every pod still sees the whole GPU.
- It does not isolate VRAM. Pods' VRAM allocations **sum** on the device — if
  they exceed total VRAM, one will CUDA-OOM. You are responsible for sizing.
- It does not give parallel compute. The driver time-multiplexes the SMs
  between contexts.

Choose `N` based on how many GPU pods you expect to run concurrently, plus a
small slot of headroom. Going larger than that does not unlock more compute,
it just allows VRAM overcommit. Start small (e.g. 4) and grow later.

### Step 1: create the time-slicing ConfigMap

```bash
kubectl apply -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: nvidia-device-plugin-config
  namespace: kube-system
data:
  config.yaml: |
    version: v1
    flags:
      migStrategy: none
    sharing:
      timeSlicing:
        resources:
          - name: nvidia.com/gpu
            replicas: 4
YAML
```

### Step 2: patch the device-plugin DaemonSet to load the ConfigMap

Mounts the ConfigMap at `/etc/k8s-device-plugin/` and passes
`--config-file=...` to the plugin container.

```bash
kubectl -n kube-system patch ds nvidia-device-plugin-daemonset \
  --type=strategic --patch "$(cat <<'YAML'
spec:
  template:
    spec:
      containers:
        - name: nvidia-device-plugin-ctr
          args:
            - --config-file=/etc/k8s-device-plugin/config.yaml
          volumeMounts:
            - name: device-plugin-config
              mountPath: /etc/k8s-device-plugin
              readOnly: true
      volumes:
        - name: device-plugin-config
          configMap:
            name: nvidia-device-plugin-config
YAML
)"

kubectl -n kube-system rollout status ds nvidia-device-plugin-daemonset --timeout=60s
```

The rollout restarts only the device-plugin pod (~10s). Existing GPU pods keep
their device handles and are **not** killed; only the kubelet's advertised
`Allocatable` changes for future scheduling.

### Step 3: verify

```bash
kubectl get nodes -o custom-columns="NAME:.metadata.name,GPU_CAP:.status.capacity.nvidia\\.com/gpu,GPU_ALLOC:.status.allocatable.nvidia\\.com/gpu"
```

Expected:
- `GPU_CAP: 4`
- `GPU_ALLOC: 4`

Also confirm time-slicing is loaded in the plugin logs:

```bash
kubectl -n kube-system logs -l name=nvidia-device-plugin-ds --tail=40 \
  | grep -A6 timeSlicing
```

You should see the `sharing.timeSlicing.resources` block with your `replicas`.

### Growing or shrinking later

To change `N` later, edit the ConfigMap and roll the DaemonSet:

```bash
kubectl -n kube-system edit cm nvidia-device-plugin-config
kubectl -n kube-system rollout restart ds nvidia-device-plugin-daemonset
```

No app-pod restart is required. Existing GPU pods keep running; the node's
`Allocatable` is re-advertised for future scheduling decisions.


## Troubleshooting map

### Symptom A: Node shows `<none>` for GPU capacity
Likely causes:
- Host driver not healthy (`nvidia-smi` fails)
- NVIDIA runtime not configured in containerd
- Device plugin not running

Actions:
1. Fix host driver until `nvidia-smi` works.
2. Reconfigure NVIDIA runtime with `nvidia-ctk` and restart services.
3. Confirm GPU Operator pods are `Running`.

### Symptom B: Device plugin event says `no runtime for "nvidia" is configured`
Cause:
- RuntimeClass exists but containerd runtime config does not include NVIDIA handler.

Action:

```bash
sudo nvidia-ctk runtime configure --runtime=containerd --set-as-default
sudo systemctl restart containerd
sudo systemctl restart k3s
```

### Symptom C: Test pod schedules but fails at startup with NVIDIA hook errors
Example error:
- `No help topic for 'disable-device-node-modification'`

Cause:
- Version mismatch between NVIDIA container toolkit components and GPU Operator expectations.

Actions:
1. Check toolkit version (`nvidia-ctk --version`).
2. Upgrade NVIDIA container toolkit to a version compatible with your GPU Operator release.
3. Restart containerd and k3s.

## 7) Quick verification checklist

Run these in order:

```bash
nvidia-smi
kubectl get pods -n gpu-operator
kubectl get nodes -o custom-columns="NAME:.metadata.name,GPU_CAP:.status.capacity.nvidia\\.com/gpu,GPU_ALLOC:.status.allocatable.nvidia\\.com/gpu"
kubectl apply -f gpu-test-pod.yaml
kubectl logs gpu-test
```

If all pass, NVIDIA GPU is accessible from your k3s Kubernetes cluster.
