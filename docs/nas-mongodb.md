# 🗄️ AMORE GYM BRO: MongoDB on the NAS

## ✅ Verified installation — 1 November 2026

**The FastAPI web app and MongoDB are running on the UGREEN NAS.** Data transfer, every document comparison, app reads, and persistence after a NAS container restart were verified. This is the current setup, not a deployment checklist to repeat.

- NAS: `192.168.2.49`; project directory: `/home/Joss/AmoreGymBro`, on NAS-local Btrfs.
- Compose file: `nas-mongodb.compose.yml`; project: `amoregymbro-nas`; container: `amoregymbro-nas-mongodb-1`.
- Image: official `mongo:8.0`; database: `amoregymbro`; authentication database: `admin`. Use the existing configured application username, not an example username. Its permissions are limited to `readWrite` on the application database; bootstrap administrator credentials are separate.
- Persistent volume: `amoregymbro_mongo_nas_data`, mounted at `/data/db`. Docker root is `/volume1/@docker`; the volume's data is `/volume1/@docker/volumes/amoregymbro_mongo_nas_data/_data`.
- NAS listener: **`127.0.0.1:27018`**, forwarded to container port 27017. No MongoDB listener is published to the LAN or internet.
- Optional Mac development/Compass URI: **`mongodb://127.0.0.1:27019`**, through the private SSH tunnel to NAS loopback port 27018.
- Web app: `amoregymbro-nas-coach-1`, LAN **http://192.168.2.49:8787**, using `mongodb://mongodb:27017` on `amoregymbro-nas_default`. App cache volume: `amoregymbro_coach_nas_data`.
- Compass: saved **AMORE GYM BRO — NAS** connection, already connected and verified.

The old Mac MongoDB on Mac port **27018** and the retained SQLite database are fallback snapshots, not synchronized replicas. Current records belong on the NAS. The 0.25 GB WiredTiger cache setting is not a hard limit on total database memory.

## 🚀 Current operation

Open **http://192.168.2.49:8787** on your home network. Sign in with the app password; normal use needs no Mac, SSH or unlocked vault. Safari can add it to the iPhone Home Screen. The app's sign-in session lasts up to 30 days unless you sign out. Away from home, use the private HTTPS address with Tailscale connected; see [remote access](remote-access.md). The LAN URL alone does not work remotely.

The coach connects directly to MongoDB over the private Docker network. This was verified by disconnecting the Mac database tunnel and loading all main authenticated pages. NAS coach and MongoDB restarts preserved saved records. The Mac production-like development server is stopped; production owns scheduled Hevy sync.

For intentional recreation, run the existing launcher **on the NAS**:

```bash
sudo /home/Joss/AmoreGymBro/start-mongodb.sh
sudo /home/Joss/AmoreGymBro/start-coach.sh
```

Both use the protected project service-account token and official CLI to resolve only required 1Password references. `start-coach.sh` loads the coach and Tailscale overlay with `.env.coach`, recreating both containers together; it joins the existing external database network and does not recreate MongoDB. Its source template is `deploy/start-nas-coach.sh`. Source lives in `source/`; image `amoregymbro-coach:local` was built on the NAS. Repository templates are `deploy/nas-coach.compose.yml`, `deploy/nas-tailscale.compose.yml` and `deploy/nas-mongodb.compose.yml`. Keep their shared network and explicit volume names stable.

For an ordinary restart, without resolving secrets again:

```bash
docker stop amoregymbro-nas-tailscale-1
docker restart amoregymbro-nas-coach-1
docker inspect --format '{{.State.Health.Status}}' amoregymbro-nas-coach-1
# After the coach is healthy:
docker start amoregymbro-nas-tailscale-1
```

The app's health check verifies database access. Wait for healthy, then check existing records and private HTTPS. A replacement coach requires sidecar recreation through the launcher, not just a restart. Restarting `amoregymbro-nas-mongodb-1` preserves its volume too. Docker commands require administrator permission; use `sudo` when needed. Do not print full container inspection or resolved Compose configuration: they include credentials. These commands require NAS management access, not normal phone use.

### Coach restarting and Tailscale stopped

The startup message “MongoDB is unavailable or authentication failed” also covers connection failures. Check network membership before changing credentials. `HostConfig.NetworkMode` can name the expected network while `NetworkSettings.Networks` is empty.

In the 1 November recovery, the coach had lost its attachment to `amoregymbro-nas_default`. MongoDB, its records and existing credentials were intact. Reattaching the existing coach restored LAN access; starting its existing Tailscale helper restored private HTTPS. What detached the network was not established.

Inspect only safe fields:

```bash
sudo docker inspect --format '{{json .NetworkSettings.Networks}}' amoregymbro-nas-coach-1
sudo docker network inspect amoregymbro-nas_default --format '{{range .Containers}}{{.Name}} {{end}}'
```

Only when the coach's expected attachment is missing and MongoDB is on that existing network, stop the helper/coach, reconnect, and start the coach:

```bash
sudo docker stop amoregymbro-nas-tailscale-1 amoregymbro-nas-coach-1
sudo docker network connect --alias coach amoregymbro-nas_default amoregymbro-nas-coach-1
sudo docker start amoregymbro-nas-coach-1
```

Wait for coach health, then start `amoregymbro-nas-tailscale-1` and verify both app addresses. Preserve the existing container IDs, volumes and passwords. If the network itself is missing, stop and investigate; do not create a replacement database/network blindly. This recovery did not include a NAS reboot or a subsequent ordinary-restart test.

## 🚇 Private tunnel and SSH

This optional administrative/development path is separate from the production app: Mac `127.0.0.1:27019` → SSH → NAS `127.0.0.1:27018` → MongoDB container `27017`. Keep both host listeners on loopback. Do not switch the app to the old Mac database when the tunnel is unavailable.

UGOS retains automatic SSH shutoff. The approved LAN-only SSH session used port 22 with a 30-minute window. An established tunnel can survive the listener stopping because SSH uses `KillMode=process`; a lost tunnel or Mac reboot needs SSH enabled again in **UGOS Control Panel → Terminal** before running `./start-local.sh`. The launcher may ask for local 1Password approval to reopen the tunnel. Its idle expiry is disabled, but lost connections still need SSH for reconnection. This affects Compass/development only; the NAS app continues without the tunnel. Do not disable shutoff or widen access just to avoid this step.

The validated exception `/etc/ssh/sshd_config.d/90-amoregymbro-mongo.conf` allows local forwarding only for user `Joss`, restricted to `127.0.0.1:27018`. Gateway ports remain disabled; other accounts retain their settings. Host-key checking remains enabled. The UGREEN login stays in the user's Personal vault and is not accessible to the NAS service account.

## 🔐 1Password and token renewal

Six existing project secrets were imported as concealed fields and privately checked field by field. The Mac `.env` and NAS `.env.coach` hold ordinary settings with secret fields blank. `.env.app.references` supplies app secrets; `.env.references` also contains the administrator password reference for deployment/tests. These files contain references, not secret values. Keep the existing settings and credentials; update secrets in 1Password rather than placing passwords in `.env`.

The NAS uses a dedicated read-only service account restricted to the **AmoreGymBro** vault. It has no Personal/family vault access or full account credentials and does not depend on the Mac vault staying unlocked. Its bootstrap token is saved in the user's Personal vault and in the protected NAS file `/home/Joss/AmoreGymBro/.secrets/onepassword-token`, with restricted directory and file permissions. This token is still a local bootstrap secret; 1Password does not eliminate that requirement.

The service account expires **89 days after creation**. Check its actual expiry in 1Password and renew/replace it before then, preserving read-only access to this project vault only. Check current feature availability and account permissions when replacing it; do not assume plan coverage or widen scope. Provision any replacement through a protected local channel, update only the protected token file, and verify reference resolution privately. Never put the token, account password, Secret Key, or application keys in chat, Git, command arguments, or logs. Do not rotate database passwords as part of token renewal.

Docker retains the existing container configuration for ordinary restarts. A fresh launch/recreation through `start-mongodb.sh` needs valid service-account access to resolve references again. Token expiry does not erase the database, but it must be resolved before that launch path can work.

## 💾 Persistence and backup limits

Keep project `amoregymbro-nas` and volume `amoregymbro_mongo_nas_data` stable. Restarting or recreating the container retains its named volume. Never use `down -v`, volume deletion, or pruning for routine maintenance or to fix authentication. Database files belong on NAS-local disk, never SMB; do not copy live database files as a backup.

A persistent volume is not an independent backup. Keep separate protected MongoDB backups before upgrades or planned imports. For this standalone database, stop all app writers while taking a consistent dump; protect archives as private workout/profile data. A successful restart proves persistence, not backup completeness, recovery readiness, or an automatic backup schedule.

Initializers create users only for an empty volume. Editing settings or adding `mongo-init.js` does not create or rotate users in the existing database. Retain the current credentials and volume; investigate an authentication failure without resetting storage.

## 🛟 Historical reference and future changes

The original plan prepared an empty NAS target, copied the Mac MongoDB, compared documents and indexes, checked app reads, and tested restart persistence before switching the Mac app. **That cutover is complete. Do not rerun restore/user initialization or overwrite the active NAS from an older snapshot.** The active NAS is not an empty target. Any later restore needs a separate recovery plan, protected backups, and explicit target selection.

For a deliberately planned fresh SQLite migration or rollback to an old fallback, see the [README migration and rollback reference](../README.md#-historical-migration-fresh-installs-and-explicit-rollback). Stop writers and preserve the current NAS copy before a rollback. The old Mac MongoDB needs its original credentials as well as port 27018. Writes made after NAS cutover are not copied back automatically.

The NAS web app already joins the existing private Docker network `amoregymbro-nas_default` through an external-network declaration. Preserve that connection when updating it. Do not substitute the Mac tunnel URI inside its container or publish MongoDB to solve networking. Keep UGREENLink, DDNS, and router forwarding out of this setup. 🔒
