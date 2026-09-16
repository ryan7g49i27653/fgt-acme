# FortiGate Admin GUI Cert Automation — Let's Encrypt via DNS-01 / Cloudflare

Automatically issues and renews a Let's Encrypt certificate for a FortiGate's
admin GUI using the ACME DNS-01 challenge, with zero inbound exposure on the
firewall. Runs entirely from an external Ubuntu host on a daily schedule;
the FortiGate is never the trigger and never has ACME challenge traffic
routed to it.

## Architecture

```
systemd timer (daily)
  -> docker compose run --rm fgt-acme
       -> certbot certonly --dns-cloudflare (DNS-01, no-op unless <30d to expiry)
            -> on actual renewal: deploy-hook.py
                 -> GET  FortiGate /cmdb/system/global        (read active cert name)
                 -> POST FortiGate /monitor/vpn-certificate/local/import  (blue/green name)
                 -> PUT  FortiGate /cmdb/system/global        (flip admin-server-cert)
                 -> DELETE FortiGate old cert object          (cleanup, non-fatal on failure)
```

**Blue/green cert naming** (`acme-fw-a` / `acme-fw-b`): every renewal
imports under whichever name isn't currently active, flips the pointer, then
deletes the old one — never overwrites an in-use certificate object.

## Prerequisites

- A real domain/subdomain delegated to Cloudflare DNS (e.g. `fw.example.com`).
  `.local` domains cannot receive a publicly trusted certificate.
- Docker + Compose v2 on the Ubuntu host.
- Network path from that host to the FortiGate's admin HTTPS port.
- The FortiGate's admin GUI reachable only from trusted networks (this
  automation doesn't change that exposure — Let's Encrypt validation
  happens entirely over outbound DNS, nothing inbound is opened).

## 1. Cloudflare API token

Dashboard -> My Profile -> API Tokens -> Create Token -> Custom token.
- Permission: `Zone / DNS / Edit`
- Zone Resources: restrict to the specific zone, not "All zones"
- **No Client IP Address Filtering**, or if you want it, keep it in sync
  with your WAN IP — a filtered token will hard-fail DNS-01 with a `9109`
  error the moment your IP changes and won't tell you why until you check.

## 2. FortiGate REST API user

```
config system accprofile
    edit "acme-cert-only"
        set sysgrp read-write
        set vpngrp read-write
    next
end

config system api-user
    edit "acme-renewal"
        set accprofile "acme-cert-only"
        set vdom "root"
        config trusthost
            edit 1
                set ipv4-trusthost <ubuntu-server-ip> 255.255.255.255
            next
        end
    next
end
execute api-user generate-key acme-renewal
```

Copy the generated token immediately — shown once only.

**Why both `sysgrp` and `vpngrp`:** certificate management lives under
`System > Certificates` in the GUI, but the underlying REST API path is
`vpn-certificate` — the RBAC check for cert import/delete is gated by
`vpngrp`, not `sysgrp`, despite the GUI location. Reading/writing
`system/global` (for the `admin-server-cert` pointer) is `sysgrp`. Missing
`vpngrp` produces a `403 Forbidden` specifically on the cert import call
while everything else appears to work — not an obvious failure to trace
back to a missing profile category.

## 3. FortiGate cert fingerprint (for pinning)

```bash
openssl s_client -connect <fgt-ip>:<port> </dev/null 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256
```

Used to pin the deploy hook's connection to the FortiGate by certificate
fingerprint rather than disabling TLS verification outright or trusting
hostname/CA-chain validation (irrelevant here since the box's cert is
self-signed until this automation replaces it).

## 4. Lay down the files

```
/opt/fgt-acme/
├── compose.yaml
├── Dockerfile
├── deploy-hook.py
├── .env                    # from .env.template
├── secrets/
│   ├── cloudflare.ini      # from cloudflare.ini.template, chmod 600
│   └── fgt_api_token       # from fgt_api_token.template, chmod 600
├── letsencrypt/            # empty, becomes certbot's persistent state volume
├── fgt-acme-renew.service
└── fgt-acme-renew.timer
```

```bash
sudo mkdir -p /opt/fgt-acme
sudo cp -r fgt-acme/* /opt/fgt-acme/
cd /opt/fgt-acme
cp .env.template .env
cp secrets/cloudflare.ini.template secrets/cloudflare.ini
cp secrets/fgt_api_token.template secrets/fgt_api_token
```

Fill in `.env`:
```
IMAGE_TAG=latest
ACME_DOMAIN=fw.example.com          # real Cloudflare-delegated domain
ACME_EMAIL=you@example.com
CERTBOT_EXTRA_ARGS=
FGT_HOST=192.168.x.x                # FortiGate's raw LAN IP, not a hostname —
                                     # this container has no path to resolve
                                     # internal DNS names unless you add one
FGT_PORT=4443                       # or whatever the admin HTTPS port is
FGT_VDOM=root                       # real vdom name; NOT "global"
FGT_CERT_FP=<sha256-fingerprint-from-step-3>
```

`secrets/cloudflare.ini`:
```ini
dns_cloudflare_api_token = <token-from-step-1>
```

`secrets/fgt_api_token` — just the raw token from step 2, nothing else.

```bash
chmod 600 secrets/cloudflare.ini secrets/fgt_api_token
chmod 700 secrets
```

## 5. First run

```bash
cd /opt/fgt-acme
docker compose build
docker compose run --rm fgt-acme
```

Confirm in a browser that `https://<FGT_HOST>:<FGT_PORT>` now presents the
Let's Encrypt cert.

## 6. Install the timer

```bash
sudo cp fgt-acme-renew.service fgt-acme-renew.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fgt-acme-renew.timer
systemctl list-timers fgt-acme-renew.timer
```

## Ongoing operation

Nothing further required. The timer runs daily; certbot only actually
renews (and only then invokes the FortiGate push) within ~30 days of
expiry.

```bash
journalctl -u fgt-acme-renew.service -n 100 --no-pager
```

### Manually re-running the deploy hook without a full renewal

Useful after a code change to `deploy-hook.py`, or to push an
already-issued cert that failed to reach the FortiGate on a prior attempt.
Certbot only fires the deploy hook on an actual issuance/renewal event, so
re-running `certonly` against a still-valid cert silently no-ops instead:

```bash
docker compose run --rm \
    -e RENEWED_LINEAGE=/etc/letsencrypt/live/<ACME_DOMAIN>/ \
    --entrypoint /usr/local/bin/deploy-hook.py \
    fgt-acme
```

## Design notes / deliberate decisions

- **No FortiOS automation stitch or webhook trigger.** Scheduling lives
  entirely on the Ubuntu host. Keeps the FortiGate out of the trigger
  path entirely — one less thing to debug blind on the firewall itself.
- **DNS-01, not HTTP-01/TLS-ALPN-01.** No inbound port 80/443 exposure
  needed on the FortiGate at all. Let's Encrypt does not publish a stable
  IP list for HTTP-01/TLS-ALPN-01 validation sources (they validate from
  multiple rotating vantage points deliberately, to prevent BGP-hijack
  style attacks), so a source-restricted local-in policy for those
  challenge types isn't reliably achievable — DNS-01 sidesteps the
  problem rather than trying to allowlist something that can't be
  allowlisted.
- **Admin GUI cert only** — no SSL-VPN wiring, by design.
- **Blue/green cert object naming** avoids any risk of "certificate in
  use" errors when replacing the object bound to `admin-server-cert`.
- **Fingerprint pinning over CA verification** for the deploy hook's
  connection to the FortiGate, since the device's cert is self-signed
  until this automation replaces it, and hostname verification is
  meaningless when connecting by raw LAN IP.
- **Docker Compose `secrets:` (file-mounted), not env vars,** for actual
  credential material — keeps tokens out of `docker inspect` output.
- **Base image is the official `certbot/dns-cloudflare`**, not a from-
  scratch `pip install certbot certbot-dns-cloudflare` — the latter lets
  pip resolve `josepy`/`pyOpenSSL` independently, which currently breaks
  due an unresolved upstream incompatibility (`pyOpenSSL` 24.2+ removed
  API surface `josepy` still imports at load time). The official image
  ships these pinned and tested together.
- **`FGT_VDOM=root`, never `"global"`.** `scope` in the cert-import
  payload is a literal enum (`"vdom"` or `"global"`) unrelated to the
  vdom's actual name; `vdom=` as a URL query parameter (used only by the
  cert delete call) wants the real vdom name. `system/global` itself
  accepts no vdom parameter at all — it's inherently global-scope and
  passing one causes a `424`.
