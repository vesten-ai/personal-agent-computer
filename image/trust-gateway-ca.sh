#!/usr/bin/env bash
# Trust the gateway's egress CA inside an agent VM. Run as root; the CA certificate (PEM) comes on stdin.
# The gateway intercepts TLS to swap placeholders for real credentials, so every TLS client in the
# VM has to trust its CA. They don't share one trust store, hence the several places below.
set -euo pipefail
AGENT=${PAC_AGENT_USER:-agent}
CA=/usr/local/share/ca-certificates/pac-gateway.crt
cat > "$CA"
openssl x509 -in "$CA" -noout -subject >/dev/null   # refuse anything that isn't a certificate
update-ca-certificates >/dev/null                    # OpenSSL, GnuTLS, Go, curl, git, system Python

# Chromium on Linux reads trust anchors from the user's NSS database, not /etc/ssl.
H=$(getent passwd "$AGENT" | cut -d: -f6)
install -d -o "$AGENT" -g "$AGENT" -m 0700 "$H/.pki" "$H/.pki/nssdb"
runuser -u "$AGENT" -- sh -c "
  [ -f '$H/.pki/nssdb/cert9.db' ] || certutil -d sql:'$H/.pki/nssdb' -N --empty-password
  certutil -d sql:'$H/.pki/nssdb' -D -n 'PAC gateway' 2>/dev/null || true
  certutil -d sql:'$H/.pki/nssdb' -A -t 'C,,' -n 'PAC gateway' -i '$CA'
"

# Runtimes that ship their own CA bundle.
cat > /etc/profile.d/pac-ca.sh <<SH
export NODE_EXTRA_CA_CERTS=$CA                          # Node, npm, Bun
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt   # Python requests (certifi)
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt        # httpx, many OpenSSL-linked tools
export UV_SYSTEM_CERTS=true                                    # uv defaults to its bundled webpki roots
export UV_NATIVE_TLS=true                                      # the older name of the same switch
SH
# Also for non-login processes (systemd user services, the gateway's exec sessions).
for kv in "NODE_EXTRA_CA_CERTS=$CA" "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt" \
          "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" "UV_SYSTEM_CERTS=true" "UV_NATIVE_TLS=true"; do
  grep -q "^${kv%%=*}=" /etc/environment && sed -i "s#^${kv%%=*}=.*#$kv#" /etc/environment || echo "$kv" >> /etc/environment
done
echo "gateway CA trusted: $(openssl x509 -in "$CA" -noout -subject)"
