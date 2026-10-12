#!/bin/bash
# Install or update the https://ansatze.ai deployment on this host.
#
#   sudo deploy/ansatze/install.sh [options]        # interactive, step by step
#   deploy/ansatze/install.sh --dry-run             # print every privileged command
#
# Steps (run in this order; each asks for confirmation unless --yes):
#   packages     apt install caddy sqlite3
#   credentials  /etc/ansatze, data key, Kimi key credentials, console.env
#   render       console mount (generated once), Caddyfile and unit files
#   services     install and (re)start ansatze-kimi, then agent-monitor
#   lean         measure the Lean trees, install the Lean broker, restart console
#   docs         npm ci && npm run docs:build (as the service user)
#   caddy        caddy validate, install /etc/caddy/Caddyfile, reload
#   admin        create the owner's admin account (agent-monitor create-admin)
#   smoke        deploy/ansatze/smoke.sh against the local services
#
# Re-running is safe: credentials, the console mount, users and data are kept.
# Nothing here opens self-registration or puts a Kimi key in the console env.
set -euo pipefail
umask 022
# Bash 5.2 expands "&" in ${var//pattern/replacement}; templates must not.
shopt -u patsub_replacement 2>/dev/null || true

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

# Fixed host layout.  The unit and Caddyfile templates name these paths.
APP_DIR=/home/repo/agent_monitor_math
SERVICE_USER=ubuntu
DOMAIN=ansatze.ai
ETC_DIR=/etc/ansatze
CRED_DIR=/etc/ansatze/credentials
BACKUP_DIR=/etc/ansatze/backup
RENDERED_DIR=/etc/ansatze/rendered
MOUNT_FILE=/etc/ansatze/mount
CONSOLE_ENV=/etc/ansatze/console.env
KIMI_LIMITS_ENV=/etc/ansatze/kimi-limits.env
STATE_ROOT=/var/lib/ansatze
CONSOLE_DATA=/var/lib/ansatze/console
NEWS_STATE=/var/lib/ansatze-news
PROBLEMS_STATE=/var/lib/ansatze-problems
KIMI_USER=ansatze-kimi
NEWS_USER=ansatze-news
PROBLEMS_USER=ansatze-problems
UNIT_DIR=/etc/systemd/system
CADDYFILE=/etc/caddy/Caddyfile
DOCS_DIST=${APP_DIR}/docs/.vitepress/dist
VENV_PYTHON=${APP_DIR}/.venv/bin/python
PYTHON_HOME=/home/ubuntu/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu
NODE_BIN=/home/ubuntu/.local/node24/bin
USER_PATH=/home/ubuntu/.local/bin:/home/ubuntu/.elan/bin:${NODE_BIN}:/home/ubuntu/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
ALL_STEPS=(packages credentials render services lean docs caddy admin smoke)

# Reviewed Lean tree pins, measured on this host with
# deploy/lean-broker/prepare_release.py --measure-only.
LEAN_TOOLCHAIN=/home/ubuntu/.elan/toolchains/leanprover--lean4---v4.14.0
LEAN_MATHLIB=/home/ubuntu/.cache/agent-monitor/mathlib-v4.14.0
LEAN_TOOLCHAIN_PIN=defeb1c29ed4009c5529b5acb06917a04923f53beb1098c28164b9c8cebc0c84
LEAN_MATHLIB_PIN=0840a02e6c2cf888721fa183eb49a397b188d5d24c6598e98534f2b3190a297e

DRY_RUN=0
ASSUME_YES=0
KIMI_ENV_FILE=${APP_DIR}/.secrets/kimi.env
UPDATE_KIMI_KEY=0
REMOVE_STAGED_KEY=0
WWW_MODE=auto
ACME_EMAIL=
ADMIN_EMAIL=
WITH_NEWS=0
WITH_PROBLEMS=0
ACCEPT_MEASURED=0
GUEST_RUN=0
UNINSTALL=0
PURGE=0
SELECTED_STEPS=
SKIPPED_STEPS=
RENDER_TO=
WORK_DIR=
MOUNT=
CONSOLE_CHANGED=0

usage() {
  cat <<'EOF'
Usage: sudo deploy/ansatze/install.sh [options]

  --dry-run              Print every privileged command instead of running it
                         (works without root; nothing on the host changes).
  -y, --yes              Do not ask before each step.
  --steps a,b,...        Run only these steps (packages credentials render
                         services lean docs caddy admin smoke).
  --skip STEP            Skip one step (repeatable).
  --kimi-env PATH        Staged file with KIMI_API_KEY= and KIMI_API_BASE=
                         (default: <checkout>/.secrets/kimi.env).
  --update-kimi-key      Replace existing Kimi credentials (key rotation).
  --remove-staged-key    Shred the staged Kimi file after storing the credential.
  --www auto|yes|no      Serve www.ansatze.ai as a 308 redirect to the apex
                         (auto: only when www resolves like the apex).
  --acme-email EMAIL     Contact address for the ACME (TLS certificate) account.
  --admin-email EMAIL    Email for the admin account step (prompted otherwise).
  --with-news            Also install ansatze-news.service and .timer.
  --with-problem-status  Also install ansatze-problems.service and .timer (the
                         live /research/problem-status.json overlay).
  --lean-toolchain PATH  Lean v4.14.0 toolchain (default: ~ubuntu/.elan/...).
  --lean-mathlib PATH    Built Mathlib v4.14.0 project.
  --accept-measured      Accept Lean tree hashes that differ from the pins.
  --guest-run            Smoke step: also run one full guest proof (spends
                         Kimi budget; takes several minutes).
  --render-to DIR        Only render the Caddyfile and units into DIR (no
                         root, nothing installed); combine with --mount to
                         review exactly what a real run would install.
  --mount /proof-HEX     Console mount to render with --render-to (default:
                         /etc/ansatze/mount, else a random example).
  --uninstall            Stop and remove the ansatze units and restore the
                         previous Caddyfile.  Keeps data and credentials.
  --purge                With --uninstall: also delete /var/lib/ansatze,
                         /etc/ansatze and the service users.  This destroys
                         every account, run and the data key.
  -h, --help             Show this help.
EOF
}

if [[ -t 1 ]]; then
  C_HEAD=$'\033[1;36m' C_OK=$'\033[1;32m' C_WARN=$'\033[1;33m' C_ERR=$'\033[1;31m' C_CMD=$'\033[36m' C_OFF=$'\033[0m'
else
  C_HEAD='' C_OK='' C_WARN='' C_ERR='' C_CMD='' C_OFF=''
fi
die() { printf '\n%serror:%s %s\n' "${C_ERR}" "${C_OFF}" "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }
warn() { printf '  %swarning:%s %s\n' "${C_WARN}" "${C_OFF}" "$*" >&2; }

# Print a command the way a shell would read it, then run it (unless --dry-run).
show() { printf '  %s+%s' "${C_CMD}" "${C_OFF}"; printf ' %q' "$@"; printf '\n'; }
run() {
  show "$@"
  if ((DRY_RUN)); then return 0; fi
  "$@"
}

# Run a command as the service user.  Read-only checks use this even in
# --dry-run; anything that changes state goes through run_as_user.
as_user() {
  if ((EUID == 0)); then
    runuser -u "${SERVICE_USER}" -- "$@"
  elif [[ $(id -un) == "${SERVICE_USER}" ]]; then
    "$@"
  else
    die "run this as root (sudo) or as ${SERVICE_USER}"
  fi
}
run_as_user() {
  if ((EUID == 0 || DRY_RUN)); then
    run runuser -u "${SERVICE_USER}" -- "$@"
  else
    run "$@"
  fi
}

step_wanted() {
  local step=$1
  if [[ -n ${SELECTED_STEPS} && ",${SELECTED_STEPS}," != *",${step},"* ]]; then
    return 1
  fi
  [[ ",${SKIPPED_STEPS}," != *",${step},"* ]]
}

# Header plus confirmation.  Returns 1 when the operator chooses to skip.
confirm_step() {
  local step=$1 summary=$2 answer
  printf '\n%s== %s:%s %s\n' "${C_HEAD}" "${step}" "${C_OFF}" "${summary}"
  if ((DRY_RUN || ASSUME_YES)); then return 0; fi
  read -r -p "   Run step '${step}'? [y]es / [s]kip / [q]uit: " answer || answer=q
  case ${answer} in
    y|Y|yes) return 0 ;;
    s|S|skip) note "skipped"; return 1 ;;
    *) die "stopped at step '${step}'" ;;
  esac
}

# Install SRC at DEST with MODE/OWNER/GROUP and report whether it changed.
install_file() {
  local src=$1 dest=$2 mode=$3 owner=$4 group=$5
  if [[ -f ${dest} ]] && cmp -s -- "${src}" "${dest}"; then
    note "unchanged: ${dest}"
    return 1
  fi
  # Checked explicitly: callers use this in conditions, where set -e is off.
  run install -D -m "${mode}" -o "${owner}" -g "${group}" -- "${src}" "${dest}" \
    || die "could not install ${dest}"
  return 0
}

backup_once() {
  local path=$1 name=$2
  if [[ -e ${path} && ! -e ${BACKUP_DIR}/${name} ]]; then
    run install -d -m 0700 -o root -g root "${BACKUP_DIR}"
    run cp -a -- "${path}" "${BACKUP_DIR}/${name}"
  fi
}

wait_for_url() {
  local url=$1 label=$2 tries=${3:-60}
  if ((DRY_RUN)); then note "(dry-run) would wait for ${label}: ${url}"; return 0; fi
  local i
  for ((i = 0; i < tries; i++)); do
    if curl -fsS --max-time 3 -o /dev/null "${url}" 2>/dev/null; then
      note "${label} is healthy"
      return 0
    fi
    sleep 1
  done
  return 1
}

check_port() {
  # Refuse to start a unit whose port is held by an unrelated process.
  local port=$1 unit=$2
  if ! ss -ltnH "sport = :${port}" 2>/dev/null | grep -q .; then return 0; fi
  if systemctl is-active --quiet "${unit}" 2>/dev/null; then return 0; fi
  if ((DRY_RUN)); then
    warn "port ${port} is in use by a process other than ${unit}; a real run would stop here"
    return 0
  fi
  die "port ${port} is in use by a process other than ${unit} (a development server?); stop it first"
}

parse_args() {
  while (($#)); do
    case $1 in
      --dry-run) DRY_RUN=1; shift ;;
      -y|--yes) ASSUME_YES=1; shift ;;
      --steps) (($# >= 2)) || die "--steps needs a value"; SELECTED_STEPS=$2; shift 2 ;;
      --skip) (($# >= 2)) || die "--skip needs a value"; SKIPPED_STEPS+=",$2"; shift 2 ;;
      --kimi-env) (($# >= 2)) || die "--kimi-env needs a path"; KIMI_ENV_FILE=$2; shift 2 ;;
      --update-kimi-key) UPDATE_KIMI_KEY=1; shift ;;
      --remove-staged-key) REMOVE_STAGED_KEY=1; shift ;;
      --www) (($# >= 2)) || die "--www needs auto|yes|no"; WWW_MODE=$2; shift 2 ;;
      --acme-email) (($# >= 2)) || die "--acme-email needs a value"; ACME_EMAIL=$2; shift 2 ;;
      --admin-email) (($# >= 2)) || die "--admin-email needs a value"; ADMIN_EMAIL=$2; shift 2 ;;
      --with-news) WITH_NEWS=1; shift ;;
      --with-problem-status) WITH_PROBLEMS=1; shift ;;
      --lean-toolchain) (($# >= 2)) || die "--lean-toolchain needs a path"; LEAN_TOOLCHAIN=$2; shift 2 ;;
      --lean-mathlib) (($# >= 2)) || die "--lean-mathlib needs a path"; LEAN_MATHLIB=$2; shift 2 ;;
      --accept-measured) ACCEPT_MEASURED=1; shift ;;
      --guest-run) GUEST_RUN=1; shift ;;
      --render-to) (($# >= 2)) || die "--render-to needs a directory"; RENDER_TO=$2; shift 2 ;;
      --mount) (($# >= 2)) || die "--mount needs /proof-<40 hex>"; MOUNT=$2; shift 2 ;;
      --uninstall) UNINSTALL=1; shift ;;
      --purge) PURGE=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
  done
  case ${WWW_MODE} in auto|yes|no) ;; *) die "--www must be auto, yes or no" ;; esac
  if [[ -n ${ACME_EMAIL} && ! ${ACME_EMAIL} =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
    die "--acme-email is not an email address"
  fi
  local step
  for step in ${SELECTED_STEPS//,/ } ${SKIPPED_STEPS//,/ }; do
    [[ " ${ALL_STEPS[*]} " == *" ${step} "* ]] || die "unknown step: ${step}"
  done
  if ((PURGE && !UNINSTALL)); then die "--purge is only valid with --uninstall"; fi
  if [[ -n ${MOUNT} ]]; then
    [[ -n ${RENDER_TO} ]] || die "--mount is only valid with --render-to (edit ${MOUNT_FILE} to change the live mount)"
    [[ ${MOUNT} =~ ^/proof-[0-9a-f]{40}$ ]] || die "--mount must be /proof-<40 lowercase hex>"
  fi
}

preflight() {
  if ((!DRY_RUN && EUID != 0)); then
    die "run as root: sudo $0 $*  (or add --dry-run to preview)"
  fi
  local checkout
  checkout=$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)
  [[ ${checkout} == "${APP_DIR}" ]] || die "the templates expect the checkout at ${APP_DIR} (this one is ${checkout})"
  id -u "${SERVICE_USER}" >/dev/null 2>&1 || die "service user ${SERVICE_USER} does not exist"
  [[ -x ${VENV_PYTHON} ]] || die "missing ${VENV_PYTHON}; create the virtualenv first (./setup.sh)"
  local home_line
  home_line=$(grep -E '^home *= *' "${APP_DIR}/.venv/pyvenv.cfg" | head -n1 | sed -E 's/^home *= *//')
  [[ ${home_line%/} == "${PYTHON_HOME}/bin" ]] || die "the virtualenv interpreter must live under ${PYTHON_HOME} (pyvenv.cfg home=${home_line}); ansatze-kimi.service bind-mounts exactly that path. Recreate it as ${SERVICE_USER}: rm -rf .venv && PYTHON=${PYTHON_HOME}/bin/python3.13 ./setup.sh"
  as_user "${VENV_PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' \
    || die "the virtualenv must run Python 3.13"
  for tool in systemctl curl install cmp ss; do
    command -v "${tool}" >/dev/null || die "required command missing: ${tool}"
  done
  WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ansatze-install.XXXXXX")
  chmod 0700 "${WORK_DIR}"
  trap 'rm -rf -- "${WORK_DIR}"' EXIT
  if ((DRY_RUN)); then
    printf '%sDRY RUN%s: printing privileged commands only; nothing on the host changes.\n' "${C_WARN}" "${C_OFF}"
  fi
}

# ── (a) packages ────────────────────────────────────────────────────────────
step_packages() {
  confirm_step packages "install caddy (2.6 from Ubuntu) and sqlite3" || return 0
  local missing=() package
  for package in caddy sqlite3; do
    if ! dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | grep -q 'install ok installed'; then
      missing+=("${package}")
    fi
  done
  if ((${#missing[@]} == 0)); then note "caddy and sqlite3 are already installed"; return 0; fi
  note "Installing starts caddy with the package's default :80 page until the"
  note "'caddy' step installs the ansatze.ai configuration."
  run apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

# Read KIMI_API_KEY / KIMI_API_BASE from the staged file into two variables
# without echoing, logging or passing either value on a command line.
read_staged_kimi() {
  local file=$1 line name value
  STAGED_KIMI_KEY=
  STAGED_KIMI_BASE=
  [[ ! -L ${file} && -f ${file} ]] || die "staged Kimi file ${file} is missing or is not a regular file"
  while IFS= read -r line || [[ -n ${line} ]]; do
    line=${line%$'\r'}
    [[ ${line} =~ ^[[:space:]]*(export[[:space:]]+)?(KIMI_API_KEY|KIMI_API_BASE)[[:space:]]*=(.*)$ ]] || continue
    name=${BASH_REMATCH[2]}
    value=${BASH_REMATCH[3]}
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    if [[ ${#value} -ge 2 && ( ${value:0:1} == '"' || ${value:0:1} == "'" ) && ${value: -1} == "${value:0:1}" ]]; then
      value=${value:1:${#value}-2}
    fi
    if [[ ${name} == KIMI_API_KEY ]]; then STAGED_KIMI_KEY=${value}; else STAGED_KIMI_BASE=${value}; fi
  done <"${file}"
  if ((${#STAGED_KIMI_KEY} < 16)) || [[ ${STAGED_KIMI_KEY} =~ [[:space:]] ]]; then
    die "staged file has no usable KIMI_API_KEY"
  fi
  if [[ -z ${STAGED_KIMI_BASE} ]]; then
    STAGED_KIMI_BASE=$(<"${APP_DIR}/deploy/public-kimi/kimi_api_base.default")
  fi
  [[ ${STAGED_KIMI_BASE} =~ ^https://[^[:space:]/?#@]+(/[^[:space:]?#]*)?$ ]] || die "KIMI_API_BASE must be a plain https:// URL"
}

write_secret() {
  # printf is a shell builtin: the value never appears in a process listing.
  local value=$1 dest=$2 tmp
  tmp=$(mktemp "${WORK_DIR}/secret.XXXXXX")
  printf '%s' "${value}" >"${tmp}"
  run install -m 0400 -o root -g root -- "${tmp}" "${dest}"
  rm -f -- "${tmp}"
}

# ── (b) directories, users and credentials ─────────────────────────────────
step_credentials() {
  confirm_step credentials "create ${ETC_DIR}, ${STATE_ROOT}, service users and root-only credentials" || return 0
  run install -d -m 0755 -o root -g root "${ETC_DIR}"
  run install -d -m 0700 -o root -g root "${CRED_DIR}"
  run install -d -m 0755 -o root -g root "${STATE_ROOT}"
  run install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${CONSOLE_DATA}"
  if ! id -u "${KIMI_USER}" >/dev/null 2>&1; then
    run useradd --system --user-group --no-create-home --home-dir "${STATE_ROOT}/kimi" \
      --shell /usr/sbin/nologin --comment "Ansatze sponsored Kimi broker" "${KIMI_USER}"
  fi
  if ((WITH_NEWS)) && ! id -u "${NEWS_USER}" >/dev/null 2>&1; then
    run useradd --system --user-group --no-create-home --home-dir "${NEWS_STATE}" \
      --shell /usr/sbin/nologin --comment "Ansatze news refresh" "${NEWS_USER}"
  fi
  if ((WITH_PROBLEMS)) && ! id -u "${PROBLEMS_USER}" >/dev/null 2>&1; then
    run useradd --system --user-group --no-create-home --home-dir "${PROBLEMS_STATE}" \
      --shell /usr/sbin/nologin --comment "Ansatze problem-status refresh" "${PROBLEMS_USER}"
  fi

  # Per-user settings key.  Never regenerate: that would make every saved
  # key and subscription setting unreadable.
  if [[ -e ${CRED_DIR}/agent_monitor_data_key ]]; then
    note "keeping existing ${CRED_DIR}/agent_monitor_data_key"
  elif ((DRY_RUN)); then
    show install -m 0400 -o root -g root "<new Fernet key>" "${CRED_DIR}/agent_monitor_data_key"
  else
    # Fernet.generate_key() is urlsafe_b64encode(os.urandom(32)); use the
    # root-owned system interpreter rather than the user-writable virtualenv.
    write_secret "$(/usr/bin/python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')" \
      "${CRED_DIR}/agent_monitor_data_key"
  fi

  # Kimi key and base: only the broker's LoadCredential= reads these.
  if [[ -e ${CRED_DIR}/kimi_api_key && ${UPDATE_KIMI_KEY} -eq 0 ]]; then
    note "keeping existing Kimi credentials (use --update-kimi-key to replace them)"
  elif ((DRY_RUN)); then
    [[ -f ${KIMI_ENV_FILE} ]] || warn "staged Kimi file ${KIMI_ENV_FILE} does not exist"
    if [[ -r ${KIMI_ENV_FILE} ]]; then
      grep -qE '^[[:space:]]*(export[[:space:]]+)?KIMI_API_KEY[[:space:]]*=.' "${KIMI_ENV_FILE}" \
        || warn "staged Kimi file has no KIMI_API_KEY line"
    fi
    show install -m 0400 -o root -g root "<KIMI_API_KEY from ${KIMI_ENV_FILE}>" "${CRED_DIR}/kimi_api_key"
    show install -m 0400 -o root -g root "<KIMI_API_BASE from ${KIMI_ENV_FILE}>" "${CRED_DIR}/kimi_api_base"
  else
    read_staged_kimi "${KIMI_ENV_FILE}"
    write_secret "${STAGED_KIMI_KEY}" "${CRED_DIR}/kimi_api_key"
    write_secret "${STAGED_KIMI_BASE}" "${CRED_DIR}/kimi_api_base"
    STAGED_KIMI_KEY=
    note "stored the Kimi key as a root-only credential (${#STAGED_KIMI_BASE}-byte base URL)"
    if ((UPDATE_KIMI_KEY)) && systemctl is-active --quiet ansatze-kimi.service; then
      run systemctl restart ansatze-kimi.service
    fi
  fi

  # Console environment: no provider keys.  Created once; edit it by hand.
  if [[ ! -e ${CONSOLE_ENV} ]]; then
    local tmp=${WORK_DIR}/console.env
    cat >"${tmp}" <<'EOF'
# Server settings for agent-monitor.service (AGENT_MONITOR_ENV_PATH).
# Root-managed and read-only to the console.  Never put KIMI_API_KEY or any
# other provider key here: Kimi comes only from ansatze-kimi.service, and
# users save their own keys (encrypted) in Settings.
#
# Outgoing email for password-reset and confirmation links (optional;
# authenticated STARTTLS, usually port 587):
# SMTP_SERVER=
# SMTP_PORT=587
# SMTP_USERNAME=
# SMTP_PASSWORD=
# SMTP_FROM_EMAIL=
#
# Google sign-in (optional; authorized JavaScript origin https://ansatze.ai):
# GOOGLE_OAUTH_CLIENT_ID=
#
# Sign-up limits (optional): only these address suffixes may register, and
# new accounts must confirm their address by email (needs SMTP above).
# AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES=.edu
# AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION=1
EOF
    install_file "${tmp}" "${CONSOLE_ENV}" 0640 root "${SERVICE_USER}" || true
  elif grep -qE '^[[:space:]]*(export[[:space:]]+)?(KIMI_API_KEY|AGENT_MONITOR_GUEST_KIMI_API_KEY)[[:space:]]*=[[:space:]]*[^[:space:]#]' "${CONSOLE_ENV}"; then
    die "${CONSOLE_ENV} contains a Kimi key; remove it (the broker is the only key holder)"
  else
    note "keeping existing ${CONSOLE_ENV}"
  fi
  if [[ ! -e ${KIMI_LIMITS_ENV} ]]; then
    local limits=${WORK_DIR}/kimi-limits.env
    cat >"${limits}" <<'EOF'
# Optional limits for ansatze-kimi.service (restart it after editing).
# Each value is clamped to a hard ceiling in PublicLimits; see
# deploy/ansatze/README.md.  Defaults shown.
# PUBLIC_KIMI_GATEWAY_CALLS_PER_HOUR=900
# PUBLIC_KIMI_GATEWAY_CONCURRENCY=8
# A call that finds every slot busy waits up to this many seconds for one
# (first come, first served; 0 = refuse at once with HTTP 429).
# PUBLIC_KIMI_GATEWAY_QUEUE_SECONDS=60
# PUBLIC_KIMI_GATEWAY_QUEUE_DEPTH=32
# Total time of one model call, queue wait included (60-900 seconds).
# PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS=600
# PUBLIC_KIMI_MAX_OUTPUT_TOKENS=8192
# PUBLIC_KIMI_SPONSOR_CALLS_PER_TOKEN=200
# PUBLIC_KIMI_SPONSOR_STARTS_PER_HOUR=60
# PUBLIC_KIMI_SPONSOR_TOKENS_PER_CLIENT=64
EOF
    install_file "${limits}" "${KIMI_LIMITS_ENV}" 0644 root root || true
  fi
  if [[ -r ${APP_DIR}/.env ]] && grep -qE '^[[:space:]]*KIMI_API_KEY[[:space:]]*=[[:space:]]*[^[:space:]#]' "${APP_DIR}/.env"; then
    warn "${APP_DIR}/.env holds a KIMI_API_KEY.  Production does not read it, but delete it from the checkout."
  fi
  if ((REMOVE_STAGED_KEY)); then
    if [[ -e ${KIMI_ENV_FILE} ]]; then
      run shred -u -- "${KIMI_ENV_FILE}"
    fi
  elif [[ -e ${KIMI_ENV_FILE} ]]; then
    warn "the staged key file ${KIMI_ENV_FILE} is still readable by ${SERVICE_USER}; remove it (--remove-staged-key) once the broker works"
  fi
}

load_or_create_mount() {
  if [[ -e ${MOUNT_FILE} ]]; then
    MOUNT=$(<"${MOUNT_FILE}")
    [[ ${MOUNT} =~ ^/proof-[0-9a-f]{40}$ ]] || die "${MOUNT_FILE} does not hold /proof-<40 hex>"
    note "keeping console mount from ${MOUNT_FILE}"
    return 0
  fi
  MOUNT=/proof-$(od -An -N20 -tx1 /dev/urandom | tr -d ' \n')
  [[ ${MOUNT} =~ ^/proof-[0-9a-f]{40}$ ]] || die "could not generate a console mount"
  printf '%s\n' "${MOUNT}" >"${WORK_DIR}/mount"
  run install -D -m 0644 -o root -g root -- "${WORK_DIR}/mount" "${MOUNT_FILE}"
  if ((DRY_RUN)); then note "(dry-run) the mount above is an example; a real run generates its own"; fi
}

www_enabled() {
  case ${WWW_MODE} in
    yes) return 0 ;;
    no) return 1 ;;
  esac
  local apex www
  apex=$(getent ahostsv4 "${DOMAIN}" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')
  www=$(getent ahostsv4 "www.${DOMAIN}" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')
  if [[ -n ${www} && ${www} == "${apex}" ]]; then return 0; fi
  note "www.${DOMAIN} does not resolve like ${DOMAIN} (www='${www% }', apex='${apex% }');"
  note "omitting the www redirect.  Add the DNS record and re-run with --www yes."
  return 1
}

render_template() {
  # render_template SRC DEST: substitute tokens and drop disabled blocks.
  local src=$1 dest=$2 text
  text=$(<"${src}")
  text=${text//@@MOUNT@@/${MOUNT}}
  if [[ -n ${ACME_EMAIL} ]]; then
    text=${text//@@ACME_EMAIL@@/${ACME_EMAIL}}
  else
    text=$(printf '%s\n' "${text}" | sed '/^[[:space:]]*#@acme-begin$/,/^[[:space:]]*#@acme-end$/d')
  fi
  if [[ ${WWW_ENABLED} -eq 0 ]]; then
    text=$(printf '%s\n' "${text}" | sed '/^[[:space:]]*#@www-begin$/,/^[[:space:]]*#@www-end$/d')
  fi
  printf '%s\n' "${text}" >"${dest}"
  if grep -q '@@[A-Z_]*@@' "${dest}"; then
    die "unrendered token left in ${dest}"
  fi
}

# ── (c) console mount, Caddyfile and units ─────────────────────────────────
step_render() {
  confirm_step render "generate the console mount once; render Caddyfile and units into ${RENDERED_DIR}" || return 0
  load_or_create_mount
  WWW_ENABLED=0
  if www_enabled; then WWW_ENABLED=1; fi
  local out=${WORK_DIR}/rendered name
  mkdir -p "${out}"
  render_template "${SCRIPT_DIR}/Caddyfile" "${out}/Caddyfile"
  for name in agent-monitor.service ansatze-kimi.service ansatze-news.service ansatze-news.timer \
      ansatze-problems.service ansatze-problems.timer; do
    render_template "${SCRIPT_DIR}/${name}" "${out}/${name}"
  done
  if command -v systemd-analyze >/dev/null; then
    (cd "${out}" && systemd-analyze verify --man=no ./ansatze-kimi.service ./agent-monitor.service \
      ./ansatze-news.service ./ansatze-news.timer ./ansatze-problems.service ./ansatze-problems.timer 2>&1 \
      | grep -v '^/usr/lib/systemd/' || true)
  fi
  if command -v caddy >/dev/null; then
    caddy adapt --config "${out}/Caddyfile" --adapter caddyfile >/dev/null \
      || die "the rendered Caddyfile does not parse"
  else
    note "caddy is not installed yet; the Caddyfile is validated in the 'caddy' step"
  fi
  run install -d -m 0755 -o root -g root "${RENDERED_DIR}"
  for name in Caddyfile agent-monitor.service ansatze-kimi.service ansatze-news.service ansatze-news.timer \
      ansatze-problems.service ansatze-problems.timer; do
    install_file "${out}/${name}" "${RENDERED_DIR}/${name}" 0644 root root || true
  done
  note "console mount: ${MOUNT}   (public entry: https://${DOMAIN}/sign-in)"
}

require_rendered() {
  if [[ -z ${MOUNT} ]]; then
    [[ -e ${MOUNT_FILE} ]] || ((DRY_RUN)) || die "run the 'render' step first"
    [[ -e ${MOUNT_FILE} ]] && MOUNT=$(<"${MOUNT_FILE}")
  fi
  if ((DRY_RUN)) && [[ ! -e ${RENDERED_DIR}/agent-monitor.service ]]; then
    RENDER_SOURCE=${WORK_DIR}/rendered
  else
    RENDER_SOURCE=${RENDERED_DIR}
  fi
  [[ -e ${RENDER_SOURCE}/agent-monitor.service ]] || die "no rendered units in ${RENDER_SOURCE}; run the 'render' step"
}

restart_and_wait() {
  local unit=$1 url=$2
  run systemctl restart "${unit}"
  if ! wait_for_url "${url}" "${unit}" 60; then
    printf '\n' >&2
    journalctl -u "${unit}" -n 40 --no-pager >&2 || true
    die "${unit} did not become healthy (${url})"
  fi
}

# ── (d) systemd services ──────────────────────────────────────────────────
step_services() {
  confirm_step services "install units; enable and (re)start ansatze-kimi, then agent-monitor" || return 0
  require_rendered
  local kimi_changed=0 name
  backup_once "${UNIT_DIR}/agent-monitor.service" agent-monitor.service.pre-ansatze
  if install_file "${RENDER_SOURCE}/ansatze-kimi.service" "${UNIT_DIR}/ansatze-kimi.service" 0644 root root; then kimi_changed=1; fi
  if install_file "${RENDER_SOURCE}/agent-monitor.service" "${UNIT_DIR}/agent-monitor.service" 0644 root root; then CONSOLE_CHANGED=1; fi
  if ((WITH_NEWS)); then
    for name in ansatze-news.service ansatze-news.timer; do
      install_file "${RENDER_SOURCE}/${name}" "${UNIT_DIR}/${name}" 0644 root root || true
    done
  fi
  if ((WITH_PROBLEMS)); then
    for name in ansatze-problems.service ansatze-problems.timer; do
      install_file "${RENDER_SOURCE}/${name}" "${UNIT_DIR}/${name}" 0644 root root || true
    done
  fi
  run systemctl daemon-reload

  check_port 4610 ansatze-kimi.service
  run systemctl enable ansatze-kimi.service
  if ((kimi_changed)) || ! systemctl is-active --quiet ansatze-kimi.service 2>/dev/null; then
    restart_and_wait ansatze-kimi.service http://127.0.0.1:4610/healthz
  else
    note "ansatze-kimi.service is unchanged and running"
  fi

  check_port 4600 agent-monitor.service
  run systemctl enable agent-monitor.service
  if ((CONSOLE_CHANGED)) || ! systemctl is-active --quiet agent-monitor.service 2>/dev/null; then
    note "restarting agent-monitor stops any proof run in progress"
    restart_and_wait agent-monitor.service http://127.0.0.1:4600/health
  else
    note "agent-monitor.service is unchanged and running"
  fi

  if ((WITH_NEWS)); then
    run systemctl enable --now ansatze-news.timer
    run systemctl start --no-block ansatze-news.service
  fi
  if ((WITH_PROBLEMS)); then
    run systemctl enable --now ansatze-problems.timer
    run systemctl start --no-block ansatze-problems.service
  fi
}

# ── (e) Lean broker ───────────────────────────────────────────────────────
step_lean() {
  confirm_step lean "measure the Lean trees, install the sandboxed Lean broker, restart the console" || return 0
  [[ -d ${LEAN_TOOLCHAIN} ]] || die "Lean toolchain not found: ${LEAN_TOOLCHAIN}"
  [[ -d ${LEAN_MATHLIB} ]] || die "Mathlib project not found: ${LEAN_MATHLIB}"
  local measure=(/usr/bin/python3 "${APP_DIR}/deploy/lean-broker/prepare_release.py"
    --toolchain "${LEAN_TOOLCHAIN}" --mathlib "${LEAN_MATHLIB}" --measure-only)
  local toolchain_sha=${LEAN_TOOLCHAIN_PIN} mathlib_sha=${LEAN_MATHLIB_PIN}
  if ((DRY_RUN)); then
    show runuser -u "${SERVICE_USER}" -- "${measure[@]}"
    note "(dry-run) not measuring the ~5 GB trees; a real run compares them with the pins"
  else
    note "measuring the Lean toolchain and Mathlib trees (about a minute)..."
    local measured
    measured=$(as_user "${measure[@]}") || die "prepare_release.py --measure-only failed"
    local got_toolchain got_mathlib
    got_toolchain=$(printf '%s' "${measured}" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["toolchain_tree_sha256"])')
    got_mathlib=$(printf '%s' "${measured}" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["mathlib_tree_sha256"])')
    [[ ${got_toolchain} =~ ^[0-9a-f]{64}$ && ${got_mathlib} =~ ^[0-9a-f]{64}$ ]] || die "unexpected measurement output"
    if [[ ${got_toolchain} != "${LEAN_TOOLCHAIN_PIN}" || ${got_mathlib} != "${LEAN_MATHLIB_PIN}" ]]; then
      note "pinned   toolchain ${LEAN_TOOLCHAIN_PIN}"
      note "measured toolchain ${got_toolchain}"
      note "pinned   mathlib   ${LEAN_MATHLIB_PIN}"
      note "measured mathlib   ${got_mathlib}"
      ((ACCEPT_MEASURED)) || die "the Lean trees changed since they were pinned; review them, then re-run with --accept-measured"
      warn "accepting the measured hashes (--accept-measured)"
      toolchain_sha=${got_toolchain}
      mathlib_sha=${got_mathlib}
    else
      note "Lean trees match the reviewed pins"
    fi
  fi
  # The broker installer is tracked without the executable bit, so run it
  # through bash rather than relying on the checkout's file mode.
  run bash "${APP_DIR}/deploy/lean-broker/install.sh" \
    --toolchain "${LEAN_TOOLCHAIN}" --mathlib "${LEAN_MATHLIB}" \
    --expected-toolchain-tree-sha256 "${toolchain_sha}" \
    --expected-mathlib-tree-sha256 "${mathlib_sha}" --activate
  # The broker's drop-in (SupplementaryGroups, NoNewPrivileges) applies only
  # after a restart of the console.
  run systemctl daemon-reload
  restart_and_wait agent-monitor.service http://127.0.0.1:4600/health
  if ((!DRY_RUN)); then
    [[ -S /run/agent-monitor-lean/core.sock && -S /run/agent-monitor-lean/mathlib.sock ]] \
      || die "the Lean broker sockets are missing under /run/agent-monitor-lean"
    note "Lean broker sockets are listening"
  fi
}

# ── (f) documentation site ────────────────────────────────────────────────
step_docs() {
  confirm_step docs "build the documentation site as ${SERVICE_USER} (npm ci && npm run docs:build)" || return 0
  [[ -x ${NODE_BIN}/npm ]] || die "npm not found at ${NODE_BIN}"
  run_as_user env -C "${APP_DIR}" HOME="/home/${SERVICE_USER}" PATH="${USER_PATH}" npm ci
  run_as_user env -C "${APP_DIR}" HOME="/home/${SERVICE_USER}" PATH="${USER_PATH}" npm run docs:build
  if ((!DRY_RUN)); then
    [[ -f ${DOCS_DIST}/index.html && -f ${DOCS_DIST}/404.html ]] || die "docs build produced no index.html/404.html"
  fi
}

# ── (g) Caddy ─────────────────────────────────────────────────────────────
step_caddy() {
  confirm_step caddy "validate and install ${CADDYFILE}, then reload caddy" || return 0
  require_rendered
  if ((!DRY_RUN)); then
    command -v caddy >/dev/null || die "caddy is not installed (run the 'packages' step)"
    [[ -f ${DOCS_DIST}/index.html ]] || die "build the docs first (step 'docs'); ${DOCS_DIST} is empty"
    if id -u caddy >/dev/null 2>&1; then
      runuser -u caddy -- test -r "${DOCS_DIST}/index.html" \
        || die "user caddy cannot read ${DOCS_DIST}; the path must be world-traversable"
    fi
  fi
  # Keep validation's scratch storage out of root's home directory.
  run env XDG_DATA_HOME="${WORK_DIR}/caddy-data" XDG_CONFIG_HOME="${WORK_DIR}/caddy-config" \
    caddy validate --config "${RENDER_SOURCE}/Caddyfile" --adapter caddyfile
  backup_once "${CADDYFILE}" Caddyfile.pre-ansatze
  local previous=${BACKUP_DIR}/Caddyfile.pre-ansatze
  if [[ -e ${CADDYFILE} ]] && ! cmp -s "${RENDER_SOURCE}/Caddyfile" "${CADDYFILE}"; then
    previous=${BACKUP_DIR}/Caddyfile.$(date -u +%Y%m%dT%H%M%SZ)
    run cp -a -- "${CADDYFILE}" "${previous}"
  fi
  # `caddy reload` posts the new config to the admin address that config
  # names.  While a running Caddy still serves its admin API on the old
  # TCP localhost:2019, moving it to the unix socket needs a restart.
  local apply=reload
  if [[ -e ${CADDYFILE} ]] && ! grep -Eq '^[[:space:]]*admin[[:space:]]+unix//' "${CADDYFILE}" \
    && grep -Eq '^[[:space:]]*admin[[:space:]]+unix//' "${RENDER_SOURCE}/Caddyfile"; then
    apply=restart
    note "moving Caddy's admin API to its unix socket: caddy restarts once (a brief edge blip)"
  fi
  if install_file "${RENDER_SOURCE}/Caddyfile" "${CADDYFILE}" 0644 root root || ! systemctl is-active --quiet caddy 2>/dev/null; then
    run systemctl enable caddy.service
    if systemctl is-active --quiet caddy 2>/dev/null; then
      if ! run systemctl "${apply}" caddy.service; then
        warn "caddy rejected the new configuration; restoring ${previous}"
        run install -m 0644 -o root -g root -- "${previous}" "${CADDYFILE}" || true
        run systemctl "${apply}" caddy.service || true
        die "caddy ${apply} failed; see journalctl -u caddy"
      fi
    else
      run systemctl start caddy.service
    fi
  fi
  note "Caddy obtains the ansatze.ai certificate on first use.  That needs the"
  note "DNS A record pointing here and ports 80/443 open in the security group;"
  note "follow progress with: journalctl -u caddy -f"
}

# ── (h) owner admin account ───────────────────────────────────────────────
step_admin() {
  confirm_step admin "create the owner's admin account (self-registration stays closed)" || return 0
  local cli=(env AGENT_MONITOR_DATA_DIR="${CONSOLE_DATA}" AGENT_MONITOR_ENV_PATH="${CONSOLE_ENV}"
    PYTHONPATH="${APP_DIR}" "${VENV_PYTHON}" -m agent_monitor.cli create-admin)
  if [[ -n ${ADMIN_EMAIL} ]]; then cli+=(--email "${ADMIN_EMAIL}"); fi
  if ((DRY_RUN)); then
    show runuser -u "${SERVICE_USER}" -- "${cli[@]}"
    show systemctl restart agent-monitor.service
    return 0
  fi
  if [[ ! -t 0 ]]; then
    warn "no terminal for the password prompt; create the admin later with:"
    note "sudo -u ${SERVICE_USER} ${cli[*]}"
    return 0
  fi
  local status=0
  as_user env -C "${APP_DIR}" "${cli[@]}" || status=$?
  case ${status} in
    0) restart_and_wait agent-monitor.service http://127.0.0.1:4600/health ;;
    3) note "an administrator already exists; nothing to do" ;;
    *) die "create-admin failed (exit ${status})" ;;
  esac
}

# ── (i) smoke checks ──────────────────────────────────────────────────────
step_smoke() {
  confirm_step smoke "check health, /sign-in, guest sessions and one tiny Kimi call" || return 0
  local args=(--mount-file "${MOUNT_FILE}")
  if ((GUEST_RUN)); then args+=(--guest-run); fi
  if grep -q "^www\.${DOMAIN//./\\.} {" "${CADDYFILE}" 2>/dev/null; then args+=(--www); fi
  if ((DRY_RUN)); then
    show runuser -u "${SERVICE_USER}" -- "${SCRIPT_DIR}/smoke.sh" "${args[@]}"
    return 0
  fi
  as_user "${SCRIPT_DIR}/smoke.sh" "${args[@]}" || warn "some smoke checks failed (see above)"
}

uninstall() {
  printf '\n%s== uninstall:%s stop and remove the ansatze units; restore the previous Caddyfile\n' "${C_HEAD}" "${C_OFF}"
  note "keeps ${STATE_ROOT}, ${ETC_DIR} (credentials) and the Lean broker"
  if ((!DRY_RUN && !ASSUME_YES)); then
    local answer
    read -r -p "   Continue? [y/N] " answer || answer=n
    [[ ${answer} == y || ${answer} == Y ]] || die "cancelled"
  fi
  local unit
  for unit in ansatze-problems.timer ansatze-problems.service ansatze-news.timer ansatze-news.service \
      agent-monitor.service ansatze-kimi.service; do
    if [[ -e ${UNIT_DIR}/${unit} ]]; then
      run systemctl disable --now "${unit}" || true
      run rm -f -- "${UNIT_DIR}/${unit}"
    fi
  done
  if [[ -e ${BACKUP_DIR}/agent-monitor.service.pre-ansatze ]]; then
    note "the pre-ansatze agent-monitor.service is saved at ${BACKUP_DIR}/agent-monitor.service.pre-ansatze"
  fi
  run systemctl daemon-reload
  if [[ -e ${BACKUP_DIR}/Caddyfile.pre-ansatze ]]; then
    run install -m 0644 -o root -g root -- "${BACKUP_DIR}/Caddyfile.pre-ansatze" "${CADDYFILE}"
    if systemctl is-active --quiet caddy 2>/dev/null; then run systemctl reload caddy.service || true; fi
  elif [[ -e ${CADDYFILE} ]]; then
    warn "no pre-install Caddyfile backup; ${CADDYFILE} still serves ansatze.ai (stop caddy to take the site down)"
  fi
  note "the Lean broker is separate: systemctl disable --now agent-monitor-lean-core.socket agent-monitor-lean-mathlib.socket"
  if ((PURGE)); then
    printf '\n%s== purge:%s delete %s, %s, %s, %s and the service users\n' "${C_ERR}" "${C_OFF}" "${STATE_ROOT}" "${ETC_DIR}" "${NEWS_STATE}" "${PROBLEMS_STATE}"
    note "this destroys every account, run, saved key and the data key"
    if ((!DRY_RUN)); then
      local answer
      read -r -p "   Type PURGE to continue: " answer || answer=
      [[ ${answer} == PURGE ]] || die "purge cancelled"
    fi
    run rm -rf --one-file-system -- "${STATE_ROOT}" "${ETC_DIR}" "${NEWS_STATE}" "${PROBLEMS_STATE}"
    for unit in "${KIMI_USER}" "${NEWS_USER}" "${PROBLEMS_USER}"; do
      if id -u "${unit}" >/dev/null 2>&1; then run userdel "${unit}"; fi
    done
  fi
}

summary() {
  printf '\n%sDone.%s\n' "${C_OK}" "${C_OFF}"
  [[ -n ${MOUNT} ]] || { [[ -e ${MOUNT_FILE} ]] && MOUNT=$(<"${MOUNT_FILE}"); } || true
  note "Site:            https://${DOMAIN}/"
  note "Console entry:   https://${DOMAIN}/sign-in  (-> ${MOUNT:-<mount>}/login)"
  note "Health:          curl http://127.0.0.1:4600/health; curl http://127.0.0.1:4610/healthz"
  note "Smoke checks:    ${SCRIPT_DIR}/smoke.sh"
  note "Next: sign in as the admin, open Settings, connect Codex / Claude Code."
  note "Rotate the Kimi key (it was pasted in chat) and set a provider spend cap:"
  note "  see 'Rotate the Kimi key' in ${SCRIPT_DIR}/README.md"
}

# Render only: no root, no host changes (review aid; also used by tests).
render_only() {
  WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ansatze-render.XXXXXX")
  trap 'rm -rf -- "${WORK_DIR}"' EXIT
  if [[ -z ${MOUNT} && -r ${MOUNT_FILE} ]]; then MOUNT=$(<"${MOUNT_FILE}"); fi
  if [[ -z ${MOUNT} ]]; then
    MOUNT=/proof-$(od -An -N20 -tx1 /dev/urandom | tr -d ' \n')
    note "no mount given; rendering with the random example ${MOUNT}"
  fi
  [[ ${MOUNT} =~ ^/proof-[0-9a-f]{40}$ ]] || die "invalid console mount"
  WWW_ENABLED=0
  if www_enabled; then WWW_ENABLED=1; fi
  mkdir -p -- "${RENDER_TO}"
  local name
  for name in Caddyfile agent-monitor.service ansatze-kimi.service ansatze-news.service ansatze-news.timer \
      ansatze-problems.service ansatze-problems.timer; do
    render_template "${SCRIPT_DIR}/${name}" "${RENDER_TO}/${name}"
  done
  note "rendered into ${RENDER_TO}"
}

main() {
  parse_args "$@"
  if [[ -n ${RENDER_TO} ]]; then
    render_only
    return 0
  fi
  preflight "$@"
  if ((UNINSTALL)); then
    uninstall
    return 0
  fi
  local step
  for step in "${ALL_STEPS[@]}"; do
    if step_wanted "${step}"; then "step_${step}"; fi
  done
  summary
}

main "$@"
