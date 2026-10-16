# Xray Checker Telegram Bot

نسخهٔ مستقل ربات تلگرام برای مانیتورینگ Xray Checker؛ بدون وابستگی به پکیج‌های Python خارجی.

## فایل‌ها

- `xray-checker-bot.py`: سورس کامل ربات
- `install.sh`: نصب تعاملی، دریافت Token و Chat ID، بررسی اتصال تلگرام و ساخت سرویس systemd

## نصب روی Ubuntu

هر دو فایل را در یک پوشه از مخزن GitHub قرار بده. روی سرور:

```bash
curl -fLO https://raw.githubusercontent.com/aliviza1/Xray-Checker-Bot/main/xray-checker-bot.py
curl -fLO https://raw.githubusercontent.com/aliviza1/Xray-Checker-Bot/main/install.sh
chmod +x install.sh
sudo bash install.sh
```

هنگام نصب، توکن به‌صورت مخفی دریافت می‌شود. پیش از نصب، توکن با Telegram API از طریق relay تعیین‌شده بررسی می‌شود و پیام آزمایشی به Chat ID فرستاده می‌شود؛ برای چت خصوصی، ابتدا ربات را در تلگرام باز کن و `/start` بفرست.

## پیش‌نیازها

- Ubuntu/Debian با Python 3 و systemd
- relay قابل دسترس Telegram API؛ پیش‌فرض `127.0.0.1:58615`
- فایل `/etc/xray-checker/xray-checker.env` با `METRICS_USERNAME` و `METRICS_PASSWORD`
- Metrics در `http://127.0.0.1:2112/metrics`
- برای گزارش زمان‌بندی‌شده، سرویس‌ها و تایمر جداگانهٔ موجود `xray-checker-telegram.service` و `xray-checker-telegram.timer`
- برای مدیریت لینک ساب، دقیقاً یک مقدار `SUBSCRIPTION_URL` در فایل env مربوط به Xray Checker

## امنیت و محدوده

- Token و Chat ID در `/etc/xray-checker-telegram.env` با مجوز `600` ذخیره می‌شوند؛ آن‌ها را در GitHub قرار نده.
- بکاپ ممکن است شامل توکن، رمز و تنظیمات خصوصی باشد؛ مسیر بکاپ مجوز `700` و فایل‌های بکاپ مجوز `600` دارند.
- ربات و نصب‌کننده BackPack را نمی‌خوانند، تغییر نمی‌دهند، متوقف یا راه‌اندازی مجدد نمی‌کنند.
- ریستور فقط فایل‌های مشخص‌شده در فهرست مجاز را جایگزین می‌کند؛ قبل از آن بکاپی از وضعیت فعلی می‌سازد و سرویس‌ها را خودکار ری‌استارت نمی‌کند.
- نصب‌کننده قبل از جایگزینی، بکاپ فایل‌های مرتبط را در `/var/backups/xray-checker-manager/` می‌سازد.
- این ابزار داشبورد Metrics را پیکربندی یا امن‌سازی نمی‌کند. دسترسی عمومی به پورت داشبورد را جداگانه بررسی کن.
