#!/usr/bin/env bash
# Spike 1: which programs in the agent VM work through the gateway's TLS interception, and does
# the placeholder -> credential swap reach the real API? Run as the agent user inside an agent VM
# whose only way out is the gateway (spike 2), after image/trust-gateway-ca.sh.
#
# Each client runs twice:
#   bare        only the system trust store and the user's NSS database (no CA environment variables)
#   configured  with /etc/profile.d/pac-ca.sh (NODE_EXTRA_CA_CERTS, REQUESTS_CA_BUNDLE, SSL_CERT_FILE, UV_NATIVE_TLS)
# A client "passes TLS" when it gets any HTTP answer from the real server. With a dummy secret on the
# gateway the model APIs answer 401, which still proves interception and swap worked end to end.
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin
OUT=${OUT:-$HOME/spike1-results.tsv}
VENV=$HOME/.spike1-venv
PH_OPENAI=sk-pac-openai-placeholder-0001
PH_ANTHROPIC=sk-ant-pac-anthropic-placeholder-0001
PH_GITHUB=ghp_pacGithubPlaceholder000000000000001
PH_ECHO=sk-pac-echo-placeholder-0001

# Command prefixes (arrays, so `timeout` can run them).
BARE=(env -i HOME="$HOME" PATH="$PATH" USER="$USER" LANG=C.UTF-8 DISPLAY=:1)
CONF=("${BARE[@]}" bash -c '. /etc/profile.d/pac-ca.sh; exec "$@"' _)
conf_env() { "${CONF[@]}" "$@"; }

# Verdict from a command's combined output: tls-ok (real server answered), tls-fail, or other.
classify() {
  local out=$1
  if grep -qiE "certificate|self.signed|unknown ?ca|unable to get local issuer|SSL routines|CERT_|x509|tls handshake|invalid peer certificate|UnknownIssuer|net::ERR_CERT" <<<"$out"; then
    echo tls-fail
  elif grep -qiE "^(200|301|302|401|403|404)$|PASS|Example Domain|incorrect api key|invalid x-api-key|authentication_error|invalid_api_key|Bad credentials|401" <<<"$out"; then
    echo tls-ok
  else
    echo other
  fi
}

run() {  # run <name> <command...> : both passes, one TSV line each
  local name=$1; shift
  for mode in bare configured; do
    local pre=("${CONF[@]}"); [ "$mode" = bare ] && pre=("${BARE[@]}")
    local out; out=$(timeout 120 "${pre[@]}" "$@" 2>&1 | tail -c 600 | tr '\n\t' '  ')
    printf '%s\t%s\t%s\t%s\n' "$name" "$mode" "$(classify "$out")" "${out:0:300}" | tee -a "$OUT"
  done
}

: > "$OUT"
printf 'client\tmode\tverdict\toutput\n' >> "$OUT"

# Set up clients that need installing (through the proxy, configured mode, so installs succeed).
conf_env uv venv -q --clear "$VENV" && conf_env uv pip install -q --python "$VENV/bin/python" requests httpx pip >/dev/null 2>&1
command -v codex >/dev/null || conf_env npm install -g --prefix "$HOME/.local" @openai/codex >/dev/null 2>&1
command -v claude >/dev/null || conf_env npm install -g --prefix "$HOME/.local" @anthropic-ai/claude-code >/dev/null 2>&1

run curl            curl -sS -o /dev/null -w '%{http_code}' https://example.com
run python-urllib   python3 -c "import urllib.request as u; print(u.urlopen('https://example.com').status)"
run python-requests "$VENV/bin/python" -c "import requests; print(requests.get('https://example.com').status_code)"
run python-httpx    "$VENV/bin/python" -c "import httpx; print(httpx.get('https://example.com').status_code)"
run node-fetch      node -e "fetch('https://example.com').then(r=>console.log(r.status)).catch(e=>{console.log(e.cause||e);process.exit(1)})"
run npm             npm view left-pad version --registry https://registry.npmjs.org --cache "$HOME/.spike1-npm" --prefer-online
run git             git ls-remote https://github.com/octocat/Hello-World HEAD
run uv              uv pip install --dry-run --python "$VENV/bin/python" --refresh six
run pip             "$VENV/bin/pip" download --no-cache-dir -d /tmp/spike1-pip six
run chromium        chromium --headless=new --disable-gpu --no-first-run --dump-dom https://example.com
run gh              env GH_TOKEN=$PH_GITHUB gh api /user
run codex           env OPENAI_API_KEY=$PH_OPENAI CODEX_API_KEY=$PH_OPENAI codex exec --skip-git-repo-check "Reply with the single word PASS"
run claude-code     env ANTHROPIC_API_KEY=$PH_ANTHROPIC claude -p "Reply with the single word PASS"

# Swap round trip on an echo service: the gateway sends the real (dummy) value and scrubs it from
# the reply, so the client must see its own placeholder come back.
echo_check() {
  local name=$1; shift
  local out; out=$(timeout 60 "${CONF[@]}" "$@" 2>&1 | tr '\n\t' '  ')
  local v=other; grep -q "$PH_ECHO" <<<"$out" && v=swap-roundtrip-ok
  printf '%s\t%s\t%s\t%s\n' "$name" echo "$v" "${out:0:300}" | tee -a "$OUT"
}
echo_check curl          curl -sS https://postman-echo.com/headers -H "Authorization: Bearer $PH_ECHO"
echo_check python        "$VENV/bin/python" -c "import requests; print(requests.get('https://postman-echo.com/headers', headers={'Authorization':'Bearer $PH_ECHO'}).text)"
echo_check node          node -e "fetch('https://postman-echo.com/headers',{headers:{authorization:'Bearer $PH_ECHO'}}).then(r=>r.text()).then(console.log)"

echo; echo "results: $OUT"
