# NGINX Ingress Setup for Foundry Local Model Deployment

This document consolidates all the fixes needed to get NGINX Ingress working with the Foundry Local `phi-4-cpu` ModelDeployment on a k3s cluster.

## Problem Summary

The Foundry Local operator generates an `Ingress` resource with NGINX-style annotations, but several issues prevented routing from working:
1. Wrong NGINX controller installed (NGINX Inc commercial vs community)
2. Traefik (k3s default) blocking port 443
3. TLS certificate missing `phi-4-cpu.local` SAN
4. Missing CA bundle secret for backend TLS verification
5. k3s LoadBalancer service had no external IP

## Architecture

```
Client (curl)
    │
    ▼ HTTPS :443
phi-4-cpu.local (192.168.1.103)
    │
    ▼
ingress-nginx-controller (LoadBalancer with externalIPs)
    │
    ▼ HTTPS (backend-protocol: HTTPS)
phi-4-cpu Service :5000
    │
    ▼ HTTPS :8443
phi-4-cpu Pod (model server)
```

---

## Prerequisites

- k3s cluster with `cert-manager` installed
- `foundry-local-operator` deployed
- `helm` v3 available
- `KUBECONFIG` configured:
  ```bash
  export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  ```

---

## Step 1: Disable Traefik (k3s Default)

k3s ships with Traefik bound to port 443. NGINX cannot bind to the same port until Traefik is scaled down.

```bash
kubectl scale deployment traefik -n kube-system --replicas=0
```

> **Note:** This is reversible — set replicas back to `1` to re-enable Traefik.

---

## Step 2: Install the Community ingress-nginx Controller

> **Critical:** There are two NGINX controllers with similar names. The Foundry operator only works with the **community** version.

| Controller | Annotation Prefix | Helm Repo |
|-----------|------------------|-----------|
| ❌ NGINX Inc (commercial) | `nginx.org/*` | `helm.nginx.com/stable` |
| ✅ Community ingress-nginx | `nginx.ingress.kubernetes.io/*` | `kubernetes.github.io/ingress-nginx` |

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.service.externalIPs[0]=192.168.1.103 \
  --set controller.ingressClassResource.default=true
```

> Replace `192.168.1.103` with your node's IP. The `externalIPs` setting is needed because k3s lacks a real LoadBalancer provider.

### Verify

```bash
kubectl get pods -n ingress-nginx
kubectl get svc -n ingress-nginx
kubectl get ingressclass
```

Expected output:
```
NAME                                       READY   STATUS    RESTARTS   AGE
ingress-nginx-controller-xxxxxxxxx-xxxxx   1/1     Running   0          30s

NAME                                 TYPE           CLUSTER-IP    EXTERNAL-IP     PORT(S)
ingress-nginx-controller             LoadBalancer   10.43.x.x     192.168.1.103   80:xxxxx/TCP,443:xxxxx/TCP
```

---

## Step 3: Configure ModelDeployment for NGINX

Update the ModelDeployment's `endpoint` block to enable ingress with NGINX:

```yaml
# modeldeployment-phi-4-cpu.yaml
spec:
  ...
  endpoint:
    enabled: true
    host: phi-4-cpu.local
    ingressClassName: nginx
    path: /
    pathType: Prefix
```

Apply the changes:

```bash
kubectl apply -f modeldeployment-phi-4-cpu.yaml
```

---

## Step 4: Create TLS Certificate with Correct SANs

The default `phi-4-cpu-tls-secret` only has internal Kubernetes DNS names. Create a new certificate that **includes `phi-4-cpu.local`** as a SAN:

```yaml
# phi-4-cpu-local-cert.yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: phi-4-cpu-local-cert
  namespace: foundry-local-operator
spec:
  secretName: phi-4-cpu-local-tls
  duration: 2160h  # 90 days
  renewBefore: 360h  # 15 days
  commonName: phi-4-cpu.local
  dnsNames:
    - phi-4-cpu.local
    - phi-4-cpu
    - phi-4-cpu.foundry-local-operator
    - phi-4-cpu.foundry-local-operator.svc
    - phi-4-cpu.foundry-local-operator.svc.cluster.local
  issuerRef:
    name: foundry-local-operator-ca-issuer
    kind: ClusterIssuer
```

Apply and verify:

```bash
kubectl apply -f phi-4-cpu-local-cert.yaml
kubectl get certificate phi-4-cpu-local-cert -n foundry-local-operator
```

Wait for `READY: True`.

---

## Step 5: Patch Ingress to Use the New TLS Certificate

The operator-generated Ingress doesn't include TLS by default. Patch it:

```bash
kubectl patch ingress phi-4-cpu -n foundry-local-operator --type merge -p '{
  "spec": {
    "tls": [{
      "hosts": ["phi-4-cpu.local"],
      "secretName": "phi-4-cpu-local-tls"
    }]
  }
}'
```

---

## Step 6: Create CA Bundle Secret for Backend TLS Verification

The Ingress annotations reference `foundry-local-operator-ca-bundle` for backend TLS verification, but it doesn't exist by default. Create it from the root CA:

```bash
kubectl get secret foundry-local-root-ca -n foundry-local-operator \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > /tmp/ca.crt

kubectl create secret generic foundry-local-operator-ca-bundle \
  -n foundry-local-operator \
  --from-file=ca.crt=/tmp/ca.crt
```

---

## Step 7: Configure DNS Resolution

Add the hostname to `/etc/hosts` on the client machine:

```bash
echo "192.168.1.103 phi-4-cpu.local" | sudo tee -a /etc/hosts
```

---

## Verification

### Test the `/v1/models` Endpoint

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
API_KEY=$(kubectl get secret phi-4-cpu-api-keys -n foundry-local-operator \
  -o jsonpath='{.data.primary-key}' | base64 -d)

curl -k "https://phi-4-cpu.local/v1/models" \
  -H "Authorization: Bearer $API_KEY"
```

Expected: HTTP 200 with model list JSON.

### Test the `/v1/chat/completions` Endpoint

```bash
curl -k -X POST "https://phi-4-cpu.local/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Phi-4-generic-cpu:2",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "max_tokens": 50
  }'
```

Expected: HTTP 200 with chat completion response.

---

## Final Ingress State

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: phi-4-cpu
  namespace: foundry-local-operator
  annotations:
    nginx.ingress.kubernetes.io/backend-protocol: HTTPS
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-ssl-name: phi-4-cpu.foundry-local-operator.svc.cluster.local
    nginx.ingress.kubernetes.io/proxy-ssl-secret: foundry-local-operator/foundry-local-operator-ca-bundle
    nginx.ingress.kubernetes.io/proxy-ssl-verify: "on"
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
    - hosts: [phi-4-cpu.local]
      secretName: phi-4-cpu-local-tls
  rules:
    - host: phi-4-cpu.local
      http:
        paths:
          - backend:
              service:
                name: phi-4-cpu
                port: { number: 5000 }
            path: /
            pathType: Prefix
```

---

## Troubleshooting

### `400 The plain HTTP request was sent to HTTPS port`
NGINX is forwarding HTTP to an HTTPS backend. Check:
- The `backend-protocol: HTTPS` annotation is present
- You're using the **community** ingress-nginx, not NGINX Inc

### `SSL routines::tlsv1 unrecognized name`
The TLS certificate doesn't have `phi-4-cpu.local` as a SAN. Re-run Step 4.

### `Address` field blank on Ingress
The NGINX controller isn't installed or isn't bound to the IP. Re-run Step 2.

### `404 page not found`
The Ingress path doesn't match the request. Verify Step 3 — `path: /` and `pathType: Prefix`.

### Backend cert verification errors in NGINX logs
The CA bundle secret is missing or wrong. Re-run Step 6.

---

## Files Reference

- [modeldeployment-phi-4-cpu.yaml](modeldeployment-phi-4-cpu.yaml) — Foundry ModelDeployment with NGINX endpoint config
- [phi-4-cpu-local-cert.yaml](phi-4-cpu-local-cert.yaml) — cert-manager Certificate with `phi-4-cpu.local` SAN

## Key Lessons

1. **NGINX controller selection matters** — `nginx.org/*` vs `nginx.ingress.kubernetes.io/*` annotations are not interchangeable.
2. **k3s ships with Traefik** — disable it before installing NGINX.
3. **k3s LoadBalancer is limited** — use `externalIPs` to expose the service on a node IP.
4. **TLS certs need correct SANs** — the cert presented by NGINX must include the hostname clients use.
5. **Backend TLS requires a CA bundle** — verify the secret referenced by `proxy-ssl-secret` exists.
