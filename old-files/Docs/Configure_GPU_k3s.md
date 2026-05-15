# Configure GPU in k3s and Verify NVIDIA Access from Kubernetes

This guide shows how to confirm that an NVIDIA GPU is usable from a k3s cluster.

## Goal

At the end, the node should advertise:
- `nvidia.com/gpu` in node Capacity
- `nvidia.com/gpu` in node Allocatable

And a GPU test pod should run `nvidia-smi` successfully.

## 1) Verify GPU on the host first

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

## 2) Verify container runtime has NVIDIA support

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

## 3) Verify GPU Operator / device plugin health

If you use NVIDIA GPU Operator, confirm all core pods are healthy:

```bash
kubectl get pods -n gpu-operator
```

Key pods to look for:
- `nvidia-device-plugin-daemonset-*`
- `gpu-feature-discovery-*`
- `nvidia-operator-validator-*`
- `nvidia-dcgm-exporter-*`

If `nvidia-device-plugin` is stuck in `Init` or `Pending`, inspect events:

```bash
kubectl describe pod -n gpu-operator <device-plugin-pod-name>
```

Common error:
- `no runtime for "nvidia" is configured`

Meaning:
- Kubernetes can schedule the pod, but container runtime is missing or misconfigured for NVIDIA.

## 4) Check if node advertises GPU resources

Default `kubectl get nodes` does not show a GPU column. Use custom columns:

```bash
kubectl get nodes -o custom-columns="NAME:.metadata.name,READY:.status.conditions[?(@.type=='Ready')].status,GPU_CAPACITY:.status.capacity.nvidia\\.com/gpu,GPU_ALLOCATABLE:.status.allocatable.nvidia\\.com/gpu"
```

Expected example:
- `GPU_CAPACITY: 1`
- `GPU_ALLOCATABLE: 1`

If values are `<none>`, device plugin/runtime path is still not healthy.

## 5) Run an end-to-end GPU test pod

Create `gpu-test-pod.yaml`:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-test
spec:
  restartPolicy: Never
  containers:
  - name: cuda-test
    image: nvidia/cuda:12.2.0-runtime-ubuntu22.04
    command: ["nvidia-smi"]
    resources:
      limits:
        nvidia.com/gpu: 1
      requests:
        nvidia.com/gpu: 1
```

Apply and inspect:

```bash
kubectl apply -f gpu-test-pod.yaml
kubectl get pod gpu-test -w
kubectl logs gpu-test
```

Expected:
- Pod reaches `Completed`.
- Logs contain `nvidia-smi` output with detected GPU.

Cleanup:

```bash
kubectl delete pod gpu-test
```

## 6) Troubleshooting map

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
