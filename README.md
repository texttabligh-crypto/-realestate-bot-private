# ربات املاک نیما

مخزن خصوصی کد ربات مدیریت و انتشار فایل‌های املاک. ربات فایل‌های ورودی را
دریافت، استخراج و اعتبارسنجی می‌کند؛ موارد تکراری را مشخص می‌کند و فایل‌های
تأییدشده را طبق زمان‌بندی در کانال مقصد منتشر می‌کند.

## نسخه مبنا

- نسخه عملیاتی: Advanced Smart Search V2
- تاریخ مبنا: 2026-10-09
- محیط اجرا: Docker Compose در `/opt/realestate-bot`
- سرویس اصلی: `realestate_bot`

## قابلیت‌های اصلی

- پنل مدیریتی تلگرام با دسترسی‌های تفکیک‌شده
- جست‌وجوی هوشمند مدیریتی با فیلترهای قیمت، محله، پارکینگ، آسانسور، انباری،
  سند و جهت ملک
- جست‌وجوی ساده و طبیعی برای مشتری
- نمایش اختصاصی فایل‌ها و پیگیری فایل‌های مشابه
- مدیریت مشتری، بازدید، یادآوری، انتشار و گزارش
- PostgreSQL و Redis با اجرای Docker Compose

## کنترل کیفیت

```bash
python3 -m compileall -q app partner_hub
python3 smoke_test.py
```

## استقرار امن

استقرار روی سرور باید مرحله‌ای باشد: بررسی نسخه و هش، تهیه بکاپ، نصب فقط در
مسیر پروژه، اجرای تست سلامت و نگهداری مسیر Rollback. هیچ سرویس یا کانتینر
نامرتبطی نباید متوقف، حذف یا با `--remove-orphans` پاک شود.

## عیب‌یابی سرور (فقط‌خواندنی)

workflow `Diagnose Production` روی runner سرور اجرا می‌شود و وضعیت کانتینرها،
مصرف منابع، لاگ‌های اخیر و تطابق فایل‌های نصب‌شده با `main` را گزارش می‌کند.
هیچ سرویسی را متوقف یا بازسازی نمی‌کند و `.env` را نمی‌خواند. توکن‌ها، رمزها و
شماره موبایل‌ها پیش از ثبت در لاگ حذف می‌شوند.

اگر کاربر runner به Docker دسترسی مستقیم ندارد، یک بار روی سرور با root نصب شود:

```bash
install -m 755 -o root -g root deploy/diagnose-realestate-bot /usr/local/sbin/diagnose-realestate-bot
echo '<runner-user> ALL=(root) NOPASSWD: /usr/local/sbin/diagnose-realestate-bot' > /etc/sudoers.d/realestate-diagnose
chmod 440 /etc/sudoers.d/realestate-diagnose && visudo -cf /etc/sudoers.d/realestate-diagnose
```

تنظیمات واقعی فقط در `.env` سرور نگهداری می‌شوند. برای فهرست متغیرهای لازم،
از `.env.example` استفاده کنید.
