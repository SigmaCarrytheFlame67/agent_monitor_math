#!/bin/bash
# Smoke-test a running ansatze.ai deployment from this host (no root needed).
#
#   deploy/ansatze/smoke.sh [--mount /proof-<hex> | --mount-file PATH] [--www]
#                           [--no-edge] [--no-kimi] [--guest-run] [--public]
#
# Loopback checks talk to the console (127.0.0.1:4600) and the sponsored Kimi
# broker (127.0.0.1:4610) directly.  Edge checks reach Caddy on 127.0.0.1 with
# `curl --resolve`, so they work before public DNS or firewall changes, but
# Caddy must already hold a certificate for ansatze.ai.  --public repeats the
# homepage and /sign-in checks through real DNS.
#
# Costs: one tiny Kimi completion (skip with --no-kimi); --guest-run starts one
# full guest proof (at most 12 CGM calls plus its derived Formal work, 5-25 min).
# Nothing secret is printed: tokens and cookies stay inside this process.
set -uo pipefail

DOMAIN=ansatze.ai
CONSOLE=http://127.0.0.1:4600
BROKER=http://127.0.0.1:4610
EDGE_ADDRESS=127.0.0.1
EDGE_PORT=443
EDGE_SCHEME=https
MOUNT_FILE=/etc/ansatze/mount
MOUNT=
CHECK_WWW=0
EDGE=1
KIMI=1
GUEST_RUN=0
PUBLIC=0
EDGE_WAIT=90
GUEST_TIMEOUT=3600
PYTHON=/usr/bin/python3
FAILURES=0

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --mount PATH          Console mount (/proof-<40 hex>); default: read --mount-file.
  --mount-file PATH     File holding the mount (default /etc/ansatze/mount).
  --www                 Also check the www.ansatze.ai -> apex redirect.
  --no-edge             Skip the Caddy checks.
  --no-kimi             Skip the tiny Kimi completion through the broker.
  --guest-run           Start one full guest proof and wait for it to finish.
  --guest-timeout SEC   Give up on --guest-run after SEC seconds (default 3600;
                        a guest CGM run is bounded at 2400 s plus one API call).
  --public              Also check https://ansatze.ai through public DNS.
  --console URL         Console base (default http://127.0.0.1:4600).
  --broker URL          Broker base (default http://127.0.0.1:4610).
  --edge-port N         Caddy port for --resolve checks (default 443).
  --edge-scheme S       https (default) or http, for a local test listener.
EOF
}

while (($#)); do
  case $1 in
    --mount) MOUNT=${2:-}; shift 2 ;;
    --mount-file) MOUNT_FILE=${2:-}; shift 2 ;;
    --www) CHECK_WWW=1; shift ;;
    --no-edge) EDGE=0; shift ;;
    --no-kimi) KIMI=0; shift ;;
    --guest-run) GUEST_RUN=1; shift ;;
    --guest-timeout) GUEST_TIMEOUT=${2:-}; shift 2 ;;
    --public) PUBLIC=1; shift ;;
    --console) CONSOLE=${2:-}; shift 2 ;;
    --broker) BROKER=${2:-}; shift 2 ;;
    --edge-port) EDGE_PORT=${2:-}; shift 2 ;;
    --edge-scheme) EDGE_SCHEME=${2:-}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z ${MOUNT} ]]; then
  [[ -r ${MOUNT_FILE} ]] || { echo "cannot read ${MOUNT_FILE}; pass --mount" >&2; exit 2; }
  MOUNT=$(<"${MOUNT_FILE}")
fi
[[ ${MOUNT} =~ ^/proof-[0-9a-f]{40}$ ]] || { echo "the console mount must be /proof-<40 hex>" >&2; exit 2; }
[[ ${GUEST_TIMEOUT} =~ ^[0-9]+$ ]] || { echo "--guest-timeout must be a number of seconds" >&2; exit 2; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
[[ -x ${PYTHON} ]] || PYTHON=$(command -v python3) || { echo "python3 is required" >&2; exit 2; }

ok() { printf '  PASS  %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
section() { printf '\n== %s\n' "$*"; }

# ── Loopback services, guest session, broker ─────────────────────────────
section "loopback services"
if ! "${PYTHON}" - "${CONSOLE}" "${BROKER}" "${MOUNT}" "${KIMI}" "${GUEST_RUN}" "${GUEST_TIMEOUT}" <<'PY'
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

console, broker, mount, kimi, guest_run, guest_timeout = sys.argv[1:7]
failures = 0


def report(passed, message):
    global failures
    print(("  PASS  " if passed else "  FAIL  ") + message)
    if not passed:
        failures += 1


jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)
)


def call(url, *, payload=None, bearer="", timeout=20, deadline=None):
    headers = {"Accept": "application/json"}
    if deadline:
        # Our own budget: the broker and gateway then answer (504) before it.
        headers["X-Ansatze-Timeout"] = str(deadline)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(4 * 1024 * 1024)
            status = response.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(64 * 1024), exc.code
    except OSError as exc:
        return 0, {"error": type(exc).__name__}
    try:
        return status, json.loads(body or b"{}")
    except ValueError:
        return status, {}


status, value = call(broker + "/healthz")
report(status == 200 and value.get("service") == "public-kimi",
       f"sponsored Kimi broker healthz ({status})")
status, value = call(console + "/health")
report(status == 200 and value.get("ok") is True, f"console /health ({status})")

status, me = call(console + mount + "/api/auth/me")
report(status == 200, f"console mount answers /api/auth/me ({status})")
if status == 200:
    policy = me.get("guest_policy") or {}
    report(me.get("registration_open") is True, "self-registration is open")
    report(me.get("public") is False, "AGENT_MONITOR_PUBLIC is off (/login stays reachable)")
    report(me.get("guest_access") is True, "guest access is enabled")
    report(policy.get("hosted_guest_available") is True,
           "console reaches the sponsored Kimi broker (hosted guest Kimi available)")

status, value = call(console + mount + "/api/auth/guest", payload={})
guest = (value.get("user") or {}) if isinstance(value, dict) else {}
report(status == 200 and guest.get("guest") is True, f"guest session created ({status})")
if status == 200:
    status, me = call(console + mount + "/api/auth/me")
    report(status == 200 and (me.get("user") or {}).get("guest") is True,
           "guest cookie authenticates the guest")

if guest_run == "1" and guest.get("guest"):
    problem = "Prove that the sum of two even integers is even."
    status, started = call(console + mount + "/api/runs",
                           payload={"engine": "kimi", "problem_text": problem}, timeout=60)
    run_id = str(started.get("run_id") or "")
    report(status == 200 and bool(run_id), f"guest Kimi run started ({status})")
    if run_id:
        deadline = time.monotonic() + int(guest_timeout)
        state = ""
        while time.monotonic() < deadline:
            status, jobs = call(console + mount + "/api/jobs")
            match = [job for job in (jobs.get("jobs") or []) if job.get("run_id") == run_id]
            state = str((match[0] if match else {}).get("status") or "")
            if state in {"finished", "failed", "stopped", "error", "cancelled"}:
                break
            time.sleep(10)
        report(state == "finished", f"guest Kimi run finished (status: {state or 'unknown'})")

if guest.get("guest"):
    call(console + mount + "/api/auth/logout", payload={})

if kimi == "1":
    status, issued = call(broker + "/internal/sessions", payload={"engine": "plain"})
    token = str(issued.get("api_token") or "")
    base = str(issued.get("base_url") or "")
    report(status == 201 and bool(token) and base.startswith(broker + "/internal/v1"),
           f"broker issues a loopback sponsor token ({status})")
    if token:
        status, answer = call(
            base + "/chat/completions",
            bearer=token,
            payload={
                "model": "kimi-k3",
                "stream": False,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            },
            timeout=240,
            deadline=240,
        )
        choices = answer.get("choices") if isinstance(answer, dict) else None
        finish = (choices[0].get("finish_reason") if choices else None)
        report(status == 200 and bool(choices),
               f"tiny Kimi completion through the broker (HTTP {status}, finish_reason={finish})")
        token = ""

sys.exit(1 if failures else 0)
PY
then
  FAILURES=$((FAILURES + 1))
fi

# ── Analyze parser packages (the console venv of this checkout) ──────────
VENV_PYTHON="$(cd "$(dirname "$0")/../.." && pwd)/.venv/bin/python"
if [[ -x ${VENV_PYTHON} ]]; then
  section "analyze parser packages"
  if "${VENV_PYTHON}" -c 'import pylatexenc, pypdf' >/dev/null 2>&1; then
    ok "pylatexenc and pypdf import in ${VENV_PYTHON}"
  else
    bad "pylatexenc or pypdf is missing from ${VENV_PYTHON} (Analyze runs stop with a configuration error)"
  fi
fi

# ── Caddy edge (curl --resolve to this host) ─────────────────────────────
edge_curl() {
  curl -sS --max-time 20 --proto '=http,https' \
    --resolve "${DOMAIN}:${EDGE_PORT}:${EDGE_ADDRESS}" \
    --resolve "www.${DOMAIN}:${EDGE_PORT}:${EDGE_ADDRESS}" "$@"
}
edge_base() {
  local host=${1:-${DOMAIN}}
  if [[ ${EDGE_SCHEME}:${EDGE_PORT} == https:443 || ${EDGE_SCHEME}:${EDGE_PORT} == http:80 ]]; then
    printf '%s://%s' "${EDGE_SCHEME}" "${host}"
  else
    printf '%s://%s:%s' "${EDGE_SCHEME}" "${host}" "${EDGE_PORT}"
  fi
}

if ((EDGE)); then
  section "Caddy edge via --resolve ${DOMAIN}:${EDGE_PORT}:${EDGE_ADDRESS}"
  base=$(edge_base)
  ready=0
  for ((i = 0; i < EDGE_WAIT; i += 3)); do
    if edge_curl -o /dev/null "${base}/" 2>/dev/null; then ready=1; break; fi
    sleep 3
  done
  if ((ready == 0)); then
    bad "Caddy is not serving ${base}/ yet.  Usually the certificate is still missing:"
    echo "        check DNS (A record -> this host), security group ports 80/443,"
    echo "        and 'journalctl -u caddy' for ACME errors, then re-run this script."
  else
    expect() {  # expect DESCRIPTION COMMAND...: PASS when COMMAND succeeds
      local description=$1
      shift
      if "$@"; then ok "${description}"; else bad "${description}"; fi
    }
    read -r code type < <(edge_curl -o /dev/null -w '%{http_code} %{content_type}\n' "${base}/")
    expect "homepage is 200 text/html (got ${code} ${type})" test "${code}" = 200 -a "${type%%;*}" = text/html
    headers=$(edge_curl -o /dev/null -D - "${base}/" | tr -d '\r')
    expect "HSTS header present" grep -qi '^strict-transport-security: max-age=' <<<"${headers}"
    expect "no Server header" test -z "$(grep -i '^server:' <<<"${headers}")"

    read -r code location < <(edge_curl -o /dev/null -w '%{http_code} %{redirect_url}\n' "${base}/sign-in")
    expect "/sign-in -> 302 <mount>/login (got ${code})" test "${code}" = 302 -a "${location}" = "${base}${MOUNT}/login"
    read -r code location < <(edge_curl -o /dev/null -w '%{http_code} %{redirect_url}\n' "${base}/sign-in?smoke=1")
    expect "/sign-in keeps its query string (got ${code})" test "${code}" = 302 -a "${location}" = "${base}${MOUNT}/login?smoke=1"
    read -r code final < <(edge_curl -L -o /dev/null -w '%{http_code} %{url_effective}\n' "${base}/sign-in")
    expect "following /sign-in ends at a 200 .../login, as the docs research client requires (got ${code})" \
      test "${code}" = 200 -a "${final%/login}" != "${final}"
    code=$(edge_curl -o /dev/null -w '%{http_code}' "${base}${MOUNT}/api/auth/me")
    expect "console API through Caddy (got ${code})" test "${code}" = 200

    jar=$(mktemp)
    cookie=$(edge_curl -c "${jar}" -o /dev/null -D - -H 'Content-Type: application/json' -d '{}' \
      "${base}${MOUNT}/api/auth/guest" | tr -d '\r' | grep -i '^set-cookie: pc_session=' | head -n1 \
      | sed -E 's/pc_session=[^;]*/pc_session=<redacted>/')
    if [[ ${EDGE_SCHEME} == https ]]; then
      expect "session cookie is Secure and HttpOnly behind Caddy (${cookie:-no cookie})" \
        test -n "${cookie}" -a -z "${cookie##*; Secure*}" -a -z "${cookie##*HttpOnly*}"
    else
      expect "guest session through Caddy (${cookie:-no cookie})" test -n "${cookie}"
    fi
    edge_curl -b "${jar}" -o /dev/null -H 'Content-Type: application/json' -d '{}' \
      "${base}${MOUNT}/api/auth/logout" || true
    rm -f -- "${jar}"

    code=$(edge_curl -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
      -d '{"engine":"plain"}' "${base}/internal/sessions")
    expect "/internal/* is not routed (got ${code})" test "${code}" = 404
    code=$(edge_curl -o /dev/null -w '%{http_code}' "${base}/no-such-page-$$")
    expect "unknown pages return 404 (got ${code})" test "${code}" = 404
    # handle_errors is a separate route list; the edge headers must reach it.
    headers=$(edge_curl -o /dev/null -D - "${base}/no-such-page-$$" | tr -d '\r')
    expect "404 page has HSTS" grep -qi '^strict-transport-security: max-age=' <<<"${headers}"
    expect "404 page has X-Content-Type-Options nosniff" grep -qi '^x-content-type-options: nosniff' <<<"${headers}"
    expect "404 page has no Server header" test -z "$(grep -i '^server:' <<<"${headers}")"
    code=$(edge_curl -o /dev/null -w '%{http_code}' "${base}/research/news.json")
    expect "/research/news.json (got ${code})" test "${code}" = 200
    # The live overlay (ansatze-problems timer) or the bundled empty one.
    headers=$(edge_curl -o /dev/null -D - "${base}/research/problem-status.json" | tr -d '\r')
    expect "/research/problem-status.json is served" grep -q '^HTTP/[0-9.]* 200' <<<"${headers}"
    expect "/research/problem-status.json is no-cache" grep -qi '^cache-control: no-cache' <<<"${headers}"
    body=$(edge_curl "${base}/research/problem-status.json")
    expect "/research/problem-status.json is a schema-1 overlay" grep -q '"schema_version": *1' <<<"${body}"
    if ((CHECK_WWW)); then
      read -r code location < <(edge_curl -o /dev/null -w '%{http_code} %{redirect_url}\n' \
        "$(edge_base "www.${DOMAIN}")/news?x=1")
      expect "www -> apex 308 (got ${code} ${location})" test "${code}" = 308 -a "${location}" = "https://${DOMAIN}/news?x=1"
      headers=$(edge_curl -o /dev/null -D - "$(edge_base "www.${DOMAIN}")/" | tr -d '\r')
      expect "www redirect has HSTS" grep -qi '^strict-transport-security: max-age=' <<<"${headers}"
      expect "www redirect has no Server header" test -z "$(grep -i '^server:' <<<"${headers}")"
    fi
  fi
fi

if ((PUBLIC)); then
  section "public DNS"
  code=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "https://${DOMAIN}/" 2>/dev/null)
  if [[ ${code} == 200 ]]; then ok "https://${DOMAIN}/ (${code})"; else bad "https://${DOMAIN}/ returned ${code:-no answer}"; fi
  code=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "https://${DOMAIN}/sign-in" 2>/dev/null)
  if [[ ${code} == 302 ]]; then ok "https://${DOMAIN}/sign-in (${code})"; else bad "https://${DOMAIN}/sign-in returned ${code:-no answer}"; fi
fi

echo
if ((FAILURES)); then
  echo "Smoke checks: ${FAILURES} failure group(s)."
  exit 1
fi
echo "Smoke checks passed."
