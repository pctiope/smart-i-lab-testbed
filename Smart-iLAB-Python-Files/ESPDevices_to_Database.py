# python 3.11

import logging
import random
import re
import psycopg2
import json
import datetime
import time
import pytz
import os
import uuid
import threading
timezone = "Asia/Singapore"

from dotenv import load_dotenv
from psycopg2 import sql
from paho.mqtt import client as mqtt_client

# §5.1 — allow-list to gate which tables the subscriber may write to.
_ALLOWED_INSERT_PREFIXES = ('apollo_air_1', 'apollo_msr_2', 'athom_smart_plug_v2', 'airgradient_one')
_TABLE_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*$')

def _safe_table_name(table_name: str) -> bool:
    """Reject anything that isn't a plain identifier under an allowed prefix."""
    if not _TABLE_NAME_RE.match(table_name):
        return False
    return any(table_name == p or table_name.startswith(p + '_') for p in _ALLOWED_INSERT_PREFIXES)


# §5.11 / §8.3 — bounded buffer with periodic flush. Protects the DB from bursts;
# drops new messages once the queue exceeds _BATCH_QUEUE_CAP rather than growing memory.
from collections import defaultdict
_BATCH_INTERVAL_S = 1.0
_BATCH_MAX_SIZE = 200
_BATCH_QUEUE_CAP = 5000
_pending = []
_pending_lock = threading.Lock()
_pending_dropped = 0


def enqueue_insert(table_name, columns, values):
    """Queue a single row insert. Bounded; drops on overflow."""
    global _pending_dropped
    with _pending_lock:
        if len(_pending) >= _BATCH_QUEUE_CAP:
            _pending_dropped += 1
            if _pending_dropped % 100 == 1:
                logging.warning("Ingest queue full (%s); dropped %s messages so far", _BATCH_QUEUE_CAP, _pending_dropped)
            return
        _pending.append((table_name, tuple(columns), tuple(values)))
        flush_now = len(_pending) >= _BATCH_MAX_SIZE
    if flush_now:
        _flush_pending()


# Cache per-table: True if the table has a unique constraint on `timestamp`
# (ON CONFLICT (timestamp) is valid), False if not (must use plain INSERT).
# Populated lazily on first INSERT failure. Deployments where migration 006
# could not add the PK on some tables (because of pre-existing duplicate
# timestamps) MUST fall back to plain INSERT for those tables.
_pk_capable = {}


def _flush_pending():
    with _pending_lock:
        if not _pending:
            return
        batch = list(_pending)
        _pending.clear()
    groups = defaultdict(list)
    for tbl, cols, vals in batch:
        groups[(tbl, cols)].append(vals)
    for (tbl, cols), rows in groups.items():
        cols_sql = sql.SQL(', ').join(map(sql.Identifier, cols))
        placeholders = sql.SQL(', ').join(sql.Placeholder() * len(cols))
        try:
            if _pk_capable.get(tbl, True):
                stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (timestamp) DO NOTHING").format(
                    sql.Identifier(tbl), cols_sql, placeholders,
                )
            else:
                stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(tbl), cols_sql, placeholders,
                )
            cur.executemany(stmt, rows)
        except psycopg2.errors.InvalidColumnReference:
            # Table lacks a unique index/PK on `timestamp` -- fall back to plain
            # INSERT. Duplicates from broker redelivery will pass through (rare).
            _pk_capable[tbl] = False
            logging.warning("Table %s lacks PK(timestamp); falling back to plain INSERT", tbl)
            try:
                stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(tbl), cols_sql, placeholders,
                )
                cur.executemany(stmt, rows)
            except Exception as err:
                logging.warning("Batch flush (fallback) failed for %s: %s", tbl, err)
        except Exception as err:
            logging.warning("Batch flush failed for %s: %s", tbl, err)


def _flush_loop():
    while not FLAG_EXIT:
        time.sleep(_BATCH_INTERVAL_S)
        try:
            _flush_pending()
        except Exception as err:
            logging.error("Flush loop error: %s", err)

load_dotenv()
# MQTT Connection Setup
BROKER_IP = os.getenv('MQTT_IP').replace("mqtt://", "")
BROKER_PORT = int(os.getenv('MQTT_PORT'))
TOPIC = ["apollo_air_1",
"apollo_msr_2",
"athom_smart_plug_v2",
"airgradient_one"]
# §5.4 — UUID suffix so two replicas don't fight over the same broker session.
CLIENT_ID = f'ESPSubscriber-{os.getenv("HOSTNAME", "")[:8] or uuid.uuid4().hex[:8]}'
# Optional depending on Broker Security level
USERNAME = os.getenv('MQTT_USERNAME')
PASSWORD = os.getenv('MQTT_PASSWORD')
sTime = time.time()

# MQTT Reconnect Setup
FIRST_RECONNECT_DELAY = 1
RECONNECT_RATE = 2
MAX_RECONNECT_COUNT = 100
MAX_RECONNECT_DELAY = 60
FLAG_EXIT = False

# §5.5 — re-poll registry tables every N seconds so new devices get subscribed without restart.
DISCOVERY_REFRESH_INTERVAL_S = 60
_subscribed_topics = set()
_subscribed_lock = threading.Lock()

# §5.6 — expected payload columns per device type. Messages with unknown keys are dropped with a log line.
EXPECTED_COLUMNS = {
    'apollo_air_1': {'timestamp','co2','pressure','temperature','humidity','nox','voc','voc_quality','pm_1_0','pm_2_5','pm_4_0','pm_10_0','state','r','g','b','brightness'},
    # MSR-2 firmware fields. Both `zone_X_occupancy` (schema-native) and
    # `radar_zone_X_occupancy` (zone5 CV-package convention) are accepted so the
    # subscriber doesn't silently drop messages when firmware reporting changes.
    'apollo_msr_2': {'timestamp','co2','pressure','temperature','light','uv_index','detection_distance','moving_distance','still_distance','zone_1_occupancy','zone_2_occupancy','zone_3_occupancy','radar_zone_1_occupancy','radar_zone_2_occupancy','radar_zone_3_occupancy','radar_target','detection_target','moving_target','still_target','state','r','g','b','brightness','buzzer_state'},
    'athom_smart_plug_v2': {'timestamp','current','energy','power','total_daily_energy','total_energy','voltage','relay_state'},
    'airgradient_one': {'timestamp','co2','temperature','humidity','nox','voc','pm_0_3','pm_1_0','pm_2_5','pm_10_0','state','r','g','b','brightness'},
}

# §5.3 — retry DB connect on startup so a brief Postgres unavailability doesn't kill the container.
def connect_db(max_attempts=30):
    last_err = None
    for attempt in range(max_attempts):
        try:
            return psycopg2.connect(host=os.getenv('DATABASE_IP'), dbname=os.getenv('DATABASE_USERNAME'), user=os.getenv('DATABASE_USERNAME'), password=os.getenv('DATABASE_PASSWORD'), port=os.getenv('DATABASE_PORT'))
        except psycopg2.OperationalError as err:
            last_err = err
            wait = min(2 ** attempt, 30)
            logging.warning("DB connection attempt %s/%s failed: %s — retrying in %ss", attempt + 1, max_attempts, err, wait)
            time.sleep(wait)
    raise last_err

# DB Connection setup
conn = connect_db()
conn.autocommit = True
cur = conn.cursor()

def refresh_subscriptions(client):
    """Re-poll registry tables and subscribe to any newly-discovered device topics.
    Called once from on_connect and periodically from a background thread (§5.5)."""
    for i in TOPIC:
        try:
            cur.execute(f"SELECT id FROM {i};")
            ids = cur.fetchall()
        except Exception as err:
            logging.warning("Discovery query failed for %s: %s", i, err)
            continue

        for id in ids:

            if id:  # if the database table for the device/s listed in TOPIC contains values, create the respective table/s for the device if it doesn't exist yet

                fid = str(id).replace("(", "").replace("'", "").replace(")", "").replace(",", "")
                topic = f"{i}_{fid}/data"
                with _subscribed_lock:
                    if topic in _subscribed_topics:
                        continue
                ddl = None
                if i == "apollo_air_1":
                    ddl = f"""CREATE TABLE IF NOT EXISTS {i}_{fid}(
                        timestamp timestamp with time zone NOT NULL,
                        co2 integer,
                        pressure double precision,
                        temperature double precision,
                        humidity double precision,
                        nox integer,
                        voc integer,
                        voc_quality text,
                        pm_1_0 double precision,
                        pm_2_5 double precision,
                        pm_4_0 double precision,
                        state boolean,
                        r integer,
                        g integer,
                        b integer,
                        brightness double precision,
                        pm_10_0 double precision,
                        PRIMARY KEY (timestamp)
                        );"""
                elif i == "apollo_msr_2":
                    ddl = f"""CREATE TABLE IF NOT EXISTS {i}_{fid}(
                        timestamp timestamp with time zone NOT NULL,
                        co2 integer,
                        pressure double precision,
                        temperature double precision,
                        light double precision,
                        uv_index double precision,
                        detection_distance double precision,
                        moving_distance double precision,
                        still_distance double precision,
                        zone_1_occupancy text,
                        zone_2_occupancy text,
                        zone_3_occupancy text,
                        detection_target text,
                        moving_target text,
                        still_target text,
                        state boolean,
                        r double precision,
                        g double precision,
                        b double precision,
                        brightness double precision,
                        buzzer_state boolean,
                        PRIMARY KEY (timestamp)
                        );"""
                elif i == "athom_smart_plug_v2":
                    ddl = f"""CREATE TABLE IF NOT EXISTS {i}_{fid}(
                        timestamp timestamp with time zone NOT NULL,
                        current double precision,
                        energy double precision,
                        power double precision,
                        total_daily_energy double precision,
                        total_energy double precision,
                        voltage double precision,
                        relay_state boolean,
                        PRIMARY KEY (timestamp)
                        );"""
                elif i == "airgradient_one":
                    ddl = f"""CREATE TABLE IF NOT EXISTS {i}_{fid}(
                        timestamp timestamp with time zone NOT NULL,
                        co2 integer,
                        temperature double precision,
                        humidity double precision,
                        nox integer,
                        voc integer,
                        pm_0_3 double precision,
                        pm_1_0 double precision,
                        pm_2_5 double precision,
                        pm_10_0 double precision,
                        state boolean,
                        r integer,
                        g integer,
                        b integer,
                        brightness double precision,
                        PRIMARY KEY (timestamp)
                        );"""
                else:
                    print(f"Device Type {i} does not exist")

                if ddl:
                    cur.execute(ddl)
                    # §5.2 — convert to a TimescaleDB hypertable so REST API time_weight queries are fast.
                    # If TimescaleDB is not installed this is a no-op.
                    try:
                        cur.execute(
                            sql.SQL("SELECT create_hypertable({}, {}, if_not_exists => TRUE);").format(
                                sql.Literal(f"{i}_{fid}"),
                                sql.Literal('timestamp'),
                            )
                        )
                    except Exception as err:
                        logging.warning("create_hypertable failed for %s_%s (likely TimescaleDB not installed): %s", i, fid, err)

                # After creating tables for each device, if not existing already, subscribe to the device topics
                client.subscribe(topic)
                with _subscribed_lock:
                    _subscribed_topics.add(topic)
                logging.info("Subscribed to %s", topic)

            else:

                print(f"No existing {i} devices listed in the database")


def discovery_refresh_loop(client):
    """Background thread: re-poll registry tables for newly-added devices (§5.5)."""
    while not FLAG_EXIT:
        time.sleep(DISCOVERY_REFRESH_INTERVAL_S)
        try:
            refresh_subscriptions(client)
        except Exception as err:
            logging.warning("Discovery refresh failed: %s", err)


def on_connect(client, userdata, flags, rc):
    if rc == 0 and client.is_connected():
        print("Connected to MQTT Broker!")
        refresh_subscriptions(client)
    else:
        print(f'Failed to connect, return code {rc}')

def on_disconnect(client, userdata, rc):

    # Do not write to DB here — the connection may itself be why we are disconnected.
    logging.warning("ESP Devices subscriber disconnected with result code: %s", rc)
    reconnect_count, reconnect_delay = 0, FIRST_RECONNECT_DELAY
    while reconnect_count < MAX_RECONNECT_COUNT:
        logging.info("Reconnecting in %d seconds...", reconnect_delay)
        time.sleep(reconnect_delay)

        try:
            client.reconnect()
            logging.info("Reconnected successfully!")
            return
        except Exception as err:
            logging.error("%s. Reconnect failed. Retrying...", err)

        reconnect_delay *= RECONNECT_RATE
        reconnect_delay = min(reconnect_delay, MAX_RECONNECT_DELAY)
        reconnect_count += 1
    logging.info("Reconnect failed after %s attempts. Exiting...", reconnect_count)
    global FLAG_EXIT
    FLAG_EXIT = True

def on_message(client, userdata, msg):

    try:

        # Turns the message into a dictionary and save its keys to the variable
        table_name = msg.topic.replace("/data", "")
        msgJSON = json.loads(msg.payload)
        columns = list(msgJSON.keys())
        values = []

        for i in columns:
            values.append(msgJSON[i])

        # §5.6 — drop messages whose keys don't match the expected schema for this device type.
        device_type = '_'.join(table_name.split('_')[:3]) if table_name.startswith(('apollo_', 'athom_')) else '_'.join(table_name.split('_')[:2])
        # apollo_air_1, apollo_msr_2, athom_smart_plug_v2, airgradient_one — handle the 4-token cases:
        for known in ('apollo_air_1', 'apollo_msr_2', 'athom_smart_plug_v2', 'airgradient_one'):
            if table_name.startswith(known + '_') or table_name == known:
                device_type = known
                break
        allowed = EXPECTED_COLUMNS.get(device_type)
        if allowed is None:
            logging.warning("Unknown device type for %s; dropping message", table_name)
            return
        unknown_keys = set(columns) - allowed
        if unknown_keys:
            logging.warning("Unknown fields %s in %s; dropping message", unknown_keys, table_name)
            return

        # §5.1 — table name allow-listed; identifiers and values are bound through psycopg2.sql.
        if not _safe_table_name(table_name):
            logging.warning("Refusing to insert into disallowed table: %s", table_name)
            return

        enqueue_insert(table_name, tuple(columns), tuple(values))

        # log message packet
        print("[{timestamp}]  Topic: {topic:<35}  |  Data: {data}".format(timestamp=msgJSON["timestamp"], topic=msg.topic, data=str(msg.payload.decode("utf-8"))))
        return

    except Exception as err:

        try:
            payload_text = msg.payload.decode("utf-8")
            if _safe_table_name(table_name):
                try:
                    cur.execute(
                        sql.SQL("INSERT INTO error_logs ({}) VALUES (%s)").format(sql.Identifier(table_name)),
                        [payload_text],
                    )
                except Exception:
                    cur.execute(
                        sql.SQL("ALTER TABLE error_logs ADD COLUMN IF NOT EXISTS {} text").format(sql.Identifier(table_name))
                    )
                    cur.execute(
                        sql.SQL("INSERT INTO error_logs ({}) VALUES (%s)").format(sql.Identifier(table_name)),
                        [payload_text],
                    )
                print("[{timestamp}]  Topic: {topic:<35}  |  Data: {data}".format(timestamp=datetime.datetime.now(pytz.timezone(timezone)).strftime("%Y-%m-%d %X%z"), topic="error_logs", data=payload_text))
            else:
                logging.warning("Dropping unreadable payload from disallowed table %s: %s", table_name, err)
            return

        except Exception:

            print(f"[{datetime.datetime.now(pytz.timezone(timezone)).strftime("%Y-%m-%d %X%z")}] Unreadable message payload from {table_name}")
            return


# Establish Broker Connection
def connect_mqtt():
    client = mqtt_client.Client(CLIENT_ID)
    client.username_pw_set(USERNAME, PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER_IP, BROKER_PORT, keepalive=120)
    client.on_disconnect = on_disconnect
    return client

def run():
    logging.basicConfig(format='%(asctime)s - %(levelname)s: %(message)s',
                        level=logging.DEBUG)
    client = connect_mqtt()
    # §5.5 — kick off the periodic discovery thread so new devices get subscribed without restart.
    threading.Thread(target=discovery_refresh_loop, args=(client,), daemon=True, name='discovery_refresh').start()
    # §5.11 / §8.3 — flush thread drains the batched insert queue every _BATCH_INTERVAL_S.
    threading.Thread(target=_flush_loop, daemon=True, name='ingest_flush').start()
    client.loop_forever()

if __name__ == '__main__':
    run()
