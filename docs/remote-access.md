# 📱 Private access away from home

Tailscale connects your signed-in iPhone to the NAS app. **Serve** provides private HTTPS; **Funnel must remain disabled**. Keep the app password enabled. No router forwarding, NAS administration access, Mac or SSH tunnel is needed for normal use.

**Verified 1 November 2026:** private HTTPS, required app login, protected NAS/database ports, and app/helper recreation with the same Tailscale identity. The user confirmed successful sign-in and Home page access on iPhone cellular with Wi-Fi off and Tailscale connected. Device key expiry remains at the account's six-month default; reauthorize before expiry. Existing tailnet access rules are unchanged.

## Setup

1. Install Tailscale on the iPhone and sign in to the intended account. Allow its VPN connection locally.
2. Inspect existing NAS services before adding a second Tailscale installation. With approved temporary management access, copy `deploy/nas-tailscale.compose.yml` beside the NAS coach file and `deploy/tailscale/` beside both files.
3. Use Docker Compose **2.17 or newer**. Load both coach and Tailscale files under the existing `amoregymbro-nas` project, through the existing 1Password launcher. Start the sidecar without replacing the healthy coach for first setup.
4. Complete the NAS device's one-time sign-in locally using the same Tailscale account. Inspect status privately: login URLs grant access and must not enter logs, screenshots, Git or chat. No reusable auth key is required.
5. Check MagicDNS, HTTPS availability and the account's access policy. Enabling HTTPS publishes the device's certificate name in public certificate-transparency records; it does **not** make the app public. Do not replace unrelated network policy. Permit only the intended users/devices. Review the NAS device's key-expiry setting so expiry does not become an unexpected outage.
6. Verify the private HTTPS login, authenticated pages and access after sidecar recreation. Confirm NAS admin, SMB, Docker and MongoDB ports are unavailable through this app node. Then close management SSH.
7. On iPhone, turn **Wi-Fi off**, leave Tailscale connected, and open the verified HTTPS address in Safari. Sign in and add that address to the Home Screen. This cellular check must succeed before claiming gym access works.

Use the actual address shown by the authorized NAS node. Do not substitute the LAN URL when away from home. Tailscale must remain connected; a disconnected VPN or expired device authorization interrupts remote access.

## What runs on the NAS

The optional Compose overlay adds one unprivileged userspace Tailscale container sharing the coach's network namespace. Serve proxies **HTTPS 443 → `127.0.0.1:8787`**. Userspace networking also makes the coach's HTTP port 8787 reachable by permitted tailnet peers over the encrypted VPN; use the HTTPS address in Safari. No helper host ports, LAN routes, exit node or SSH service are published. The coach's existing LAN listener remains available at home; MongoDB stays on the private Docker network.

The named NAS-local volume `amoregymbro_tailscale_nas_state` retains the node identity and private keys. Protect it as secret material; never commit, expose, delete or copy it into public backups. The read-only `tailscale/serve.json` contains a domain placeholder, not credentials. Docker logging is disabled for this helper because initial authentication may emit sensitive login URLs; use narrowly filtered live status for diagnosis.

## Restart and update safely

**Coach and helper are a deployment pair.** Every coach replacement must recreate Tailscale against the new container's network namespace. Always include both Compose files and explicitly recreate both services through the NAS launcher. `depends_on.restart` helps with Compose-managed changes but does not cover every runtime failure or a direct `docker restart`.

Pull/build images and validate configuration before interruption. Stop the helper, recreate the coach and helper, wait for health, then verify HTTPS. For a simple coach restart that retains its ID: stop the helper, restart the coach, wait for health, and start the helper again. After a NAS/Docker reboot, verify both health and HTTPS; a green coach check alone does not verify remote access.

Preserve all three named volumes and the existing database network. Do not use `--remove-orphans`, `down -v` or pruning: MongoDB belongs to the same Compose project but a separate file. If remote access fails, the LAN app remains the recovery path; don't expose database/admin ports as a workaround.

[Official container parameters](https://tailscale.com/docs/features/containers/docker/docker-params) · [Private Serve](https://tailscale.com/docs/features/tailscale-serve)
