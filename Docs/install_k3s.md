# Installing K3s

K3s is provisioned as part of the machine image setup. The steps below document how K3s is installed and configured.

## Place the K3s data-dir on `/acsa`

The default K3s data directory `/var/lib/rancher/k3s` lives on the root partition (`/dev/sda6`, ~48 GB on this image). Container image stores and pod logs grow quickly there — the Parakeet image alone is ~20 GB on disk after extraction — so route the data home onto the dedicated `/acsa` partition (`/dev/sda11`, ~275 GB) by symlinking `/var/lib/rancher/k3s` to `/acsa/k3s`.

> **Why a symlink instead of K3s's `data-dir:` config key?** K3s embeds the literal path `/var/lib/rancher/k3s/server/tls/*` into the kubeconfigs it writes under `server/cred/*.kubeconfig`. Overriding `data-dir` after install leaves those hard-coded paths dangling and K3s refuses to start with `unable to read client-cert /var/lib/rancher/k3s/server/tls/client-supervisor.crt: no such file or directory`. The symlink keeps the original path resolvable so every embedded reference still works.

### Option A — Fresh install (recommended)

Set this up **before** running the K3s install script in the next section. K3s will lay down etcd state, certs, kubeconfigs, the containerd image store, and pod logs directly on `/acsa`.

```bash
sudo mkdir -p /acsa/k3s
sudo chown root:root /acsa/k3s
sudo chmod 0700 /acsa/k3s
sudo mkdir -p /var/lib/rancher
sudo ln -s /acsa/k3s /var/lib/rancher/k3s
ls -ld /var/lib/rancher/k3s   # should print 'lrwxrwxrwx ... -> /acsa/k3s'
```

### Option B — Migrate an existing install

If K3s is already running on `/var/lib/rancher/k3s`, move its data once. The cluster is briefly down (~5 min on a 25 GB data-dir); running pods auto-recover on restart.

```bash
# 1. Stop k3s. Orphan containerd-shim processes survive the stop by design
#    (KillMode=process) and are harmless — they get re-adopted on restart.
sudo systemctl stop k3s

# 2. Inventory current data-dir + free space
sudo du -sh /var/lib/rancher/k3s
df -h /acsa

# 3. rsync to /acsa/k3s. -H preserves containerd's overlayfs hardlinks
#    (critical); -A is intentionally omitted because rsync 3.4.1 on Azure
#    Linux 3.0 has no ACL support and would error out.
sudo mkdir -p /acsa/k3s
sudo chown root:root /acsa/k3s
sudo chmod 0700 /acsa/k3s
sudo rsync -aHX --numeric-ids --info=progress2 /var/lib/rancher/k3s/ /acsa/k3s/

# 4. Replace the original path with a symlink
sudo mv /var/lib/rancher/k3s /var/lib/rancher/k3s.old
sudo ln -s /acsa/k3s /var/lib/rancher/k3s

# 5. Start k3s and verify
sudo systemctl start k3s
sudo /usr/local/bin/k3s kubectl get nodes                   # should be Ready
sudo /usr/local/bin/k3s ctr -n k8s.io images ls | wc -l     # image count unchanged

# 6. Once pods are Running, free the root partition
sudo /usr/local/bin/k3s kubectl get pods -A --no-headers | awk '{print $4}' | sort | uniq -c
sudo rm -rf /var/lib/rancher/k3s.old
```

> **Cleanup tip after migration.** Disk-pressure during the failed pre-migration extracts can leave many pods in `Evicted`/`Error`/`ContainerStatusUnknown` state. Sweep them with `kubectl get pods -A -o json | jq -r '.items[] | select(.status.reason=="Evicted" or .status.phase=="Failed") | "\(.metadata.namespace) \(.metadata.name)"' | xargs -L1 kubectl delete pod -n` (or just delete by `--field-selector=status.phase=Failed`).

## Install K3s

Run the official K3s install script. K3s bundles Traefik as the default ingress controller. Disable this to use ingress-nginx instead.

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_SKIP_SELINUX_RPM=true sh -s - --disable traefik
```


This installs the K3s binary to `/usr/local/bin/k3s` and creates a systemd service (`k3s.service`) that starts automatically. With the symlink from the previous section in place, all of K3s's state lives under `/acsa/k3s`.


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

## Make kubectl work for your user

The k3s kubeconfig at `/etc/rancher/k3s/k3s.yaml` is owned by `root` with mode `0600`, so `clouduser` cannot read it directly. Create a personal copy at the default location `kubectl` (and `helm`) load automatically. 

```bash
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown "$USER:$(id -gn)" ~/.kube/config
chmod 600 ~/.kube/config
kubectl get nodes
```

No `KUBECONFIG` export is required — `kubectl` and `helm` discover `~/.kube/config` by default.

## Enable kubectl on your Windows machine (optional)

- Copy the content of the /home/clouduser/.kube/config to a local file on your Windows machine and save that file as .kubeconfig
- Change the server IP in this config to use the Node IP. 
- Open the Command Prompt and run the command

```bash
set KUBECONFIG=/your/path/to/.kubeconfig
```

- Verify that your kubeconfig is pointing to right cluster

```bash
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'
```

## Arc enable your Kubernetes cluster

You can manage this Kubernetes cluster using Arc. 

```azurecli
az connectedk8s connect --resource-group <ANY_RESOURCE_GROUP> --name <ANY_NAME> --kube-config "<PATH-TO-KUBECONFIG>"
``` 

Once this command completes, you can see this cluster in the Azure Portal.

## Next steps

- [Setup Nginx Ingress](connect.md)

