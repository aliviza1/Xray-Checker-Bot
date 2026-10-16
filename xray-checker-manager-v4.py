#!/usr/bin/env python3
"""Xray Checker Manager v4.2.0 — standalone manager.

BackPack is intentionally excluded: this program never reads, changes, stops,
enables, disables, or restarts BackPack services/configuration.
"""
MANAGER_VERSION = "4.3.0"
import argparse, os, re, shutil, subprocess, sys, tarfile, tempfile, urllib.request
from datetime import datetime
from pathlib import Path

MANAGER_PATH = Path('/root/xray-checker-manager-v4.py')
MANAGER_URL = 'https://raw.githubusercontent.com/aliviza1/Xray-Checker-Bot/main/xray-checker-manager-v4.py'
BOT_URL = 'https://raw.githubusercontent.com/aliviza1/Xray-Checker-Bot/refs/heads/main/xray-checker-bot.py'
BOT = Path('/usr/local/bin/xray-checker-bot.py')
BOT_SERVICE = 'xray-checker-bot.service'
TIMER = 'xray-checker-telegram.timer'
REPORT_SERVICE = 'xray-checker-telegram.service'
METRICS_URL = 'http://45.139.11.96:2112/'
COMMAND_PATH = Path('/usr/local/bin/xray-checker-manager')
BACKUP_DIR = Path('/var/backups/xray-checker-manager')
INTERVAL_FILE = Path('/var/lib/xray-checker-telegram-bot/interval_minutes')
INTERVALS = (5, 10, 15, 30, 45, 60, 360, 720)
# Explicit allowlist only. No /etc/backpack or BackPack service/config paths.
BACKUP_PATHS = [
    Path('/usr/local/bin/xray-checker-bot.py'),
    Path('/usr/local/bin/xray-checker-telegram.py'),
    Path('/etc/xray-checker-telegram.env'),
    Path('/etc/xray-checker/xray-checker.env'),
    Path('/etc/systemd/system/xray-checker-bot.service'),
    Path('/etc/systemd/system/xray-checker-telegram.service'),
    Path('/etc/systemd/system/xray-checker-telegram.timer'),
    Path('/etc/systemd/system/xray-checker-telegram.timer.d/override.conf'),
    INTERVAL_FILE,
]

def require_root():
    if os.geteuid() != 0:
        raise SystemExit('Please run as root: sudo python3 /root/xray-checker-manager-v4.py')

def run(cmd, check=False):
    return subprocess.run(cmd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

def fetch(url, limit=2_000_000):
    req = urllib.request.Request(url, headers={'User-Agent': 'Xray-Checker-Manager/' + MANAGER_VERSION})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if not data or len(data) > limit:
        raise RuntimeError('Download empty or unexpectedly large')
    return data.decode('utf-8')

def extract_version(source):
    for pattern in (r'^MANAGER_VERSION\s*=\s*[\'\"]([^\'\"]+)', r'^BOT_VERSION\s*=\s*[\'\"]([^\'\"]+)'):
        m = re.search(pattern, source, re.M)
        if m: return m.group(1)
    return 'unknown (no version marker)'

def bot_versions():
    installed = extract_version(BOT.read_text(errors='replace')) if BOT.is_file() else 'not installed'
    try:
        latest_source = fetch(BOT_URL)
        latest = extract_version(latest_source)
        if latest == 'unknown (no version marker)': latest = 'unversioned GitHub source'
    except Exception as e:
        latest = 'unavailable (' + type(e).__name__ + ')'
    return installed, latest

def show_versions():
    print('Manager version:', MANAGER_VERSION)
    print('Installed manager file:', MANAGER_PATH if MANAGER_PATH.exists() else 'not found at standard path')
    installed, latest = bot_versions()
    print('Installed bot version:', installed)
    print('Latest GitHub bot version:', latest)
    print('Note: legacy bot files without BOT_VERSION cannot be identified reliably.')

def make_backup(label='manual'):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    archive = BACKUP_DIR / f'xray-checker-{label}-{stamp}.tar.gz'
    included = [p for p in BACKUP_PATHS if p.exists() and (p.is_file() or p.is_symlink())]
    with tarfile.open(archive, 'w:gz') as tf:
        for p in included:
            tf.add(p, arcname=str(p).lstrip('/'), recursive=False)
    os.chmod(archive, 0o600)
    archives = sorted(BACKUP_DIR.glob('xray-checker-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in archives[8:]:
        try: old.unlink()
        except OSError as e: print('Warning: cannot prune', old, e)
    print('Backup created:', archive)
    print('Included files:', len(included))
    print('Security: archive may contain Telegram tokens, passwords, or subscription URLs; keep it private.')
    print('BackPack configuration and services were excluded.')
    return archive

def list_backups():
    files = sorted(BACKUP_DIR.glob('xray-checker-*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True) if BACKUP_DIR.exists() else []
    if not files: print('No local backups found.'); return
    for p in files: print(f'{p.stat().st_size:>10} bytes  {datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}  {p}')

def safe_restore(archive_path):
    archive = Path(archive_path).resolve()
    if not archive.is_file() or archive.suffixes[-2:] != ['.tar', '.gz']:
        raise RuntimeError('Select an existing .tar.gz backup file')
    allowed = {str(p).lstrip('/') for p in BACKUP_PATHS}
    with tarfile.open(archive, 'r:gz') as tf:
        members = []
        for m in tf.getmembers():
            name = m.name.lstrip('./')
            if name not in allowed:
                raise RuntimeError('Archive contains an unexpected path; restore aborted: ' + m.name)
            if not (m.isfile() or m.issym()):
                raise RuntimeError('Archive contains a non-file entry; restore aborted')
            members.append((m, name))
        if not members: raise RuntimeError('No supported files in this archive')
        print('Files to restore:')
        for _, name in members: print(' ', '/' + name)
        if input('Type RESTORE to continue: ').strip() != 'RESTORE': print('Cancelled.'); return
        # Extract only after strict allowlist validation, one file at a time.
        for m, name in members:
            dest = Path('/') / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None: continue
            data = src.read()
            tmp = dest.with_name(dest.name + '.restore-tmp')
            tmp.write_bytes(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
            print('Restored:', dest)
    run(['systemctl', 'daemon-reload'])
    print('Restore completed. Services were not restarted automatically.')

def update_manager():
    if not MANAGER_PATH.is_file():
        print('Manager file not found:', MANAGER_PATH); return
    temp = None
    try:
        source = fetch(MANAGER_URL)
        compile(source, str(MANAGER_PATH), 'exec')
        version = extract_version(source)
        if version == 'unknown (no version marker)' or 'BackPack is intentionally excluded' not in source:
            raise RuntimeError('GitHub file is missing expected version/safety markers')
        if version == MANAGER_VERSION:
            print('GitHub manager version matches this version:', version)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        BACKUP_DIR.mkdir(parents=True, exist_ok=True); os.chmod(BACKUP_DIR, 0o700)
        backup = BACKUP_DIR / f'xray-checker-manager-pre-update-{stamp}.py'
        shutil.copy2(MANAGER_PATH, backup); os.chmod(backup, 0o600)
        fd, tmp_name = tempfile.mkstemp(prefix='.manager-', suffix='.py', dir=str(MANAGER_PATH.parent))
        temp = Path(tmp_name)
        with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write(source)
        os.chmod(temp, 0o700); os.replace(temp, MANAGER_PATH)
        print('Manager updated successfully. Downloaded version:', version)
        print('Previous version backup:', backup)
        print('Exit and run the manager again to load the updated version.')
    except Exception as e:
        if temp and temp.exists(): temp.unlink()
        print(f'Update failed; current manager was preserved: {type(e).__name__}: {e}')

def update_bot_from_github():
    try:
        source = fetch(BOT_URL)
        compile(source, str(BOT), 'exec')
        if 'def ' not in source or 'BOT' not in source:
            raise RuntimeError('Downloaded source does not look like a Python bot')
        if BOT.exists():
            make_backup('pre-bot-update')
        # Preserve an existing version marker only if source has no marker; otherwise do not invent one.
        fd, tmp_name = tempfile.mkstemp(prefix='.xray-checker-bot-', suffix='.py', dir='/usr/local/bin')
        with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write(source)
        os.chmod(tmp_name, 0o700); os.replace(tmp_name, BOT)
        print('Bot source installed from GitHub. Source version:', extract_version(source))
        print('No service was restarted automatically.')
        if input('Restart only xray-checker-bot.service now? Type YES to confirm: ').strip() == 'YES':
            result = run(['systemctl', 'restart', BOT_SERVICE])
            print(result.stdout or 'Restart command completed.')
    except Exception as e: print(f'Bot update failed: {type(e).__name__}: {e}')

def set_interval():
    print('Available intervals: 5, 10, 15, 30, 45 minutes; 1, 6, 12 hours')
    try: minutes = int(input('Interval in minutes (5/10/15/30/45/60/360/720): ').strip())
    except ValueError: print('Invalid interval.'); return
    if minutes not in INTERVALS: print('Invalid interval.'); return
    INTERVAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    INTERVAL_FILE.write_text(str(minutes) + '\n', encoding='ascii'); os.chmod(INTERVAL_FILE, 0o600)
    dropin = Path('/etc/systemd/system/xray-checker-telegram.timer.d/override.conf')
    dropin.parent.mkdir(parents=True, exist_ok=True)
    duration = f'{minutes // 60}h' if minutes >= 60 else f'{minutes}min'
    dropin.write_text('[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=' + duration + '\n', encoding='utf-8')
    os.chmod(dropin, 0o600)
    run(['systemctl', 'daemon-reload'])
    if run(['systemctl', 'is-active', TIMER]).stdout.strip() == 'active': run(['systemctl', 'restart', TIMER])
    print('Reporting interval updated. Only the reporting timer was changed.')

def show_metrics_url():
    print('Metrics / dashboard URL:')
    print(METRICS_URL)
    print('Open this URL in a browser. The endpoint may require authentication or a specific path depending on your server configuration.')

def install_command_shortcut():
    target = Path('/root/xray-checker-manager-v4.py')
    if not target.is_file():
        print('Manager file not found:', target)
        print('Install this file at the standard path first.')
        return
    wrapper = "#!/bin/sh\nexec /usr/bin/python3 /root/xray-checker-manager-v4.py \"$@\"\n"
    COMMAND_PATH.write_text(wrapper, encoding='utf-8')
    os.chmod(COMMAND_PATH, 0o755)
    print('Command installed:', COMMAND_PATH)
    print('From now on, type: xray-checker-manager')

def status():
    for unit in (BOT_SERVICE, TIMER, REPORT_SERVICE):
        result = run(['systemctl', 'is-active', unit])
        print(f'{unit}: {result.stdout.strip() or "unknown"}')

def uninstall():
    print('This removes bot/reporting components only. BackPack and xray-checker.service will not be touched.')
    if input('Type REMOVE BOT to confirm: ').strip() != 'REMOVE BOT': print('Cancelled.'); return
    make_backup('pre-uninstall')
    for unit in (BOT_SERVICE, TIMER): run(['systemctl', 'disable', '--now', unit])
    paths = [Path('/usr/local/bin/xray-checker-bot.py'), Path('/usr/local/bin/xray-checker-telegram.py'),
             Path('/etc/systemd/system/xray-checker-bot.service'), Path('/etc/systemd/system/xray-checker-telegram.service'),
             Path('/etc/systemd/system/xray-checker-telegram.timer'), Path('/etc/systemd/system/xray-checker-telegram.timer.d/override.conf'),
             Path('/etc/xray-checker-telegram.env'), INTERVAL_FILE]
    for p in paths:
        try:
            if p.is_file() or p.is_symlink(): p.unlink(); print('Removed:', p)
        except OSError as e: print('Failed to remove', p, e)
    run(['systemctl', 'daemon-reload'])
    print('Uninstall completed. Backups remain in', BACKUP_DIR)

def menu():
    while True:
        installed, _ = bot_versions()
        print(f'\n========== Xray Checker Manager v{MANAGER_VERSION} ==========')
        print('Installed bot version:', installed)
        print('1) Install / update bot source from GitHub')
        print('2) Restart the bot service')
        print('3) Create a local backup (keep newest 8)')
        print('4) List local backups')
        print('5) Restore a local backup')
        print('6) Set automatic report interval')
        print('7) Show bot and reporting service status')
        print('8) Uninstall bot and reporting components')
        print('9) Update Manager itself from GitHub')
        print('10) Show installed and latest versions')
        print('11) Show metrics dashboard URL')
        print('12) Install terminal command shortcut')
        print('0) Exit')
        choice = input('Select an option: ').strip()
        try:
            if choice == '1': update_bot_from_github()
            elif choice == '2':
                if input('Restart only xray-checker-bot.service? Type YES to confirm: ').strip() == 'YES':
                    result = run(['systemctl', 'restart', BOT_SERVICE]); print(result.stdout or 'Restart command completed.')
            elif choice == '3': make_backup()
            elif choice == '4': list_backups()
            elif choice == '5': safe_restore(input('Full path to the .tar.gz backup: ').strip())
            elif choice == '6': set_interval()
            elif choice == '7': status()
            elif choice == '8': uninstall()
            elif choice == '9': update_manager()
            elif choice == '10': show_versions()
            elif choice == '11': show_metrics_url()
            elif choice == '12': install_command_shortcut()
            elif choice == '0': return
            else: print('Invalid option.')
        except Exception as e: print(f'Error: {type(e).__name__}: {e}')
        input('Press Enter to continue...')

def main():
    require_root()
    parser = argparse.ArgumentParser(description='Standalone Xray Checker Manager')
    parser.add_argument('--version', action='store_true')
    args = parser.parse_args()
    if args.version: print(f'xray-checker-manager-v4.py {MANAGER_VERSION}'); return
    menu()

if __name__ == '__main__': main()
