#!/usr/bin/env bash
# Restore test (SoT 14.3): decrypt the newest backup of an environment, restore its database into a scratch
# database, check every document in the newest full + later incrementals against the manifest, then clean up.
#   inward-restore-test.sh uat|live
set -euo pipefail
ENVNAME="${1:?usage: inward-restore-test.sh uat|live}"
ROOT="/srv/inward/$ENVNAME"
DEST="$ROOT/backups/daily"
PASS="$ROOT/backup.pass"
SCRATCH="inward_restoretest"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"; sudo -u postgres dropdb --if-exists "$SCRATCH" >/dev/null 2>&1 || true' EXIT
umask 077

LATEST="$(ls -1t "$DEST"/inward-*.tar.gpg | head -1)"
FULL="$(ls -1t "$DEST"/inward-*-full.tar.gpg | head -1)"
echo "newest: $(basename "$LATEST")   newest full: $(basename "$FULL")"
( cd "$DEST" && sha256sum -c --quiet "$LATEST.sha256" "$FULL.sha256" )
mkdir "$WORK/latest" "$WORK/docs"
gpg --batch --quiet --decrypt --passphrase-file "$PASS" "$LATEST" | tar -C "$WORK/latest" -xf -
# database into a scratch database
sudo -u postgres createdb "$SCRATCH"
sudo -u postgres pg_restore --no-owner -d "$SCRATCH" < "$WORK/latest/database.dump"
TABLES="$(sudo -u postgres psql -tA -d "$SCRATCH" -c "select count(*) from information_schema.tables where table_schema='public'")"
IWS="$(sudo -u postgres psql -tA -d "$SCRATCH" -c "select count(*) from iw")"
echo "database restored: $TABLES tables, $IWS IWs"
# documents: newest full, then every later incremental in order
for f in $(ls -1tr "$DEST"/inward-*.tar.gpg | awk -v full="$FULL" 'found || $0 == full {found=1; print}'); do
  mkdir -p "$WORK/x"; gpg --batch --quiet --decrypt --passphrase-file "$PASS" "$f" | tar -C "$WORK/x" -xf -
  zstd -q -dc "$WORK"/x/documents-*.tar.zst | tar --listed-incremental=/dev/null -C "$WORK/docs" -xf -
  rm -rf "$WORK/x"
done
( cd "$WORK/docs" && sha256sum -c --quiet "$WORK/latest/manifest.sha256" )
echo "documents verified: $(wc -l < "$WORK/latest/manifest.sha256") files match the manifest"
echo "RESTORE TEST OK"
