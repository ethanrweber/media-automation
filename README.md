# media-automation
docker-compose file containing the services required for setting up my personal media automation

# updating containers
```
docker-compose pull
docker-compose up --force-recreate --build -d
docker image prune -f
```

# refreshing proton vpn wireguard configuration
expires yearly, november 26th ish.
to refresh, go to the proton vpn wireguard configuration page [here](https://account.proton.me/u/0/vpn/WireGuard). This link is also available in the docker-compose file.
click the existing configuration
click extend to push its expiration back another year
run:
```
docker-compose down
docker-compose up --force-recreate --build -d
```

don't forget to also click the link inside the tailscale logs to reactivate tailscale!