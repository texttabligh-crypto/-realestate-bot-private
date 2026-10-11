import os
import db
from formatter import public_caption
from security import encrypt_token,decrypt_token

required={"partner_agencies","partner_members","partner_brand_settings","partner_bot_connections","partner_channel_connections","partner_submissions","partner_submission_media","partner_publication_jobs","partner_audit_log"}
rows=db.all_rows("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
found={r['table_name'] for r in rows}
missing=required-found
assert not missing, f"missing tables: {sorted(missing)}"
sample={"tracking_code":"TEST","district":"رشدیه","address_text":"نمونه","area_m2":100,"bedrooms":2,"deal_type":"SALE","current_price_toman":10_000_000_000}
caption=public_caption(sample,{"display_name":"آزمایش","public_mobile":"09120000000"})
assert "رشدیه" in caption and "آزمایش" in caption and "۰۹۱۲" in caption
secret=encrypt_token("123456:abcdefghijklmnopqrstuvwxyzABCDE")
assert decrypt_token(secret)=="123456:abcdefghijklmnopqrstuvwxyzABCDE"
print("✅ PARTNER_HUB_DB_SECURITY_FORMAT_SMOKE_OK")
