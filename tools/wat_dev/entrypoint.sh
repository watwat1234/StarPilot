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

# The repos folder and caches sit on a root-owned bind mount on first run
: "${REPOS_DIR:?set by docker-compose (DEV_ROOT/workspace)}"
: "${CCACHE_DIR:?set by docker-compose (DEV_ROOT/ccache)}"
: "${UV_CACHE_DIR:?set by docker-compose (DEV_ROOT/uv_cache)}"
mkdir -p "$REPOS_DIR" "$CCACHE_DIR" "$UV_CACHE_DIR"
chown "$DEV_USER": "$REPOS_DIR" "$CCACHE_DIR" "$UV_CACHE_DIR" 2>/dev/null || true

# uv hardlinks cache -> .venv; that needs the same mount and a filesystem that
# allows it. uv falls back to copying (6 GB per venv), so just warn.
t=$(mktemp -p "$UV_CACHE_DIR" .linktest.XXXXXX)
if ln "$t" "$REPOS_DIR/.linktest.$$" 2>/dev/null; then
  echo "hardlinks between uv cache and repos: OK"
else
  echo "WARNING: cannot hardlink between $UV_CACHE_DIR and $REPOS_DIR; uv will copy (use a direct pool path, not /mnt/user)"
fi
rm -f "$t" "$REPOS_DIR/.linktest.$$"

# sshd sessions don't inherit container env; pam_env reads /etc/environment
sed -i '/^\(UV_CACHE_DIR\|CCACHE_DIR\|REPOS_DIR\|UV_LINK_MODE\)=/d' /etc/environment 2>/dev/null || true
printf '%s\n' "UV_CACHE_DIR=$UV_CACHE_DIR" "CCACHE_DIR=$CCACHE_DIR" "REPOS_DIR=$REPOS_DIR" >> /etc/environment

# sshd sessions don't inherit container env; give them DISPLAY. Rewritten on every
# start (the old block hardcoded CCACHE_DIR=/ccache and sat in the persistent home
# volume). The range ends at the first line ending in activate".
sed -i '/^# openpilot dev env$/,/activate"$/d' "$HOME_DIR/.bashrc" 2>/dev/null || true
cat >> "$HOME_DIR/.bashrc" <<'EOF'
# openpilot dev env
export DISPLAY=:1
[ -f "$HOME/.venv/bin/activate" ] && source "$HOME/.venv/bin/activate"
EOF

# Docker socket (optional mount): give the dev user the socket's group
if [ -S /var/run/docker.sock ]; then
  sock_gid=$(stat -c %g /var/run/docker.sock)
  getent group "$sock_gid" >/dev/null || groupadd -g "$sock_gid" dockerhost
  usermod -aG "$sock_gid" "$DEV_USER"
fi

exec /usr/bin/supervisord -n
