#!/bin/bash
set -e

# SSH: persistent host keys + key from env (avoids bind-mount ownership issues)
mkdir -p /etc/ssh/keys
[ -f /etc/ssh/keys/ssh_host_ed25519_key ] || ssh-keygen -t ed25519 -f /etc/ssh/keys/ssh_host_ed25519_key -N ""
cat > /etc/ssh/sshd_config.d/dev.conf <<EOF
HostKey /etc/ssh/keys/ssh_host_ed25519_key
PasswordAuthentication no
PermitRootLogin no
AllowUsers ${DEV_USER}
EOF

HOME_DIR=$(getent passwd "$DEV_USER" | cut -d: -f6)
mkdir -p "$HOME_DIR/.ssh"
echo "${AUTHORIZED_KEY:?set AUTHORIZED_KEY to your ssh public key}" > "$HOME_DIR/.ssh/authorized_keys"
chown -R "$DEV_USER": "$HOME_DIR/.ssh"
chmod 700 "$HOME_DIR/.ssh"; chmod 600 "$HOME_DIR/.ssh/authorized_keys"

# The repos folder and caches are mounted as root-owned volumes on first run
: "${REPOS_DIR:?set REPOS_DIR (host folder holding the clones)}"
chown "$DEV_USER": /ccache "$REPOS_DIR" 2>/dev/null || true

# sshd sessions don't inherit container env; give them PATH/DISPLAY
grep -q "openpilot dev env" "$HOME_DIR/.bashrc" 2>/dev/null || cat >> "$HOME_DIR/.bashrc" <<'EOF'
# openpilot dev env
export DISPLAY=:1 CCACHE_DIR=/ccache
source "$HOME/.venv/bin/activate"
EOF

# Docker socket (optional mount): give the dev user the socket's group
if [ -S /var/run/docker.sock ]; then
  sock_gid=$(stat -c %g /var/run/docker.sock)
  getent group "$sock_gid" >/dev/null || groupadd -g "$sock_gid" dockerhost
  usermod -aG "$sock_gid" "$DEV_USER"
fi

exec /usr/bin/supervisord -n
