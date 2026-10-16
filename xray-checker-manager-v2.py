#!/usr/bin/env python3
"""Xray Checker Bot manager and clean replacement installer.

Commands (run as root):
  python3 xray-checker-manager-v2.py backup
  python3 xray-checker-manager-v2.py install
  python3 xray-checker-manager-v2.py list-backups
  python3 xray-checker-manager-v2.py restore /path/to/backup.tar.gz

This script never restarts, stops, enables, disables, or edits BackPack.
It never restarts xray-checker.service automatically.
"""
import argparse
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

BOT = Path('/usr/local/bin/xray-checker-bot.py')
BACKUP_DIR = Path('/var/backups/xray-checker-manager')
INTERVAL_FILE = Path('/var/lib/xray-checker-telegram-bot/interval_minutes')
BACKUP_PATHS = [
    '/usr/local/bin/xray-checker-bot.py',
    '/usr/local/bin/xray-checker-telegram.py',
    '/etc/xray-checker-telegram.env',
    '/etc/xray-checker/xray-checker.env',
    '/etc/systemd/system/xray-checker-bot.service',
    '/etc/systemd/system/xray-checker-telegram.service',
    '/etc/systemd/system/xray-checker-telegram.timer',
    '/etc/systemd/system/xray-checker-telegram.timer.d/override.conf',
    '/var/lib/xray-checker-telegram-bot/interval_minutes',
]

BOT_SOURCE = r'''#!/usr/bin/env python3
import os
import re
import json
import ssl
import socket
import html
import base64
import time
import subprocess
import tempfile
import shutil
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from http.client import HTTPSConnection
from urllib.request import Request, urlopen

BOT_ENV = "/etc/xray-checker-telegram.env"
XRAY_ENV = "/etc/xray-checker/xray-checker.env"
METRICS_URL = "http://127.0.0.1:2112/metrics"
HOST = "api.telegram.org"
# Existing BackPack TCP relay; this bot only connects to this local endpoint.
RELAY = ("127.0.0.1", 58615)
STATE = Path("/var/lib/xray-checker-telegram-bot/offset")
INTERVAL_FILE = Path("/var/lib/xray-checker-telegram-bot/interval_minutes")
INTERVALS = (5, 10, 15, 30, 45, 60, 360, 720)
awaiting_sub_url = False
pending_sub_url = None


def read_env(path):
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip("'\"")
    return values


bot = read_env(BOT_ENV)
TOKEN = bot.get("TELEGRAM_BOT_TOKEN", "")
ADMIN = bot.get("TELEGRAM_CHAT_ID", "")
if not TOKEN or not ADMIN:
    raise SystemExit("Bot credentials missing from environment file")


class RelayHTTPS(HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(RELAY, timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def api(method, params=None, timeout=40):
    params = params or {}
    conn = RelayHTTPS(HOST, timeout=timeout, context=ssl.create_default_context())
    try:
        if method == "getUpdates":
            path = f"/bot{TOKEN}/{method}?" + urlencode(params)
            conn.request("GET", path)
        else:
            body = urlencode(params).encode()
            conn.request("POST", f"/bot{TOKEN}/{method}", body=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        raw = response.read().decode("utf-8", "replace")
        data = json.loads(raw)
        if response.status != 200 or not data.get("ok"):
            description = data.get("description", f"HTTP {response.status}")
            if "message is not modified" in description.lower():
                return data
            raise RuntimeError(f"Telegram API {method}: {description}")
        return data
    finally:
        conn.close()


def get_configs():
    env = read_env(XRAY_ENV)
    user, password = env.get("METRICS_USERNAME", ""), env.get("METRICS_PASSWORD", "")
    if not user or not password:
        raise RuntimeError("Metrics credentials are missing")
    req = Request(METRICS_URL)
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    with urlopen(req, timeout=10) as response:
        raw = response.read().decode("utf-8", "replace")
    items = {}
    pattern = re.compile(r'^(xray_proxy_status|xray_proxy_latency_ms)\{(.*?)\}\s+([-+0-9.eE]+)\s*$')
    for line in raw.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        metric, label_text, value = match.groups()
        labels = dict(re.findall(r'(\w+)="((?:\\.|[^"])*)"', label_text))
        name = labels.get("name") or labels.get("address") or "Unknown"
        key = labels.get("stable_id") or name
        item = items.setdefault(key, {"name": name, "status": 0, "latency": 0})
        if metric == "xray_proxy_status":
            item["status"] = float(value)
        else:
            item["latency"] = float(value)
    if not items:
        raise RuntimeError("No Xray Checker metrics found")
    return sorted(items.values(), key=lambda x: x["name"].casefold())


def report_text():
    configs = get_configs()
    active = [c for c in configs if c["status"] == 1]
    inactive = [c for c in configs if c["status"] != 1]
    lines = [
        "📡 <b>گزارش وضعیت Xray Checker</b>",
        f"🕒 <code>{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</code>",
        "",
        f"📊 <b>جمع‌بندی:</b> 🟢 {len(active)} فعال | 🔴 {len(inactive)} غیرفعال",
        "",
        "🟢 <b>کانفیگ‌های فعال</b>",
    ]
    if active:
        for c in active:
            ping = f"{c['latency']:.0f} ms" if c["latency"] > 0 else "پینگ معتبر موجود نیست"
            lines.append(f"• {html.escape(c['name'])} — <b>{ping}</b>")
    else:
        lines.append("• هیچ کانفیگ فعالی یافت نشد")
    lines += ["", "🔴 <b>کانفیگ‌های غیرفعال</b>"]
    if inactive:
        for c in inactive:
            lines.append(f"• {html.escape(c['name'])} — بدون اتصال معتبر")
    else:
        lines.append("• موردی نیست")
    return "\n".join(lines)


def timer_active():
    result = subprocess.run(["systemctl", "is-active", "xray-checker-telegram.timer"],
                            capture_output=True, text=True, timeout=10)
    return result.stdout.strip() == "active"


def keyboard(rows):
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def main_menu():
    return keyboard([
        [{"text": "📊 وضعیت کانفیگ‌ها", "callback_data": "status"},
         {"text": "🔄 گزارش فوری", "callback_data": "report"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "settings"},
         {"text": "❔ راهنما", "callback_data": "help"}],
        [{"text": "🔗 مدیریت لینک ساب", "callback_data": "subscription"}],
    ])


def format_interval(minutes):
    if minutes == 60:
        return "۱ ساعت"
    if minutes == 360:
        return "۶ ساعت"
    if minutes == 720:
        return "۱۲ ساعت"
    return f"{minutes} دقیقه"


def interval_minutes():
    try:
        value = int(INTERVAL_FILE.read_text().strip())
        return value if value in INTERVALS else 30
    except (OSError, ValueError):
        return 30


def set_interval(minutes):
    if minutes not in INTERVALS:
        raise ValueError("Unsupported interval")
    INTERVAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_state = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=str(INTERVAL_FILE.parent),
                                         prefix=".interval-", delete=False) as f:
            temp_state = f.name
            f.write(str(minutes) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_state, 0o600)
        os.replace(temp_state, INTERVAL_FILE)
        temp_state = None
    finally:
        if temp_state and os.path.exists(temp_state):
            os.unlink(temp_state)

    dropin = Path("/etc/systemd/system/xray-checker-telegram.timer.d/override.conf")
    dropin.parent.mkdir(parents=True, exist_ok=True)
    duration = f"{minutes // 60}h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes}min"
    content = "[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=" + duration + "\n"
    temp_dropin = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(dropin.parent),
                                         prefix=".timer-override-", delete=False) as f:
            temp_dropin = f.name
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_dropin, 0o600)
        os.replace(temp_dropin, dropin)
        temp_dropin = None
    finally:
        if temp_dropin and os.path.exists(temp_dropin):
            os.unlink(temp_dropin)

    subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True, text=True, timeout=15)
    # Only the report timer may be restarted here. BackPack is never referenced or touched.
    if timer_active():
        subprocess.run(["systemctl", "restart", "xray-checker-telegram.timer"],
                       check=True, capture_output=True, text=True, timeout=20)


def interval_menu():
    current = interval_minutes()
    rows, row = [], []
    for minutes in INTERVALS:
        label = f"{'✓ ' if minutes == current else ''}{format_interval(minutes)}"
        row.append({"text": label, "callback_data": f"interval_{minutes}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⬅️ بازگشت به تنظیمات", "callback_data": "settings"}])
    return keyboard(rows)


def settings_menu():
    state = timer_active()
    toggle = "⏸ خاموش‌کردن گزارش خودکار" if state else "▶️ روشن‌کردن گزارش خودکار"
    return keyboard([
        [{"text": toggle, "callback_data": "toggle_auto"}],
        [{"text": f"⏱ بازهٔ گزارش: هر {format_interval(interval_minutes())}", "callback_data": "intervals"}],
        [{"text": "🔄 تازه‌سازی وضعیت", "callback_data": "settings"}],
        [{"text": "⬅️ منوی اصلی", "callback_data": "menu"}],
    ])


def settings_text():
    state = "🟢 روشن" if timer_active() else "🔴 خاموش"
    return ("⚙️ <b>تنظیمات گزارش‌دهی</b>\n\n"
            f"گزارش خودکار: <b>{state}</b>\n"
            f"بازهٔ فعلی: <b>هر {format_interval(interval_minutes())}</b>\n\n"
            "خاموش‌کردن گزارش خودکار فقط ارسال زمان‌بندی‌شده را متوقف می‌کند؛ "
            "بات، مانیتورینگ و تونل‌ها روشن می‌مانند.")


def send_message(chat_id, text, markup=None):
    args = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    if markup:
        args["reply_markup"] = markup
    return api("sendMessage", args)


def edit_message(chat_id, message_id, text, markup=None):
    args = {"chat_id": str(chat_id), "message_id": str(message_id), "text": text, "parse_mode": "HTML"}
    if markup:
        args["reply_markup"] = markup
    return api("editMessageText", args)


def show(chat_id, text, markup=None, message=None):
    if message:
        edit_message(chat_id, message["message_id"], text, markup)
    else:
        send_message(chat_id, text, markup)


def valid_subscription_url(value):
    if not value or len(value) > 4096:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    if '"' in value or "\\" in value or "'" in value or "`" in value:
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        _ = parsed.port
        return True
    except ValueError:
        return False


def set_subscription_url(value):
    if not valid_subscription_url(value):
        raise ValueError("Invalid subscription URL")
    path = Path(XRAY_ENV)
    if not path.is_file():
        raise RuntimeError("Xray Checker env file not found")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    matches = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if re.match(r"^\s*(?:export\s+)?SUBSCRIPTION_URL\s*=", body):
            matches.append((index, body, line[len(body):]))
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one SUBSCRIPTION_URL entry")
    index, body, ending = matches[0]
    prefix = re.match(r"^(\s*(?:export\s+)?SUBSCRIPTION_URL\s*=)", body).group(1)
    lines[index] = prefix + '"' + value + '"' + ending
    st = path.stat()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
    backup = path.with_name(path.name + ".bak." + stamp)
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(path.parent),
                                         prefix=".xray-checker-env-", delete=False) as handle:
            temp_path = handle.name
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp_path, st.st_uid, st.st_gid)
        os.chmod(temp_path, stat.S_IMODE(st.st_mode))
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    return backup.name


def handle(update):
    global awaiting_sub_url, pending_sub_url
    callback = update.get("callback_query")
    if callback:
        api("answerCallbackQuery", {"callback_query_id": callback["id"]})
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if chat_id != ADMIN:
            return
        message_ref = message if message.get("message_id") else None
        action = callback.get("data", "")
        if action == "subscription":
            awaiting_sub_url, pending_sub_url = True, None
            show(chat_id, "🔗 <b>مدیریت لینک ساب</b>\n\nلینک جدید را ارسال کن. تا زمان تأیید، فایل تنظیمات تغییر نمی‌کند.\nبرای امنیت، لینک در پیام تأیید نمایش داده نمی‌شود.",
                 keyboard([[{"text": "❌ لغو", "callback_data": "sub_cancel"}], [{"text": "⬅️ منوی اصلی", "callback_data": "menu"}]]), message_ref)
        elif action == "sub_confirm":
            if not pending_sub_url:
                awaiting_sub_url = False
                show(chat_id, "⌛ درخواست منقضی شده؛ دوباره از منوی مدیریت لینک ساب شروع کن.", main_menu(), message_ref)
            else:
                try:
                    backup_name = set_subscription_url(pending_sub_url)
                    pending_sub_url, awaiting_sub_url = None, False
                    show(chat_id, "✅ <b>لینک ساب در فایل تنظیمات ذخیره شد.</b>\n"
                         f"📦 نسخهٔ پشتیبان: <code>{html.escape(backup_name)}</code>\n\n"
                         "برای اعمال تنظیم جدید، دکمهٔ Restart را جداگانه بزن.",
                         keyboard([[{"text": "🔄 راه‌اندازی مجدد Xray Checker", "callback_data": "sub_restart"}],
                                   [{"text": "⬅️ منوی اصلی", "callback_data": "menu"}]]), message_ref)
                except Exception as exc:
                    pending_sub_url, awaiting_sub_url = None, False
                    print(f"Subscription update failed: {type(exc).__name__}", flush=True)
                    show(chat_id, "⚠️ ذخیره لینک انجام نشد. فایل تنظیمات و گزارش سرویس را بررسی کن.", main_menu(), message_ref)
        elif action == "sub_cancel":
            awaiting_sub_url, pending_sub_url = False, None
            show(chat_id, "❌ عملیات تغییر لینک لغو شد.", main_menu(), message_ref)
        elif action == "sub_restart":
            try:
                loaded = subprocess.run(["systemctl", "show", "xray-checker.service", "-p", "LoadState", "--value"],
                                        capture_output=True, text=True, timeout=10)
                if loaded.returncode != 0 or loaded.stdout.strip() != "loaded":
                    msg = "⚠️ سرویس xray-checker.service در systemd قابل دسترسی نیست."
                else:
                    result = subprocess.run(["systemctl", "restart", "xray-checker.service"],
                                            capture_output=True, text=True, timeout=45)
                    check = subprocess.run(["systemctl", "is-active", "xray-checker.service"],
                                           capture_output=True, text=True, timeout=10)
                    msg = ("✅ سرویس Xray Checker راه‌اندازی شد." if result.returncode == 0 and check.stdout.strip() == "active"
                           else "⚠️ فعال‌شدن سرویس پس از Restart تأیید نشد؛ وضعیت سرویس را بررسی کن.")
            except Exception:
                msg = "⚠️ بررسی یا راه‌اندازی مجدد سرویس ناموفق بود؛ وضعیت سرویس را دستی بررسی کن."
            show(chat_id, msg, main_menu(), message_ref)
        elif action == "menu":
            awaiting_sub_url, pending_sub_url = False, None
            show(chat_id, "📡 <b>Xray Checker Bot</b>\nیک گزینه را انتخاب کن:", main_menu(), message_ref)
        elif action in ("status", "report"):
            awaiting_sub_url, pending_sub_url = False, None
            try:
                show(chat_id, report_text(), main_menu(), message_ref)
            except Exception as exc:
                show(chat_id, f"⚠️ دریافت گزارش ناموفق بود: <code>{html.escape(str(exc))}</code>", main_menu(), message_ref)
        elif action == "settings":
            awaiting_sub_url, pending_sub_url = False, None
            show(chat_id, settings_text(), settings_menu(), message_ref)
        elif action == "intervals":
            show(chat_id, "⏱ <b>انتخاب بازهٔ گزارش خودکار</b>\n\n"
                 f"بازهٔ فعلی: هر {format_interval(interval_minutes())}", interval_menu(), message_ref)
        elif action.startswith("interval_"):
            try:
                minutes = int(action.split("_", 1)[1])
                set_interval(minutes)
                show(chat_id, f"✅ بازهٔ گزارش خودکار روی <b>هر {format_interval(minutes)}</b> تنظیم شد.\n\n" + settings_text(),
                     settings_menu(), message_ref)
            except Exception as exc:
                show(chat_id, "⚠️ تغییر بازه انجام نشد: <code>" + html.escape(str(exc)) + "</code>", settings_menu(), message_ref)
        elif action == "toggle_auto":
            if timer_active():
                subprocess.run(["systemctl", "disable", "--now", "xray-checker-telegram.timer"], check=True, capture_output=True, text=True, timeout=20)
            else:
                subprocess.run(["systemctl", "enable", "--now", "xray-checker-telegram.timer"], check=True, capture_output=True, text=True, timeout=20)
            show(chat_id, settings_text(), settings_menu(), message_ref)
        elif action == "help":
            awaiting_sub_url, pending_sub_url = False, None
            show(chat_id, "❔ <b>راهنما</b>\n/start — منوی اصلی\n/status — وضعیت کانفیگ‌ها\n/report — ارسال گزارش فوری\n/settings — تنظیمات گزارش خودکار",
                 main_menu(), message_ref)
        return

    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if chat_id != ADMIN:
        return
    incoming = (message.get("text") or "").strip()
    command = incoming.split("@", 1)[0].lower()
    if awaiting_sub_url and incoming and not incoming.startswith("/"):
        awaiting_sub_url = False
        if not valid_subscription_url(incoming):
            pending_sub_url = None
            send_message(chat_id, "⚠️ لینک معتبر نیست. آدرس کامل HTTP یا HTTPS بدون فاصله و اطلاعات ورود بفرست؛ سپس دوباره از منوی مدیریت ساب شروع کن.", main_menu())
            return
        pending_sub_url = incoming
        send_message(chat_id, "🔐 <b>تأیید تغییر لینک ساب</b>\n\nلینک از نظر ساختار اولیه قابل قبول است. برای ذخیره تأیید کن؛ هنوز فایلی تغییر نکرده است.",
                     keyboard([[{"text": "✅ تأیید و ذخیره", "callback_data": "sub_confirm"}], [{"text": "❌ لغو", "callback_data": "sub_cancel"}]]))
        return
    if command in ("/start", "/menu"):
        awaiting_sub_url, pending_sub_url = False, None
        show(chat_id, "📡 <b>Xray Checker Bot</b>\nخوش آمدی! از منوی زیر استفاده کن:", main_menu())
    elif command in ("/status", "/report"):
        awaiting_sub_url, pending_sub_url = False, None
        try:
            show(chat_id, report_text(), main_menu())
        except Exception as exc:
            show(chat_id, f"⚠️ دریافت گزارش ناموفق بود: <code>{html.escape(str(exc))}</code>", main_menu())
    elif command == "/settings":
        awaiting_sub_url, pending_sub_url = False, None
        show(chat_id, settings_text(), settings_menu())
    elif command == "/help":
        awaiting_sub_url, pending_sub_url = False, None
        show(chat_id, "❔ <b>راهنما</b>\n/start — منوی اصلی\n/status — وضعیت کانفیگ‌ها\n/report — ارسال گزارش فوری\n/settings — تنظیمات گزارش خودکار", main_menu())


def load_offset():
    try:
        return int(STATE.read_text().strip())
    except (OSError, ValueError):
        return 0


def save_offset(value):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=str(STATE.parent), prefix=".offset-", delete=False) as f:
            temp_path = f.name
            f.write(str(value))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, STATE)
        temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def main():
    offset = load_offset()
    print("Interactive Xray Checker bot started", flush=True)
    while True:
        try:
            result = api("getUpdates", {"offset": offset, "timeout": 25,
                                         "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=40)
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle(update)
                except Exception as exc:
                    print(f"Update handling failed: {type(exc).__name__}: {exc}", flush=True)
                finally:
                    save_offset(offset)
        except Exception as exc:
            print(f"Polling error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
'''


def require_root():
    if os.geteuid() != 0:
        raise SystemExit('ERROR: run as root, for example: sudo python3 xray-checker-manager-v2.py install')


def timestamp():
    return time.strftime('%Y%m%d-%H%M%S', time.gmtime())


def ensure_backup_dir():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)


def backup_files(label='manual'):
    ensure_backup_dir()
    archive = BACKUP_DIR / f'xray-checker-{label}-{timestamp()}.tar.gz'
    included = []
    with tarfile.open(archive, 'w:gz') as tar:
        for raw in BACKUP_PATHS:
            p = Path(raw)
            if p.is_file():
                tar.add(str(p), arcname=raw.lstrip('/'), recursive=False)
                included.append(raw)
        manifest = ('Xray Checker Bot backup\n'
                    f'Created UTC: {time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}\n'
                    'BackPack files are intentionally excluded and never modified.\n'
                    'Included files:\n' + ''.join(f'- {p}\n' for p in included)).encode()
        info = tarfile.TarInfo('MANIFEST.txt')
        info.size = len(manifest)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(manifest))
    os.chmod(archive, 0o600)
    print(f'Backup created: {archive}')
    print(f'Included {len(included)} files.')
    print('WARNING: backup may contain Telegram tokens, passwords, and private configuration. Keep it private.')
    return archive


def list_backups():
    ensure_backup_dir()
    files = sorted(BACKUP_DIR.glob('xray-checker-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f'No backups found in {BACKUP_DIR}')
    for p in files:
        print(f'{p} ({p.stat().st_size} bytes)')


def install():
    require_root()
    # Validate embedded source before touching the installed bot.
    compile(BOT_SOURCE, str(BOT), 'exec')
    backup_files('pre-install-v2')
    BOT.parent.mkdir(parents=True, exist_ok=True)
    old_stat = BOT.stat() if BOT.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix='.xray-checker-bot-v2-', dir=str(BOT.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(BOT_SOURCE)
            f.flush()
            os.fsync(f.fileno())
        if old_stat:
            os.chown(temp_name, old_stat.st_uid, old_stat.st_gid)
            os.chmod(temp_name, stat.S_IMODE(old_stat.st_mode))
        else:
            os.chmod(temp_name, 0o755)
        os.replace(temp_name, BOT)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    subprocess.run(['/usr/bin/python3', '-m', 'py_compile', str(BOT)], check=True, capture_output=True, text=True)
    # Ensure interval state is one of the supported values; preserve valid existing setting.
    INTERVAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = int(INTERVAL_FILE.read_text().strip())
    except (OSError, ValueError):
        current = 30
    if current not in (5, 10, 15, 30, 45, 60, 360, 720):
        current = 30
    INTERVAL_FILE.write_text(f'{current}\n', encoding='ascii')
    os.chmod(INTERVAL_FILE, 0o600)
    # Keep the persisted choice and the systemd timer override in sync.
    dropin = Path('/etc/systemd/system/xray-checker-telegram.timer.d/override.conf')
    dropin.parent.mkdir(parents=True, exist_ok=True)
    duration = f'{current // 60}h' if current >= 60 and current % 60 == 0 else f'{current}min'
    dropin.write_text('[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=' + duration + '\n', encoding='utf-8')
    os.chmod(dropin, 0o600)
    subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=True, capture_output=True, text=True)
    # If active, refresh only the report timer so the selected interval takes effect.
    timer_state = subprocess.run(['/usr/bin/systemctl', 'is-active', 'xray-checker-telegram.timer'],
                                 capture_output=True, text=True)
    if timer_state.stdout.strip() == 'active':
        subprocess.run(['/usr/bin/systemctl', 'restart', 'xray-checker-telegram.timer'],
                       check=True, capture_output=True, text=True)
    print(f'Installed replacement bot source to {BOT}')
    print('Python syntax check passed.')
    print('Only xray-checker-telegram.timer may have been refreshed to apply its reporting interval.')
    print('Bot service has NOT been restarted. xray-checker.service has NOT been restarted.')
    print('BackPack was not read, modified, stopped, or restarted.')
    print('When you are ready, manually restart only the bot with: sudo systemctl restart xray-checker-bot.service')


def restore(archive_path):
    require_root()
    archive = Path(archive_path).resolve()
    if not archive.is_file() or not tarfile.is_tarfile(archive):
        raise SystemExit('ERROR: invalid backup archive path.')
    print('Restore overwrites only files included in this backup. BackPack files are excluded.')
    print('No service will be restarted by restore.')
    if input('Type RESTORE to continue: ').strip() != 'RESTORE':
        print('Restore cancelled.')
        return
    backup_files('pre-restore')
    allowed = {p.lstrip('/') for p in BACKUP_PATHS}
    with tarfile.open(archive, 'r:gz') as tar:
        members = [m for m in tar.getmembers() if m.name != 'MANIFEST.txt']
        for m in members:
            p = Path(m.name)
            if p.is_absolute() or '..' in p.parts or m.name not in allowed or not m.isfile():
                raise SystemExit(f'ERROR: unsafe/unexpected archive member: {m.name}')
        for m in members:
            target = Path('/') / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(m)
            if source is None:
                raise SystemExit(f'ERROR: cannot read {m.name}')
            fd, temp_name = tempfile.mkstemp(prefix='.xray-restore-', dir=str(target.parent))
            try:
                with os.fdopen(fd, 'wb') as out:
                    shutil.copyfileobj(source, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.chmod(temp_name, m.mode & 0o777)
                try:
                    os.chown(temp_name, m.uid, m.gid)
                except PermissionError:
                    pass
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            print(f'Restored {target}')
    print('Restore complete. No service restart was performed.')


def main():
    parser = argparse.ArgumentParser(description='Xray Checker Bot clean installer, backup and restore manager')
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('backup', help='Create a private backup archive')
    subs.add_parser('install', help='Back up then install the full replacement bot')
    subs.add_parser('list-backups', help='List saved backup archives')
    r = subs.add_parser('restore', help='Restore an archive after confirmation')
    r.add_argument('archive', help='Full path to backup archive')
    args = parser.parse_args()
    require_root()
    if args.command == 'backup':
        backup_files('manual-v2')
    elif args.command == 'install':
        install()
    elif args.command == 'list-backups':
        list_backups()
    elif args.command == 'restore':
        restore(args.archive)


if __name__ == '__main__':
    main()
