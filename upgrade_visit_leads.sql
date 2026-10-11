BEGIN;

-- =========================================================
-- CUSTOMER PROFILE
-- One permanent profile per Telegram user.
-- Phone number is requested only once.
-- =========================================================

CREATE TABLE IF NOT EXISTS customer_profiles (
    id BIGSERIAL PRIMARY KEY,

    telegram_user_id BIGINT NOT NULL UNIQUE,
    telegram_username VARCHAR(255),

    first_name VARCHAR(255),
    last_name VARCHAR(255),

    phone_number VARCHAR(64),
    phone_shared_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_interaction_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_customer_profiles_phone
ON customer_profiles(phone_number);


-- =========================================================
-- PENDING CUSTOMER ACTION
--
-- Keeps the customer's current action in PostgreSQL instead
-- of only Telegram context.user_data.
--
-- This means a bot restart while waiting for a phone number
-- does not destroy the flow.
--
-- Future examples:
-- VISIT
-- SELL_PROPERTY
-- =========================================================

CREATE TABLE IF NOT EXISTS customer_pending_actions (
    id BIGSERIAL PRIMARY KEY,

    customer_id BIGINT NOT NULL UNIQUE
        REFERENCES customer_profiles(id)
        ON DELETE CASCADE,

    action_type VARCHAR(50) NOT NULL,

    property_id BIGINT
        REFERENCES properties(id)
        ON DELETE SET NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_customer_pending_action_type
ON customer_pending_actions(action_type);


-- =========================================================
-- VISIT REQUESTS
-- =========================================================

CREATE TABLE IF NOT EXISTS visit_requests (
    id BIGSERIAL PRIMARY KEY,

    customer_id BIGINT NOT NULL
        REFERENCES customer_profiles(id)
        ON DELETE CASCADE,

    property_id BIGINT
        REFERENCES properties(id)
        ON DELETE SET NULL,

    unit_code_snapshot VARCHAR(100),
    channel_post_url_snapshot TEXT,

    status VARCHAR(30) NOT NULL DEFAULT 'NEW',

    source VARCHAR(50) NOT NULL DEFAULT 'CHANNEL_BUTTON',

    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at TIMESTAMPTZ,

    contacted_at TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT visit_requests_status_check
    CHECK (
        status IN (
            'NEW',
            'CONTACTED',
            'SCHEDULED',
            'CLOSED',
            'CANCELLED'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_visit_requests_customer
ON visit_requests(customer_id);

CREATE INDEX IF NOT EXISTS idx_visit_requests_property
ON visit_requests(property_id);

CREATE INDEX IF NOT EXISTS idx_visit_requests_status
ON visit_requests(status);

-- A customer cannot create 20 simultaneous open requests
-- for exactly the same property by repeatedly pressing Start.
CREATE UNIQUE INDEX IF NOT EXISTS uq_visit_request_open
ON visit_requests(customer_id, property_id)
WHERE status IN (
    'NEW',
    'CONTACTED',
    'SCHEDULED'
);


-- =========================================================
-- VISIT OPERATORS
--
-- Operators are linked to existing management users.
-- We will build the management UI for toggling them next.
-- =========================================================

CREATE TABLE IF NOT EXISTS visit_operators (
    id BIGSERIAL PRIMARY KEY,

    user_id BIGINT NOT NULL UNIQUE
        REFERENCES users(id)
        ON DELETE CASCADE,

    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    notify_new_visits BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- For first installation:
-- existing active ADMIN accounts receive visit notifications.
-- Later they can be managed from the bot UI.
INSERT INTO visit_operators (
    user_id,
    is_active,
    notify_new_visits
)
SELECT
    id,
    TRUE,
    TRUE
FROM users
WHERE role = 'ADMIN'
  AND is_active = TRUE
ON CONFLICT (user_id)
DO NOTHING;

COMMIT;
