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

To inspect every variant of a single model (e.g. all CPU/GPU/OpenVINO/TensorRT builds for `qwen2.5-0.5b`):

```bash
kubectl get cm foundry-local-catalog -n foundry-local-operator -o json \
  | jq -r '.data."catalog.json"' \
  | jq '.models[] | select(.alias=="qwen2.5-0.5b") | {alias, variants}'
```

The values to remember from this step:

- **ALIAS** — used as a friendly name in the `Model` CR `spec.source.catalog.alias`.
- **MODEL_ID** — the form `qwen2.5-0.5b-instruct-generic-cpu:4`. Split it into `name` (`qwen2.5-0.5b-instruct-generic-cpu`) and `version` (`"4"`) when pinning a specific variant in a `ModelDeployment`.

---

## 2. Deploy the Qwen2.5 0.5B CPU model

Save the following as [`foundry_yamls/model_qwen2.5_cpu.yaml`](../foundry_yamls/model_qwen2.5_cpu.yaml):

```yaml
apiVersion: foundrylocal.azure.com/v1
kind: ModelDeployment
metadata:
  name: qwen2-5-0-5b
  namespace: foundry-local-operator
  labels:
    app.kubernetes.io/name: qwen2-5-0-5b
    app.kubernetes.io/component: inference
    foundry.azure.com/hardware: cpu
spec:
  displayName: "Qwen2.5 0.5B CPU"
  model:
    catalog:
      name: qwen2.5-0.5b-instruct-generic-cpu
      version: "4"
  workloadType: generative
  compute: cpu
  replicas: 1
  resources:
    requests:
      cpu: "1"
      memory: "2Gi"
    limits:
      cpu: "2"
      memory: "4Gi"
  # This section is relevant in case you use an Ingress Controller
  endpoint:
    enabled: true
    host: qwen-cpu.local
    ingressClassName: nginx
    path: /
    pathType: Prefix
```

Field notes:

| Field | Why this value |
|-------|----------------|
| `metadata.name: qwen2-5-0-5b` | Periods are **not** allowed in `ModelDeployment.metadata.name` (DNS-1035). Use hyphens. |
| `spec.model.catalog.{name,version}` | Pins the exact CPU variant (`qwen2.5-0.5b-instruct-generic-cpu:4`) found in step 1. |
| `spec.workloadType: generative` | Enum: `generative` (LLM/chat) or `predictive` (classification/detection). |
| `spec.compute: cpu` | Must match the variant's `compute` field. |
| `spec.endpoint.host: qwen-cpu.local` | The hostname clients hit. Must be added to `/etc/hosts` on each client (see step 3). |
| `spec.endpoint.ingressClassName: nginx` | Required for the operator's NGINX-style annotations to take effect. |

### Apply

```bash
kubectl apply -f foundry_yamls/model_qwen2.5_cpu.yaml
```

### Watch the deployment come up

```bash
kubectl get modeldeployment -n foundry-local-operator -w
kubectl get pods -n foundry-local-operator -l app.kubernetes.io/name=qwen2-5-0-5b
kubectl get svc,ingress -n foundry-local-operator
```

The operator will create a Pod, Service (`qwen2-5-0-5b`), and Ingress (`qwen2-5-0-5b`). The Ingress has the NGINX annotations but **no TLS block** — that is added by the certificate step below.

---

## 3. Issue a TLS certificate for the Ingress hostname

The operator-generated Ingress points at `qwen-cpu.local`, but the cluster's default certificate doesn't include that as a SAN. Create a dedicated `Certificate` (issued by `foundry-local-operator-ca-issuer`, installed with the operator) that covers both the public hostname and the in-cluster Service DNS names.

Save as [`foundry_yamls/model_qwen2.5_cpu_cert.yaml`](../foundry_yamls/model_qwen2.5_cpu_cert.yaml):

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: qwen2-5-0-5b-local-cert
  namespace: foundry-local-operator
spec:
  secretName: qwen2-5-0-5b-local-tls
  duration: 2160h   # 90 days
  renewBefore: 360h # 15 days
  commonName: qwen-cpu.local
  dnsNames:
    - qwen-cpu.local
    - qwen2-5-0-5b
    - qwen2-5-0-5b.foundry-local-operator
    - qwen2-5-0-5b.foundry-local-operator.svc
    - qwen2-5-0-5b.foundry-local-operator.svc.cluster.local
  issuerRef:
    name: foundry-local-operator-ca-issuer
    kind: ClusterIssuer
```

> The internal SANs (`qwen2-5-0-5b.*`) match the Service the operator creates for `metadata.name: qwen2-5-0-5b`. They allow backend TLS verification by NGINX (see [Nginx_setup.md](Nginx_setup.md) Step 6). If you only need the external Ingress to work, just `qwen-cpu.local` is enough.

### Apply and verify

```bash
kubectl apply -f foundry_yamls/model_qwen2.5_cpu_cert.yaml

kubectl get certificate qwen2-5-0-5b-local-cert -n foundry-local-operator
kubectl describe certificate qwen2-5-0-5b-local-cert -n foundry-local-operator | tail -20
```

Wait for `READY: True`. The corresponding TLS secret will be:

```bash
kubectl get secret qwen2-5-0-5b-local-tls -n foundry-local-operator
```

### Patch the Ingress to use the new cert

```bash
kubectl patch ingress qwen2-5-0-5b -n foundry-local-operator --type merge -p '{
  "spec": {
    "tls": [{
      "hosts": ["qwen-cpu.local"],
      "secretName": "qwen2-5-0-5b-local-tls"
    }]
  }
}'
```

### Remove the operator's broken `rewrite-target` annotation

The operator generates the Ingress with:

```yaml
nginx.ingress.kubernetes.io/rewrite-target: /$2
```

`/$2` is a regex backreference, but the rule's `path: /` (with `pathType: Prefix`) has **no capture groups**, so NGINX rewrites every request to empty/`/` and the backend returns `404` for `/v1/models`, `/v1/chat/completions`, etc. (You can confirm this in the model pod's nginx access log: `GET / HTTP/1.1 404`.)

The model server already serves directly at `/v1/...`, so no rewrite is needed. Remove the annotation:

```bash
kubectl annotate ingress qwen2-5-0-5b -n foundry-local-operator \
  nginx.ingress.kubernetes.io/rewrite-target-
```

Verify the annotation is gone:

```bash
kubectl get ingress qwen2-5-0-5b -n foundry-local-operator \
  -o jsonpath='{.metadata.annotations}{"\n"}' | tr ',' '\n' | grep -i rewrite || echo "rewrite-target removed"
```

### Add the host to your client's `/etc/hosts`

```bash
NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "$NODE_IP qwen-cpu.local" | sudo tee -a /etc/hosts
```

### Smoke test

```bash
API_KEY=$(kubectl get secret qwen2-5-0-5b-api-keys -n foundry-local-operator \
  -o jsonpath='{.data.primary-key}' | base64 -d)

# List models
curl -k "https://qwen-cpu.local/v1/models" \
  -H "Authorization: Bearer $API_KEY"

# Chat completion
curl -k -X POST "https://qwen-cpu.local/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-0.5b-instruct-generic-cpu:4",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 20
  }'
```

Expected: HTTP 200 with a JSON list containing `qwen2.5-0.5b-instruct-generic-cpu:4` for `/v1/models`, and a chat completion JSON response for `/v1/chat/completions`.

The model server is OpenAI-API compatible and exposes `/v1/models`, `/v1/chat/completions`, `/v1/completions`, and `/v1/embeddings` (where supported).

---

## 4. List live endpoints on a model deployment

The model server doesn't ship an OpenAPI spec, so the easiest way to enumerate what's actually responding is to probe a known set of paths from inside the cluster (so you bypass any Ingress/rewrite issues).

```bash
DEPLOY=qwen2-5-0-5b
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

### Probe via the Ingress (external view)

After the Ingress and `/etc/hosts` are configured (see step 3), the same probe works externally:

```bash
HOST=qwen-cpu.local
API_KEY=$(kubectl get secret qwen2-5-0-5b-api-keys -n foundry-local-operator \
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
