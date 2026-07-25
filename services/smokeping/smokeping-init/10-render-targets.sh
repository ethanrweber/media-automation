#!/usr/bin/with-contenv bash
# Render the repo-managed Targets template into the live config.
#
# smokeping's config format has no variable interpolation, and TS_DOMAIN is gitignored
# (the tailnet hostname is deliberately kept out of this public repo), so the committed
# template carries a %%TS_DOMAIN%% placeholder that gets substituted here at startup.
#
# lscr.io/linuxserver/* runs anything executable in /custom-cont-init.d before the app
# starts, inheriting the container environment — so TS_DOMAIN from compose is visible.
set -euo pipefail

TEMPLATE=/config/Targets.template
OUT=/config/Targets

if [[ ! -f "${TEMPLATE}" ]]; then
    echo "[render-targets] ${TEMPLATE} not found; leaving ${OUT} as-is"
    exit 0
fi

if [[ -z "${TS_DOMAIN:-}" ]]; then
    # not fatal: every non-funnel target still works, the three funnel ones just won't resolve
    echo "[render-targets] WARNING: TS_DOMAIN is unset — funnel targets will not resolve"
fi

sed "s/%%TS_DOMAIN%%/${TS_DOMAIN:-}/g" "${TEMPLATE}" > "${OUT}"
echo "[render-targets] rendered ${OUT} from ${TEMPLATE}"

# The Tailnet section says `probe = Curl`, but the probe *definition* lives in /config/Probes —
# a stock, image-supplied file we deliberately don't repo-manage (see README). A target naming a
# probe that isn't defined is a fatal smokeping parse error with an unhelpful message, so if the
# image ever stops shipping the Curl block, say so plainly instead.
if grep -qE '^probe *= *Curl' "${OUT}" && ! grep -qE '^\+ *Curl' /config/Probes 2>/dev/null; then
    echo "[render-targets] WARNING: ${OUT} uses 'probe = Curl' but /config/Probes defines no '+ Curl' block"
    echo "[render-targets]          smokeping will fail to start — restore it from /defaults/smoke-conf/Probes"
fi
