# Install Foundry Local

This guide walks through installing Foundry Local on a single-node k3s cluster as the non-root user `clouduser`. It covers configuring kubeconfig access, then installing `cert-manager`, `trust-manager`, and the Foundry Local inference operator.

---

## Prerequisites

Before starting, confirm the following are in place:

- **Kubernetes cluster connected to Azure Arc**
  - The cluster must be onboarded as an [Azure Arc-enabled Kubernetes](https://learn.microsoft.com/azure/azure-arc/kubernetes/quickstart-connect-cluster) resource. Verify with:
    ```bash
    kubectl get deploy -n azure-arc
    az connectedk8s show --name <cluster-name> --resource-group <rg-name>
    ```
- **Kubernetes version 1.29+**
  - Confirm with:
    ```bash
    kubectl version -o yaml | grep -E 'gitVersion'
    ```
- **Install `kubectl`** (skip if already present from k3s)
  ```bash
  curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
  sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
  kubectl version --client
  ```
- **Install `helm`** (v3.x required)
  ```bash
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
  helm version
  ```

Additional host requirements:

| Requirement | Check | Notes |
|-------------|-------|-------|
| Network egress | `curl -I https://mcr.microsoft.com` | Needed to pull the inference-operator OCI chart and Jetstack charts. |
| Sudo access | `sudo -v` | Needed once to copy the k3s kubeconfig into `~/.kube/config`. |

If any of the above is missing, install/fix it before proceeding.

---

## Disable Traefik (k3s default)

k3s ships with Traefik bound to port 443. The Foundry Local operator's Ingress requires the community `ingress-nginx` controller (see [Docs/Nginx_setup.md](Docs/Nginx_setup.md)), and NGINX cannot bind to the same port until Traefik is scaled down.

```bash
kubectl scale deployment traefik -n kube-system --replicas=0
```

> **Note:** This is reversible — set replicas back to `1` to re-enable Traefik.

---


## Install cert-manager

Foundry Local requires `cert-manager` for issuing TLS certificates to model deployments.

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update

helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.19.2 \
  --set crds.enabled=true \
  --set crds.keep=true \
  --set image.tag=v1.19.2 \
  --set webhook.image.tag=v1.19.2 \
  --set cainjector.image.tag=v1.19.2 \
  --set acmesolver.image.tag=v1.19.2 \
  --set startupapicheck.image.tag=v1.19.2 \
  --wait
```

### Verify cert-manager

```bash
kubectl get pods -n cert-manager
kubectl get crds | grep cert-manager.io
```

All pods (`cert-manager`, `cert-manager-cainjector`, `cert-manager-webhook`) should be `Running` / `Ready`.

---

## Install trust-manager

`trust-manager` distributes CA bundles (used by Foundry Local for backend TLS verification) and is installed into the same `cert-manager` namespace.

```bash
helm upgrade --install trust-manager jetstack/trust-manager \
  --namespace cert-manager \
  --version v0.20.3 \
  --set image.tag=v0.20.3 \
  --set defaultPackage.enabled=false \
  --set secretTargets.enabled=true \
  --set secretTargets.authorizedSecretsAll=true \
  --wait
```

### Verify trust-manager

```bash
kubectl get pods -n cert-manager -l app.kubernetes.io/name=trust-manager
kubectl get crds | grep trust.cert-manager.io
```

The `trust-manager` pod should be `Running` / `Ready` and the `bundles.trust.cert-manager.io` CRD should be present.

---

## Install the Foundry Local inference operator

The inference operator chart is published to Microsoft Container Registry (MCR) as an OCI artifact, so it is installed directly with `helm upgrade --install` (no `helm repo add` needed).

```bash
helm upgrade --install inference-operator oci://mcr.microsoft.com/microsoft.foundry/foundrylocalenabledbyarc/helmcharts/helm/inference-operator --version 0.260430.8 -n foundry-local-operator --create-namespace --set entraAuth.enabled=false 
```

### Verify the operator

```bash
kubectl get pods -n foundry-local-operator
kubectl get crd | grep foundry
```

Expected output:

```text
NAME                                  READY   STATUS    RESTARTS   AGE
inference-operator-7d6b474947-xxxxx   2/2     Running   0          60s

inferenceservices.foundrylocal.azure.com   <date>
modeldeployments.foundrylocal.azure.com    <date>
models.foundrylocal.azure.com              <date>
```

The operator pod should reach `Running` / `Ready`, and the three Foundry Local CRDs (`models`, `modeldeployments`, `inferenceservices`) should be registered.

---

## List available models in the Foundry Local catalog

After the inference operator is running, it ships a `ConfigMap` named `foundry-local-catalog` that lists every model the operator can deploy. The command below renders it as a readable table (alias, target device, download size, full model ID):

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

Use any value from the **ALIAS** column as `spec.model.catalog.name` (or `spec.source.catalog.alias` on a `Model`) when creating a `ModelDeployment`.

## Next steps

- [Deploy models](deploy_models.md)