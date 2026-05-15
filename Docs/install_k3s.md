# Installing K3s

K3s is provisioned as part of the machine image setup. The steps below document how K3s is installed and configured.

## Install K3s

Run the official K3s install script:

```bash
curl -sfL https://get.k3s.io | sh -
```

This installs the K3s binary to `/usr/local/bin/k3s` and creates a systemd service (`k3s.service`) that starts automatically.

## Disable Traefik

K3s bundles Traefik as the default ingress controller. To disable it (e.g. to use ingress-nginx instead), create or edit the K3s config file:

```bash
sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/config.yaml <<EOF
# Disable the bundled Traefik ingress controller so ingress-nginx can claim
# the host's :80/:443 via k3s ServiceLB (klipper-lb).
disable:
  - traefik
EOF
```

If K3s is already running, restart it to apply the change:

```bash
sudo systemctl restart k3s
```

## Verify the Installation

Check that K3s is running:

```bash
sudo systemctl status k3s
k3s --version
```

Confirm Traefik is not deployed:

```bash
sudo k3s kubectl get pods -A | grep traefik
```

This should return no results.
