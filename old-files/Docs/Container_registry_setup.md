# Hello World Python Container — Walkthrough

## Overview

This walkthrough covers building a Python "Hello World" container, pushing it to a local registry, and deploying it to a Kubernetes cluster running on the same machine (k3s).

---

## 1. Create the Python App (`app.py`)

A minimal HTTP server using Python's built-in `http.server` module — no external dependencies needed. It listens on port `8080` and responds to any `GET` request with `Hello, World!`.

---

## 2. Create the Dockerfile

```dockerfile
FROM python:3.12-slim   # small base image (~130MB vs ~1GB for full python)
WORKDIR /app
COPY app.py .
EXPOSE 8080             # documents the port (doesn't actually open it)
CMD ["python", "app.py"]
```

---

## 3. Start a Local Container Registry

Kubernetes needs to pull images from *somewhere*. Instead of pushing to Docker Hub, we run a local registry using the official `registry:2` image:

```bash
sudo docker run -d --network host --name local-registry registry:2
```

`--network host` is used because the default bridge networking in this environment doesn't map ports to the host correctly. Host networking means the container shares the host's network stack directly, so the registry listens immediately on `localhost:5000`.

---

## 4. Configure k3s to Trust the Local Registry

By default, k3s (the lightweight Kubernetes running here) refuses to pull from unencrypted HTTP registries. Create `/etc/rancher/k3s/registries.yaml`:

```yaml
mirrors:
  "localhost:5000":
    endpoint:
      - "http://localhost:5000"
```

This tells k3s: *when a pod requests an image from `localhost:5000`, fetch it over plain HTTP*. Restart k3s to apply the change:

```bash
sudo systemctl restart k3s
```

---

## 5. Build and Push the Image

```bash
sudo docker build -t localhost:5000/hello-python:latest .
sudo docker push localhost:5000/hello-python:latest
```

The image tag `localhost:5000/hello-python:latest` tells Docker to treat the local registry as the destination. After pushing, the image is stored in the registry and k3s can pull it.

---

## 6. Create the Kubernetes Manifest (`deployment.yaml`)

Two resources are defined in one file:

### Deployment

Tells Kubernetes to run 1 replica of the container, pulling the image from the local registry. Resource requests and limits are set so the scheduler knows how much capacity the pod needs.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hello-python
spec:
  replicas: 1
  selector:
    matchLabels:
      app: hello-python
  template:
    metadata:
      labels:
        app: hello-python
    spec:
      containers:
        - name: hello-python
          image: localhost:5000/hello-python:latest
          ports:
            - containerPort: 8080
          resources:
            requests:
              cpu: "100m"
              memory: "64Mi"
            limits:
              cpu: "250m"
              memory: "128Mi"
```

### Service (NodePort)

Exposes the pod to traffic from outside the cluster. It maps:

```
external port 30080  →  Service port 80  →  Pod port 8080
```

`NodePort` reserves a port on the cluster node itself, making the app reachable at `http://localhost:30080`.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: hello-python
spec:
  selector:
    app: hello-python
  type: NodePort
  ports:
    - port: 80
      targetPort: 8080
      nodePort: 30080
```

---

## 7. Deploy and Verify

```bash
# Apply both resources from the manifest
kubectl apply -f deployment.yaml

# Wait until the pod is fully running
kubectl rollout status deployment/hello-python

# Test the endpoint
curl http://localhost:30080
# → Hello, World!
```

`kubectl apply` is declarative — it reads the manifest and reconciles the cluster state to match. Kubernetes schedules the pod, k3s pulls the image from the local registry, and the container starts serving traffic.

---

## 8. Update the Container Image After a Change

Use this repeatable flow each time you update the app.

### 1) Edit your app or build inputs

- Update `app.py`, `Dockerfile`, or any files copied into the image.

### 2) Build a new image tag

Use a new version tag instead of reusing `latest`.

```bash
IMAGE_TAG=v2
sudo docker build -t localhost:5000/hello-python:$IMAGE_TAG .
```

### 3) Push the new image to the local registry

```bash
sudo docker push localhost:5000/hello-python:$IMAGE_TAG
```

### 4) Update the running deployment to the new image

```bash
kubectl set image deployment/hello-python hello-python=localhost:5000/hello-python:$IMAGE_TAG
```

### 5) Verify rollout and test

```bash
kubectl rollout status deployment/hello-python
kubectl get pods -l app=hello-python -o wide
curl -s http://localhost:30080
```

You should see the updated response from your app.

### Best Practices

- Always use a unique tag (`v2`, `v3`, commit SHA, or timestamp).
- Avoid relying on `latest` for repeatable deployments.
- Keep `deployment.yaml` updated with the same image tag used in deployment so the manifest stays the source of truth.
