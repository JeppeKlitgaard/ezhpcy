#!/usr/bin/env bash
# Filestash, run by EzHPCy as an app on a host, over the host's files.
#
# The binary is copied into the run directory to be executable: assets are
# read-only, and lose their executable bit when uploaded from Windows. Its state
# lives in the run directory too, from `config.json`, a copy of the asset's,
# which Filestash writes to. The plugins, which it only reads, are linked to.
#
# There is no admin: Filestash needs the admin password's bcrypt hash, so it is
# given one of its own for each run, of random bytes, which no password has.
# That locks the admin console, and the local storage's admin login, to all.
set -euo pipefail

# Without pipefail: `tr` dies of SIGPIPE once `head` has enough.
salt_and_hash=$(set +o pipefail; LC_ALL=C tr -dc './A-Za-z0-9' </dev/urandom | head -c 53)
export ADMIN_PASSWORD="\$2a\$10\$$salt_and_hash"

binary="$EZHPCY_RUN_DIR/filestash"
install -m 700 "$EZHPCY_ASSETS/filestash_linux_x86_64.bin" "$binary"
mkdir -p "$FILESTASH_PATH/state/config" "$FILESTASH_PATH/state/plugins"
install -m 600 "$EZHPCY_ASSETS/config.json" "$FILESTASH_PATH/state/config/config.json"
ln -s "$EZHPCY_ASSETS"/plugins/*.zip "$FILESTASH_PATH/state/plugins/"
exec "$binary"
