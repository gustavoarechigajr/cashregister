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

## Working on the till from a workstation

```bash
tools/deploy.sh          # push app + restart services
tools/deploy.sh --seed   # also reload the catalogue
tools/shot.sh            # screenshot the real screen
```

`tools/shot.sh` uses **grim** against the cage compositor's Wayland socket, run as
`tienda` with `XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0`. It retries
until the capture is larger than a blank frame, because Chromium takes ~10 s to paint
after a kiosk restart and a screenshot taken too early is pure white.

The attached display is **1600×900**, not the 1280×800 the mockups assumed. The product
grid uses `repeat(auto-fill, minmax(198px, 1fr))` so it fills whatever it is given —
four columns here.
