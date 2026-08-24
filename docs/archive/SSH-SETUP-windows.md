> ⚠️ **OBSOLETE.** Written when the register was staying on Windows. The machine is
> being reimaged to Debian with no dual-boot, so this procedure no longer applies.
> Kept for history only.

---
tags: [cashregister, ssh, windows, setup]
---

# Cash Register — SSH Access Setup

**Host:** Lenovo ThinkPad, Windows, wired to `HSE-House-SW-4` **`Te1/0/45`**
**Decision (2026-08-23):** lives on **VLAN 10 (MGMT)** for now. LAN-only access, no Tailscale.

> ⚠️ VLAN 10 is the SSH-ACL trust boundary. A payment terminal here is a known,
> accepted segmentation gap — same category as Alex's gaming PC on `Twe1/1/4`.
> Revisit when the register is live. Moving it later = a new VLAN + `switchport
> trunk allowed vlan add` on the uplinks (see [[add-new-vlan]]).

## Addressing

| Item | Value | Why |
|---|---|---|
| IP | **`10.0.0.22/24`** | Static. Free in ARP + sweep 2026-08-23 |
| Gateway | `10.0.0.2` | Core_Switch holds the VLAN 10 SVI, **not** `10.0.0.1` |
| DNS | `10.0.0.254` | AdGuard |
| Domain | `mgnt` | |

**Static, not DHCP** — same reasoning as the iDRAC at `10.0.0.12`: a machine that
takes money must stay reachable when DHCP isn't. `10.0.0.22` sits inside the
static block (VLAN 10 DHCP exclusions stop at `.30`, see CHANGELOG:2046), so
**no gateway DHCP config is needed**.

## Switch side — nothing to do

`Te1/0/45` is already `switchport access vlan 10` and up. Only cosmetic issue: it
still carries the stale description `TEMP-R730XD-WLC-NIC` from the R730XD staging
work. Optional cleanup (needs `id_ed25519_cisco`, which is Alex's key):

```
ssh alex@10.0.0.4
conf t
interface TenGigabitEthernet1/0/45
 description CASHREGISTER-THINKPAD-10.0.0.22
end
write memory
```

## Windows side — do this AT THE THINKPAD

None of this can be done remotely; there is no access yet to bootstrap from.

### 1. Set the static IP

Settings → Network & Internet → Ethernet → IP assignment → Edit → Manual → IPv4:

```
IP        10.0.0.22
Mask      255.255.255.0   (/24)
Gateway   10.0.0.2
DNS       10.0.0.254
```

### 2. Create the service account

A dedicated **standard (non-admin)** user. This matters — see the trap in step 4.

```powershell
# Run as Administrator
New-LocalUser -Name cashier -Description "SSH service account" -NoPassword
Set-LocalUser -Name cashier -PasswordNeverExpires $true
Add-LocalGroupMember -Group Users -Member cashier
```

### 3. Install and start OpenSSH Server

```powershell
# Run as Administrator
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

Set-Service -Name sshd -StartupType Automatic
Start-Service sshd

# Make PowerShell the login shell instead of cmd.exe
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
  -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
  -PropertyType String -Force
```

Confirm the firewall rule exists (the install usually creates it):

```powershell
Get-NetFirewallRule -Name *OpenSSH-Server* | Format-Table Name,Enabled,Direction
# If missing:
New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server (sshd)" `
  -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

### 4. Install the public key

⚠️ **The big Windows gotcha:** for accounts in the **Administrators** group, Windows
OpenSSH ignores `~/.ssh/authorized_keys` entirely and reads
`C:\ProgramData\ssh\administrators_authorized_keys` instead. This silently breaks
key auth and is the single most common reason "it just asks for a password".

Because `cashier` is a **standard** user, the normal path works:

```powershell
# Run as cashier (or fix ownership afterward)
New-Item -ItemType Directory -Force -Path C:\Users\cashier\.ssh
Set-Content -Path C:\Users\cashier\.ssh\authorized_keys -Encoding ascii -Value `
'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJyNQTB35JbiAg8GDNCtRLAG6GIgTRphS8q6fmMszHiI gus@GusPC -> cashregister'

icacls C:\Users\cashier\.ssh\authorized_keys /inheritance:r /grant "cashier:R" /grant "SYSTEM:F"
```

### 5. Restart sshd

```powershell
Restart-Service sshd
```

## Verify — from Gus's PC

The key and `~/.ssh/config` entry are already in place on `10.0.0.42`:

```bash
ssh cashregister          # alias -> cashier@10.0.0.22
```

If it fails, in order:

```bash
ping 10.0.0.22                        # Windows blocks ICMP by default -> may fail even when fine
ip neigh | grep 10.0.0.22             # ARP works regardless of firewall; this is the real link test
nc -vz 10.0.0.22 22                   # is sshd listening + firewall open
ssh -vvv cashregister                 # auth-level detail
```

On the ThinkPad, the log that actually explains key-auth failures:

```powershell
Get-EventLog -LogName Application -Source sshd -Newest 20
```

## Keys

| Path | What |
|---|---|
| `~/.ssh/id_ed25519_cashregister` | private, on Gus's PC (`10.0.0.42`) |
| `~/.ssh/id_ed25519_cashregister.pub` | the key pasted in step 4 |

Generated 2026-08-23, dedicated to this host — not reused from `id_ed25519` or
`GusPC_private_key`.

## Once SSH works

Update the network repo, per its own workflow:
- add `10.0.0.22` to `docs/reference/ssh-access-inventory.md`
- note the static in `docs/reference/network-topology.md`
- append to `CHANGELOG.md`
- fix the `Te1/0/45` description so it stops saying `TEMP-R730XD-WLC-NIC`
