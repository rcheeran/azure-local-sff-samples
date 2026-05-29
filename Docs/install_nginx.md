# Install the Community ingress-nginx Controller

## Install Helm

 **Install `helm`** (v3.x required)

  ```bash
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
  helm version
  ```

## Install Nginx
> **Critical:** There are two NGINX controllers with similar names. The Foundry operator only works with the **community** version.

| Controller | Annotation Prefix | Helm Repo |
|-----------|------------------|-----------|
| ❌ NGINX Inc (commercial) | `nginx.org/*` | `helm.nginx.com/stable` |
| ✅ Community ingress-nginx | `nginx.ingress.kubernetes.io/*` | `kubernetes.github.io/ingress-nginx` |

The `externalIPs` setting below is needed because k3s lacks a real LoadBalancer provider. Discover the node's primary IP and export it so the install command stays portable:

```bash
NODE_IP=$(kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "Using NODE_IP=$NODE_IP"
```

> If you have multiple nodes or interfaces, set `NODE_IP` explicitly to the address you want NGINX to bind to (e.g. `export NODE_IP=<your-node-ip>`).

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.service.externalIPs[0]="$NODE_IP" \
  --set controller.ingressClassResource.default=true
```

### Verify

```bash
kubectl get pods -n ingress-nginx
kubectl get svc -n ingress-nginx
kubectl get ingressclass
```

Expected output (the `EXTERNAL-IP` column should match `$NODE_IP`):
```
NAME                                       READY   STATUS    RESTARTS   AGE
ingress-nginx-controller-xxxxxxxxx-xxxxx   1/1     Running   0          30s

NAME                                 TYPE           CLUSTER-IP    EXTERNAL-IP     PORT(S)
ingress-nginx-controller             LoadBalancer   10.43.x.x     <NODE_IP>       80:xxxxx/TCP,443:xxxxx/TCP
```

#### Confirm `nginx` is the default IngressClass

The Foundry Local operator picks up the cluster's default IngressClass when generating model Ingress resources. Verify:

```bash
kubectl get ingressclass nginx \
  -o jsonpath='{.metadata.annotations.ingressclass\.kubernetes\.io/is-default-class}{"\n"}'
```

Expected output:

```text
true
```

If it prints empty or `false`, mark it as default:

```bash
kubectl annotate ingressclass nginx \
  ingressclass.kubernetes.io/is-default-class=true --overwrite
```

> Only one IngressClass should be the default at a time. If another class (e.g. `traefik`) is also marked default, remove its annotation:
>
> ```bash
> kubectl annotate ingressclass traefik \
>   ingressclass.kubernetes.io/is-default-class- 
> ```

---