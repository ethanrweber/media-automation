# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Docker Compose stack for personal media automation (the *arr apps, qBittorrent behind a VPN, Komga, Calibre-Web, Homepage, etc.) running on an Ubuntu VM. The compose files here drive **live containers** — `docker compose` commands affect running services, so confirm with the user before restarting or recreating anything.

This VM is one piece of a larger setup that is *not* in this repo:
- The VM runs on a Proxmox host (`pve-1`).
- Plex runs in a separate LXC on the host, not as a compose service here.
- Media lives on host storage mounted into the VM at `/mnt/storage/media` (`MEDIA_ROOT`).

## Architecture

- `docker-compose.yml` is only an `include:` list — one compose file per service under `services/`. To disable a service, comment out its include line (the old container keeps running until removed; `docker compose up -d` won't touch it).
- Simple services are a single yml directly in `services/`; services with config files get their own directory (e.g. `services/homepage/`).
- Shared settings come from `.env` (gitignored; `.env.example` is the committed template): `CONFIG_ROOT` (container appdata), `MEDIA_ROOT`, `DOWNLOADS_ROOT`, plus Tailscale/VPN/API keys. Homepage additionally has its own `homepage.env`.

## Commands

```bash
docker compose up -d                                # apply compose changes
docker compose pull && docker compose up --force-recreate -d && docker image prune -f   # update all containers
bash scripts/list-ports.sh                          # table of host/container ports across all services
```

The read-only helper scripts under `scripts/` are documented in `scripts/README.md`.

## VPN constraint (important)

qBittorrent has no network of its own — it uses `network_mode: service:gluetun` so all torrent traffic goes through ProtonVPN. Never give qbittorrent its own ports or network; its web UI port (8080) is published on the **gluetun** container. The ProtonVPN WireGuard key expires yearly (~Nov 26); the refresh procedure is in README.md.

## Tailscale sidecar pattern

Services exposed over Tailscale get a sidecar container that `extends` `services/tailscale-sidecar.yml`, with a `ts-<service>-config/serve-config.json` mounted for routing. The service joins the sidecar's network via `network_mode: service:ts-<name>` and `depends_on` it with `condition: service_healthy`. Follow the step-by-step "adding a new service with a tailscale sidecar" section in README.md; `services/comics/komga.yml` is a good reference example.
