#!/usr/bin/env bash
# Deploy the dashboard on the Raspberry Pi from the unimelb GitLab repo.
#
# Run ON the Pi from the dashboard folder (or pass REPO=/path):
#   ./scripts/deploy-pi.sh                       # pull main, update deps, restart services
#   ./scripts/deploy-pi.sh --token-stdin < tok   # first time: save a GitLab token for pulls
#   ./scripts/deploy-pi.sh --tunnel-token-stdin  # first time: paste the smart-garden tunnel token
#   OLD_SERVICE=name ./scripts/deploy-pi.sh      # also stop + disable an older unit
#
# First run on a folder that was copied over by hand (not a git clone): the
# folder is backed up to ../<name>-backup-<date>.tar.gz (without data/), turned
# into a clone of GitLab main, and data/ (readings, camera, settings) is kept.
#
# Installs / restarts the systemd services:
#   smart-garden-ingest     MQTT -> SQLite/CSV, the only DB writer (scripts/mqtt_to_sqlite.py)
#   smart-garden-dashboard  public Streamlit app on :8501 (src/server.py), no controls
#   smart-garden-admin      the full app on :8502 behind a password (scripts/set_admin_password.py)
#   smart-garden-notifier   always-on alert push (scripts/alert_notifier.py)
#   smart-garden-camera     Pi camera capture loop (src/camera_capture.py)
#   smart-garden-timelapse  keeps the timelapse video up to date hourly (scripts/build_timelapse.py)
#   smart-garden-detector   pre-trained net-pot / plant detector (scripts/locate_plants.py),
#                           only on a Pi with 3.5 GB+ RAM (installs requirements-ai.txt)
#   smart-garden-tunnel     Cloudflare named tunnel "smart-garden" (smartgarden.tech, admin.),
#                           once cloudflared is installed and the tunnel token is saved
#
# Ends by checking every service is active, both dashboards answer and the tunnel
# has connected to Cloudflare; exits non-zero otherwise. It also tries both
# public addresses over HTTPS and warns if they do not answer.
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
REMOTE="https://gitlab.unimelb.edu.au/smart-garden-capstone/smart-garden-iot-dashboard.git"
BRANCH="${BRANCH:-main}"
RUN_USER="${RUN_USER:-$(id -un)}"
UNITS=(smart-garden-ingest smart-garden-dashboard smart-garden-admin smart-garden-notifier
       smart-garden-camera smart-garden-timelapse smart-garden-serial-bridge)
CLOUDFLARED="$(command -v cloudflared || true)"
TUNNEL_TOKEN_FILE="${TUNNEL_TOKEN_FILE:-$HOME/.cloudflared/smart-garden.token}"
PUBLIC_HOSTS=(smartgarden.tech admin.smartgarden.tech)
# With SUDO_ASKPASS set (non-interactive deploys over ssh), sudo asks it for the password.
SUDO=(sudo)
[[ -n "${SUDO_ASKPASS:-}" ]] && SUDO=(sudo -A)

cd "$REPO"
echo "[deploy] repo $REPO, user $RUN_USER, branch $BRANCH"

if [[ "${1:-}" == "--tunnel-token-stdin" ]]; then
  # Zero Trust -> Networks -> Tunnels -> smart-garden -> Configure: the long
  # string after --token in the install command (pasting the whole command works too).
  [[ -t 0 ]] && printf 'smart-garden tunnel token (not shown): '
  read -rs tunnel_token || true
  echo
  read -ra words <<< "$tunnel_token"
  tunnel_token="${words[*]: -1}"  # the token is the last word of the install command
  [[ -n "$tunnel_token" ]] || { echo "[deploy] no token given" >&2; exit 1; }
  mkdir -p "$(dirname "$TUNNEL_TOKEN_FILE")"
  (umask 077 && printf '%s\n' "$tunnel_token" > "$TUNNEL_TOKEN_FILE")
  echo "[deploy] tunnel token saved to $TUNNEL_TOKEN_FILE"
fi

TUNNEL=0
if [[ -z "$CLOUDFLARED" ]]; then
  echo "[deploy] no cloudflared: skipping the tunnel (install steps in README, Remote access)"
elif systemctl is-active -q cloudflared 2>/dev/null; then
  echo "[deploy] tunnel runs as cloudflared.service (cloudflared service install); leaving it"
elif [[ ! -s "$TUNNEL_TOKEN_FILE" ]]; then
  echo "[deploy] no tunnel token: run ./scripts/deploy-pi.sh --tunnel-token-stdin"
else
  TUNNEL=1
  UNITS+=(smart-garden-tunnel)
fi

if [[ "${1:-}" == "--token-stdin" ]]; then
  read -r token
  # Token lives only in ~/.git-credentials (mode 600), never in the remote URL.
  git config --global credential.https://gitlab.unimelb.edu.au.helper store
  umask 077
  touch ~/.git-credentials
  grep -v '@gitlab.unimelb.edu.au' ~/.git-credentials > ~/.git-credentials.tmp || true
  printf 'https://oauth2:%s@gitlab.unimelb.edu.au\n' "$token" >> ~/.git-credentials.tmp
  mv ~/.git-credentials.tmp ~/.git-credentials
  echo "[deploy] GitLab token saved for future pulls"
fi

if [[ ! -d .git ]]; then
  backup="../$(basename "$REPO")-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  echo "[deploy] not a git clone yet; backing up to $backup"
  tar --exclude=./data --exclude=./.venv -czf "$backup" .
  git init -q
  git remote add origin "$REMOTE"
fi
git remote set-url origin "$REMOTE"
git fetch -q origin "$BRANCH"
# Tracked files follow GitLab exactly; untracked / ignored files (data/, .venv,
# secrets.toml) are left alone.
git checkout -q -B "$BRANCH" "origin/$BRANCH" --force
git reset -q --hard "origin/$BRANCH"
echo "[deploy] at $(git log -1 --format='%h %s')"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data/camera

# The pre-trained detector needs torch (~1 GB with its model): only where it fits.
# If it does not install, everything else still deploys.
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
# PyPI's ARM torch drags in ~1.6 GB of NVIDIA CUDA wheels a Pi cannot use, which
# overflow the RAM-backed /tmp: take the CPU-only build from PyTorch's own index,
# and unpack on the SD card.
if (( MEM_KB >= 3500000 )); then
  mkdir -p "$HOME/.cache/piptmp"
  AI_LOG="$HOME/.cache/piptmp/requirements-ai.log"
  if TMPDIR="$HOME/.cache/piptmp" .venv/bin/pip install -q torch \
       --index-url https://download.pytorch.org/whl/cpu > "$AI_LOG" 2>&1 \
     && TMPDIR="$HOME/.cache/piptmp" .venv/bin/pip install -q -r requirements-ai.txt >> "$AI_LOG" 2>&1; then
    UNITS+=(smart-garden-detector)
  else
    echo "[deploy] WARNING: requirements-ai.txt failed to install; plant detector skipped:"
    tail -5 "$AI_LOG" | sed 's/^/[deploy]   /'
  fi
else
  echo "[deploy] $((MEM_KB / 1024)) MB RAM: too little for the plant detector, skipped"
fi

if ! command -v ffmpeg >/dev/null; then
  echo "[deploy] timelapse video needs: sudo apt install -y ffmpeg"
fi

if ! /usr/bin/python3 -c "import picamera2, cv2" 2>/dev/null; then
  echo "[deploy] camera needs: sudo apt install -y python3-picamera2 python3-opencv"
fi

if [[ -n "${OLD_SERVICE:-}" ]]; then
  echo "[deploy] retiring $OLD_SERVICE"
  "${SUDO[@]}" systemctl disable --now "$OLD_SERVICE" || true
fi

# The old quick tunnel (trycloudflare.com) must not keep running alongside or instead.
if [[ "$TUNNEL" == 0 ]] && [[ -f /etc/systemd/system/smart-garden-tunnel.service ]]; then
  "${SUDO[@]}" systemctl disable --now smart-garden-tunnel 2>/dev/null || true
fi

for unit in "${UNITS[@]}"; do
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@REPO@|$REPO|g" -e "s|@CLOUDFLARED@|$CLOUDFLARED|g" \
    -e "s|@TUNNEL_TOKEN_FILE@|$TUNNEL_TOKEN_FILE|g" \
    "deploy/systemd/$unit.service" \
    | "${SUDO[@]}" tee "/etc/systemd/system/$unit.service" > /dev/null
done
"${SUDO[@]}" systemctl daemon-reload
for unit in "${UNITS[@]}"; do
  "${SUDO[@]}" systemctl enable -q "$unit"
  "${SUDO[@]}" systemctl restart "$unit"
done

# Wait up to 60 s for both dashboards to answer (slow on a Pi just after boot).
code=000
admin_code=000
for _ in $(seq 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8501/_stcore/health || true)
  admin_code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8502/_stcore/health || true)
  [[ "$code" == 200 && "$admin_code" == 200 ]] && break
  sleep 2
done
failed=0
for unit in "${UNITS[@]}"; do
  state="$(systemctl is-active "$unit" || true)"
  printf '[deploy] %-24s %s\n' "$unit" "$state"
  [[ "$state" == active ]] || failed=1
done
echo "[deploy] dashboard health: HTTP $code, admin: HTTP $admin_code"
[[ "$code" == 200 && "$admin_code" == 200 ]] || failed=1
if ! grep -q '^admin_password_hash' "$REPO/.streamlit/secrets.toml" 2>/dev/null; then
  echo "[deploy] no admin password yet: .venv/bin/python scripts/set_admin_password.py"
fi

if [[ "$TUNNEL" == 1 ]]; then
  # Read only this run's log: cloudflared logs each edge connection it registers.
  run_id=$(systemctl show -p InvocationID --value smart-garden-tunnel)
  connected=0
  for _ in $(seq 30); do
    if "${SUDO[@]}" journalctl "_SYSTEMD_INVOCATION_ID=$run_id" --no-pager -o cat 2>/dev/null \
        | grep -q 'Registered tunnel connection'; then
      connected=1
      break
    fi
    sleep 2
  done
  if [[ "$connected" == 1 ]]; then
    echo "[deploy] tunnel smart-garden: connected to Cloudflare"
  else
    echo "[deploy] tunnel smart-garden: not connected (journalctl -u smart-garden-tunnel)"
    failed=1
  fi
fi
if [[ -n "$CLOUDFLARED" ]]; then
  # From outside: needs the hostnames on the tunnel and an active edge certificate.
  for host in "${PUBLIC_HOSTS[@]}"; do
    https_code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$host/_stcore/health" || true)
    if [[ "$https_code" == 200 ]]; then
      echo "[deploy] https://$host: OK"
    else
      echo "[deploy] warning: https://$host answered HTTP $https_code (000: no TLS or no" \
           "connection; check the tunnel's public hostnames and SSL/TLS -> Edge Certificates)"
    fi
  done
fi
exit "$failed"
