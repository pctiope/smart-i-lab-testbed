-- Registry tables — one row per known device. Per-device sensor tables
-- (apollo_air_1_<id> etc.) are created by the Python subscriber.

CREATE TABLE IF NOT EXISTS apollo_air_1 (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS apollo_msr_2 (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS athom_smart_plug_v2 (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS airgradient_one (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sensibo (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS zigbee2mqtt (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,        -- 'lights' | 'switch' | 'blinds' | 'group'
    base_topic TEXT NOT NULL
);
