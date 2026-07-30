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
| homepage | `https://homepage.<your-tailnet>.ts.net` | `services/homepage/ts-homepage-config/serve-config.json` |
| komga | `https://komga.<your-tailnet>.ts.net` | `services/comics/ts-komga-config/serve-config.json` |
| calibre-web-automated | `https://calibre.<your-tailnet>.ts.net` | `services/calibre-web-automated/ts-calibre-web-automated-config/serve-config.json` |

serve configs are stored in the repo alongside their service compose files and mounted directly into the sidecar container.

**funnel means the public internet, with no authentication in front of it.** tailscale funnel has no auth of its own, so anything reachable through a funneled origin is anonymous. homepage in particular proxies its widgets server-side using the credentials in `homepage.env`, so its funneled dashboard exposes whatever those widgets display (torrent names and paths, *arr queues, plex activity) to anyone with the url. that is bounded — non-widget endpoints return 403, `/api/config/*` returns 422, and no credential is ever sent to the client — but treat every funneled service as world-readable.

## adding a new service with a tailscale sidecar
1. create a `ts-<service>-config/serve-config.json` next to the service's compose file (copy from an existing one and update the port). **copy from a tailnet-only service such as `services/sonarr/ts-sonarr-config/`, not from komga or calibre — those carry `AllowFunnel` and would publish the new service to the internet.**
2. add a sidecar to the service's compose file using `extends`:
   ```yaml
   ts-myservice:
     extends:
       file: ../tailscale-sidecar.yml
       service: tailscale-sidecar
     container_name: ts-myservice
     hostname: myservice
     volumes:
       - ${CONFIG_ROOT}/ts-myservice/state:/var/lib/tailscale
       - ./ts-myservice-config:/config:ro
   ```
   the config mount is the repo directory from step 1, not a `${CONFIG_ROOT}` path — tailscale only ever reads `serve-config.json`, so `:ro` is safe. docker creates the state directory on first start; it needs no setup.
3. set the service's `network_mode: service:ts-myservice` and add a `depends_on` with `condition: service_healthy`
4. enable the `funnel` node attribute in the [tailscale ACL policy](https://login.tailscale.com/admin/acls) if not already done (only needs to be done once for your tailscale account, _not_ once per service) — note this grants the capability tailnet-wide, so the only thing keeping a service private is the absence of `AllowFunnel` in its serve config

## gotchas

- `TS_AUTHKEY` in `.env` is only read the first time a sidecar registers; existing sidecars auth from their state dirs. tailscale auth keys expire (90 days max), so mint a fresh reusable key at https://login.tailscale.com/admin/settings/keys before adding a new sidecar.
- tailscale serve's go reverse proxy silently drops semicolon-separated query params. old cgi apps like smokeping use `;` as a query separator — use `&` instead in any url that goes through a sidecar (smokeping accepts both).
- serve path handlers strip the mount prefix before proxying (`/smokeping/foo` reaches the backend as `/foo`). if the backend expects the prefix, repeat it in the proxy target: `"/smokeping/": {"Proxy": "http://ts-smokeping:80/smokeping/"}`.
- a serve handler can proxy to another container over the docker network (e.g. `"/foo/": {"Proxy": "http://ts-foo:80/foo/"}`), which surfaces a tailnet-only service through an already-funneled host without funneling it separately. **be careful doing this from a funneled host** — serve matches on path prefix only and cannot see query strings, so you publish the backend's *entire* http surface, not the one page you had in mind. ts-homepage used to proxy `/smokeping/` this way and it put the whole smokeping ui on the public internet; see "publishing a graph without publishing the app" below for what replaced it.
- older `tailscale serve` clis refused non-localhost proxy targets outright ("only localhost or 127.0.0.1 proxies are currently supported", [tailscale#8751](https://github.com/tailscale/tailscale/issues/8751), still open as a feature request about custom domains). that restriction is gone: as of 1.98 the cli accepts a non-localhost host so long as the target includes a scheme (the only related error left in the binary is `non-localhost target %q must include a scheme`), and the `TS_SERVE_CONFIG` json path never enforced it. so cross-container handlers are supported outright, not a loophole.

## sidecar for a service that already has a network namespace (qbittorrent)

qbittorrent can't use the normal pattern — it's pinned to `network_mode: service:gluetun` so its torrent traffic exits through protonvpn, and a container gets exactly one `network_mode`. so `ts-qbittorrent` inverts the pattern: **nothing joins the sidecar's namespace**, and the sidecar reverse-proxies to the namespace owner instead:

```json
"Handlers": { "/": { "Proxy": "http://gluetun:8080" } }
```

gluetun's firewall already permits its own directly-connected docker subnet, so no `FIREWALL_OUTBOUND_SUBNETS` change is needed. neither gluetun nor qbittorrent is modified, so the vpn path is untouched. the same trick works for any service locked into another container's namespace.

two things to keep in mind:
- there is deliberately **no `AllowFunnel`** on ts-qbittorrent — the webui must stay tailnet-only.
- `hostname: qbittorrent` on the sidecar collides with nothing, because the `qbittorrent` container has no network endpoint of its own (it borrows gluetun's) and so registers no docker dns name. note the flip side: `qbittorrent` now resolves to the **sidecar**, where it previously returned NXDOMAIN. the sidecar listens only on 443, so `http://qbittorrent:8080` gets connection refused. for container-to-container access to the webui, use `gluetun:8080`.

the LAN fallback at `http://<vm-ip>:8080` still works, since that port is published on gluetun.

# smokeping targets

the smokeping target list lives in the repo at `services/smokeping/smokeping-config/Targets` and is bind-mounted over the copy in `${CONFIG_ROOT}`. to change what gets probed:

1. edit `services/smokeping/smokeping-config/Targets`
2. `docker compose restart smokeping`

the funnel targets contain the tailnet hostname literally. smokeping's config format has no variable interpolation, and the domain is already present in this repo's git history, so the placeholder-plus-render-script indirection that briefly lived here bought nothing and has been removed.

removing a target leaves its `.rrd` data file behind in `${CONFIG_ROOT}/SmokePing/data/` (harmless); re-adding a target at the same path resumes its history.

## publishing a graph without publishing the app

homepage is funneled, so anything it proxies is on the public internet. tailscale serve matches on path prefix and cannot see query strings, and smokeping serves a graph (`displaymode=a`) and its whole browsable ui (`displaymode=n`) from the same `/smokeping/` path — so there is no serve config that exposes one without the other. proxying it published every target, the LAN addressing scheme, and the ISP first hop.

instead, `smokeping-graph-snapshot` (a ~7MB busybox loop in `services/smokeping/smokeping.yml`) fetches the two dashboard graphs every 300s and writes them to `${CONFIG_ROOT}/Homepage/graphs`, which homepage mounts at `/app/public/graphs` and serves as static files. the widgets point at `/graphs/funnel-latency.png` and `/graphs/isp-latency.png`, so exactly two immutable paths are public with no query string to manipulate. smokeping itself is reachable only on the tailnet (`https://smokeping.<your-tailnet>.ts.net`) and on the LAN (`:8085`), which is where the tiles' click-through links go.

no freshness is lost — the probe step is 300s, so a live request could not show anything newer than the snapshot.

three deliberate details: the fetch writes to a temp file and renames it, so a failed or partial fetch can never replace a good graph with a truncated one; it checks the png magic bytes first, because smokeping answers `200` with an html error page for a bad target and that would otherwise be promoted to a "fresh" graph; and the container's healthcheck fails if either png goes older than 15 minutes, so a dead snapshotter shows up as an unhealthy container instead of a silently frozen graph.

**anything added to the snapshot list is published to the public internet**, so its `title` in `Targets` must not contain an address — smokeping renders the title into the image. (`host` is fine; it is never drawn.) this is why `ISP.FirstHop`'s title no longer carries the hop address. the LAN targets still do, which is safe only because they aren't snapshotted.

on a fresh deploy, create the output directory before first start — the snapshotter runs as `1000:1000` and cannot chown a bind mount that docker auto-creates as root:

```
mkdir -p ${CONFIG_ROOT}/Homepage/graphs && chown 1000:1000 ${CONFIG_ROOT}/Homepage/graphs
```

## the Tailnet section

the `+ Tailnet` targets use the `Curl` probe to measure end-to-end https response time through the tailscale funnel, for the three publicly funneled services. this is early warning for the sidecar failure mode described below — healthy is ~0.3s across all three, and one node drifting into seconds while its siblings stay flat is the signature.

tailnet-only sidecars can't be probed this way: they have no public url, and smokeping (in `ts-smokeping`'s userspace-networking namespace) can't route to tailnet addresses.

### how Probes and Targets relate

smokeping's config is one file per section — `/etc/smokeping/config` is just seven `@include` lines pointing at `/config/{General,Alerts,Database,Presentation,Probes,Slaves,Targets}`. `Probes` declares the measurement tools and their defaults (each `+ Name` block maps to `/usr/share/smokeping/Smokeping/probes/Name.pm`); `Targets` picks one per subtree with `probe = Name`. a target naming an undefined probe is a fatal parse error.

only `Targets` is repo-managed. the other six are **stock** — byte-identical to `/defaults/smoke-conf/` in the image, which lscr.io copies into `/config` whenever a file is missing. that includes the `+ Curl` block the Tailnet section depends on, so it survives losing `${CONFIG_ROOT}` on its own.

we deliberately don't vendor `Probes`: bind-mounting a stock file would pin it and silently opt out of upstream fixes. if the image ever stopped shipping the `+ Curl` block, smokeping would refuse to start with a specific message naming the file, line, and missing probe (`ERROR: /config/Targets, line N: probe Curl missing from the Probes section`), so no extra guard is warranted.

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

**ts-qbittorrent is the exception to both steps.** nothing shares its namespace, so restarting the sidecar alone is the whole fix — bouncing qbittorrent afterwards would interrupt torrents for no reason. and the app-is-innocent check has to go via the namespace owner, since nothing listens on localhost in the sidecar's netns:

```
docker exec ts-qbittorrent wget -qO /dev/null http://gluetun:8080/
```

if it's `ts-homepage` that has degraded, the dashboard carrying the graph is itself slow to load (the graph is a static png, so slowness there is the funnel path, never smokeping). read smokeping directly instead — it's published onto the LAN, bypassing every sidecar, and unlike the dashboard tiles this gives you the full interactive ui:

```
http://<vm-ip>:8085/smokeping/?displaymode=a&start=-24h&end=now&target=Tailnet.AllFunnels
```

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

## watch history

tautulli's web ui lists individual plays but can't group them, so this rolls episode plays up per series:

```
python3 scripts/watch-history.py --user <name>
```

it reads `${CONFIG_ROOT}/Tautulli/Config/tautulli.db` directly, opened read-only so it is safe to run while the container is live. omit `--user` for all users.

```
python3 scripts/watch-history.py --users                        # list usernames in the history
python3 scripts/watch-history.py --user me --sort hours --top 20
python3 scripts/watch-history.py --user me --since 2026-01-01
python3 scripts/watch-history.py --user me --movies             # group movies instead of episodes
python3 scripts/watch-history.py --user me --json
```

`plays` counts every play event (a rewatched or resumed episode counts more than once); `eps` counts distinct episodes. `hours` is play time minus time paused.

tautulli stores the title as it was at play time, so a show renamed upstream, or removed and re-added to the library, would otherwise split across several rows. each rating key is resolved to its most recent title, and titles differing only in case are folded onto the newest spelling, so a renamed show reports under the title it goes by now.