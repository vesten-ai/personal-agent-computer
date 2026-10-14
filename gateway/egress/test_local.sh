#!/usr/bin/env bash
# Exercise credential_swap.py with a local mitmdump and a dummy secret, against public echo services.
# Needs mitmdump (mitmproxy 12) and curl. Nothing real is sent: the "secret" is a dummy string.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
S=$(mktemp -d); PORT=${PORT:-18980}; DNSPORT=${DNSPORT:-18953}
PH=sk-pac-test-placeholder-0001
cat > "$S/policy.json" <<JSON
{"default":"allow","deny_hosts":[".internal"],"audit_log":"$S/audit.jsonl","dns":{"max_label":40},
 "secrets":[{"name":"test","placeholder":"$PH","env":"PAC_SECRET_TEST","hosts":["postman-echo.com"]}]}
JSON
PAC_SECRET_TEST=REAL-DUMMY-SECRET-424242 mitmdump --listen-host 127.0.0.1 --mode "regular@$PORT" --mode "dns@$DNSPORT" \
  --set confdir="$S/mitm" \
  --set connection_strategy=lazy -s "$HERE/credential_swap.py" --set pac_policy="$S/policy.json" > "$S/mitm.log" 2>&1 &
MP=$!; trap 'kill $MP 2>/dev/null; rm -rf "$S"' EXIT
for _ in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.5; done
P=(-sS -m 15 -x "http://127.0.0.1:$PORT" --cacert "$S/mitm/mitmproxy-ca-cert.pem")
fail=0
check() { if grep -q -- "$2" <<<"$3"; then echo "ok    $1"; else echo "FAIL  $1: $3"; fail=1; fi; }
r=$(curl "${P[@]}" https://postman-echo.com/headers -H "Authorization: Bearer $PH")
check "header swapped upstream, scrubbed on the way back" "Bearer $PH" "$r"
r=$(curl "${P[@]}" https://postman-echo.com/post -H 'content-type: application/json' -d "{\"key\":\"$PH\"}")
check "JSON body swapped and scrubbed" "$PH" "$r"
r=$(curl "${P[@]}" https://httpbin.org/headers -H "Authorization: Bearer $PH")
check "placeholder to an unbound host is blocked" "placeholder for test sent to httpbin.org" "$r"
r=$(curl "${P[@]}" https://postman-echo.com/headers -H "Host: httpbin.org" -H "Authorization: Bearer $PH")
check "Host header differing from SNI is blocked" "blocked by Personal Agent Computer" "$r"
r=$(curl "${P[@]}" http://postman-echo.com/headers -H "Authorization: Bearer $PH")
check "placeholder over plain HTTP is blocked" "sent without TLS" "$r"
r=$(curl "${P[@]}" http://169.254.169.254/latest/meta-data)
check "metadata address is refused" "internal address" "$r"
r=$(curl "${P[@]}" http://127.0.0.1:22/)
check "loopback is refused" "internal address" "$r"
r=$(curl "${P[@]}" https://foo.internal/ 2>&1)
check "deny-listed host is refused at CONNECT" "403" "$r"
if command -v dig >/dev/null; then
  D=(+time=5 +tries=1 @127.0.0.1 -p "$DNSPORT")
  r=$(dig +short "${D[@]}" example.com A)
  check "DNS is answered through the proxy" "[0-9]" "$r"
  r=$(dig "${D[@]}" db.internal A)
  check "deny-listed name is refused in DNS" "status: REFUSED" "$r"
  r=$(dig "${D[@]}" "$(printf 'a%.0s' $(seq 1 50)).example.com" A)
  check "DNS name with a 50-character label is refused" "status: REFUSED" "$r"
  r=$(cat "$S/audit.jsonl")
  check "DNS queries are in the audit log" '"kind": "dns", "client": "127.0.0.1", "name": "example.com"' "$r"
else
  echo "skip  DNS checks (no dig)"
fi
# The gateway rewrites the policy as agents come and go; the proxy must pick it up and honour client binding.
sleep 1
cat > "$S/policy.json" <<JSON
{"default":"allow","deny_hosts":[".internal"],"audit_log":"$S/audit.jsonl",
 "secrets":[{"name":"other-vm","placeholder":"sk-pac-other-vm-0002","env":"PAC_SECRET_TEST","hosts":["postman-echo.com"],
             "clients":["10.9.9.9"]},
            {"name":"test","placeholder":"$PH","env":"PAC_SECRET_TEST","hosts":["postman-echo.com"]}]}
JSON
r=$(curl "${P[@]}" https://postman-echo.com/headers -H "Authorization: Bearer sk-pac-other-vm-0002")
check "rewritten policy is reloaded; another VM's placeholder is refused" "may not use other-vm" "$r"
r=$(curl "${P[@]}" https://postman-echo.com/headers -H "Authorization: Bearer $PH")
check "a shared secret is scrubbed back to the caller's own placeholder" "Bearer $PH" "$r"
grep -q '"swapped": \["test"\], "scrubbed": \["test"\]' "$S/audit.jsonl" && echo "ok    audit log records swap and scrub" || { echo "FAIL  audit log"; fail=1; }
grep -iE "traceback|error in script" "$S/mitm.log" && fail=1
exit $fail
