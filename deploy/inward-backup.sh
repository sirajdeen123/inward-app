#!/usr/bin/env bash
# Nightly encrypted backup of one Inward environment (SoT 14.3). Run as root by inward-backup@<env>.timer.
#   inward-backup.sh uat|live
# Set: PostgreSQL dump + document store (weekly full on Sunday, daily incremental) + manifest (SHA-256 of every
# document) -> one .tar encrypted with AES-256 (gpg, passphrase in /srv/inward/<env>/backup.pass, root only).
# Retention: daily 30, weekly 12 (Sunday), monthly 24 (1st). Copied to OneDrive when rclone is configured.
# Nothing outside /srv/inward/<env> is read or written (the rainbow applications are not touched).
set -euo pipefail
ENVNAME="${1:?usage: inward-backup.sh uat|live}"
ROOT="/srv/inward/$ENVNAME"
DEST="$ROOT/backups"
PASS="$ROOT/backup.pass"
STATUS="$ROOT/data/backup_status.json"
RCLONE_CONF="/srv/inward/.config/rclone/rclone.conf"
umask 077

set -a; . "$ROOT/env"; set +a
DB="$(printf '%s' "$INWARD_DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
DOCS="$INWARD_DOCUMENT_ROOT"
STAMP="$(date +%Y%m%d-%H%M)"
DOW="$(date +%u)"; DOM="$(date +%d)"
mkdir -p "$DEST/daily" "$DEST/weekly" "$DEST/monthly" "$DEST/state"
[ -s "$PASS" ] || { openssl rand -base64 48 > "$PASS"; chmod 600 "$PASS"; echo "backup passphrase created: $PASS (keep a copy offline)"; }

fail() {
  printf '{"ok": false, "at": "%s", "error": "%s"}\n' "$(date -Iseconds)" "$1" > "$STATUS.tmp"
  chown inward:inward "$STATUS.tmp"; mv "$STATUS.tmp" "$STATUS"; echo "BACKUP FAILED: $1" >&2; exit 1
}
trap 'fail "line $LINENO"' ERR

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
# 1. database (custom format, consistent snapshot)
sudo -u inward pg_dump -Fc --no-owner "$DB" > "$WORK/database.dump"
# 2. documents: level 0 on Sunday (or when no snapshot yet), otherwise only files new since the last run
SNAP="$DEST/state/documents.snar"
LEVEL="incremental"
if [ "$DOW" = "7" ] || [ ! -s "$SNAP" ]; then rm -f "$SNAP"; LEVEL="full"; fi
tar --listed-incremental="$SNAP" -C "$DOCS" -cf - . | zstd -q -T0 -o "$WORK/documents-$LEVEL.tar.zst"
# 3. manifest
( cd "$DOCS" && find . -type f ! -name '*.tmp' -printf '%P\n' | sort | xargs -r sha256sum ) > "$WORK/manifest.sha256"
printf 'environment=%s\ndatabase=%s\ncreated=%s\ndocuments=%s\nfiles=%s\n' "$ENVNAME" "$DB" "$(date -Iseconds)" \
  "$LEVEL" "$(wc -l < "$WORK/manifest.sha256")" > "$WORK/README.txt"
# 4. one encrypted archive
OUT="$DEST/daily/inward-$ENVNAME-$STAMP-$LEVEL.tar.gpg"
tar -C "$WORK" -cf - . | gpg --batch --yes --quiet --symmetric --cipher-algo AES256 --passphrase-file "$PASS" -o "$OUT"
sha256sum "$OUT" > "$OUT.sha256"
[ "$DOW" = "7" ] && cp "$OUT" "$OUT.sha256" "$DEST/weekly/"
[ "$DOM" = "01" ] && cp "$OUT" "$OUT.sha256" "$DEST/monthly/"
# 5. retention (keep the files a restore still needs: dailies after the newest full)
find "$DEST/daily" -name 'inward-*' -mtime +30 -delete
find "$DEST/weekly" -name 'inward-*' -mtime +84 -delete
find "$DEST/monthly" -name 'inward-*' -mtime +730 -delete
# 6. off-site copy to OneDrive when configured
OFFSITE="not configured"
if command -v rclone >/dev/null && [ -s "$RCLONE_CONF" ] && rclone --config "$RCLONE_CONF" listremotes | grep -q '^onedrive:'; then
  if rclone --config "$RCLONE_CONF" copy "$DEST" "onedrive:Inward Backups/$ENVNAME" --exclude 'state/**' -q; then
    OFFSITE="copied"; else OFFSITE="FAILED"; fi
fi
SIZE="$(du -h "$OUT" | cut -f1)"
printf '{"ok": true, "at": "%s", "file": "%s", "size": "%s", "documents": "%s", "offsite": "%s"}\n' \
  "$(date -Iseconds)" "$(basename "$OUT")" "$SIZE" "$LEVEL" "$OFFSITE" > "$STATUS.tmp"
chown inward:inward "$STATUS.tmp"; mv "$STATUS.tmp" "$STATUS"
echo "backup ok: $OUT ($SIZE, documents $LEVEL, offsite $OFFSITE)"
