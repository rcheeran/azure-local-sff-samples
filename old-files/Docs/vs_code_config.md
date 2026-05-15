# Configuring SSH Access to a Linux Machine (Windows PEM Key + VS Code)

## Goal

Set up secure SSH access to a Linux machine using a PEM private key, verify
login from a terminal, and connect through the VS Code **Remote - SSH**
extension.

## Applies to

- Windows client machine
- Linux target machine
- PEM private-key authentication
- VS Code Remote-SSH workflow

## Prerequisites

- Machine IP or hostname &nbsp;— e.g. `192.168.1.197`
- Linux username &nbsp;— e.g. `clouduser`
- PEM private key on Windows &nbsp;— e.g. `C:\Azure Local SFF\Keys\lenovo-demo-ssh.pem`
- OpenSSH client installed on Windows
- VS Code with the **Remote - SSH** extension installed

---

## Step 1 — Configure the SSH client on Windows

Edit your SSH config file at:

```text
C:\Users\<WINDOWS_USER>\.ssh\config
```

Add a host block:

```ssh-config
Host lenovo-demo
    HostName 192.168.1.197
    User clouduser
    IdentityFile C:/Azure Local SFF/Keys/lenovo-demo-ssh.pem
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
$keyPath = "C:\Azure Local SFF\Keys\lenovo-demo-ssh.pem"
icacls $keyPath /inheritance:r
icacls $keyPath /remove:g "BUILTIN\Users" "Everyone" "Authenticated Users"
$user = "$env:USERDOMAIN\$env:USERNAME"
icacls $keyPath /grant:r "${user}:(R)"
icacls $keyPath
```

If ownership issues persist:

```powershell
takeown /f "C:\Azure Local SFF\Keys\lenovo-demo-ssh.pem"
icacls "C:\Azure Local SFF\Keys\lenovo-demo-ssh.pem" /setowner "$env:USERDOMAIN\$env:USERNAME"
```

> In PowerShell, use `$env:USERNAME`, **not** `%USERNAME%`.

---

## Step 3 — Test SSH from a terminal

```powershell
ssh -vvv `
    -o PreferredAuthentications=publickey `
    -o PubkeyAuthentication=yes `
    -o IdentitiesOnly=yes `
    -i "C:\Azure Local SFF\Keys\lenovo-demo-ssh.pem" `
    clouduser@192.168.1.197
```

**What success looks like**

- No `UNPROTECTED PRIVATE KEY FILE` warning.
- The client attempts public-key authentication.
- Login succeeds without a password prompt (a passphrase prompt is fine if the
  key is encrypted).

**If it fails**

- The server has a matching public key in `~/.ssh/authorized_keys`.
- Server-side file permissions are strict:

  ```bash
  chmod 700 ~/.ssh
  chmod 600 ~/.ssh/authorized_keys
  ```

- Server `sshd_config` allows pubkey auth:

  ```text
  PubkeyAuthentication yes
  AuthorizedKeysFile .ssh/authorized_keys
  ```

---

## Step 4 — Connect with VS Code Remote SSH

1. Open VS Code.
2. Install the **Remote - SSH** extension (Microsoft).
3. Open the Command Palette &nbsp;— `Ctrl+Shift+P`.
4. Run **Remote-SSH: Connect to Host…**.
5. Select `lenovo-demo`.
6. When prompted, choose **Linux** as the remote platform.

**If VS Code fails but the terminal works**

- Make sure VS Code is using the same SSH config file.
- Confirm the selected host is `lenovo-demo`.
- Remove duplicate / conflicting `Host` entries.

---

## Optional — Azure Arc SSH ProxyCommand

If connecting through the Azure Arc relay, add a host entry that uses
`ProxyCommand` with `az ssh arc`:

```ssh-config
Host arc-lenovo-demo
    HostName <ARC_MACHINE_NAME_OR_ALIAS>
    User clouduser
    ProxyCommand az ssh arc --subscription <SUB_ID> --resource-group <RG> --name <ARC_MACHINE_NAME> --local-user clouduser --private-key-file "C:\Azure Local SFF\Keys\lenovo-demo-ssh.pem"
    IdentitiesOnly yes
    PreferredAuthentications publickey
    PubkeyAuthentication yes
```

Then test:

```powershell
ssh -vvv arc-lenovo-demo
```

---

## Troubleshooting checklist

- [ ] PEM file exists at the configured path.
- [ ] PEM file ACL is restricted to your user only.
- [ ] SSH config has correct `HostName`, `User`, `IdentityFile`.
- [ ] No conflicting duplicate `Host` blocks.
- [ ] Server `authorized_keys` contains the matching public key.
- [ ] Server `.ssh` and `authorized_keys` permissions are strict.
- [ ] VS Code Remote-SSH uses the same host entry that works in the terminal.

---

# Quick Reference (Templated)

## Purpose

Configure and validate SSH key-based access to a Linux machine, then connect
via VS Code.

## Inputs required

| Variable | Example |
|---|---|
| `<HOST_OR_IP>` | `192.168.1.197` |
| `<LINUX_USER>` | `clouduser` |
| `<PEM_PATH>` | `C:\Azure Local SFF\Keys\lenovo-demo-ssh.pem` |
| `<WINDOWS_USER>` | current Windows account name |

## 1. SSH client config

File: `C:\Users\<WINDOWS_USER>\.ssh\config`

```ssh-config
Host lenovo-demo
    HostName <HOST_OR_IP>
    User <LINUX_USER>
    IdentityFile C:/Azure Local SFF/Keys/lenovo-demo-ssh.pem
    IdentitiesOnly yes
    PreferredAuthentications publickey
    PubkeyAuthentication yes
```

## 2. Lock down PEM permissions (PowerShell)

```powershell
$keyPath = "<PEM_PATH>"
icacls $keyPath /inheritance:r
icacls $keyPath /remove:g "BUILTIN\Users" "Everyone" "Authenticated Users"
$user = "$env:USERDOMAIN\$env:USERNAME"
icacls $keyPath /grant:r "${user}:(R)"
icacls $keyPath
```

If needed:

```powershell
takeown /f "<PEM_PATH>"
icacls "<PEM_PATH>" /setowner "$env:USERDOMAIN\$env:USERNAME"
```

## 3. Terminal validation

```powershell
ssh -vvv `
    -o PreferredAuthentications=publickey `
    -o PubkeyAuthentication=yes `
    -o IdentitiesOnly=yes `
    -i "<PEM_PATH>" `
    <LINUX_USER>@<HOST_OR_IP>
```

**Pass criteria**

- No `UNPROTECTED PRIVATE KEY FILE` warning.
- Public-key auth is attempted.
- Login succeeds (password not required unless the key is missing on the
  server).

## 4. VS Code connection

1. Install the **Remote - SSH** extension.
2. Command Palette → **Remote-SSH: Connect to Host…**.
3. Select `lenovo-demo`.
4. Select remote platform: **Linux**.

## 5. Failure triage

| Symptom | Action |
|---|---|
| Client-side key permission warning | Re-run Step 2. |
| Public-key auth rejected | Ensure matching key in remote `~/.ssh/authorized_keys`. |
| `Permission denied` on `~/.ssh` | `chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys`. |
| Pubkey auth disabled | Set `PubkeyAuthentication yes` and `AuthorizedKeysFile .ssh/authorized_keys` in `sshd_config`. |
| VS Code can't connect but terminal can | Remove duplicate `Host` blocks targeting the same host. |

## 6. Optional — verify public-key match

```powershell
ssh-keygen -y -f "<PEM_PATH>"
```

Compare the output to the remote `~/.ssh/authorized_keys` line for this user.

## Arc variant (if using Azure Arc proxy)

```ssh-config
Host arc-lenovo-demo
    HostName <ARC_MACHINE_NAME_OR_ALIAS>
    User <LINUX_USER>
    ProxyCommand az ssh arc --subscription <SUB_ID> --resource-group <RG> --name <ARC_MACHINE_NAME> --local-user <LINUX_USER> --private-key-file "<PEM_PATH>"
    IdentitiesOnly yes
    PreferredAuthentications publickey
    PubkeyAuthentication yes
```

Validation:

```powershell
ssh -vvv arc-lenovo-demo
```

## Operational quick check

- [ ] SSH config entry exists and is correct.
- [ ] PEM ACL restricted to the current user only.
- [ ] `ssh -vvv` succeeds from the terminal.
- [ ] VS Code connects using the same host alias.
