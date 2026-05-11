-- Enable TimescaleDB and the toolkit extension (used by REST API time_weight queries).
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit;
