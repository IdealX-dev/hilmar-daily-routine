#!/usr/bin/env bash
# setup-vm.sh — one-shot provisioning for the C3 Azure VM (Ubuntu 22.04 LTS or 24.04).
# Run as a sudoer: `sudo bash setup-vm.sh`
#
# What this does:
#   1. Installs the system-default Python 3 + system libs WeasyPrint needs
#      for PDF rendering. pyproject requires-python>=3.10, so noble's 3.12
#      and jammy's 3.11 (or 3.10 via deadsnakes-free) both work. The Python
#      version on the VM is whatever `apt install python3` resolves to.
#      WeasyPrint is in pyproject as a deploy-time dep; render.py uses
#      reportlab today but the GTK libs make WeasyPrint importable for any
#      future code path that wants HTML→PDF.
#   2. Creates a `hilmar` service user with /opt/hilmar-tracker as $HOME
#   3. Clones the repo (idempotent — git pull on rerun)
#   4. Creates a venv, installs the package
#   5. Installs systemd service + timer (runs `hilmar-run` weekdays 07:00 ET)
#   6. Sets up /var/log/hilmar-tracker + logrotate
#
# Pre-requisites BEFORE running this script:
#   * SSH read access to github.com:IdealX-dev/hilmar-tracker — either an
#     ed25519 deploy key on the VM (recommended; add the public half to the
#     repo's Settings > Deploy keys) OR `gh auth login` for the service user.
#   * For CI deploy (.github/workflows/deploy.yml), the repo also needs the
#     three Actions secrets:
#       - C3_VM_HOST
#       - C3_VM_USER
#       - C3_VM_SSH_KEY     (private half of the SSH key whose public half is
#                            in the VM user's ~/.ssh/authorized_keys)
#
# Idempotent: safe to re-run. Won't overwrite /etc/hilmar-tracker/.env if it
# exists.

set -euo pipefail

REPO_URL="${REPO_URL:-git@github.com:IdealX-dev/hilmar-tracker.git}"
INSTALL_DIR="/opt/hilmar-tracker"
SERVICE_USER="hilmar"
LOG_DIR="/var/log/hilmar-tracker"
ENV_DIR="/etc/hilmar-tracker"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (sudo bash setup-vm.sh)" >&2
  exit 1
fi

echo "[1/6] APT packages…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
  python3 python3-venv python3-dev python3-pip \
  git ca-certificates \
  libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf2.0-0 libffi-dev shared-mime-info fonts-dejavu \
  logrotate cron tzdata

echo "[2/6] Service user $SERVICE_USER…"
# NB: --no-create-home is intentional. Previously --create-home populated
# $INSTALL_DIR with /etc/skel content (.bashrc, .profile, .bash_logout)
# which then caused "[3/6] git clone" to refuse the non-empty target.
# The user's HOME is set in /etc/passwd via --home-dir but the directory
# itself is created (empty) by the mkdir below, then `git clone` fills
# it with the working tree.
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$INSTALL_DIR" "$LOG_DIR" "$ENV_DIR"
# IMPORTANT: $ENV_DIR must be hilmar-owned (not root) or the service user
# can't traverse into it to read the .env file inside, even though the
# .env file itself is hilmar-owned with mode 0600. Symptom on the prior
# run: `bash -lc 'source /etc/hilmar-tracker/.env'` failed with EACCES,
# which made HILMAR_TOKEN_CACHE go unset, which made hilmar-auth-login
# write the MSAL token cache to its default fallback (~/.hilmar-tracker/)
# instead of the configured /etc/hilmar-tracker/token-cache.bin — and
# subsequent cron runs with EnvironmentFile=/etc/hilmar-tracker/.env
# couldn't find a cache there.
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$LOG_DIR" "$ENV_DIR"
chmod 750 "$ENV_DIR"

echo "[3/6] Repo checkout…"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull --ff-only
else
  # If $INSTALL_DIR exists but isn't empty (e.g. earlier failed run left
  # skel files behind), wipe everything below it. The dir itself stays
  # because chown/perms are already applied. Hidden files (`.[!.]*`)
  # are matched explicitly because shell glob excludes them by default.
  if [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    echo "  → $INSTALL_DIR is non-empty before clone; clearing residue from a prior failed run."
    rm -rf "$INSTALL_DIR"/* "$INSTALL_DIR"/.[!.]* 2>/dev/null || true
  fi
  sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo "[4/6] Python venv + deps…"
sudo -u "$SERVICE_USER" bash -c "
  set -e
  cd '$INSTALL_DIR'
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip wheel
  # Install the package (no [dev] extras on the VM — pytest is CI-only).
  .venv/bin/pip install -e .
"

echo "[5/6] systemd service + timer…"
install -m 0644 "$INSTALL_DIR/deploy/systemd/hilmar-tracker.service" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/systemd/hilmar-tracker.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable hilmar-tracker.timer
systemctl start  hilmar-tracker.timer

echo "[6/6] /etc/hilmar-tracker/.env + logrotate…"
if [[ ! -f "$ENV_DIR/.env" ]]; then
  cp "$INSTALL_DIR/.env.example" "$ENV_DIR/.env"
  chmod 600 "$ENV_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$ENV_DIR/.env"
  echo "  → POPULATE $ENV_DIR/.env with the Graph creds before the next timer fire."
else
  echo "  → $ENV_DIR/.env already exists, leaving as-is."
fi

cat > /etc/logrotate.d/hilmar-tracker <<EOF
$LOG_DIR/*.log {
    weekly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    create 0640 $SERVICE_USER $SERVICE_USER
}
EOF

echo
echo "DONE. Verify (in this order):"
echo
echo "  # 1. Bootstrap MSAL token cache (one-time interactive device-code login)"
echo "  sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/hilmar-auth-login"
echo
echo "  # 2. Manual dry-run (HILMAR_DRY_RUN=true halts before send + upload)"
echo "  sudo -u $SERVICE_USER HILMAR_DRY_RUN=true $INSTALL_DIR/.venv/bin/hilmar-run"
echo
echo "  # 3. Confirm the timer is armed for weekdays 07:00 ET"
echo "  systemctl list-timers hilmar-tracker.timer"
echo
echo "  # 4. Once the dry-run output passes the DOD parity check vs the"
echo "  #    last Cowork-mode run, flip HILMAR_DRY_RUN=false in $ENV_DIR/.env"
echo "  #    and let the next timer fire ship the daily live."
echo
echo "Reminder: populate $ENV_DIR/.env (chmod 600) before any live run —"
echo "the .env.example template ships in the repo at $INSTALL_DIR/.env.example."
