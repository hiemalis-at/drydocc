---
name: nextcloud
status: active
owner: doccap
summary: Nextcloud + Postgres + eturnal TURN server
created: 2026-05-18
last_applied: 2026-05-18T14:22:00Z
last_applied_commit: 0000000
depends_on: []
containers: [nextcloud-app, nextcloud-db, eturnal]
networks: [nextcloud-net]
volumes: [nextcloud-data, nextcloud-db-data]
ports: ["443/tcp", "3478/udp"]
has_secrets: true
spec:
  networks:
    nextcloud-net: {}
  volumes:
    nextcloud-data: { mode: "0750" }
    nextcloud-db-data: { mode: "0700" }
  containers:
    nextcloud-db:
      image: docker.io/postgres:16.3-alpine
      network: nextcloud-net
      env_from: /etc/drydocc/secrets/nextcloud.env
      mounts:
        - { volume: nextcloud-db-data, target: /var/lib/postgresql/data, ro: false }
    nextcloud-app:
      image: docker.io/nextcloud:29.0.4-apache
      network: nextcloud-net
      env_from: /etc/drydocc/secrets/nextcloud.env
      env:
        NEXTCLOUD_LOGLEVEL: "info"
      mounts:
        - { volume: nextcloud-data, target: /var/www/html, ro: false }
      ports:
        - "443:443/tcp"
      depends_on: [nextcloud-db]
    eturnal:
      image: docker.io/eturnal/eturnal:1.12.2
      network: nextcloud-net
      env_from: /etc/drydocc/secrets/nextcloud.env
      ports:
        - "3478:3478/udp"
---

# Nextcloud

## Intent
Personal Nextcloud instance for file sync, calendar, and contacts, plus
an eturnal TURN server so Nextcloud Talk works behind NATs.

## Architecture
Three containers on a shared `nextcloud-net` network: `nextcloud-app`
(the PHP application), `nextcloud-db` (Postgres 16), and `eturnal` (TURN
relay on UDP 3478). Volumes: `nextcloud-data` (mounted at `/var/www/html`
in the app container) and `nextcloud-db-data` (Postgres data dir). Port
443 is reverse-proxied by an upstream Caddy use-case; only 3478/udp is
exposed directly by `drydocc` here.

## Runbook
- **Bring-up**: `/apply-use-case nextcloud` (or manual `systemctl --user
  start nextcloud-*.service` if quadlets already present).
- **Common ops**: `occ` admin commands via `podman exec -it
  nextcloud-app php occ ...`.
- **Known issues**: First boot needs the admin password from the secrets
  manifest; eturnal needs `secret` env var matching Nextcloud's TURN
  configuration.

## Quadlets
Generated on apply from frontmatter `spec:`. See on-node
`~/.config/containers/systemd/nextcloud-*.{container,network}`. The
render mapping (spec → ini text) is documented in
`skills/podman-quadlets/SKILL.md` under
`## Render from spec`.
