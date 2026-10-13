#!/usr/bin/env bash
# Provision the Personal Agent Computer desktop image. Runs as root inside a fresh
# images:ubuntu/24.04/cloud VM on the build bridge; the result is published as an Incus image.
#
# What the agent gets:
#   - user "agent" (UID 1000), passwordless sudo (turn off with PAC_AGENT_SUDO=0)
#   - XFCE on Xvfb :1 at 1280x800, AT-SPI on, started as systemd user services (no display manager)
#   - Chromium (xtradeb PPA, not the snap), LibreOffice, Noto CJK, fcitx5 pinyin + Mozc
#   - git, gh, curl, jq, ripgrep, build-essential, Python 3 + uv, Node 24
#   - cua-driver (pinned, checksum-verified) for computer use, x11vnc on loopback for takeover
#
# What it deliberately does not have: real credentials, sshd, snapd, unattended-upgrades,
# a screen locker or a keyring.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

AGENT=agent
AGENT_UID=1000
SCREEN=${PAC_SCREEN:-1280x800x24}
CUA_VERSION=0.34.0
CUA_SHA256=629ac96eff829d4dfd5cf221f3f2165c2d813aed91e5efb7b20777a741cd70a7   # linux x86_64 binary tarball
NODE_MAJOR=24

log() { printf '\n== %s\n' "$*"; }

log "base system"
# The cloud image's default user would take UID 1000.
if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu 2>/dev/null || userdel ubuntu; fi
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl gnupg software-properties-common sudo locales tzdata \
  git jq ripgrep unzip zip xz-utils file less nano vim-tiny htop procps psmisc \
  build-essential pkg-config python3 python3-venv python3-pip pipx \
  dbus-user-session dbus-x11 nftables >/dev/null

log "locales (agent shell stays C.UTF-8)"
sed -i -E 's/^# *(en_US.UTF-8|zh_CN.UTF-8|ja_JP.UTF-8) UTF-8/\1 UTF-8/' /etc/locale.gen
locale-gen >/dev/null
update-locale LANG=C.UTF-8

log "agent user"
if ! id "$AGENT" >/dev/null 2>&1; then
  useradd -m -u "$AGENT_UID" -s /bin/bash "$AGENT"
fi
if [ "${PAC_AGENT_SUDO:-1}" = 1 ]; then
  echo "$AGENT ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-pac-agent
  chmod 440 /etc/sudoers.d/90-pac-agent
fi
loginctl enable-linger "$AGENT"

log "desktop: Xvfb, XFCE, AT-SPI, fonts, input methods"
apt-get install -y -qq --no-install-recommends \
  xvfb x11-utils xauth xdotool xclip wmctrl imagemagick \
  xfce4-session xfwm4 xfdesktop4 xfce4-panel xfce4-settings xfce4-terminal thunar mousepad \
  at-spi2-core libatk-adaptor libatk-bridge2.0-0 \
  fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji fonts-crosextra-carlito fonts-crosextra-caladea \
  fcitx5 fcitx5-chinese-addons fcitx5-mozc fcitx5-frontend-gtk3 fcitx5-frontend-qt5 im-config \
  x11vnc xdg-utils libnss3-tools >/dev/null

log "Chromium from the xtradeb PPA (Ubuntu's chromium-browser is a snap shim)"
add-apt-repository -y ppa:xtradeb/apps >/dev/null
cat > /etc/apt/preferences.d/pac-chromium <<'PIN'
Package: chromium*
Pin: release o=LP-PPA-xtradeb-apps
Pin-Priority: 1001
PIN
apt-get update -qq
apt-get install -y -qq --no-install-recommends chromium >/dev/null
# Managed policy: no password manager, no sign-in prompts, no first-run noise.
install -d /etc/chromium/policies/managed
cat > /etc/chromium/policies/managed/pac.json <<'JSON'
{
  "PasswordManagerEnabled": false,
  "BrowserSignin": 0,
  "SyncDisabled": true,
  "MetricsReportingEnabled": false,
  "DefaultBrowserSettingEnabled": false,
  "PromotionalTabsEnabled": false
}
JSON

log "LibreOffice"
apt-get install -y -qq --no-install-recommends libreoffice-calc libreoffice-writer libreoffice-impress libreoffice-gtk3 >/dev/null

log "Node $NODE_MAJOR and uv"
curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" -o /tmp/nodesource.sh
bash /tmp/nodesource.sh >/dev/null
apt-get install -y -qq nodejs >/dev/null
rm -f /tmp/nodesource.sh
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null

log "gh"
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  > /etc/apt/sources.list.d/github-cli.list
apt-get update -qq
apt-get install -y -qq gh >/dev/null

log "cua-driver $CUA_VERSION"
tmp=$(mktemp -d)
asset="cua-driver-rs-$CUA_VERSION-linux-x86_64-binary.tar.gz"
curl -fsSL --retry 2 -o "$tmp/$asset" "https://github.com/trycua/cua/releases/download/cua-driver-rs-v$CUA_VERSION/$asset"
echo "$CUA_SHA256  $tmp/$asset" | sha256sum -c - >/dev/null
tar -xzf "$tmp/$asset" -C "$tmp" cua-driver
install -m 0755 "$tmp/cua-driver" /usr/local/bin/cua-driver
rm -rf "$tmp"
runuser -u "$AGENT" -- env HOME=/home/$AGENT cua-driver telemetry disable >/dev/null
runuser -u "$AGENT" -- env HOME=/home/$AGENT cua-driver telemetry status --json | jq -e '.enabled == false' >/dev/null

log "pac-computerd (pushed next to this script by image/build.sh)"
install -m 0755 "$(dirname "$0")/computerd.py" /usr/local/bin/pac-computerd

log "desktop session as agent's systemd user services"
units=/home/$AGENT/.config/systemd/user
install -d -o "$AGENT" -g "$AGENT" /home/$AGENT/.config "$units"
cat > "$units/pac-xvfb.service" <<UNIT
[Unit]
Description=PAC desktop: Xvfb :1
[Service]
ExecStart=/usr/bin/Xvfb :1 -screen 0 $SCREEN -dpi 96 -nolisten tcp
Restart=always
[Install]
WantedBy=default.target
UNIT
cat > "$units/pac-xfce.service" <<'UNIT'
[Unit]
Description=PAC desktop: XFCE on :1
Requires=pac-xvfb.service
After=pac-xvfb.service
[Service]
Environment=DISPLAY=:1 XDG_SESSION_TYPE=x11 XDG_CURRENT_DESKTOP=XFCE GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx XMODIFIERS=@im=fcitx
ExecStartPre=/bin/sh -c "for i in $$(seq 1 50); do xdpyinfo -display :1 >/dev/null 2>&1 && exit 0; sleep 0.2; done; exit 1"
ExecStart=/usr/bin/startxfce4
Restart=on-failure
[Install]
WantedBy=default.target
UNIT
# Takeover: VNC on loopback only. The gateway reaches it through the Incus API, never over the network.
cat > "$units/pac-x11vnc.service" <<'UNIT'
[Unit]
Description=PAC desktop: x11vnc on 127.0.0.1:5900
Requires=pac-xvfb.service
After=pac-xfce.service
[Service]
ExecStart=/usr/bin/x11vnc -display :1 -localhost -rfbport 5900 -forever -shared -nopw -noxdamage -quiet
Restart=always
[Install]
WantedBy=default.target
UNIT
for u in pac-xvfb pac-xfce pac-x11vnc; do
  install -d -o "$AGENT" -g "$AGENT" "$units/default.target.wants"
  ln -sf "../$u.service" "$units/default.target.wants/$u.service"
done
chown -R "$AGENT:$AGENT" /home/$AGENT/.config

# Shared environment for anything that drives the desktop (cua-driver, xdotool, the gateway's exec).
cat > /etc/profile.d/pac-desktop.sh <<'SH'
export DISPLAY=:1
export XDG_SESSION_TYPE=x11
SH

log "turn off what an agent desktop must not run"
systemctl disable --now ssh.service ssh.socket 2>/dev/null || true
apt-get purge -y -qq openssh-server unattended-upgrades snapd >/dev/null 2>&1 || true
apt-get purge -y -qq gnome-keyring xfce4-screensaver light-locker xscreensaver >/dev/null 2>&1 || true
apt-get autoremove -y -qq >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/*

log "image ready"
