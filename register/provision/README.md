# Register provisioning

What was done to the ThinkCentre after the Debian 13 netinst, kept as scripts so it
can be reproduced on replacement hardware rather than remembered.

Run in order, as root, on a fresh install:

```bash
bash 01-admin-account.sh    # needs the SSH public key in $ADMIN_KEY
bash 02-kiosk.sh
```

## Accounts

| User | Purpose | sudo | SSH |
|---|---|---|---|
| `tienda` | the kiosk session — cage/chromium run as this | **no** | password only |
| `gus` | administration | yes, `NOPASSWD` | key only |

`tienda` deliberately has no sudo: it is the account the till auto-logs into, and a
cashier must not be able to become root. Debian's installer already produced this
split by asking for a root password (which excludes the first user from `sudo`).

`gus` uses `NOPASSWD` because SSH is key-only for it — possession of the key *is* the
authentication, and requiring a second secret for every command buys little on a
headless machine. Change `/etc/sudoers.d/90-gus` if that trade is not wanted.

## Boot

Default target is `multi-user.target` — the machine boots to a console, not GNOME.
GNOME is left installed but never started; runtime memory dropped from 2.2 GB to
751 MB. Purging it is safe once the kiosk is proven, and deliberately deferred until
then so a failed `apt remove` cannot leave an unbootable register.

`cashregister-kiosk.service` runs **cage**, a Wayland compositor that displays exactly
one fullscreen application. There is no desktop, no launcher and no window list, so
there is nothing for a cashier to escape into. It is installed but **not enabled**
until the application exists — booting to a black screen is worse than booting to a
console.

## Updates

`unattended-upgrades` is **not installed**, and should stay that way. A till that
reboots or swaps a library mid-season is a worse outcome than one running a
three-month-old package. Update deliberately, in the off-season, never in March.
