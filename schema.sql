CREATE TABLE IF NOT EXISTS channels (
    id BIGSERIAL PRIMARY KEY,
    telegram_channel_id BIGINT UNIQUE,
    title VARCHAR(255),
    username VARCHAR(255),
    channel_type VARCHAR(30) NOT NULL
        CHECK (channel_type IN ('SOURCE', 'INPUT', 'DESTINATION', 'LOG')),
    access_mode VARCHAR(30) NOT NULL DEFAULT 'BOT'
        CHECK (access_mode IN ('BOT', 'CLIENT', 'MANUAL')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS properties (
    id BIGSERIAL PRIMARY KEY,

    internal_sequence INTEGER UNIQUE,
    unit_code VARCHAR(50) UNIQUE,

    property_type VARCHAR(50) DEFAULT 'APARTMENT',

    district VARCHAR(255),
    address_text TEXT,

    area_m2 NUMERIC(10,2),
    bedrooms INTEGER,
    floor_text VARCHAR(100),
    build_year INTEGER,

    has_parking BOOLEAN,
    has_elevator BOOLEAN,
    has_storage BOOLEAN,

    direction VARCHAR(100),

    current_price_toman BIGINT,
    current_price_billion NUMERIC(12,3),

    price_hashtag VARCHAR(255),

    status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE'
        CHECK (
            status IN (
                'ACTIVE',
                'SOLD',
                'INACTIVE',
                'INVALID',
                'DUPLICATE'
            )
        ),

    raw_text TEXT,
    normalized_text TEXT,

    published_message_id BIGINT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ
);


CREATE TABLE IF NOT EXISTS property_sources (
    id BIGSERIAL PRIMARY KEY,

    property_id BIGINT NOT NULL
        REFERENCES properties(id)
        ON DELETE CASCADE,

    source_channel_id BIGINT
        REFERENCES channels(id)
        ON DELETE SET NULL,

    source_message_id BIGINT,

    source_unit_code VARCHAR(100),

    source_post_url TEXT,

    source_price_toman BIGINT,
    source_raw_text TEXT,

    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    last_synced_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (source_channel_id, source_message_id)
);


CREATE TABLE IF NOT EXISTS property_media (
    id BIGSERIAL PRIMARY KEY,

    property_id BIGINT NOT NULL
        REFERENCES properties(id)
        ON DELETE CASCADE,

    telegram_file_id TEXT,
    telegram_file_unique_id TEXT,

    media_type VARCHAR(20) NOT NULL DEFAULT 'PHOTO'
        CHECK (media_type IN ('PHOTO', 'VIDEO', 'DOCUMENT')),

    media_group_id VARCHAR(255),
    position INTEGER NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS used_hashtags (
    id BIGSERIAL PRIMARY KEY,
    hashtag VARCHAR(255) NOT NULL UNIQUE,
    first_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    usage_count INTEGER NOT NULL DEFAULT 1
);


CREATE TABLE IF NOT EXISTS publish_queue (
    id BIGSERIAL PRIMARY KEY,

    property_id BIGINT NOT NULL
        REFERENCES properties(id)
        ON DELETE CASCADE,

    queue_status VARCHAR(30) NOT NULL DEFAULT 'READY'
        CHECK (
            queue_status IN (
                'NEW',
                'PARSED',
                'READY',
                'SCHEDULED',
                'PUBLISHED',
                'FAILED',
                'SKIPPED'
            )
        ),

    scheduled_for TIMESTAMPTZ,
    published_at TIMESTAMPTZ,

    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS schedule_windows (
    id BIGSERIAL PRIMARY KEY,

    weekday INTEGER NOT NULL
        CHECK (weekday BETWEEN 0 AND 6),

    start_time TIME NOT NULL,
    end_time TIME NOT NULL,

    interval_minutes INTEGER NOT NULL DEFAULT 30
        CHECK (interval_minutes >= 1),

    max_posts INTEGER,

    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS app_settings (
    setting_key VARCHAR(255) PRIMARY KEY,
    setting_value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS activity_logs (
    id BIGSERIAL PRIMARY KEY,

    event_type VARCHAR(100) NOT NULL,
    property_id BIGINT
        REFERENCES properties(id)
        ON DELETE SET NULL,

    channel_id BIGINT
        REFERENCES channels(id)
        ON DELETE SET NULL,

    message TEXT,
    metadata JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE INDEX IF NOT EXISTS idx_properties_unit_code
ON properties(unit_code);

CREATE INDEX IF NOT EXISTS idx_properties_status
ON properties(status);

CREATE INDEX IF NOT EXISTS idx_properties_price
ON properties(current_price_toman);

CREATE INDEX IF NOT EXISTS idx_property_sources_property
ON property_sources(property_id);

CREATE INDEX IF NOT EXISTS idx_property_sources_source_code
ON property_sources(source_unit_code);

CREATE INDEX IF NOT EXISTS idx_publish_queue_status
ON publish_queue(queue_status);

CREATE INDEX IF NOT EXISTS idx_publish_queue_schedule
ON publish_queue(scheduled_for);


INSERT INTO app_settings (setting_key, setting_value)
VALUES
(
    'unit_sequence',
    '{"current": 99}'::jsonb
)
ON CONFLICT (setting_key) DO NOTHING;
