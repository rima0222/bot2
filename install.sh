#!/usr/bin/env bash
# =============================================================================
# Trading Bot installer (Ubuntu 20.04+ / Debian 11+). Safe to re-run: it upgrades
# the code and keeps your data, settings and admin login.
#
#   Run from the unpacked project:      sudo bash install.sh
#   Port / public panel:                sudo bash install.sh --port 9100 --public
#   One line, fetching the archive:
#     for i in $(seq 8); do curl -fL -C - -o install.sh https://YOUR.HOST/install.sh && break || sleep 3; done; \
#       sudo BOT_ARCHIVE_URL=https://YOUR.HOST/trading-bot.zip bash install.sh
#
# What it does NOT do (by design, so it cannot disturb Xray / V2Ray / WireGuard / Nginx / SSH):
#   * no firewall, iptables, nftables, routing, DNS, sysctl or proxy changes
#   * no changes to other services or their ports
#   * no listening on anything except the one panel port (127.0.0.1 unless --public)
# It only installs python packages, creates a "tradingbot" system user, a private
# virtualenv in /opt/trading-bot, and one resource-capped systemd service.
#
# Every download is resumable: apt resumes partial .debs, files are fetched with
# `curl -C -` (wget -c fallback) in a retry loop, and Python wheels are downloaded
# individually the same way and then installed offline from a local wheelhouse.
# =============================================================================
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/trading-bot}"
SERVICE_NAME="${SERVICE_NAME:-trading-bot}"
SERVICE_USER="${SERVICE_USER:-tradingbot}"
PANEL_PORT="${PANEL_PORT:-8888}"
PANEL_HOST="${PANEL_HOST:-127.0.0.1}"
MIN_PY_MINOR=11
SKIP_APT="${SKIP_APT:-0}"          # 1 = do not touch apt (python3.11+, venv, curl already present)
SKIP_SYSTEMD="${SKIP_SYSTEMD:-0}"  # 1 = install files only (containers / testing)
BOT_ARCHIVE_URL="${BOT_ARCHIVE_URL:-}"
UNINSTALL=0
PURGE=0

if [[ -t 1 ]]; then C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_B=$'\033[1m'; C_0=$'\033[0m'; else C_G=; C_Y=; C_R=; C_B=; C_0=; fi
log()  { printf '%s==>%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s!!%s  %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%sxx%s  %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }
trap 'rc=$?; [[ $BASHPID -eq $$ ]] && die "failed at line $LINENO (exit $rc). Fix the cause and run the script again; finished steps are skipped or resumed."' ERR

SELF="${BASH_SOURCE[0]:-}"   # empty when the script is piped into bash (curl ... | bash)
usage() { if [[ -n "$SELF" && -f "$SELF" ]]; then sed -n "2,24p" "$SELF" | sed 's/^# \{0,1\}//'; else echo "install.sh [--port N] [--host H] [--public] [--uninstall [--purge]]  (see README)"; fi; }

# ----------------------------------------------------------------------------- helpers
retry() {  # retry <times> <cmd...>
  local n=$1 i=1; shift
  until "$@"; do
    (( i >= n )) && return 1
    warn "command failed (attempt $i/$n): $* - retrying"; sleep $(( i < 6 ? i * 3 : 15 )); i=$(( i + 1 ))
  done
}

# fetch <url> <dest> [tries]  - resumable download; keeps the partial file between attempts.
fetch() {
  local url=$1 dest=$2 tries=${3:-40} i rc code
  mkdir -p "$(dirname "$dest")"
  for (( i = 1; i <= tries; i++ )); do
    if command -v curl >/dev/null 2>&1; then
      rc=0
      # No --retry here on purpose: curl's own retry restarts the file from byte 0, while this
      # loop calls curl again with `-C -`, which continues from the bytes already on disk.
      code=$(curl -fL -C - --connect-timeout 20 --speed-time 30 --speed-limit 1024 \
                  -sS -w '%{http_code}' -o "$dest" "$url") || rc=$?
      [[ $rc -eq 0 ]] && return 0
      if [[ "$code" == "416" ]]; then return 0; fi                    # we already had the whole file
      if [[ $rc -eq 33 || $rc -eq 36 ]]; then rm -f "$dest"; fi        # server cannot resume: start clean
    elif command -v wget >/dev/null 2>&1; then
      wget -c -t 5 -T 30 --waitretry=3 -O "$dest" "$url" && return 0
    else
      die "need curl or wget"
    fi
    warn "download interrupted (attempt $i/$tries) - resuming from $(stat -c %s "$dest" 2>/dev/null || echo 0) bytes"
    sleep $(( i < 10 ? i * 2 : 20 ))
  done
  return 1
}

need_root() { [[ $EUID -eq 0 ]] || die "run as root:  sudo bash install.sh"; }

py_ok() {  # py_ok <python-binary>: is it >= 3.11 and has venv+ensurepip?
  command -v "$1" >/dev/null 2>&1 || return 1
  "$1" - <<PY >/dev/null 2>&1
import sys, venv, ensurepip
sys.exit(0 if sys.version_info >= (3, $MIN_PY_MINOR) else 1)
PY
}

find_python() {
  local c
  for c in "${PYTHON_BIN:-}" python3.13 python3.12 python3.11 python3; do
    [[ -n "$c" ]] && py_ok "$c" && { command -v "$c"; return 0; }
  done
  return 1
}

apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y --no-install-recommends -o Acquire::Retries=10 -o Acquire::http::Timeout=30 \
    -o Acquire::https::Timeout=30 "$@"
}

ensure_system_packages() {
  if [[ "$SKIP_APT" == 1 ]]; then log "SKIP_APT=1: not touching apt"; return; fi
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found - this installer targets Ubuntu/Debian. Install python3.11+, python3-venv, curl, then use SKIP_APT=1."
  log "Updating package lists (resumable, 10 retries)"
  retry 5 apt-get update -o Acquire::Retries=10 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30
  apt_install ca-certificates curl
  if ! find_python >/dev/null; then
    log "Python 3.$MIN_PY_MINOR+ not found - installing it"
    local pkg
    for pkg in python3.12 python3.11; do
      if apt-cache show "$pkg" >/dev/null 2>&1; then apt_install "$pkg" "$pkg-venv" && break; fi
    done
    if ! find_python >/dev/null && [[ -r /etc/os-release ]] && grep -qi ubuntu /etc/os-release; then
      warn "Adding the deadsnakes PPA for Python 3.11 (this only adds an apt source; nothing else changes)"
      apt_install software-properties-common
      retry 3 add-apt-repository -y ppa:deadsnakes/ppa
      retry 5 apt-get update
      apt_install python3.11 python3.11-venv
    fi
  fi
  find_python >/dev/null || apt_install python3-venv python3-pip || true
}

# ----------------------------------------------------------------------------- source / files
locate_source() {
  if [[ -n "$SELF" ]]; then
    local here; here="$(cd "$(dirname "$SELF")" && pwd)"
    if [[ -f "$here/main.py" && -f "$here/requirements.txt" ]]; then SRC="$here"; return; fi
  fi
  [[ -n "$BOT_ARCHIVE_URL" ]] || die "project files not found next to install.sh. Unpack the whole project first, or set BOT_ARCHIVE_URL=https://.../trading-bot.zip"
  local tmp; tmp="$(mktemp -d /tmp/bot-src.XXXXXX)"
  local arch="$tmp/archive"; log "Downloading $BOT_ARCHIVE_URL (resumable)"
  fetch "$BOT_ARCHIVE_URL" "$arch" || die "could not download the archive"
  mkdir -p "$tmp/x"
  case "$BOT_ARCHIVE_URL" in
    *.zip) python3 -m zipfile -e "$arch" "$tmp/x" ;;
    *)     tar -xf "$arch" -C "$tmp/x" ;;
  esac
  SRC="$(dirname "$(find "$tmp/x" -maxdepth 3 -name main.py -print -quit)")"
  [[ -f "$SRC/main.py" ]] || die "archive does not contain main.py"
}

install_files() {
  log "Installing code into $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
  if [[ "$(cd "$SRC" && pwd)" != "$(cd "$INSTALL_DIR" && pwd)" ]]; then
    (cd "$SRC" && tar --exclude='./data' --exclude='./logs' --exclude='./venv' --exclude='./.env' \
        --exclude='./.git' --exclude='./.wheelhouse' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='.pytest_cache' --exclude='*.zip' -cf - .) | tar -xf - -C "$INSTALL_DIR"
  fi
}

ensure_user() {
  if [[ "$SERVICE_USER" == "root" ]]; then warn "running the service as root is not recommended"; return; fi
  id -u "$SERVICE_USER" >/dev/null 2>&1 || {
    log "Creating system user $SERVICE_USER"
    useradd --system --home-dir "$INSTALL_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  }
}

# ----------------------------------------------------------------------------- python env
setup_venv() {
  PY="$(find_python)" || die "no usable Python 3.$MIN_PY_MINOR+ with venv support. Re-run without SKIP_APT."
  log "Using $($PY --version) at $PY"
  VENV="$INSTALL_DIR/venv"
  if [[ -x "$VENV/bin/python" ]] && ! "$VENV/bin/python" -c "import sys; sys.exit(0 if sys.version_info >= (3,$MIN_PY_MINOR) else 1)" 2>/dev/null; then
    rm -rf "$VENV"
  fi
  [[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
  export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=60
  retry 5 "$VENV/bin/pip" install --retries 10 --timeout 60 -q --upgrade pip wheel
  install_python_deps
}

# Resumable pip: ask pip which files it would download, fetch each with `curl -C -`,
# verify the sha256, then install offline from the local wheelhouse.
install_python_deps() {
  local pip="$VENV/bin/pip" wh="$INSTALL_DIR/.wheelhouse" report list url sha name
  report="$wh/report.json"; list="$wh/list.tsv"
  mkdir -p "$wh"
  log "Resolving Python packages"
  if retry 4 "$pip" install --dry-run --ignore-installed -q --report "$report" --retries 10 --timeout 60 -r "$INSTALL_DIR/requirements.txt"; then
    "$VENV/bin/python" - "$report" > "$list" <<'PY'
import json, sys, urllib.parse
for it in json.load(open(sys.argv[1]))["install"]:
    d = it["download_info"]
    if not d["url"].startswith("http"):
        continue
    sha = d.get("archive_info", {}).get("hashes", {}).get("sha256", "")
    print(d["url"], sha, urllib.parse.unquote(urllib.parse.urlsplit(d["url"]).path.rsplit("/", 1)[1]), sep="\t")
PY
    local total n=0; total=$(wc -l < "$list")
    while IFS=$'\t' read -r url sha name; do
      n=$(( n + 1 ))
      if [[ -f "$wh/$name" && -n "$sha" ]] && [[ "$(sha256sum "$wh/$name" | cut -d' ' -f1)" == "$sha" ]]; then continue; fi
      log "[$n/$total] $name"
      fetch "$url" "$wh/$name" 40 || die "could not download $name"
      if [[ -n "$sha" && "$(sha256sum "$wh/$name" | cut -d' ' -f1)" != "$sha" ]]; then
        rm -f "$wh/$name"; die "checksum mismatch for $name - run the installer again"
      fi
    done < "$list"
    if "$pip" install --no-index --find-links "$wh" -q -r "$INSTALL_DIR/requirements.txt"; then rm -rf "$wh"; return; fi
    warn "offline install failed; falling back to a normal pip install"
  else
    warn "pip could not produce a download report (old pip?); using a normal pip install with retries"
  fi
  retry 6 "$pip" install --retries 10 --timeout 60 -r "$INSTALL_DIR/requirements.txt"
  rm -rf "$wh"
}

# ----------------------------------------------------------------------------- config & service
port_in_use() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q . ; }

pick_port() {
  if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then return; fi  # re-install: our own service owns it
  local p=$PANEL_PORT
  while port_in_use "$p"; do warn "port $p is already used by another program - trying $(( p + 1 ))"; p=$(( p + 1 )); done
  PANEL_PORT=$p
}

init_app() {
  log "Preparing configuration and admin login"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
  [[ -f "$INSTALL_DIR/.env" ]] || install -m 600 -o "$SERVICE_USER" -g "$SERVICE_USER" /dev/null "$INSTALL_DIR/.env"
  local args=(--init --host "$PANEL_HOST")
  # keep an existing port on re-install unless one was requested explicitly
  if [[ -n "${PORT_EXPLICIT:-}" ]] || ! grep -q '^BOT_PORT=' "$INSTALL_DIR/.env"; then args+=(--port "$PANEL_PORT"); else PANEL_PORT=$(grep '^BOT_PORT=' "$INSTALL_DIR/.env" | cut -d= -f2); fi
  INIT_JSON=$(cd "$INSTALL_DIR" && runuser -u "$SERVICE_USER" -- env BOT_ENV_FILE="$INSTALL_DIR/.env" BOT_DATA_DIR="$INSTALL_DIR/data" BOT_LOG_DIR="$INSTALL_DIR/logs" "$VENV/bin/python" main.py "${args[@]}")
  ADMIN_PASS=$(printf '%s' "$INIT_JSON" | "$VENV/bin/python" -c 'import json,sys; print(json.load(sys.stdin).get("password") or "")')
  "$VENV/bin/python" -m compileall -q "$INSTALL_DIR" >/dev/null 2>&1 || true
  chmod 600 "$INSTALL_DIR/.env"
}

write_unit() {
  local ram_mb high max soft hard
  ram_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
  if   (( ram_mb <= 640 ));  then high=280; max=350
  elif (( ram_mb <= 1100 )); then high=340; max=420
  else                            high=520; max=640; fi
  soft=$(( high - 30 )); hard=$(( max - 30 ))
  log "Writing /etc/systemd/system/$SERVICE_NAME.service (RAM ${ram_mb} MB: soft cap ${high}M, hard cap ${max}M, CPU 50%)"
  cat > "/etc/systemd/system/$SERVICE_NAME.service" <<UNIT
[Unit]
Description=Trading Bot (isolated, resource-capped)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=BOT_ENV_FILE=$INSTALL_DIR/.env BOT_DATA_DIR=$INSTALL_DIR/data BOT_LOG_DIR=$INSTALL_DIR/logs
Environment=BOT_MEM_SOFT_MB=$soft BOT_MEM_HARD_MB=$hard PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/python $INSTALL_DIR/main.py
Restart=always
RestartSec=5
TimeoutStopSec=20

# --- never starve the other services on this VPS ---
Nice=10
CPUQuota=50%
CPUWeight=20
IOWeight=20
IOSchedulingClass=best-effort
IOSchedulingPriority=7
MemoryHigh=${high}M
MemoryMax=${max}M
TasksMax=96
LimitNOFILE=2048
# if the machine runs out of memory the kernel should sacrifice this bot before Xray / Nginx / sshd
OOMScoreAdjust=500

# --- sandbox: applies to this service only, no system-wide effect ---
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$INSTALL_DIR/data $INSTALL_DIR/logs
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
LockPersonality=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
UNIT
  chmod 644 "/etc/systemd/system/$SERVICE_NAME.service"
}

start_service() {
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
  systemctl restart "$SERVICE_NAME"
  log "Waiting for the panel on port $PANEL_PORT"
  local i
  for i in $(seq 1 45); do
    if curl -fsS -m 3 "http://127.0.0.1:$PANEL_PORT/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  warn "the panel did not answer within 45 s. Last log lines:"
  journalctl -u "$SERVICE_NAME" -n 25 --no-pager >&2 || true
  return 1
}

server_ip() { curl -fsS -m 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "YOUR_SERVER_IP"; }

summary() {
  local ip; ip="$(server_ip)"
  echo
  printf '%s%s%s\n' "$C_B" "Trading Bot is installed and running (paper trading mode)." "$C_0"
  echo "-------------------------------------------------------------------"
  if [[ "$PANEL_HOST" == "127.0.0.1" || "$PANEL_HOST" == "localhost" ]]; then
    echo "  Panel (private):   http://127.0.0.1:$PANEL_PORT"
    echo "  From your PC:      ssh -L $PANEL_PORT:127.0.0.1:$PANEL_PORT root@$ip     then open http://127.0.0.1:$PANEL_PORT"
    echo "  Make it public:    sudo bash install.sh --public --port $PANEL_PORT   (then open that port yourself; see README)"
  else
    echo "  Panel URL:         http://$ip:$PANEL_PORT"
    echo "  This script did not open the port. The panel speaks plain HTTP: put it behind HTTPS (Nginx/Caddy) or a VPN."
  fi
  if [[ -n "$ADMIN_PASS" ]]; then
    echo "  Username:          admin"
    echo "  Password:          $ADMIN_PASS      <- shown once, save it now"
  else
    echo "  Login:             unchanged (forgot it?  sudo -u $SERVICE_USER $VENV/bin/python $INSTALL_DIR/main.py --reset-password)"
  fi
  echo "-------------------------------------------------------------------"
  echo "  Logs:    journalctl -u $SERVICE_NAME -f      Status: systemctl status $SERVICE_NAME"
  echo "  Stop:    systemctl stop $SERVICE_NAME        Remove: sudo bash install.sh --uninstall"
  echo "  The bot starts in PAPER mode. Nothing trades real money until you switch it in Settings."
}

do_uninstall() {
  need_root
  systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE_NAME.service"; systemctl daemon-reload 2>/dev/null || true
  if (( PURGE )); then rm -rf "$INSTALL_DIR"; log "Removed $INSTALL_DIR including data"
  else
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name data ! -name .env ! -name logs -exec rm -rf {} +
    log "Service and code removed. Your data and .env are kept in $INSTALL_DIR (use --purge to delete them)"
  fi
}

main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port)      PANEL_PORT="${2:?port}"; PORT_EXPLICIT=1; shift 2 ;;
      --host)      PANEL_HOST="${2:?host}"; shift 2 ;;
      --public)    PANEL_HOST="0.0.0.0"; shift ;;
      --uninstall) UNINSTALL=1; shift ;;
      --purge)     PURGE=1; shift ;;
      -h|--help)   usage; exit 0 ;;
      *) die "unknown option: $1 (try --help)" ;;
    esac
  done
  [[ "$PANEL_PORT" =~ ^[0-9]+$ ]] && (( PANEL_PORT >= 1024 && PANEL_PORT <= 65535 )) || die "port must be 1024-65535"
  (( UNINSTALL )) && { do_uninstall; exit 0; }
  need_root
  ensure_system_packages
  locate_source
  ensure_user
  install_files
  setup_venv
  pick_port
  init_app
  if [[ "$SKIP_SYSTEMD" == 1 ]]; then
    log "SKIP_SYSTEMD=1: files installed. Start manually:  cd $INSTALL_DIR && sudo -u $SERVICE_USER $VENV/bin/python main.py"
    summary; exit 0
  fi
  write_unit
  start_service || die "service failed to start - see the log lines above"
  summary
}

if [[ -z "$SELF" || "$SELF" == "$0" ]]; then main "$@"; fi
