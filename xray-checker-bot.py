#!/usr/bin/env python3
"""Xray Checker Telegram Bot. Standard-library only; see install.sh for setup."""
import base64
import html
import io
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import tarfile
import tempfile
import time
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

BOT_ENV = Path(os.environ.get("BOT_ENV", "/etc/xray-checker-telegram.env"))
XRAY_ENV = Path(os.environ.get("XRAY_ENV", "/etc/xray-checker/xray-checker.env"))
METRICS_URL = os.environ.get("METRICS_URL", "http://127.0.0.1:2112/metrics")
STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/xray-checker-telegram-bot"))
STATE = STATE_DIR / "offset"
INTERVAL_FILE = STATE_DIR / "interval_minutes"
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/var/backups/xray-checker-manager"))
DASHBOARD_URL_DEFAULT = "http://45.139.11.96:2112/"
INTERVALS = (5, 10, 15, 30, 45, 60, 360, 720)
BACKUP_PATHS = [
    "/usr/local/bin/xray-checker-bot.py",
    "/usr/local/bin/xray-checker-telegram.py",
    "/etc/xray-checker-telegram.env",
    "/etc/xray-checker/xray-checker.env",
    "/etc/systemd/system/xray-checker-bot.service",
    "/etc/systemd/system/xray-checker-telegram.service",
    "/etc/systemd/system/xray-checker-telegram.timer",
    "/etc/systemd/system/xray-checker-telegram.timer.d/override.conf",
    "/var/lib/xray-checker-telegram-bot/interval_minutes",
]
awaiting_sub_url = False
pending_sub_url = None
pending_restore_name = None
backup_choices = {}


def read_env(path):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


_cfg = read_env(BOT_ENV)
TOKEN = _cfg.get("TELEGRAM_BOT_TOKEN", "")
ADMIN = _cfg.get("TELEGRAM_CHAT_ID", "")
RELAY_HOST = _cfg.get("TELEGRAM_RELAY_HOST", "127.0.0.1")
try:
    RELAY_PORT = int(_cfg.get("TELEGRAM_RELAY_PORT", "58615"))
except ValueError:
    RELAY_PORT = 58615
DASHBOARD_URL = _cfg.get("DASHBOARD_URL", DASHBOARD_URL_DEFAULT)
if not TOKEN or not ADMIN:
    raise SystemExit(f"Telegram credentials missing in {BOT_ENV}")


class RelayHTTPS(HTTPSConnection):
    """TLS to Telegram through the configured local TCP relay, preserving Telegram SNI."""
    def connect(self):
        raw = socket.create_connection((RELAY_HOST, RELAY_PORT), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def api(method, params=None, timeout=40):
    params = params or {}
    conn = RelayHTTPS("api.telegram.org", timeout=timeout, context=ssl.create_default_context())
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
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"Telegram API returned HTTP {response.status} with invalid JSON")
        if response.status != 200 or not data.get("ok"):
            description = data.get("description", f"HTTP {response.status}")
            if "message is not modified" in description.lower():
                return data
            raise RuntimeError(f"Telegram API {method}: {description}")
        return data
    finally:
        conn.close()


def send_document(chat_id, path, caption=None):
    """Upload a private backup archive to the configured admin chat."""
    boundary = "----XrayCheckerBoundary" + str(time.time_ns())
    fields = [("chat_id", str(chat_id))]
    if caption:
        fields.append(("caption", caption[:900]))
    chunks = []
    for key, value in fields:
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode())
    filename = Path(path).name
    chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\nContent-Type: application/gzip\r\n\r\n").encode())
    with open(path, "rb") as f:
        chunks.append(f.read())
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    conn = RelayHTTPS("api.telegram.org", timeout=90, context=ssl.create_default_context())
    try:
        conn.request("POST", f"/bot{TOKEN}/sendDocument", body=body,
                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                              "Content-Length": str(len(body))})
        response = conn.getresponse()
        raw = response.read().decode("utf-8", "replace")
        data = json.loads(raw)
        if response.status != 200 or not data.get("ok"):
            raise RuntimeError(data.get("description", f"HTTP {response.status}"))
        return data
    finally:
        conn.close()


def get_configs():
    env = read_env(XRAY_ENV)
    user, password = env.get("METRICS_USERNAME", ""), env.get("METRICS_PASSWORD", "")
    if not user or not password:
        raise RuntimeError("Metrics credentials missing in Xray Checker env file")
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
        item = items.setdefault(key, {"name": name, "status": 0.0, "latency": 0.0})
        if metric == "xray_proxy_status":
            item["status"] = float(value)
        else:
            item["latency"] = float(value)
    if not items:
        raise RuntimeError("No xray_proxy_status/xray_proxy_latency_ms metrics found")
    return sorted(items.values(), key=lambda item: item["name"].casefold())


def report_text():
    configs = get_configs()
    active = [item for item in configs if item["status"] == 1]
    inactive = [item for item in configs if item["status"] != 1]
    lines = ["📡 <b>گزارش وضعیت Xray Checker</b>",
             f"🕒 <code>{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</code>", "",
             f"📊 <b>جمع‌بندی:</b> 🟢 {len(active)} فعال | 🔴 {len(inactive)} غیرفعال", "",
             "🟢 <b>کانفیگ‌های فعال</b>"]
    for item in active:
        ping = f"{item['latency']:.0f} ms" if item["latency"] > 0 else "پینگ معتبر موجود نیست"
        lines.append(f"• {html.escape(item['name'])} — <b>{ping}</b>")
    if not active:
        lines.append("• هیچ کانفیگ فعالی یافت نشد")
    lines += ["", "🔴 <b>کانفیگ‌های غیرفعال</b>"]
    lines.extend([f"• {html.escape(item['name'])} — بدون اتصال معتبر" for item in inactive] or ["• موردی نیست"])
    return "\n".join(lines)


def run_systemctl(*args, timeout=20, check=False):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout, check=check)


def unit_exists(unit):
    result = run_systemctl("show", unit, "-p", "LoadState", "--value")
    return result.returncode == 0 and result.stdout.strip() == "loaded"


def timer_active():
    if not unit_exists("xray-checker-telegram.timer"):
        return False
    result = run_systemctl("is-active", "xray-checker-telegram.timer")
    return result.stdout.strip() == "active"


def keyboard(rows):
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def main_menu():
    rows = [
        [{"text": "📊 وضعیت کانفیگ‌ها", "callback_data": "status"}, {"text": "🔄 گزارش فوری", "callback_data": "report"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "settings"}, {"text": "❔ راهنما", "callback_data": "help"}],
        [{"text": "🔗 مدیریت لینک ساب", "callback_data": "subscription"}],
        [{"text": "📦 بکاپ و ریستور", "callback_data": "backup_menu"}, {"text": "♻️ ری‌استارت ربات", "callback_data": "bot_restart_confirm"}],
    ]
    if DASHBOARD_URL and valid_dashboard_url(DASHBOARD_URL):
        rows.append([{"text": "🌐 بازکردن داشبورد Metrics", "url": DASHBOARD_URL}])
    return keyboard(rows)


def valid_dashboard_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def format_interval(minutes):
    return {60: "۱ ساعت", 360: "۶ ساعت", 720: "۱۲ ساعت"}.get(minutes, f"{minutes} دقیقه")


def interval_minutes():
    try:
        value = int(INTERVAL_FILE.read_text().strip())
        return value if value in INTERVALS else 30
    except (OSError, ValueError):
        return 30


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(path.parent), prefix=".tmp-", delete=False) as f:
            temp_path = f.name
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def set_interval(minutes):
    if minutes not in INTERVALS:
        raise ValueError("Unsupported interval")
    atomic_write(INTERVAL_FILE, f"{minutes}\n")
    dropin = Path("/etc/systemd/system/xray-checker-telegram.timer.d/override.conf")
    if not unit_exists("xray-checker-telegram.timer") and not Path("/etc/systemd/system/xray-checker-telegram.timer").exists():
        raise RuntimeError("xray-checker-telegram.timer is not installed; interval saved but timer unit is unavailable")
    duration = f"{minutes // 60}h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes}min"
    atomic_write(dropin, "[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=" + duration + "\n")
    run_systemctl("daemon-reload", check=True)
    if timer_active():
        run_systemctl("restart", "xray-checker-telegram.timer", check=True)


def interval_menu():
    current, rows, row = interval_minutes(), [], []
    for minutes in INTERVALS:
        row.append({"text": f"{'✓ ' if minutes == current else ''}{format_interval(minutes)}", "callback_data": f"interval_{minutes}"})
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⬅️ بازگشت به تنظیمات", "callback_data": "settings"}])
    return keyboard(rows)


def settings_menu():
    active = timer_active()
    toggle = "⏸ خاموش‌کردن گزارش خودکار" if active else "▶️ روشن‌کردن گزارش خودکار"
    return keyboard([[{"text": toggle, "callback_data": "toggle_auto"}],
                     [{"text": f"⏱ بازهٔ گزارش: هر {format_interval(interval_minutes())}", "callback_data": "intervals"}],
                     [{"text": "🔄 تازه‌سازی وضعیت", "callback_data": "settings"}],
                     [{"text": "⬅️ منوی اصلی", "callback_data": "menu"}]])


def settings_text():
    state = "🟢 روشن" if timer_active() else "🔴 خاموش / تایمر نصب نیست"
    return ("⚙️ <b>تنظیمات گزارش‌دهی</b>\n\n"
            f"گزارش خودکار: <b>{state}</b>\n"
            f"بازهٔ فعلی: <b>هر {format_interval(interval_minutes())}</b>\n\n"
            "خاموش‌کردن گزارش خودکار فقط تایمر گزارش را متوقف می‌کند؛ این ربات و سرویس‌های دیگر دست‌نخورده می‌مانند.")


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
    if message and message.get("message_id"):
        try:
            edit_message(chat_id, message["message_id"], text, markup)
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
    send_message(chat_id, text, markup)


def valid_subscription_url(value):
    if not value or len(value) > 4096 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    if any(ch in value for ch in ('"', "'", "\\", "`")):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return False
        _ = parsed.port
        return True
    except ValueError:
        return False


def set_subscription_url(value):
    if not valid_subscription_url(value):
        raise ValueError("Invalid subscription URL")
    if not XRAY_ENV.is_file():
        raise RuntimeError("Xray Checker env file not found")
    lines = XRAY_ENV.read_text(encoding="utf-8").splitlines(keepends=True)
    matches = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if re.match(r"^\s*(?:export\s+)?SUBSCRIPTION_URL\s*=", body):
            matches.append((index, body, line[len(body):]))
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one SUBSCRIPTION_URL entry in Xray Checker env file")
    index, body, ending = matches[0]
    prefix = re.match(r"^(\s*(?:export\s+)?SUBSCRIPTION_URL\s*=)", body).group(1)
    st = XRAY_ENV.stat()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"
    backup = XRAY_ENV.with_name(XRAY_ENV.name + ".bak." + stamp)
    shutil.copy2(XRAY_ENV, backup); os.chmod(backup, 0o600)
    lines[index] = prefix + '"' + value + '"' + ending
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(XRAY_ENV.parent), prefix=".xray-env-", delete=False) as f:
            temp_path = f.name; f.writelines(lines); f.flush(); os.fsync(f.fileno())
        os.chown(temp_path, st.st_uid, st.st_gid); os.chmod(temp_path, stat.S_IMODE(st.st_mode)); os.replace(temp_path, XRAY_ENV); temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path): os.unlink(temp_path)
    return backup.name


def backup_timestamp():
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000:06d}"


def prune_backups(keep=8):
    files = sorted(BACKUP_DIR.glob("xray-checker-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try: old.unlink()
        except OSError as exc: print(f"Could not prune backup {old.name}: {exc}", flush=True)


def create_backup(label="telegram"):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True); os.chmod(BACKUP_DIR, 0o700)
    archive = BACKUP_DIR / f"xray-checker-{label}-{backup_timestamp()}.tar.gz"
    included = []
    with tarfile.open(archive, "w:gz") as tar:
        for raw in BACKUP_PATHS:
            path = Path(raw)
            if path.is_file():
                tar.add(str(path), arcname=raw.lstrip("/"), recursive=False); included.append(raw)
        manifest = ("Xray Checker Bot backup\n" + f"Created UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}\n"
                    "BackPack files are intentionally excluded and never modified.\nIncluded files:\n" + "".join(f"- {p}\n" for p in included)).encode()
        info = tarfile.TarInfo("MANIFEST.txt"); info.size = len(manifest); info.mode = 0o600; tar.addfile(info, io.BytesIO(manifest))
    os.chmod(archive, 0o600); prune_backups(8)
    return archive


def available_backups():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True); os.chmod(BACKUP_DIR, 0o700)
    return sorted(BACKUP_DIR.glob("xray-checker-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)


def restore_backup(filename):
    if not re.fullmatch(r"xray-checker-[A-Za-z0-9_-]+-[0-9]{8}-[0-9]{6}-[0-9]{6}\.tar\.gz", filename):
        raise ValueError("Invalid backup filename")
    archive = (BACKUP_DIR / filename).resolve()
    if archive.parent != BACKUP_DIR.resolve() or not archive.is_file() or not tarfile.is_tarfile(archive):
        raise ValueError("Backup not found or invalid")
    allowed = {item.lstrip("/") for item in BACKUP_PATHS}
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name != "MANIFEST.txt"]
        if not members: raise ValueError("Backup contains no files")
        total = 0
        for member in members:
            p = Path(member.name); total += max(member.size, 0)
            if p.is_absolute() or ".." in p.parts or member.name not in allowed or not member.isfile() or member.size > 100_000_000:
                raise ValueError("Unsafe or unsupported file in backup")
        if total > 250_000_000: raise ValueError("Backup exceeds size safety limit")
    create_backup("pre-restore")
    restored = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "MANIFEST.txt": continue
            target = Path("/") / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None: raise ValueError("Could not read backup member")
            fd, temp_name = tempfile.mkstemp(prefix=".xray-restore-", dir=str(target.parent))
            try:
                with os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(source, output); output.flush(); os.fsync(output.fileno())
                os.chmod(temp_name, member.mode & 0o777)
                try: os.chown(temp_name, member.uid, member.gid)
                except (PermissionError, OSError): pass
                os.replace(temp_name, target); restored.append(str(target))
            finally:
                if os.path.exists(temp_name): os.unlink(temp_name)
    return restored


def backup_menu_text():
    return ("📦 <b>مدیریت بکاپ و ریستور</b>\n\nبکاپ شامل سورس ربات، تنظیمات مجاز و فایل‌های سرویس/تایمر است.\n"
            "فایل‌های BackPack عمداً خارج از بکاپ و ریستور هستند.\nآرشیو ممکن است شامل توکن و رمز باشد؛ آن را خصوصی نگه دار.\n\n"
            "ریستور فایل‌های داخل بکاپ را جایگزین می‌کند؛ پیش از آن از وضعیت فعلی بکاپ گرفته می‌شود و هیچ سرویسی ری‌استارت نمی‌شود.")


def backup_menu_keyboard():
    return keyboard([[{"text": "📦 ساخت و ارسال بکاپ به تلگرام", "callback_data": "backup_create"}],
                     [{"text": "🗂 فهرست بکاپ‌ها", "callback_data": "backup_list"}],
                     [{"text": "⬅️ منوی اصلی", "callback_data": "menu"}]])


def restart_bot_later():
    # Delay restart so Telegram receives the callback response first.
    result = subprocess.run(["systemd-run", "--quiet", "--on-active=3s", "/bin/systemctl", "restart", "xray-checker-bot.service"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError("Could not schedule bot restart")


def handle(update):
    global awaiting_sub_url, pending_sub_url, pending_restore_name, backup_choices
    callback = update.get("callback_query")
    if callback:
        try: api("answerCallbackQuery", {"callback_query_id": callback["id"]})
        except Exception: pass
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if chat_id != ADMIN: return
        message_ref = message if message.get("message_id") else None
        action = callback.get("data", "")
        if action == "backup_menu":
            show(chat_id, backup_menu_text(), backup_menu_keyboard(), message_ref)
        elif action == "backup_create":
            try:
                archive = create_backup("telegram")
                send_document(chat_id, archive, "Xray Checker backup. Contains private configuration; keep it secure.")
                show(chat_id, f"✅ بکاپ ساخته و به همین گفت‌وگو ارسال شد.\n<code>{html.escape(archive.name)}</code>\nحجم: {archive.stat().st_size} بایت", backup_menu_keyboard(), message_ref)
            except Exception as exc:
                print(f"Backup creation/send failed: {type(exc).__name__}: {exc}", flush=True)
                show(chat_id, "⚠️ ساخت یا ارسال بکاپ ناموفق بود. لاگ سرویس ربات را بررسی کن.", backup_menu_keyboard(), message_ref)
        elif action == "backup_list":
            files = available_backups()[:8]
            if not files: show(chat_id, "🗂 هنوز بکاپی پیدا نشد.", backup_menu_keyboard(), message_ref)
            else:
                rows = []; backup_choices = {str(i): p.name for i, p in enumerate(files)}
                for i, item in enumerate(files):
                    date_text = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(item.stat().st_mtime))
                    rows.append([{"text": f"♻️ {date_text} · {item.stat().st_size // 1024} KB", "callback_data": f"backup_pick_{i}"}])
                rows.append([{"text": "⬅️ بازگشت", "callback_data": "backup_menu"}])
                show(chat_id, "🗂 <b>بکاپ‌های اخیر</b>\nبرای انتخاب نسخه جهت ریستور، روی آن بزن:", keyboard(rows), message_ref)
        elif action.startswith("backup_pick_"):
            filename = backup_choices.get(action[len("backup_pick_"):])
            if not filename or filename not in {p.name for p in available_backups()}:
                show(chat_id, "⚠️ این انتخاب منقضی شده؛ فهرست بکاپ‌ها را دوباره باز کن.", backup_menu_keyboard(), message_ref)
            else:
                pending_restore_name = filename
                show(chat_id, "⚠️ <b>تأیید ریستور</b>\n\n" + f"نسخه: <code>{html.escape(filename)}</code>\n\n"
                     "فایل‌های داخل بکاپ جایگزین می‌شوند. ابتدا بکاپ وضعیت فعلی ساخته می‌شود. هیچ سرویسی ری‌استارت نمی‌شود و BackPack دست‌نخورده می‌ماند.\n\nادامه می‌دهی؟",
                     keyboard([[{"text": "♻️ بله، ریستور کن", "callback_data": "backup_restore_yes"}], [{"text": "❌ لغو", "callback_data": "backup_restore_no"}]]), message_ref)
        elif action == "backup_restore_no":
            pending_restore_name = None; show(chat_id, "❌ ریستور لغو شد.", backup_menu_keyboard(), message_ref)
        elif action == "backup_restore_yes":
            filename = pending_restore_name; pending_restore_name = None
            if not filename: show(chat_id, "⌛ انتخاب بکاپ منقضی شده؛ دوباره انتخاب کن.", backup_menu_keyboard(), message_ref)
            else:
                try:
                    restored = restore_backup(filename)
                    show(chat_id, f"✅ ریستور انجام شد. تعداد فایل‌های بازیابی‌شده: {len(restored)}\nهیچ سرویسی ری‌استارت نشد.", backup_menu_keyboard(), message_ref)
                except Exception as exc:
                    print(f"Restore failed: {type(exc).__name__}: {exc}", flush=True)
                    show(chat_id, "⚠️ ریستور ناموفق بود. لاگ ربات و بکاپ را بررسی کن.", backup_menu_keyboard(), message_ref)
        elif action == "subscription":
            awaiting_sub_url, pending_sub_url = True, None
            show(chat_id, "🔗 <b>مدیریت لینک ساب</b>\nلینک جدید را ارسال کن. تا زمان تأیید، فایل تنظیمات تغییر نمی‌کند.\nبرای امنیت، لینک در پیام تأیید نمایش داده نمی‌شود.",
                 keyboard([[{"text": "❌ لغو", "callback_data": "sub_cancel"}], [{"text": "⬅️ منوی اصلی", "callback_data": "menu"}]]), message_ref)
        elif action == "sub_confirm":
            if not pending_sub_url:
                awaiting_sub_url = False; show(chat_id, "⌛ درخواست منقضی شده؛ دوباره شروع کن.", main_menu(), message_ref)
            else:
                try:
                    backup_name = set_subscription_url(pending_sub_url); pending_sub_url = None; awaiting_sub_url = False
                    show(chat_id, "✅ لینک ساب ذخیره شد.\n" + f"📦 بکاپ تنظیمات: <code>{html.escape(backup_name)}</code>\n\nبرای اعمال تنظیم جدید، در صورت نیاز سرویس Xray Checker را جداگانه ری‌استارت کن.",
                         keyboard([[{"text": "🔄 راه‌اندازی مجدد Xray Checker", "callback_data": "sub_restart_confirm"}], [{"text": "⬅️ منوی اصلی", "callback_data": "menu"}]]), message_ref)
                except Exception as exc:
                    pending_sub_url = None; awaiting_sub_url = False
                    print(f"Subscription update failed: {type(exc).__name__}: {exc}", flush=True)
                    show(chat_id, "⚠️ ذخیره لینک انجام نشد. فایل تنظیمات و لاگ سرویس را بررسی کن.", main_menu(), message_ref)
        elif action == "sub_cancel":
            awaiting_sub_url, pending_sub_url = False, None; show(chat_id, "❌ عملیات تغییر لینک لغو شد.", main_menu(), message_ref)
        elif action == "sub_restart_confirm":
            show(chat_id, "⚠️ این کار فقط سرویس <code>xray-checker.service</code> را ری‌استارت می‌کند، نه ربات و نه BackPack. تأیید می‌کنی؟",
                 keyboard([[{"text": "🔄 بله، ری‌استارت Xray Checker", "callback_data": "sub_restart"}], [{"text": "❌ لغو", "callback_data": "menu"}]]), message_ref)
        elif action == "sub_restart":
            try:
                if not unit_exists("xray-checker.service"):
                    msg = "⚠️ سرویس xray-checker.service در systemd نصب نیست."
                else:
                    run_systemctl("restart", "xray-checker.service", timeout=45, check=True)
                    active = run_systemctl("is-active", "xray-checker.service").stdout.strip() == "active"
                    msg = "✅ سرویس Xray Checker فعال است." if active else "⚠️ بعد از ری‌استارت فعال‌بودن سرویس تأیید نشد."
            except Exception as exc:
                print(f"Xray Checker restart failed: {type(exc).__name__}: {exc}", flush=True); msg = "⚠️ ری‌استارت ناموفق بود؛ وضعیت سرویس را بررسی کن."
            show(chat_id, msg, main_menu(), message_ref)
        elif action == "bot_restart_confirm":
            show(chat_id, "⚠️ فقط <code>xray-checker-bot.service</code> ری‌استارت می‌شود. سرویس Xray Checker، تایمر و BackPack دست‌نخورده می‌مانند. ادامه می‌دهی؟",
                 keyboard([[{"text": "♻️ تأیید ری‌استارت ربات", "callback_data": "bot_restart_yes"}], [{"text": "❌ لغو", "callback_data": "menu"}]]), message_ref)
        elif action == "bot_restart_yes":
            try:
                restart_bot_later(); show(chat_id, "♻️ درخواست ری‌استارت ربات ثبت شد؛ چند ثانیه دیگر دوباره آنلاین می‌شوم.", main_menu(), message_ref)
            except Exception as exc:
                print(f"Bot restart scheduling failed: {type(exc).__name__}: {exc}", flush=True)
                show(chat_id, "⚠️ زمان‌بندی ری‌استارت انجام نشد. دسترسی systemd را بررسی کن.", main_menu(), message_ref)
        elif action == "menu":
            awaiting_sub_url, pending_sub_url = False, None; show(chat_id, "📡 <b>Xray Checker Bot</b>\nیک گزینه را انتخاب کن:", main_menu(), message_ref)
        elif action in ("status", "report"):
            awaiting_sub_url, pending_sub_url = False, None
            try: show(chat_id, report_text(), main_menu(), message_ref)
            except Exception as exc: show(chat_id, f"⚠️ دریافت گزارش ناموفق بود: <code>{html.escape(str(exc))}</code>", main_menu(), message_ref)
        elif action == "settings":
            awaiting_sub_url, pending_sub_url = False, None; show(chat_id, settings_text(), settings_menu(), message_ref)
        elif action == "intervals":
            show(chat_id, "⏱ <b>انتخاب بازهٔ گزارش خودکار</b>\n\n" + f"بازهٔ فعلی: هر {format_interval(interval_minutes())}", interval_menu(), message_ref)
        elif action.startswith("interval_"):
            try:
                minutes = int(action.split("_", 1)[1]); set_interval(minutes)
                show(chat_id, f"✅ بازه روی <b>هر {format_interval(minutes)}</b> تنظیم شد.\n\n" + settings_text(), settings_menu(), message_ref)
            except Exception as exc:
                print(f"Interval change failed: {type(exc).__name__}: {exc}", flush=True); show(chat_id, "⚠️ تغییر بازه انجام نشد؛ نصب بودن تایمر و لاگ را بررسی کن.", settings_menu(), message_ref)
        elif action == "toggle_auto":
            try:
                if not unit_exists("xray-checker-telegram.timer"):
                    raise RuntimeError("report timer not installed")
                if timer_active(): run_systemctl("disable", "--now", "xray-checker-telegram.timer", check=True)
                else: run_systemctl("enable", "--now", "xray-checker-telegram.timer", check=True)
                show(chat_id, settings_text(), settings_menu(), message_ref)
            except Exception as exc:
                print(f"Timer toggle failed: {type(exc).__name__}: {exc}", flush=True); show(chat_id, "⚠️ تغییر وضعیت گزارش خودکار انجام نشد؛ وجود تایمر گزارش را بررسی کن.", settings_menu(), message_ref)
        elif action == "help":
            show(chat_id, "❔ <b>راهنما</b>\n/start — منوی اصلی\n/status — وضعیت کانفیگ‌ها\n/report — گزارش فوری\n/settings — تنظیمات گزارش خودکار\n\nاین ربات BackPack را مدیریت یا تغییر نمی‌دهد.", main_menu(), message_ref)
        return

    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if chat_id != ADMIN: return
    incoming = (message.get("text") or "").strip()
    command = incoming.split("@", 1)[0].lower()
    if awaiting_sub_url and incoming and not incoming.startswith("/"):
        awaiting_sub_url = False
        if not valid_subscription_url(incoming):
            pending_sub_url = None; send_message(chat_id, "⚠️ لینک معتبر نیست. آدرس کامل HTTP یا HTTPS بدون فاصله بفرست.", main_menu()); return
        pending_sub_url = incoming
        send_message(chat_id, "🔐 <b>تأیید تغییر لینک ساب</b>\nساختار لینک معتبر به نظر می‌رسد. برای ذخیره تأیید کن؛ هنوز فایلی تغییر نکرده است.",
                     keyboard([[{"text": "✅ تأیید و ذخیره", "callback_data": "sub_confirm"}], [{"text": "❌ لغو", "callback_data": "sub_cancel"}]])); return
    if command in ("/start", "/menu"):
        awaiting_sub_url, pending_sub_url = False, None; show(chat_id, "📡 <b>Xray Checker Bot</b>\nخوش آمدی! از منوی زیر استفاده کن:", main_menu())
    elif command in ("/status", "/report"):
        awaiting_sub_url, pending_sub_url = False, None
        try: show(chat_id, report_text(), main_menu())
        except Exception as exc: show(chat_id, f"⚠️ دریافت گزارش ناموفق بود: <code>{html.escape(str(exc))}</code>", main_menu())
    elif command == "/settings":
        awaiting_sub_url, pending_sub_url = False, None; show(chat_id, settings_text(), settings_menu())
    elif command == "/help":
        show(chat_id, "❔ <b>راهنما</b>\n/start — منوی اصلی\n/status — وضعیت کانفیگ‌ها\n/report — گزارش فوری\n/settings — تنظیمات", main_menu())


def load_offset():
    try: return int(STATE.read_text().strip())
    except (OSError, ValueError): return 0


def save_offset(value):
    atomic_write(STATE, f"{value}\n")


def main():
    offset = load_offset()
    print(f"Xray Checker Bot started; relay={RELAY_HOST}:{RELAY_PORT}", flush=True)
    while True:
        try:
            result = api("getUpdates", {"offset": offset, "timeout": 25,
                                         "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=40)
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                try: handle(update)
                except Exception as exc: print(f"Update handling failed: {type(exc).__name__}: {exc}", flush=True)
                finally:
                    try: save_offset(offset)
                    except Exception as exc: print(f"Could not save update offset: {exc}", flush=True)
        except Exception as exc:
            print(f"Polling error: {type(exc).__name__}: {exc}", flush=True); time.sleep(5)


if __name__ == "__main__":
    main()
