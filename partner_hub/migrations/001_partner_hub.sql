BEGIN;

CREATE TABLE IF NOT EXISTS partner_agencies (
    id BIGSERIAL PRIMARY KEY,
    legal_name VARCHAR(255) NOT NULL,
    brand_name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    city VARCHAR(100),
    address_text TEXT,
    license_number VARCHAR(100),
    manager_name VARCHAR(255) NOT NULL,
    manager_phone VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
      CHECK (status IN ('PENDING','ACTIVE','SUSPENDED','REJECTED')),
    plan_code VARCHAR(30) NOT NULL DEFAULT 'FREE',
    monthly_file_limit INTEGER NOT NULL DEFAULT 30,
    central_publish_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    own_publish_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    verified_at TIMESTAMPTZ,
    verified_by_telegram_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS partner_members (
    id BIGSERIAL PRIMARY KEY,
    agency_id BIGINT NOT NULL REFERENCES partner_agencies(id) ON DELETE CASCADE,
    telegram_user_id BIGINT NOT NULL,
    username VARCHAR(255),
    display_name VARCHAR(255),
    phone VARCHAR(50),
    role VARCHAR(20) NOT NULL DEFAULT 'ADVISOR'
      CHECK (role IN ('AGENCY_OWNER','AGENCY_ADMIN','ADVISOR')),
    can_add_file BOOLEAN NOT NULL DEFAULT TRUE,
    can_publish_own BOOLEAN NOT NULL DEFAULT FALSE,
    can_request_central BOOLEAN NOT NULL DEFAULT TRUE,
    can_manage_brand BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agency_id, telegram_user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_active_identity
ON partner_members(telegram_user_id) WHERE is_active=TRUE;

CREATE TABLE IF NOT EXISTS partner_brand_settings (
    agency_id BIGINT PRIMARY KEY REFERENCES partner_agencies(id) ON DELETE CASCADE,
    display_name VARCHAR(255) NOT NULL,
    logo_file_id TEXT,
    primary_color VARCHAR(20) DEFAULT '#0B2748',
    accent_color VARCHAR(20) DEFAULT '#D4AF37',
    public_phone VARCHAR(50),
    public_mobile VARCHAR(50),
    telegram_username VARCHAR(255),
    instagram_username VARCHAR(255),
    footer_text TEXT,
    cta_text TEXT DEFAULT 'برای دریافت اطلاعات و هماهنگی بازدید پیام بدهید.',
    show_registrar_name BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS partner_bot_connections (
    id BIGSERIAL PRIMARY KEY,
    agency_id BIGINT NOT NULL UNIQUE REFERENCES partner_agencies(id) ON DELETE CASCADE,
    bot_id BIGINT UNIQUE,
    bot_username VARCHAR(255),
    encrypted_token TEXT NOT NULL,
    token_fingerprint VARCHAR(64) NOT NULL,
    webhook_secret VARCHAR(128),
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
      CHECK (status IN ('ACTIVE','DISABLED','ERROR')),
    last_error TEXT,
    last_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS partner_channel_connections (
    id BIGSERIAL PRIMARY KEY,
    agency_id BIGINT NOT NULL REFERENCES partner_agencies(id) ON DELETE CASCADE,
    telegram_channel_id BIGINT NOT NULL,
    channel_username VARCHAR(255),
    channel_title VARCHAR(255),
    is_default BOOLEAN NOT NULL DEFAULT TRUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agency_id, telegram_channel_id)
);

CREATE TABLE IF NOT EXISTS partner_invites (
    id BIGSERIAL PRIMARY KEY,
    agency_id BIGINT NOT NULL REFERENCES partner_agencies(id) ON DELETE CASCADE,
    code VARCHAR(32) UNIQUE NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'ADVISOR',
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,
    created_by_member_id BIGINT REFERENCES partner_members(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS partner_submissions (
    id BIGSERIAL PRIMARY KEY,
    tracking_code VARCHAR(40) UNIQUE NOT NULL,
    agency_id BIGINT NOT NULL REFERENCES partner_agencies(id) ON DELETE RESTRICT,
    submitted_by_member_id BIGINT NOT NULL REFERENCES partner_members(id) ON DELETE RESTRICT,
    central_property_id BIGINT REFERENCES properties(id) ON DELETE SET NULL,
    property_type VARCHAR(50) NOT NULL,
    deal_type VARCHAR(30) NOT NULL DEFAULT 'SALE',
    district VARCHAR(255) NOT NULL,
    address_text TEXT NOT NULL,
    area_m2 NUMERIC(10,2) NOT NULL,
    bedrooms INTEGER,
    floor_text VARCHAR(100),
    total_floors INTEGER,
    units_per_floor INTEGER,
    build_year INTEGER,
    direction VARCHAR(100),
    parking_text TEXT,
    elevator_text TEXT,
    storage_text TEXT,
    deed_text TEXT,
    installations_text TEXT,
    current_price_toman BIGINT,
    rent_toman BIGINT,
    deposit_toman BIGINT,
    special_notes TEXT,
    owner_name TEXT,
    owner_phone TEXT,
    owner_address_private TEXT,
    status VARCHAR(30) NOT NULL DEFAULT 'DRAFT'
      CHECK (status IN ('DRAFT','SUBMITTED','UNDER_REVIEW','APPROVED','REJECTED','ACTIVE','SOLD','INACTIVE')),
    duplicate_score NUMERIC(5,2),
    duplicate_of_submission_id BIGINT REFERENCES partner_submissions(id) ON DELETE SET NULL,
    moderation_note TEXT,
    approved_by_telegram_id BIGINT,
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS partner_submission_media (
    id BIGSERIAL PRIMARY KEY,
    submission_id BIGINT NOT NULL REFERENCES partner_submissions(id) ON DELETE CASCADE,
    telegram_file_id TEXT NOT NULL,
    telegram_file_unique_id TEXT,
    media_type VARCHAR(20) NOT NULL DEFAULT 'PHOTO',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (submission_id, telegram_file_unique_id)
);

CREATE TABLE IF NOT EXISTS partner_publication_jobs (
    id BIGSERIAL PRIMARY KEY,
    submission_id BIGINT NOT NULL REFERENCES partner_submissions(id) ON DELETE CASCADE,
    target_type VARCHAR(30) NOT NULL
      CHECK (target_type IN ('AGENCY_CHANNEL','CENTRAL_CHANNEL')),
    channel_connection_id BIGINT REFERENCES partner_channel_connections(id) ON DELETE SET NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING'
      CHECK (status IN ('PENDING','SCHEDULED','PROCESSING','PUBLISHED','FAILED','CANCELLED')),
    scheduled_for TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    telegram_message_id BIGINT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS partner_audit_log (
    id BIGSERIAL PRIMARY KEY,
    agency_id BIGINT REFERENCES partner_agencies(id) ON DELETE SET NULL,
    actor_telegram_id BIGINT,
    action VARCHAR(100) NOT NULL,
    entity_type VARCHAR(50),
    entity_id BIGINT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_partner_submissions_agency_status ON partner_submissions(agency_id,status);
CREATE INDEX IF NOT EXISTS idx_partner_submissions_owner_phone ON partner_submissions(owner_phone);
CREATE INDEX IF NOT EXISTS idx_partner_jobs_due ON partner_publication_jobs(status,scheduled_for);
CREATE INDEX IF NOT EXISTS idx_partner_audit_agency ON partner_audit_log(agency_id,created_at DESC);

COMMIT;
