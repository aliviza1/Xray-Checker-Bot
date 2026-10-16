#!/usr/bin/env bash
set -Eeuo pipefail

BOT_NAME="xray-checker-bot"
BOT_SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/xray-checker-bot.py"
BOT_DST="/usr/local/bin/xray-checker-bot.py"
ENV_FILE="/etc/xray-checker-telegram.env"
SERVICE_FILE="/etc/systemd/system/xray-checker-bot.service"
STATE_DIR="/var/lib/xray-checker-telegram-bot"
BACKUP_DIR="/var/backups/xray-checker-manager"

if [[ "${EUID}" -ne 0 ]]; then echo "Run as root: sudo bash install.sh" >&2; exit 1; fi
if [[ ! -f "$BOT_SRC" ]]; then echo "ERROR: xray-checker-bot.py must be in the same directory as install.sh" >&2; exit 1; fi
if ! command -v python3 >/dev/null 2>&1; then echo "ERROR: python3 is required" >&2; exit 1; fi
python3 - "$BOT_SRC" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
compile(p.read_text(encoding='utf-8'), str(p), 'exec')
print('Python syntax check passed.')
PY

printf '\n=== Xray Checker Telegram Bot installer ===\n'
printf 'This installer does not read, modify, stop, or restart BackPack.\n'
printf 'It installs/replaces only the Telegram bot service and may update its own env file.\n\n'

read -r -s -p 'Telegram Bot Token (input hidden): ' TOKEN; echo
if [[ -z "$TOKEN" || "$TOKEN" == *$'\n'* || "$TOKEN" == *$'\r'* ]]; then echo 'ERROR: token is empty or invalid.' >&2; exit 1; fi
read -r -p 'Telegram Chat ID (numeric; groups usually start with -100): ' CHAT_ID
if [[ ! "$CHAT_ID" =~ ^-?[0-9]+$ ]]; then echo 'ERROR: Chat ID must be numeric.' >&2; exit 1; fi
read -r -p 'Telegram relay host [127.0.0.1]: ' RELAY_HOST; RELAY_HOST="${RELAY_HOST:-127.0.0.1}"
read -r -p 'Telegram relay port [58615]: ' RELAY_PORT; RELAY_PORT="${RELAY_PORT:-58615}"
if [[ ! "$RELAY_PORT" =~ ^[0-9]+$ ]] || (( RELAY_PORT < 1 || RELAY_PORT > 65535 )); then echo 'ERROR: relay port must be 1-65535.' >&2; exit 1; fi
read -r -p 'Metrics dashboard URL [http://45.139.11.96:2112/]: ' DASHBOARD_URL; DASHBOARD_URL="${DASHBOARD_URL:-http://45.139.11.96:2112/}"
if [[ ! "$DASHBOARD_URL" =~ ^https?://[^[:space:]]+$ ]]; then echo 'ERROR: dashboard URL must be an HTTP(S) URL.' >&2; exit 1; fi

# Validate credentials and relay before replacing any installed files.
TOKEN="$TOKEN" CHAT_ID="$CHAT_ID" RELAY_HOST="$RELAY_HOST" RELAY_PORT="$RELAY_PORT" python3 - <<'PY'
import http.client, json, os, socket, ssl, sys
host = os.environ['RELAY_HOST']; port = int(os.environ['RELAY_PORT']); token = os.environ['TOKEN']; chat = os.environ['CHAT_ID']
class RelayHTTPS(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection((host, port), timeout=8)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
def call(method, fields=None):
    c = RelayHTTPS('api.telegram.org', timeout=15, context=ssl.create_default_context())
    try:
        from urllib.parse import urlencode
        body = urlencode(fields or {}).encode()
        c.request('POST', f'/bot{token}/{method}', body=body, headers={'Content-Type':'application/x-www-form-urlencoded'})
        r = c.getresponse(); raw = r.read().decode('utf-8','replace')
        try: data = json.loads(raw)
        except Exception: raise RuntimeError(f'Telegram returned HTTP {r.status} with invalid JSON')
        if r.status != 200 or not data.get('ok'): raise RuntimeError(data.get('description', f'HTTP {r.status}'))
        return data
    finally: c.close()
try:
    me = call('getMe')['result']
    call('sendMessage', {'chat_id': chat, 'text': f"✅ Xray Checker Bot installer: Telegram credentials validated for @{me.get('username','bot')}."})
except Exception as e:
    print(f'ERROR: Telegram validation failed: {type(e).__name__}: {e}', file=sys.stderr)
    print('No installed bot files or services have been changed.', file=sys.stderr)
    sys.exit(1)
print(f"Telegram token and Chat ID validated. Bot username: @{me.get('username','unknown')}")
PY

stamp="$(python3 -c 'import time; print(time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + f"{time.time_ns() % 1000000:06d}")')"
mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
# Preserve known bot-related files before replacing anything. BackPack is deliberately excluded.
if [[ -f "$BOT_DST" || -f "$ENV_FILE" || -f "$SERVICE_FILE" ]]; then
  BACKUP_ARCHIVE="$BACKUP_DIR/xray-checker-pre-install-${stamp}.tar.gz"
  files=()
  for f in "$BOT_DST" "$ENV_FILE" "$SERVICE_FILE" \
           /usr/local/bin/xray-checker-telegram.py /etc/xray-checker/xray-checker.env \
           /etc/systemd/system/xray-checker-telegram.service /etc/systemd/system/xray-checker-telegram.timer \
           /etc/systemd/system/xray-checker-telegram.timer.d/override.conf "$STATE_DIR/interval_minutes"; do
    [[ -f "$f" ]] && files+=("${f#/}")
  done
  if ((${#files[@]})); then tar -czf "$BACKUP_ARCHIVE" -C / "${files[@]}"; chmod 600 "$BACKUP_ARCHIVE"; echo "Pre-install backup: $BACKUP_ARCHIVE"; fi
fi

install -d -m 700 "$STATE_DIR"
install -d -m 700 "$BACKUP_DIR"
install -m 755 "$BOT_SRC" "$BOT_DST"

# Preserve unrelated keys from an existing env file, but update only the bot's own settings.
TMP_ENV="$(mktemp /etc/.xray-checker-telegram.env.XXXXXX)"
if [[ -f "$ENV_FILE" ]]; then
  awk '!/^[[:space:]]*(export[[:space:]]+)?(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_RELAY_HOST|TELEGRAM_RELAY_PORT|DASHBOARD_URL)[[:space:]]*=/' "$ENV_FILE" > "$TMP_ENV"
fi
{
  printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN"
  printf 'TELEGRAM_CHAT_ID=%s\n' "$CHAT_ID"
  printf 'TELEGRAM_RELAY_HOST=%s\n' "$RELAY_HOST"
  printf 'TELEGRAM_RELAY_PORT=%s\n' "$RELAY_PORT"
  printf 'DASHBOARD_URL=%s\n' "$DASHBOARD_URL"
} >> "$TMP_ENV"
chmod 600 "$TMP_ENV"
chown root:root "$TMP_ENV"
mv -f "$TMP_ENV" "$ENV_FILE"
unset TOKEN

cat > "$SERVICE_FILE" <<'UNIT'
[Unit]
Description=Xray Checker Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
EnvironmentFile=/etc/xray-checker-telegram.env
ExecStart=/usr/bin/python3 /usr/local/bin/xray-checker-bot.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/usr/local/bin /etc/xray-checker-telegram.env /etc/xray-checker /etc/systemd/system /var/lib/xray-checker-telegram-bot /var/backups/xray-checker-manager

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 "$SERVICE_FILE"

if [[ ! -f "$STATE_DIR/interval_minutes" ]]; then printf '30\n' > "$STATE_DIR/interval_minutes"; chmod 600 "$STATE_DIR/interval_minutes"; fi
python3 - "$BOT_DST" <<'PYCODE'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
compile(p.read_text(encoding="utf-8"), str(p), "exec")
PYCODE

systemctl daemon-reload
systemctl enable --now xray-checker-bot.service
sleep 2
if systemctl is-active --quiet xray-checker-bot.service; then
  echo
  echo 'Installation successful: xray-checker-bot.service is active.'
else
  echo 'WARNING: service did not become active. Recent logs:' >&2
  journalctl -u xray-checker-bot.service -n 30 --no-pager >&2 || true
  exit 1
fi

echo
echo 'Notes:'
echo ' - BackPack was not read, modified, stopped, or restarted.'
echo ' - xray-checker.service was not restarted.'
echo ' - The existing reporting service/timer was not replaced.'
echo ' - Automatic-report settings require xray-checker-telegram.timer to already exist.'
echo ' - Backups may contain tokens/passwords; keep them private.'
echo ' - Dashboard exposure/authentication should be reviewed separately.'
echo ' - Logs: journalctl -u xray-checker-bot.service -f'
