-- Device groups for aggregate control.
CREATE TABLE IF NOT EXISTS groups (
    id                       TEXT PRIMARY KEY,
    apollo_air_1_ids         TEXT[],
    apollo_msr_2_ids         TEXT[],
    athom_smart_plug_v2_ids  TEXT[],
    zigbee2mqtt_ids          TEXT[]
);
