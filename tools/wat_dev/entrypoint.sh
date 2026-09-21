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

# VNC password for x11vnc (native clients on 5900 and noVNC on 6080 both use it).
# VNC only looks at the first 8 characters.
x11vnc -storepasswd "${VNC_PASSWORD:?set VNC_PASSWORD in .env}" /etc/x11vnc.pass >/dev/null
chown "$DEV_USER": /etc/x11vnc.pass; chmod 600 /etc/x11vnc.pass

HOME_DIR=$(getent passwd "$DEV_USER" | cut -d: -f6)

# /home/batman is a bind mount: empty on first run, root-owned, and it hides the
# image's copy. Seed missing files from the image; never overwrite existing ones.
chown "$DEV_USER": "$HOME_DIR"
rsync -a --ignore-existing /opt/home-seed/ "$HOME_DIR/"

# /tmp is a bind mount too (persists across restarts): make it sticky+world-writable
# and clear X's lock/socket from the previous run so Xvfb can start.
chmod 1777 /tmp
rm -rf /tmp/.X1-lock /tmp/.X11-unix
mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
mkdir -p "$HOME_DIR/.ssh"
echo "${AUTHORIZED_KEY:?set AUTHORIZED_KEY to your ssh public key}" > "$HOME_DIR/.ssh/authorized_keys"
chown -R "$DEV_USER": "$HOME_DIR/.ssh"
chmod 700 "$HOME_DIR/.ssh"; chmod 600 "$HOME_DIR/.ssh/authorized_keys"

# The repos folder and caches sit on a root-owned bind mount on first run
: "${DEV_ROOT:?set by docker-compose}"
: "${REPOS_DIR:?set by docker-compose (DEV_ROOT/workspace)}"
: "${CCACHE_DIR:?set by docker-compose (DEV_ROOT/ccache)}"
: "${UV_CACHE_DIR:?set by docker-compose (DEV_ROOT/uv_cache)}"
: "${COMMA_SYSROOT_DIR:?set by docker-compose (DEV_ROOT/sysroot)}"
: "${COMMA_HOST_CACHE_DIR:?set by docker-compose (DEV_ROOT/build_cache)}"
mkdir -p "$REPOS_DIR" "$CCACHE_DIR" "$UV_CACHE_DIR" "$COMMA_SYSROOT_DIR" "$COMMA_HOST_CACHE_DIR"
chown "$DEV_USER": "$REPOS_DIR" "$CCACHE_DIR" "$UV_CACHE_DIR" "$COMMA_SYSROOT_DIR" "$COMMA_HOST_CACHE_DIR" 2>/dev/null || true

# uv hardlinks cache -> .venv; that needs the same mount and a filesystem that
# allows it. uv falls back to copying (6 GB per venv), so just warn.
t=$(mktemp -p "$UV_CACHE_DIR" .linktest.XXXXXX)
if ln "$t" "$REPOS_DIR/.linktest.$$" 2>/dev/null; then
  echo "hardlinks between uv cache and repos: OK"
else
  echo "WARNING: cannot hardlink between $UV_CACHE_DIR and $REPOS_DIR; uv will copy (use a direct pool path, not /mnt/user)"
fi
rm -f "$t" "$REPOS_DIR/.linktest.$$"

# Per-clone git config (hooks path, rerere) lives in each clone's .git/config, so apply it to
# every StarPilot clone/worktree under REPOS_DIR on each start. Never fatal.
for d in "$REPOS_DIR"/*/; do
  [ -e "${d}.git" ] && [ -x "${d}tools/wat_dev/bin/wat-setup" ] || continue
  runuser -u "$DEV_USER" -- bash -c 'cd "$1" && tools/wat_dev/bin/wat-setup' _ "$d" || echo "WARNING: wat-setup failed in $d"
done

# sshd sessions don't inherit container env; pam_env reads /etc/environment
sed -i '/^\(DEV_ROOT\|UV_CACHE_DIR\|CCACHE_DIR\|REPOS_DIR\|UV_LINK_MODE\)=/d' /etc/environment 2>/dev/null || true
printf '%s\n' "DEV_ROOT=$DEV_ROOT" "UV_CACHE_DIR=$UV_CACHE_DIR" "CCACHE_DIR=$CCACHE_DIR" "REPOS_DIR=$REPOS_DIR" \
  "COMMA_SYSROOT_DIR=$COMMA_SYSROOT_DIR" "COMMA_HOST_SYSROOT_DIR=$COMMA_HOST_SYSROOT_DIR" "COMMA_HOST_CACHE_DIR=$COMMA_HOST_CACHE_DIR" >> /etc/environment

# sshd sessions don't inherit container env; give them DISPLAY. Rewritten on every
# start (the old block hardcoded CCACHE_DIR=/ccache and sat in the persistent home
# volume). The range ends at the first line ending in activate".
sed -i '/^# openpilot dev env$/,/activate"$/d' "$HOME_DIR/.bashrc" 2>/dev/null || true
cat >> "$HOME_DIR/.bashrc" <<'EOF'
# openpilot dev env
export DISPLAY=:1
# start in DEV_ROOT, but only for shells that open in $HOME (keeps VS Code's folder)
[ "$PWD" = "$HOME" ] && [ -d "$DEV_ROOT" ] && cd "$DEV_ROOT"
[ -f "$HOME/.venv/bin/activate" ] && source "$HOME/.venv/bin/activate"
EOF

# Docker socket (optional mount): give the dev user the socket's group
if [ -S /var/run/docker.sock ]; then
  sock_gid=$(stat -c %g /var/run/docker.sock)
  getent group "$sock_gid" >/dev/null || groupadd -g "$sock_gid" dockerhost
  usermod -aG "$sock_gid" "$DEV_USER"
fi

exec /usr/bin/supervisord -n
