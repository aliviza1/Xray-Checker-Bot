#!/usr/bin/env python3
"""Xray Checker Bot Manager v4 bootstrap + numeric interactive menu.

Fetches the user's v3 GitHub manager as baseline, applies v4 changes to its
embedded bot source, then delegates backup/install/restore operations to it.
BackPack is deliberately never read, changed, stopped, enabled, disabled, or restarted.
"""
import os, re, sys, time, stat, shutil, subprocess, tempfile, urllib.request
from pathlib import Path

RAW_URL = "https://raw.githubusercontent.com/aliviza1/Xray-Checker-Bot/main/xray-checker-manager-v3.py"
CACHE_DIR = Path("/var/lib/xray-checker-manager-v4")
GENERATED = CACHE_DIR / "xray-checker-manager-v4-generated.py"
BOT = Path('/usr/local/bin/xray-checker-bot.py')
BOT_SERVICE = 'xray-checker-bot.service'
TIMER = 'xray-checker-telegram.timer'
BACKUP_DIR = Path('/var/backups/xray-checker-manager')
INTERVAL_FILE = Path('/var/lib/xray-checker-telegram-bot/interval_minutes')
INTERVALS = (5, 10, 15, 30, 45, 60, 360, 720)


def require_root():
    if os.geteuid() != 0:
        raise SystemExit('Please run as root: sudo python3 xray-checker-manager-v4.py')


def run(cmd, check=True):
    return subprocess.run(cmd, check=check, text=True)


def fetch_baseline():
    req = urllib.request.Request(RAW_URL, headers={'User-Agent': 'Xray-Checker-Manager-v4'})
    with urllib.request.urlopen(req, timeout=25) as response:
        source = response.read().decode('utf-8')
    if 'BOT_SOURCE = r\'\'\'' not in source or 'def backup_files(' not in source or 'BackPack files are intentionally excluded' not in source:
        raise RuntimeError('Downloaded file does not match the expected v3 structure; installation aborted.')
    return source


def patch_bot(bot_source):
    # Send a private backup document through the existing local Telegram relay.
    if 'def send_document(' not in bot_source:
        anchor = 'def edit_message(chat_id, message_id, text, markup=None):'
        helper = '''def send_document(chat_id, file_path, caption="Xray Checker backup v4"):
    boundary = "----XrayChecker" + str(time.time_ns())
    path = Path(file_path)
    with path.open("rb") as handle:
        file_bytes = handle.read()
    fields = [
        ("chat_id", str(chat_id)),
        ("caption", caption),
    ]
    chunks = []
    for name, value in fields:
        chunks.append(("--" + boundary + "\\r\\n").encode())
        chunks.append((f'Content-Disposition: form-data; name="{name}"\\r\\n\\r\\n').encode())
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\\r\\n")
    chunks.append(("--" + boundary + "\\r\\n").encode())
    chunks.append((f'Content-Disposition: form-data; name="document"; filename="{path.name}"\\r\\n').encode())
    chunks.append(b"Content-Type: application/gzip\\r\\n\\r\\n")
    chunks.append(file_bytes)
    chunks.append(b"\\r\\n")
    chunks.append(("--" + boundary + "--\\r\\n").encode())
    body = b"".join(chunks)
    conn = RelayHTTPS(HOST, timeout=90, context=ssl.create_default_context())
    try:
        conn.request("POST", f"/bot{TOKEN}/sendDocument", body=body,
                     headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                              "Content-Length": str(len(body))})
        response = conn.getresponse()
        raw = response.read().decode("utf-8", "replace")
        data = json.loads(raw)
        if response.status != 200 or not data.get("ok"):
            raise RuntimeError("Telegram sendDocument failed: " + str(data.get("description", response.status)))
        return data
    finally:
        conn.close()

'''
        if anchor not in bot_source: raise RuntimeError('send_document insertion point was not found in bot source')
        bot_source = bot_source.replace(anchor, helper + anchor, 1)

    # Keep only the newest 8 local archives after each archive is created.
    old = '''    os.chmod(archive, 0o600)
    return archive

def available_backups():'''
    new = '''    os.chmod(archive, 0o600)
    # Retention policy: keep newest eight archives; delete oldest first.
    backups = sorted(BACKUP_DIR.glob("xray-checker-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in backups[8:]:
        try:
            stale.unlink()
        except OSError as exc:
            print(f"Could not prune old backup {stale.name}: {exc}", flush=True)
    return archive

def available_backups():'''
    if old in bot_source:
        bot_source = bot_source.replace(old, new, 1)

    # Add restart-self button to the main menu.
    old_menu = '''        [{"text": "📦 Backup / Restore", "callback_data": "backup_menu"}],
    ])'''
    new_menu = '''        [{"text": "📦 Backup / Restore", "callback_data": "backup_menu"}],
        [{"text": "♻️ Restart Bot", "callback_data": "bot_restart_confirm"}],
    ])'''
    if old_menu in bot_source:
        bot_source = bot_source.replace(old_menu, new_menu, 1)

    # Add confirm/cancel flow; restart is deferred so the bot can send the reply first.
    anchor = '''        if action == "backup_menu":'''
    restart_handler = '''        if action == "bot_restart_confirm":
            show(chat_id, "⚠️ <b>راه‌اندازی مجدد خود ربات</b>\\n\\nفقط سرویس xray-checker-bot.service ری‌استارت می‌شود. Xray Checker و BackPack دست‌نخورده می‌مانند. ادامه می‌دهی؟",
                 keyboard([[{"text": "♻️ Yes, restart", "callback_data": "bot_restart_yes"}],
                           [{"text": "❌ Cancel", "callback_data": "menu"}]]), message_ref)
        elif action == "bot_restart_yes":
            try:
                unit = "xray-checker-bot-restart-" + str(time.time_ns())
                subprocess.run(["systemd-run", "--quiet", "--unit=" + unit, "--on-active=3s",
                                "/usr/bin/systemctl", "restart", "xray-checker-bot.service"],
                               check=True, capture_output=True, text=True, timeout=15)
                show(chat_id, "✅ Bot restart scheduled. The bot may be unavailable for a few seconds.", None, message_ref)
            except Exception as exc:
                print(f"Bot restart scheduling failed: {type(exc).__name__}: {exc}", flush=True)
                show(chat_id, "⚠️ Could not schedule the restart. Check systemd-run permissions and service logs.", main_menu(), message_ref)
        elif action == "backup_menu":'''
    if anchor in bot_source and 'bot_restart_confirm' not in bot_source:
        bot_source = bot_source.replace(anchor, restart_handler, 1)

    # Send each manually requested backup to the configured admin chat.
    old_backup = '''                archive = create_backup("telegram")
                show(chat_id, "✅ <b>بکاپ ساخته شد.</b>\\n\\n"'''
    new_backup = '''                archive = create_backup("telegram")
                send_document(chat_id, archive, "Xray Checker Bot backup v4 — may contain secrets; keep private")
                show(chat_id, "✅ <b>بکاپ ساخته و به همین چت ارسال شد.</b>\\n\\n"'''
    if old_backup in bot_source:
        bot_source = bot_source.replace(old_backup, new_backup, 1)
    return bot_source


def generate_manager():
    source = fetch_baseline()
    match = re.search(r"BOT_SOURCE = r'''(.*?)\n'''", source, re.S)
    if not match:
        raise RuntimeError('BOT_SOURCE block was not found in the v3 source')
    bot_source = patch_bot(match.group(1))
    source = source[:match.start(1)] + bot_source + source[match.end(1):]

    # Retain only eight manager-created archives too.
    old = '''    os.chmod(archive, 0o600)
    print(f'Backup created: {archive}')'''
    new = '''    os.chmod(archive, 0o600)
    # Keep newest eight local archives, deleting oldest first.
    backups = sorted(BACKUP_DIR.glob('xray-checker-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in backups[8:]:
        try: stale.unlink()
        except OSError as exc: print(f'WARNING: could not remove old backup {stale}: {exc}')
    print(f'Backup created: {archive}')'''
    if old in source: source = source.replace(old, new, 1)

    # Add a default interactive numeric menu, keeping CLI subcommands available.
    old_main = '''    parser = argparse.ArgumentParser(description='Xray Checker Bot clean installer, backup and restore manager')
    subs = parser.add_subparsers(dest='command', required=True)'''
    new_main = '''    parser = argparse.ArgumentParser(description='Xray Checker Bot v4 manager')
    subs = parser.add_subparsers(dest='command', required=False)'''
    if old_main in source: source = source.replace(old_main, new_main, 1)
    old_args = '''    args = parser.parse_args()
    require_root()
    if args.command == 'backup':'''
    new_args = '''    args = parser.parse_args()
    require_root()
    if args.command is None:
        interactive_menu()
        return
    if args.command == 'backup':'''
    if old_args in source: source = source.replace(old_args, new_args, 1)

    interactive = r'''
def interactive_menu():
    while True:
        print("\n========== Xray Checker Manager v4 ==========")
        print("1) Install / update bot from GitHub")
        print("2) Restart the bot service")
        print("3) Create a local backup (keep newest 8)")
        print("4) List local backups")
        print("5) Restore a local backup")
        print("6) Set automatic report interval")
        print("7) Show bot and reporting service status")
        print("8) Uninstall bot and reporting components")
        print("0) Exit")
        choice = input("Select an option: ").strip()
        try:
            if choice == "1":
                install()
                if input("Restart the bot service now too? [y/N]: ").strip().lower() == "y":
                    subprocess.run(["systemctl", "restart", "xray-checker-bot.service"], check=True)
                    print("Bot service restarted.")
            elif choice == "2":
                if input("Restart only xray-checker-bot.service? Type YES to confirm: ").strip() == "YES":
                    subprocess.run(["systemctl", "restart", "xray-checker-bot.service"], check=True)
                    print("Restart command completed.")
            elif choice == "3": backup_files("manual-v4")
            elif choice == "4": list_backups()
            elif choice == "5":
                archive = input("Full path to the .tar.gz backup: ").strip()
                restore(archive)
            elif choice == "6":
                print("Available intervals: 5, 10, 15, 30, 45 minutes; 1, 6, 12 hours")
                raw = input("Interval in minutes (5/10/15/30/45/60/360/720): ").strip()
                minutes = int(raw)
                if minutes not in (5, 10, 15, 30, 45, 60, 360, 720):
                    print("Invalid interval."); continue
                INTERVAL_FILE.parent.mkdir(parents=True, exist_ok=True)
                INTERVAL_FILE.write_text(str(minutes) + "\n", encoding="ascii")
                os.chmod(INTERVAL_FILE, 0o600)
                dropin = Path("/etc/systemd/system/xray-checker-telegram.timer.d/override.conf")
                dropin.parent.mkdir(parents=True, exist_ok=True)
                duration = f"{minutes // 60}h" if minutes >= 60 else f"{minutes}min"
                dropin.write_text("[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=" + duration + "\n", encoding="utf-8")
                os.chmod(dropin, 0o600)
                subprocess.run(["systemctl", "daemon-reload"], check=True)
                state = subprocess.run(["systemctl", "is-active", "xray-checker-telegram.timer"], capture_output=True, text=True)
                if state.stdout.strip() == "active": subprocess.run(["systemctl", "restart", "xray-checker-telegram.timer"], check=True)
                print("Reporting interval updated. Only the reporting timer was changed.")
            elif choice == "7":
                for unit in ("xray-checker-bot.service", "xray-checker-telegram.timer", "xray-checker-telegram.service"):
                    result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
                    print(f"{unit}: {result.stdout.strip() or 'unknown'}")
            elif choice == "8":
                print("This removes only bot/reporting components. BackPack and xray-checker.service will not be removed or changed.")
                if input("Type REMOVE BOT to confirm: ").strip() != "REMOVE BOT":
                    print("Cancelled."); continue
                backup_files("pre-uninstall-v4")
                subprocess.run(["systemctl", "disable", "--now", "xray-checker-bot.service"], check=False)
                subprocess.run(["systemctl", "disable", "--now", "xray-checker-telegram.timer"], check=False)
                paths = ["/usr/local/bin/xray-checker-bot.py", "/usr/local/bin/xray-checker-telegram.py",
                         "/etc/systemd/system/xray-checker-bot.service", "/etc/systemd/system/xray-checker-telegram.service",
                         "/etc/systemd/system/xray-checker-telegram.timer", "/etc/systemd/system/xray-checker-telegram.timer.d/override.conf",
                         "/etc/xray-checker-telegram.env", str(INTERVAL_FILE)]
                for raw_path in paths:
                    p = Path(raw_path)
                    try:
                        if p.is_file() or p.is_symlink(): p.unlink(); print("Removed:", p)
                    except OSError as exc: print("Failed to remove", p, exc)
                subprocess.run(["systemctl", "daemon-reload"], check=False)
                print("Uninstall completed. The pre-uninstall backup remains in the backup directory.")
            elif choice == "0": return
            else: print("Invalid option.")
        except Exception as exc:
            print(f"Error: {type(exc).__name__}: {exc}")
        input("Press Enter to continue...")

'''
    # Put menu before manager's main(), not inside embedded BOT_SOURCE.
    marker = '\ndef main():\n    parser = argparse.ArgumentParser(description=\'Xray Checker Bot v4 manager\')'
    if marker in source and 'def interactive_menu()' not in source:
        source = source.replace(marker, '\n' + interactive + marker, 1)
    elif 'def interactive_menu()' not in source:
        raise RuntimeError('Could not find the insertion point for the interactive menu')

    compile(source, str(GENERATED), 'exec')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CACHE_DIR, 0o700)
    temp = GENERATED.with_suffix('.tmp')
    temp.write_text(source, encoding='utf-8')
    os.chmod(temp, 0o600)
    os.replace(temp, GENERATED)
    return GENERATED


def main():
    require_root()
    print('Xray Checker Manager v4 — fetching baseline from GitHub and applying v4 changes')
    try:
        generated = generate_manager()
    except Exception as exc:
        raise SystemExit(f'Failed to build v4; no operational files were replaced. Reason: {type(exc).__name__}: {exc}')
    args = sys.argv[1:]
    if not args: args = []
    result = subprocess.run(['/usr/bin/python3', str(generated), *args])
    raise SystemExit(result.returncode)

if __name__ == '__main__':
    main()
