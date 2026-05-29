# Connnecting to the Linux machine using SSH  

## Goal

Set up secure SSH access to a Linux machine using the PEM private key, verify
login from a terminal, and connect through the VS Code **Remote - SSH**
extension.

## Prerequisites

- Windows client machine
- Azure Local Linux target machine
- Downloaded PEM private-key generated during Azure provisioning
- VS Code Remote-SSH workflow


## Inputs required

| Variable | Example |
|---|---|
| `<HOST_OR_IP>` | `192.168.1.197` |
| `<LINUX_USER>` | This is fixed and is `clouduser` |
| `<PEM_PATH>` | Path to the PEM file: `C:\Azure Local SFF\Keys\lenovo-demo-ssh.pem` |
| `<WINDOWS_USER>` | current Windows account name |


---

## Step 1 — Configure the SSH client on Windows

Edit your SSH config file at:

```text
C:\Users\<WINDOWS_USER>\.ssh\config
```

Add a host block:

```ssh-config
Host <HOST_OR_IP>
    HostName <HOST_OR_IP>
    User clouduser
    IdentityFile <PEM_PATH>
    IdentitiesOnly yes
    PreferredAuthentications publickey
    PubkeyAuthentication yes
```

**Notes**

- Use forward slashes in `IdentityFile`.
- Keep one clear host entry to avoid conflicting settings.
- If multiple `Host` blocks match the same target, SSH may apply more than one.

---

## Step 2 — Fix PEM file permissions on Windows

If SSH says the key permissions are too open, restrict the ACL so only your
user can read the key. Run in **PowerShell**:

```powershell
ssh-keygen -R <HOST_OR_IP>
$keyPath = "<PEM_PATH>"
icacls $keyPath /inheritance:r
icacls $keyPath /remove:g "BUILTIN\Users" "Everyone" "Authenticated Users"
$user = "$env:USERDOMAIN\$env:USERNAME"
icacls $keyPath /grant:r "${user}:(R)"
icacls $keyPath
```

If ownership issues persist:

```powershell
takeown /f "<PEM_PATH>"
icacls "<PEM_PATH>" /setowner "$env:USERDOMAIN\$env:USERNAME"
```

> In PowerShell, use `$env:USERNAME`, **not** `%USERNAME%`.

---

## Step 3 — Test SSH from a terminal

```powershell
ssh -vvv `
    -o PreferredAuthentications=publickey `
    -o PubkeyAuthentication=yes `
    -o IdentitiesOnly=yes `
    -i "<PEM_PATH>" `
    clouduser@<HOST_OR_IP>
```

**What success looks like**

- No `UNPROTECTED PRIVATE KEY FILE` warning.
- The client attempts public-key authentication.
- Login succeeds without a password prompt (a passphrase prompt is fine if the
  key is encrypted).


## Step 4 — Connect with VS Code Remote SSH

1. Open VS Code.
2. Install the **Remote - SSH** extension (Microsoft).
3. Open the Command Palette &nbsp;— `Ctrl+Shift+P`.
4. Run **Remote-SSH: Connect to Host…**.
5. Select <HOST_OR_IP> base don what you entered in the C:\Users\<WINDOWS_USER>\.ssh\config file. 
6. When prompted, choose **Linux** as the remote platform.

**If VS Code fails but the terminal works**

- Make sure VS Code is using the same SSH config file.
- Confirm the selected host is <HOST_OR_IP>.
- Remove duplicate / conflicting `Host` entries.

---

## Optional — Azure Arc SSH ProxyCommand

If connecting through the Azure Arc relay, add a host entry that uses
`ProxyCommand` with `az ssh arc`:

```ssh-config
Host arc-<HOST_OR_IP>
    HostName <ARC_MACHINE_NAME_OR_ALIAS>
    User clouduser
    ProxyCommand az ssh arc --subscription <SUB_ID> --resource-group <RG> --name <ARC_MACHINE_NAME> --local-user clouduser --private-key-file <PEM_PATH>
    IdentitiesOnly yes
    PreferredAuthentications publickey
    PubkeyAuthentication yes
```

Then test:

```powershell
ssh -vvv arc-<HOST_OR_IP>
```

---

## Troubleshooting checklist

- [ ] PEM file exists at the configured path.
- [ ] PEM file ACL is restricted to your user only.
- [ ] SSH config has correct `HostName`, `User`, `IdentityFile`.
- [ ] No conflicting duplicate `Host` blocks.
- [ ] VS Code Remote-SSH uses the same host entry that works in the terminal.

---

## Next steps

- [Install K3S on your machine](install_k3s.md)
