#!/usr/bin/env python3
"""
Certbot --deploy-hook script.
Fires only when certbot actually renews (not on every no-op cron run).
Pushes the new cert/key to the FortiGate admin GUI via REST API using a
blue/green certificate name so we never overwrite an in-use object.

Required env vars (certbot sets RENEWED_LINEAGE / RENEWED_DOMAINS itself):
    FGT_HOST        e.g. 192.168.1.1 or fw.home.local
    FGT_PORT        admin HTTPS port, e.g. 443 or 10443
    FGT_API_TOKEN   REST API admin token (Bearer)
    FGT_VDOM        the actual vdom name (e.g. "root") — used only by
                    delete_cert's cleanup call. NOT the literal string
                    "global": FortiOS's vdom query parameter wants a real
                    vdom name, and on a single-vdom box that's "root" even
                    with multi-vdom mode off.
    FGT_CERT_FP     (optional but recommended) SHA256 fingerprint of the
                    FortiGate's current admin cert, colon-hex, for pinning
                    instead of blanket TLS verification bypass
"""
import base64
import hashlib
import json
import os
import ssl
import sys
import logging

import requests
import urllib3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deploy-hook")

def _read_secret(env_var_file, env_var_plain=None):
    """
    Support the Docker/12-factor _FILE convention: prefer reading the value
    from a mounted secret file (env_var_file points at the path), fall back
    to a plain env var only if explicitly provided for local testing.
    """
    path = os.environ.get(env_var_file)
    if path:
        return open(path).read().strip()
    if env_var_plain and env_var_plain in os.environ:
        return os.environ[env_var_plain]
    raise RuntimeError(f"Neither {env_var_file} nor {env_var_plain} is set")


FGT_HOST = os.environ["FGT_HOST"]
FGT_PORT = os.environ.get("FGT_PORT", "443")
FGT_TOKEN = _read_secret("FGT_API_TOKEN_FILE", "FGT_API_TOKEN")
FGT_VDOM = os.environ.get("FGT_VDOM", "global")
FGT_CERT_FP = os.environ.get("FGT_CERT_FP")  # optional pin
CERT_NAME_A = "acme-fw-a"
CERT_NAME_B = "acme-fw-b"

BASE = f"https://{FGT_HOST}:{FGT_PORT}/api/v2"
HEADERS = {"Authorization": f"Bearer {FGT_TOKEN}", "Content-Type": "application/json"}


class _PinnedFingerprintAdapter(requests.adapters.HTTPAdapter):
    """Minimal fingerprint-pinning TLS adapter."""
    def __init__(self, expected_fp):
        self.expected_fp = expected_fp.replace(":", "").lower()
        super().__init__()

    def send(self, request, **kwargs):
        # requests doesn't expose a clean per-cert-fingerprint hook without
        # a custom SSLContext + socket wrapper; simplest reliable approach
        # here is a pre-flight raw TLS handshake to check the fingerprint,
        # then delegate the actual request. verify is forced False by the
        # caller (see build_session) since this method IS the verification —
        # requests' own CA-chain check is deliberately bypassed in favor of
        # the pin.
        import socket
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((FGT_HOST, int(FGT_PORT)), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=FGT_HOST) as ssock:
                der = ssock.getpeercert(binary_form=True)
        actual_fp = hashlib.sha256(der).hexdigest()
        if actual_fp != self.expected_fp:
            raise ssl.SSLCertVerificationError(
                f"FortiGate cert fingerprint mismatch: expected "
                f"{self.expected_fp}, got {actual_fp}. Refusing to proceed."
            )
        return super().send(request, **kwargs)


def build_session():
    """
    Returns a requests.Session with the pinning adapter mounted for the
    FortiGate's origin if FGT_CERT_FP is set, otherwise falls back to
    unverified TLS with a loud warning. Either way, every call in this
    script passes verify=False explicitly — trust is decided here, once,
    not left ambiguous per-request.
    """
    session = requests.Session()
    origin = f"https://{FGT_HOST}:{FGT_PORT}"
    if FGT_CERT_FP:
        session.mount(origin, _PinnedFingerprintAdapter(FGT_CERT_FP))
    else:
        log.warning(
            "FGT_CERT_FP not set — falling back to unverified TLS for the "
            "FortiGate API calls. Set FGT_CERT_FP to pin instead."
        )
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def _raise_with_body(resp):
    if not resp.ok:
        log.error("FortiGate API error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()


def get_active_cert_name(session):
    resp = session.get(
        f"{BASE}/cmdb/system/global",
        headers=HEADERS,
        verify=False,
        timeout=15,
    )
    _raise_with_body(resp)
    return resp.json()["results"]["admin-server-cert"]


def import_cert(session, certname, cert_pem, key_pem):
    payload = {
        "type": "regular",
        "certname": certname,
        "scope": "vdom",
        "file_content": base64.b64encode(cert_pem).decode(),
        "key_file_content": base64.b64encode(key_pem).decode(),
    }
    resp = session.post(
        f"{BASE}/monitor/vpn-certificate/local/import",
        headers=HEADERS,
        data=json.dumps(payload),
        verify=False,
        timeout=30,
    )
    _raise_with_body(resp)
    result = resp.json()
    if result.get("status") != "success":
        raise RuntimeError(f"Cert import failed: {result}")
    log.info("Imported cert as %s", certname)


def set_active_cert(session, certname):
    resp = session.put(
        f"{BASE}/cmdb/system/global",
        headers=HEADERS,
        data=json.dumps({"admin-server-cert": certname}),
        verify=False,
        timeout=15,
    )
    _raise_with_body(resp)
    log.info("admin-server-cert now set to %s", certname)


def delete_cert(session, certname):
    resp = session.delete(
        f"{BASE}/cmdb/vpn.certificate/local/{certname}",
        params={"vdom": FGT_VDOM},
        headers=HEADERS,
        verify=False,
        timeout=15,
    )
    if resp.status_code not in (200, 404):
        # non-fatal: leftover unused cert object is cosmetic, not a renewal
        # failure, so log and move on rather than raising
        log.warning("Could not delete old cert %s: %s", certname, resp.text)
    else:
        log.info("Deleted old cert object %s", certname)


def main():
    lineage = os.environ["RENEWED_LINEAGE"]
    cert_pem = open(os.path.join(lineage, "fullchain.pem"), "rb").read()
    key_pem = open(os.path.join(lineage, "privkey.pem"), "rb").read()

    session = build_session()

    active = get_active_cert_name(session)
    log.info("Currently active FortiGate cert: %s", active)

    new_name = CERT_NAME_B if active == CERT_NAME_A else CERT_NAME_A
    old_name = active if active in (CERT_NAME_A, CERT_NAME_B) else None

    import_cert(session, new_name, cert_pem, key_pem)
    set_active_cert(session, new_name)

    if old_name:
        delete_cert(session, old_name)

    log.info("FortiGate admin GUI cert rotation complete: %s -> %s", active, new_name)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Deploy hook failed")
        sys.exit(1)
