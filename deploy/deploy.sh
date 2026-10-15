#!/usr/bin/env bash
# Deploy a release of the Inward application on the VPS (run as root).
#   deploy.sh uat  [git-ref]     # TEST copy  -> uat.inward.rainbowlogistics.co.in
#   deploy.sh live [git-ref]     # live       -> inward.rainbowlogistics.co.in
# Native install (SoT 14.6, approved 15-Oct-2026): own Linux user 'inward', own PostgreSQL role and
# databases (peer login), own systemd service and Nginx site. Nothing of /srv/rainbow is touched.
set -euo pipefail

ENVNAME="${1:?usage: deploy.sh uat|live [git-ref]}"
REF="${2:-main}"
case "$ENVNAME" in
  uat)  PORT=8100; DB=inward_uat; HOST=uat.inward.rainbowlogistics.co.in; MODE=TEST ;;
  live) PORT=8110; DB=inward;     HOST=inward.rainbowlogistics.co.in;     MODE=LIVE ;;
  *) echo "environment must be uat or live" >&2; exit 2 ;;
esac

ROOT=/srv/inward/$ENVNAME
REPO_URL="${INWARD_REPO_URL:-git@github.com:OWNER/inward-app.git}"
as_inward() { sudo -u inward -H "$@"; }

install -d -o inward -g inward -m 750 "$ROOT" "$ROOT/data" "$ROOT/data/documents" "$ROOT/data/static" "$ROOT/logs"

# 1. code (read-only deploy key of the inward user)
if [ ! -d "$ROOT/app/.git" ]; then
  as_inward git clone --quiet "$REPO_URL" "$ROOT/app"
fi
as_inward git -C "$ROOT/app" fetch --quiet --all --tags
as_inward git -C "$ROOT/app" checkout --quiet --force "$REF"
as_inward git -C "$ROOT/app" reset --quiet --hard "origin/$REF" 2>/dev/null || true
echo "code: $(as_inward git -C "$ROOT/app" log -1 --format='%h %s')"

# 2. environment file (created once; secrets generated here and never printed)
if [ ! -f "$ROOT/env" ]; then
  SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')
  cat > "$ROOT/env" <<ENV
INWARD_ENV=prod
INWARD_MODE=$MODE
INWARD_BIND=127.0.0.1:$PORT
DJANGO_SECRET_KEY=$SECRET
INWARD_ALLOWED_HOSTS=$HOST
INWARD_DATABASE_URL=postgresql+psycopg://inward@/$DB
INWARD_DOCUMENT_ROOT=$ROOT/data/documents
INWARD_STATIC_ROOT=$ROOT/data/static
INWARD_DEPOT_CODE=035
INWARD_ADMIN_URL=control-035/
INWARD_GMAIL_ADDRESS=
INWARD_GMAIL_APP_PASSWORD=
INWARD_TEST_MAIL_TO=
INWARD_SIT_SUBJECT="BPIL Depot Stock In Transit Report"
INWARD_MESSAGE_ID_DOMAIN=inward.rainbowlogistics.co.in
ENV
  chown inward:inward "$ROOT/env"; chmod 600 "$ROOT/env"
  echo "env: created $ROOT/env (fill in Gmail settings there)"
fi

# 3. Python environment and release steps
# system libraries for MRA PDFs (WeasyPrint: Pango + HarfBuzz); shared libraries only, other apps unaffected
PDF_PKGS="libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core rclone"
dpkg -s $PDF_PKGS >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PDF_PKGS >/dev/null
[ -x "$ROOT/venv/bin/python" ] || as_inward python3 -m venv "$ROOT/venv"
as_inward "$ROOT/venv/bin/pip" install --quiet --upgrade pip
as_inward "$ROOT/venv/bin/pip" install --quiet -e "$ROOT/app"
run_manage() { as_inward bash -c "set -a; . '$ROOT/env'; set +a; cd '$ROOT/app'; '$ROOT/venv/bin/python' manage.py $*"; }
run_manage check --deploy --fail-level ERROR
run_manage migrate --noinput
run_manage seed_reference_data   # adds new roles / permissions / package types; never overwrites edits
run_manage collectstatic --noinput --clear -v 0

# 4. service and web site
install -d -o inward -g inward -m 700 /srv/inward/.config /srv/inward/.config/rclone  # OneDrive (rclone) connection
install -m 644 "$ROOT/app/deploy/inward@.service" /etc/systemd/system/inward@.service
install -m 644 "$ROOT/app/deploy/inward-worker@.service" /etc/systemd/system/inward-worker@.service
install -m 644 "$ROOT/app/deploy/inward_proxy.conf" /etc/nginx/inward_proxy.conf
install -m 755 "$ROOT/app/deploy/inward-manage" /usr/local/bin/inward-manage
install -m 755 "$ROOT/app/deploy/inward-backup.sh" /usr/local/bin/inward-backup
install -m 755 "$ROOT/app/deploy/inward-restore-test.sh" /usr/local/bin/inward-restore-test
install -m 644 "$ROOT/app/deploy/inward-backup@.service" /etc/systemd/system/inward-backup@.service
install -m 644 "$ROOT/app/deploy/inward-backup@.timer" /etc/systemd/system/inward-backup@.timer
systemctl daemon-reload
systemctl enable --quiet "inward@$ENVNAME"
systemctl restart "inward@$ENVNAME"
systemctl enable --quiet "inward-worker@$ENVNAME"
systemctl enable --quiet --now "inward-backup@$ENVNAME.timer"
systemctl restart "inward-worker@$ENVNAME"
sleep 3
curl -fsS -H "Host: $HOST" "http://127.0.0.1:$PORT/healthz" >/dev/null && echo "service inward@$ENVNAME healthy on :$PORT"
