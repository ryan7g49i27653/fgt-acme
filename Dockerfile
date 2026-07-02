FROM certbot/dns-cloudflare:latest

# Adds only what certbot itself doesn't ship — the FortiGate push logic's
# HTTP client. Deliberately NOT touching certbot/josepy/pyopenssl/cryptography
# here: those come pre-resolved and tested together in the base image, and
# reinstalling any of them independently risks reintroducing exactly the
# josepy/pyOpenSSL X509Req incompatibility this base image avoids.
RUN pip install --no-cache-dir requests

COPY deploy-hook.py /usr/local/bin/deploy-hook.py
RUN chmod +x /usr/local/bin/deploy-hook.py

VOLUME ["/etc/letsencrypt", "/var/log/letsencrypt"]

ENTRYPOINT ["/bin/sh", "-c", "\
    certbot certonly \
      --non-interactive \
      --agree-tos \
      --dns-cloudflare \
      --dns-cloudflare-credentials /run/secrets/cloudflare.ini \
      --dns-cloudflare-propagation-seconds 30 \
      -d \"$ACME_DOMAIN\" \
      -m \"$ACME_EMAIL\" \
      --deploy-hook /usr/local/bin/deploy-hook.py \
      $CERTBOT_EXTRA_ARGS \
    "]
