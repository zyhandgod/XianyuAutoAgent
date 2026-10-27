# Deployment capabilities

## Deployment
- Status: available
- Target: root@45.136.13.98, https://cards.45.136.13.98.sslip.io
- Entrypoint: versioned source upload over SSH, documented in docs/server-deployment.md.
- Current symlink: /opt/card-console/current -> /opt/card-console/releases/<release>
- Persistent data: /var/lib/card-console; release data and .env must be symlinks to persistent paths, never copied directories.
- Procedure: verify no cards.py worker is running; back up persistent storage and current release; copy current release to new version; upload changed source; preserve persistent symlinks; restart card-console; verify systemd, HTTPS and source hashes.
- Browser runtime: /opt/card-console/venv, /opt/card-console/ms-playwright; Xvfb. Install as described in docs/server-deployment.md. No external browser/VNC port.
- Rollback: compatible release symlink only; never restore stale business data. Existing multi-card rollback boundary applies.

## Acceptance
- HTTP: public / and /app.js return 200; unauthenticated browser APIs reject access.
- Browser: under card-console account, start, obtain frame, exercise harmless pointer/wheel, close, confirm process exit. Do not complete human verification or start fulfillment.
- Human: user completes official verification in the embedded remote browser and explicitly saves/detects. Platform acceptance remains pending until user performs it.
