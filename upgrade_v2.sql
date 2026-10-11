CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(100) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name VARCHAR(255),
    role VARCHAR(30) NOT NULL DEFAULT 'USER'
        CHECK (role IN ('ADMIN', 'USER')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL
        REFERENCES users(id)
        ON DELETE CASCADE,
    telegram_user_id BIGINT UNIQUE NOT NULL,
    logged_in_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS neighborhood VARCHAR(255);

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS street VARCHAR(255);

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS units_per_floor INTEGER;

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS deed_text VARCHAR(255);

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS installations_text VARCHAR(255);

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS parking_text VARCHAR(255);

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS storage_text VARCHAR(255);

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS elevator_text VARCHAR(255);

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS special_notes TEXT;

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS has_media BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS channel_post_url TEXT;

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS deleted_from_channel_at TIMESTAMPTZ;

ALTER TABLE properties
ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ DEFAULT NOW();

CREATE TABLE IF NOT EXISTS reset_runs (
    id BIGSERIAL PRIMARY KEY,
    requested_by_user_id BIGINT
        REFERENCES users(id)
        ON DELETE SET NULL,
    cutoff_date TIMESTAMPTZ NOT NULL,
    matched_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING'
        CHECK (
            status IN (
                'PENDING',
                'RUNNING',
                'COMPLETED',
                'PARTIAL',
                'FAILED',
                'CANCELLED'
            )
        ),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reset_items (
    id BIGSERIAL PRIMARY KEY,
    reset_run_id BIGINT NOT NULL
        REFERENCES reset_runs(id)
        ON DELETE CASCADE,
    property_id BIGINT
        REFERENCES properties(id)
        ON DELETE SET NULL,
    telegram_message_id BIGINT,
    result_status VARCHAR(30) NOT NULL DEFAULT 'PENDING'
        CHECK (
            result_status IN (
                'PENDING',
                'DELETED',
                'FAILED',
                'SKIPPED'
            )
        ),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_properties_district
ON properties(district);

CREATE INDEX IF NOT EXISTS idx_properties_neighborhood
ON properties(neighborhood);

CREATE INDEX IF NOT EXISTS idx_properties_published_at
ON properties(published_at);

CREATE INDEX IF NOT EXISTS idx_properties_active_price
ON properties(status, current_price_billion);

CREATE INDEX IF NOT EXISTS idx_user_sessions_telegram_user
ON user_sessions(telegram_user_id);

CREATE INDEX IF NOT EXISTS idx_reset_runs_created
ON reset_runs(created_at);
