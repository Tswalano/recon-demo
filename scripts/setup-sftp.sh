#!/usr/bin/env bash
# Sets up an SFTP-only drop box on the server for the recon demo: a user (e.g. the bank) that can log in with SFTP
# only, sees only the app's files folder, and can only write into inbox/. The worker imports what lands there.
#
# Run it on the EC2 server, once, as root:
#   sudo ./scripts/setup-sftp.sh --app-user ubuntu --key ~/bank.pub
#   sudo ./scripts/setup-sftp.sh --app-user ubuntu --password          # a password instead of a key
#
#   --app-user   the Linux user the app's processes run as (the Ops Console's): it must own the folders.
#                Find it on the server: ps -o user= -p <worker pid shown on the app's page>
#   --sftp-user  the drop box's login (default: bankdrop)
#   --dir        the files folder (default: /srv/recon-files); set the app's FILES_DIR to the same path
#   --key        a public key file for the SFTP user (recommended)
#   --password   log in with a password instead (you'll be asked for it)
#
# Then, in the Ops Console: the app's settings → environment → FILES_DIR=/srv/recon-files, and redeploy.
# It is safe to run again (it replaces its own sshd block). Undo: see --remove.
set -euo pipefail

APP_USER="" SFTP_USER="bankdrop" DIR="/srv/recon-files" KEY="" PASSWORD=no REMOVE=no
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-user) APP_USER="$2"; shift 2 ;;
    --sftp-user) SFTP_USER="$2"; shift 2 ;;
    --dir) DIR="${2%/}"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --password) PASSWORD=yes; shift ;;
    --remove) REMOVE=yes; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run it as root (sudo)." >&2; exit 1; }
SSHD_CONFIG=/etc/ssh/sshd_config
KEYS_DIR=/etc/ssh/recon-sftp
BEGIN="# BEGIN recon-sftp ($SFTP_USER)" END="# END recon-sftp ($SFTP_USER)"

strip_block() {  # sshd_config without our block
  awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1;next} $0==e{skip=0;next} !skip' "$SSHD_CONFIG"
}
reload_sshd() {
  sshd -t || return 1
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
}

if [[ $REMOVE == yes ]]; then
  cp -a "$SSHD_CONFIG" "$SSHD_CONFIG.bak.$(date +%s)"
  strip_block > "$SSHD_CONFIG.new" && mv "$SSHD_CONFIG.new" "$SSHD_CONFIG"
  reload_sshd
  userdel "$SFTP_USER" 2>/dev/null || true
  rm -f "$KEYS_DIR/$SFTP_USER.keys"
  echo "Removed the SFTP user $SFTP_USER and its sshd block. The files in $DIR are left as they are."
  exit 0
fi

[[ -n $APP_USER ]] || { echo "--app-user is required (the user the app runs as; see --help)." >&2; exit 2; }
id "$APP_USER" >/dev/null 2>&1 || { echo "No user $APP_USER on this server." >&2; exit 1; }
[[ -n $KEY || $PASSWORD == yes ]] || { echo "Give --key <public key file> or --password." >&2; exit 2; }
[[ -z $KEY || -r $KEY ]] || { echo "Can't read $KEY." >&2; exit 1; }
[[ $DIR == /* ]] || { echo "--dir must be an absolute path." >&2; exit 2; }

# sshd's chroot rule: the folder and every folder above it must be owned by root and writable by no one else
# (the folder itself is set that way below).
p=$(dirname "$DIR")
while [[ $p != / ]]; do
  [[ -e $p ]] || { p=$(dirname "$p"); continue; }
  owner=$(stat -c %U "$p"); mode=$(stat -c %a "$p")
  if [[ $owner != root || $(( 8#$mode & 8#022 )) -ne 0 ]]; then
    echo "$p is owned by $owner with mode $mode: for an SFTP chroot it must be root's and not group/world-writable." >&2
    echo "Pick another --dir (e.g. /srv/recon-files)." >&2; exit 1
  fi
  p=$(dirname "$p")
done

echo "== folders in $DIR"
mkdir -p "$DIR"
chown root:root "$DIR"; chmod 755 "$DIR"
APP_GROUP=$(id -gn "$APP_USER")
getent group recon-drop >/dev/null || groupadd recon-drop
install -d -o "$APP_USER" -g recon-drop -m 2775 "$DIR/inbox"            # the drop box: SFTP user and app can write
for d in processing archive failed; do install -d -o "$APP_USER" -g "$APP_GROUP" -m 750 "$DIR/$d"; done  # the app's only

echo "== user $SFTP_USER (SFTP only, no shell)"
NOLOGIN=$(command -v nologin || echo /usr/sbin/nologin)
if id "$SFTP_USER" >/dev/null 2>&1; then
  usermod -d "$DIR" -s "$NOLOGIN" -aG recon-drop "$SFTP_USER"
else
  useradd -M -d "$DIR" -s "$NOLOGIN" -G recon-drop "$SFTP_USER"
fi
install -d -m 755 "$KEYS_DIR"
if [[ -n $KEY ]]; then
  install -m 644 "$KEY" "$KEYS_DIR/$SFTP_USER.keys"
else
  : > "$KEYS_DIR/$SFTP_USER.keys"
fi
[[ $PASSWORD == yes ]] && passwd "$SFTP_USER"

echo "== sshd: chroot $SFTP_USER into $DIR, SFTP only"
cp -a "$SSHD_CONFIG" "$SSHD_CONFIG.bak.$(date +%s)"
{
  strip_block
  cat <<EOF
$BEGIN
Match User $SFTP_USER
    ChrootDirectory $DIR
    ForceCommand internal-sftp -d /inbox -u 0002
    AuthorizedKeysFile $KEYS_DIR/%u.keys
    PasswordAuthentication $PASSWORD
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
$END
EOF
} > "$SSHD_CONFIG.new"
mv "$SSHD_CONFIG.new" "$SSHD_CONFIG"
if ! reload_sshd; then
  echo "sshd rejected the new config: restoring the previous one." >&2
  cp -a "$(ls -t "$SSHD_CONFIG".bak.* | head -1)" "$SSHD_CONFIG"
  exit 1
fi

HOST=$(curl -s -m 2 http://checkip.amazonaws.com 2>/dev/null || hostname -f)
cat <<EOF

Done. Next:
  1. Ops Console → the app → environment: FILES_DIR=$DIR, then redeploy (the web app and the worker must both have it).
     The app's page shows the folder it uses under "Send files": check it says $DIR.
  2. The EC2 security group must allow port 22 from wherever the files come from (the bank's address, or yours).
  3. Try it:
       sftp $( [[ -n $KEY ]] && echo "-i <private key> " )$SFTP_USER@$HOST
       sftp> put samples/ledger_2026-11-26.csv
       sftp> put samples/bank_2026-11-26.csv
     The worker picks each file up within a few seconds; the page shows it under Files.
EOF
