# fgt-acme

Automated Let's Encrypt certificate issuance and renewal for a FortiGate
admin GUI, using the ACME **DNS-01** challenge via Cloudflare — no inbound
port 80/443 exposure on the firewall required.

## Why

FortiOS's built-in ACME client only supports HTTP-01 and TLS-ALPN-01, both
of which require accepting inbound validation traffic from Let's Encrypt.
Let's Encrypt doesn't publish a stable IP range for validation traffic (by
design — they validate from multiple rotating vantage points to resist
BGP-hijack attacks), so there's no way to tightly source-restrict that
inbound exposure. This project sidesteps the problem entirely: DNS-01
validation happens over outbound DNS only, so the FortiGate never needs to
accept anything inbound for certificate issuance at all.

## How it works

An external host (anything with Docker) runs certbot on a daily schedule
via systemd timer. Certbot handles the Cloudflare DNS-01 challenge and,
**only on an actual renewal** (not every no-op daily check), invokes a
deploy hook that pushes the new certificate to the FortiGate over its REST
API and flips `admin-server-cert` to point at it. The FortiGate is never
the trigger and is only ever a target of outbound API calls from the host
running this — it has no role in scheduling or initiating anything.

Certificates rotate using a blue/green naming scheme
(`acme-fw-a` / `acme-fw-b`, overridable via `FGT_CERT_NAME_A` /
`FGT_CERT_NAME_B`) so a renewal never overwrites the certificate object
currently bound to the admin GUI.

```
systemd timer (daily)
  -> docker compose run --rm fgt-acme
       -> certbot (DNS-01 via Cloudflare, no-op unless <30d to expiry)
            -> deploy-hook.py (on actual renewal only)
                 -> FortiGate REST API: import new cert, flip admin-server-cert,
                    delete old cert object
```

## Contents

| File | Purpose |
|---|---|
| `Dockerfile` | Builds on the official `certbot/dns-cloudflare` image, adds `deploy-hook.py` |
| `compose.yaml` | Compose service definition; non-secret config via `.env`, credentials via Compose `secrets:` (file-mounted, not env vars) |
| `deploy-hook.py` | Pushes a renewed cert to the FortiGate via REST API; handles the blue/green rotation and TLS fingerprint pinning |
| `.env.template` | Copy to `.env` — non-secret runtime config (domain, FortiGate host/port, vdom, cert fingerprint, cert object names) |
| `secrets/*.template` | Copy to real filenames (drop `.template`) — Cloudflare API token and FortiGate REST API token |
| `fgt-acme-renew.service` / `.timer` | systemd units for daily scheduling |
| `SETUP.md` | Full step-by-step setup instructions, including FortiGate-side config and the reasoning behind several non-obvious choices |

## Quick start

See **[SETUP.md](./SETUP.md)** for the full walkthrough. Short version:

```bash
cp .env.template .env                                       # fill in
cp secrets/cloudflare.ini.template secrets/cloudflare.ini    # fill in
cp secrets/fgt_api_token.template secrets/fgt_api_token      # fill in
chmod 600 secrets/cloudflare.ini secrets/fgt_api_token

docker compose build
docker compose run --rm fgt-acme                             # first issuance

sudo cp fgt-acme-renew.service fgt-acme-renew.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fgt-acme-renew.timer
```

## Requirements

- A domain/subdomain delegated to Cloudflare DNS (`.local` domains can't
  receive a publicly trusted certificate)
- Docker + Compose v2
- Network reachability from the host running this to the FortiGate's admin
  HTTPS port
- A dedicated FortiGate REST API admin — see `SETUP.md` step 2 for the
  exact profile/permissions required (notably: certificate management is
  gated by the `vpngrp` accprofile category, not `sysgrp`, despite living
  under System > Certificates in the GUI)

## Manually re-triggering the push

Certbot only fires the deploy hook on an actual issuance/renewal event.
To re-push an already-issued certificate (e.g. after a `deploy-hook.py`
change, without waiting for the next real renewal):

```bash
docker compose run --rm \
    -e RENEWED_LINEAGE=/etc/letsencrypt/live/<your-domain>/ \
    --entrypoint /usr/local/bin/deploy-hook.py \
    fgt-acme
```

## Scope

- Admin GUI certificate only. No SSL-VPN wiring.
- No FortiOS automation stitch or webhook — scheduling lives entirely on
  the host running Docker, by design, to keep the firewall out of the
  trigger path.

## License

MIT — see [LICENSE](./LICENSE).
