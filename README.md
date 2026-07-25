# home-server
docker compose stack for my home server: *arr media automation, torrenting behind a vpn, e-book and comic servers, plex analytics, network monitoring, a homepage dashboard, and a tailscale sidecar per service for remote access.

# updating containers
```
docker compose pull
docker compose up --force-recreate -d
docker image prune -f
```

# restarting containers
if you've disabled a service by commenting out its include line but haven't removed the old container, `docker compose up -d` will restart only the enabled services without touching the disabled one:

```
docker compose up -d
```

# refreshing proton vpn wireguard configuration
expires yearly, november 26th ish.
to refresh, go to the proton vpn wireguard configuration page [here](https://account.proton.me/u/0/vpn/WireGuard). This link is also available in the docker compose file.
click the existing configuration
click extend to push its expiration back another year
run:
```
docker compose down
docker compose up --force-recreate -d
```

don't forget to also click the link inside the tailscale logs to reactivate tailscale!

# tailscale serve & funnel

some services have their own tailscale sidecar container for remote access. each sidecar extends a shared base template (`services/tailscale-sidecar.yml`) and uses a `serve-config.json` to configure routing.

## how it works
- each sidecar gets its own hostname on the tailnet (e.g., `komga.<your-tailnet>.ts.net`)
- the service shares the sidecar's network namespace via `network_mode: service:<sidecar>`
- tailscale serve proxies HTTPS on port 443 to the service's internal port
- setting `AllowFunnel` to `true` in the serve config makes the service publicly accessible

## current funneled services
| service | url | serve config |
|---------|-----|-------------|
| komga | `https://komga.<your-tailnet>.ts.net` | `services/comics/ts-komga-config/serve-config.json` |
| calibre-web-automated | `https://calibre.<your-tailnet>.ts.net` | `services/calibre-web-automated/ts-calibre-web-automated-config/serve-config.json` |

serve configs are stored in the repo alongside their service compose files and mounted directly into the sidecar container.

## adding a new service with a tailscale sidecar
1. create a state directory: `${CONFIG_ROOT}/ts-<service>/state`
2. create a `ts-<service>-config/serve-config.json` next to the service's compose file (copy from an existing one and update the port)
3. add a sidecar to the service's compose file using `extends`:
   ```yaml
   ts-myservice:
     extends:
       file: ../tailscale-sidecar.yml
       service: tailscale-sidecar
     container_name: ts-myservice
     hostname: myservice
     volumes:
       - ${CONFIG_ROOT}/ts-myservice/state:/var/lib/tailscale
       - ${CONFIG_ROOT}/ts-myservice/config:/config
   ```
4. set the service's `network_mode: service:ts-myservice` and add a `depends_on` with `condition: service_healthy`
5. enable the `funnel` node attribute in the [tailscale ACL policy](https://login.tailscale.com/admin/acls) if not already done (only needs to be done once for your tailscale account, _not_ once per service)

## gotchas

- `TS_AUTHKEY` in `.env` is only read the first time a sidecar registers; existing sidecars auth from their state dirs. tailscale auth keys expire (90 days max), so mint a fresh reusable key at https://login.tailscale.com/admin/settings/keys before adding a new sidecar.
- tailscale serve's go reverse proxy silently drops semicolon-separated query params. old cgi apps like smokeping use `;` as a query separator — use `&` instead in any url that goes through a sidecar (smokeping accepts both).
- serve path handlers strip the mount prefix before proxying (`/smokeping/foo` reaches the backend as `/foo`). if the backend expects the prefix, repeat it in the proxy target: `"/smokeping/": {"Proxy": "http://ts-smokeping:80/smokeping/"}`.
- a serve handler can proxy to another container over the docker network (e.g. ts-homepage proxying `/smokeping/` to `ts-smokeping:80`). this is how a tailnet-only service can be surfaced through an already-funneled host without funneling it separately — and how the homepage smokeping widget stays a relative url that works both on- and off-tailnet.
- the `tailscale serve` **cli** rejects non-localhost proxy targets ("only localhost or 127.0.0.1 proxies are currently supported", [tailscale#8751](https://github.com/tailscale/tailscale/issues/8751)). the `TS_SERVE_CONFIG` json path does not enforce that check, which is what makes the cross-container handlers above work. don't be thrown off by the cli error message.

## sidecar for a service that already has a network namespace (qbittorrent)

qbittorrent can't use the normal pattern — it's pinned to `network_mode: service:gluetun` so its torrent traffic exits through protonvpn, and a container gets exactly one `network_mode`. so `ts-qbittorrent` inverts the pattern: **nothing joins the sidecar's namespace**, and the sidecar reverse-proxies to the namespace owner instead:

```json
"Handlers": { "/": { "Proxy": "http://gluetun:8080" } }
```

gluetun's firewall already permits its own directly-connected docker subnet, so no `FIREWALL_OUTBOUND_SUBNETS` change is needed. neither gluetun nor qbittorrent is modified, so the vpn path is untouched. the same trick works for any service locked into another container's namespace.

two things to keep in mind:
- there is deliberately **no `AllowFunnel`** on ts-qbittorrent — the webui must stay tailnet-only.
- `hostname: qbittorrent` on the sidecar is safe because the `qbittorrent` container has no network endpoint of its own (it borrows gluetun's), so it registers no docker dns name to collide with.

the LAN fallback at `http://<vm-ip>:8080` still works, since that port is published on gluetun.

# smokeping targets

the smokeping target list lives in the repo at `services/smokeping/smokeping-config/Targets.template`. it is **not** mounted directly — `services/smokeping/smokeping-init/10-render-targets.sh` (mounted into `/custom-cont-init.d`, which lscr.io images execute before the app starts) renders it to `/config/Targets` at every container start, substituting `%%TS_DOMAIN%%` with `TS_DOMAIN` from `.env`.

the indirection exists because smokeping's config format has no variable interpolation and the tailnet hostname is deliberately kept out of this public repo. everything else in the file is literal.

to change what gets probed:

1. edit `services/smokeping/smokeping-config/Targets.template`
2. `docker compose restart smokeping` (the restart is what re-renders)

removing a target leaves its `.rrd` data file behind in `${CONFIG_ROOT}/SmokePing/data/` (harmless); re-adding a target at the same path resumes its history.

## the Tailnet section

the `+ Tailnet` targets use the `Curl` probe to measure end-to-end https response time through the tailscale funnel, for the three publicly funneled services. this is early warning for the sidecar failure mode described below — healthy is ~0.3s across all three, and one node drifting into seconds while its siblings stay flat is the signature.

tailnet-only sidecars can't be probed this way: they have no public url, and smokeping (in `ts-smokeping`'s userspace-networking namespace) can't route to tailnet addresses.

caveat: the `Curl` probe *definition* lives in `${CONFIG_ROOT}/SmokePing/config/Probes`, which is **not** repo-managed. if that file is ever lost, smokeping will fail to start because `Targets` references a probe that no longer exists.

# tailscale sidecar funnel latency

a `ts-*` sidecar's DERP connection can silently rot, making funnel TLS handshakes take seconds while the app behind it stays instant. the container healthcheck stays green throughout — tailscale's `/healthz` says nothing about relay quality — and nothing is logged.

to confirm, compare the tls phase across two funneled sidecars:

```
curl -s -o /dev/null -w 'tls=%{time_appconnect}s\n' https://<service>.<your-tailnet>.ts.net/
```

~0.2-0.4s is healthy. if one node is seconds and another on the same host is fine, it's that node. verify the app is innocent with `docker exec ts-<svc> wget -qO /dev/null http://127.0.0.1:<port>/`, then:

```
docker compose restart ts-<svc>    # wait for healthy
docker compose restart <svc>       # app shares the sidecar's netns, so it must follow
```

order matters — the app is stranded until it restarts too. use `restart`, not `up -d`: a plain `up -d` leaves `ts-*` sidecars alone and would recreate only the app, missing the problem entirely.

# scripts

## listing ports

to see which ports are in use across all services:

```
bash scripts/list-ports.sh
```

## season audit

read-only report of sonarr seasons that were downloaded episode-by-episode (mixed release groups, codecs, qualities, import dates spread over weeks) and are good candidates for replacing with a season pack via an interactive season search:

```
bash scripts/season-audit.sh
```

the wrapper resolves the ts-sonarr container ip and reads the sonarr api key from `${CONFIG_ROOT}/Sonarr/Config/config.xml`, so no configuration is needed. arguments pass through to the underlying python script:

```
bash scripts/season-audit.sh --min-score 20   # only the worst offenders
bash scripts/season-audit.sh --json           # full detail (groups, codecs, audio) as json
bash scripts/season-audit.sh --all            # include seasons that look like packs
```

the `MultiAud` column counts files with 2+ audio languages — useful for spotting anime seasons missing dual audio. seasons with missing episodes, specials, unmonitored seasons, and currently-airing seasons are excluded.