import logging
import re
import psycopg2
import json
import time
import datetime
import pytz
import os
import uuid
import threading
timezone = "Asia/Singapore"

from dotenv import load_dotenv
from psycopg2 import sql
from paho.mqtt import client as mqtt_client
load_dotenv()

# §5.1 — gate which tables the subscriber may write to. Per-device tables follow
# the zigbee2mqtt_<id> shape; reject anything else derived from MQTT topics.
_TABLE_NAME_RE = re.compile(r'^zigbee2mqtt(_[a-z0-9_]+)?$')

def _safe_table_name(table_name: str) -> bool:
    return bool(_TABLE_NAME_RE.match(table_name))

# mqtt broker details
BROKER_IP = os.getenv('MQTT_IP').replace("mqtt://", "")
BROKER_PORT = int(os.getenv('MQTT_PORT'))
BROKER_USERNAME = os.getenv('MQTT_USERNAME')
BROKER_PASSWORD = os.getenv('MQTT_PASSWORD')
# §5.4 — UUID suffix so two replicas don't fight over the same broker session.
BROKER_CLIENT_ID = f'Zigbee2MQTT-{os.getenv("HOSTNAME", "")[:8] or uuid.uuid4().hex[:8]}'
FIRST_RECONNECT_DELAY = 1
RECONNECT_RATE = 2
MAX_RECONNECT_COUNT = 100
MAX_RECONNECT_DELAY = 60
FLAG_EXIT = False

# §5.5 — re-poll registry every minute so new devices get subscribed without restart.
DISCOVERY_REFRESH_INTERVAL_S = 60
_subscribed_topics = set()
_subscribed_lock = threading.Lock()

# database variables
database_row, database_table_name = {}, ""
data, columns, values = {}, {}, {}
id_index, type_index, base_topic_index = None, None, None
light_state_index, light_brightness_index, light_color_temp_index = None, None, None
switch_state_index = None
blinds_state_index, blinds_position_index, blinds_motor_state_index, blinds_running_index = None, None, None, None


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

# initialize connection to database and create cursor
conn = connect_db()
conn.autocommit = True
cur1 = conn.cursor()
cur2 = conn.cursor()



def on_disconnect(client, userdata, rc):
    logging.info("Disconnected with result code: %s", rc)
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
    return

def on_connect(client, userdata, flags, rc):
    if rc == 0 and client.is_connected():
        print("\nConnected to MQTT Broker!\n")

        global database_row, database_table_name
        global data, columns, values
        global id_index, type_index, base_topic_index
        global light_state_index, light_brightness_index, light_color_temp_index
        global switch_state_index
        global blinds_state_index, blinds_position_index, blinds_motor_state_index, blinds_running_index

        # get all the zigbee2mqtt device IDs from the database
        cur1.execute("SELECT * FROM zigbee2mqtt;")
        id, type, base_topic = "", "", ""

        # get the column order of the zigbee2mqtt table
        cur2.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'zigbee2mqtt';")
        data = tuple(cur2)
        id_index, type_index, base_topic_index = data.index(('id', 'text')), data.index(('type', 'text')), data.index(('base_topic', 'text'))

        _subscribe_devices(client, cur1)
        print()
    else:
        print(f'Failed to connect, return code {rc}')
    return


def _subscribe_devices(client, cursor):
    """Subscribe to any device topics we haven't subscribed to yet (§5.5)."""
    global data
    global light_state_index, light_brightness_index, light_color_temp_index
    global switch_state_index
    global blinds_state_index, blinds_position_index, blinds_motor_state_index, blinds_running_index

    for row in cursor:
        id = row[id_index]
        type = row[type_index]
        base_topic = row[base_topic_index]

        # do not subscribe to group topics, get the column order of the tables for each type of device
        if type == 'group':
            continue
        topic_set = f"{base_topic}/{id}/set"
        with _subscribed_lock:
            if topic_set in _subscribed_topics:
                continue
        if type == 'lights' and light_state_index is None:
            cur2.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s;", [f"zigbee2mqtt_{id}"])
            data = tuple(cur2)
            light_state_index = data.index(('state', 'text'))
            light_brightness_index = data.index(('brightness', 'integer'))
            light_color_temp_index = data.index(('color_temp', 'integer'))
        elif type == 'switch' and switch_state_index is None:
            cur2.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s;", [f"zigbee2mqtt_{id}"])
            data = tuple(cur2)
            switch_state_index = data.index(('state', 'text'))
        elif type == 'blinds' and blinds_state_index is None:
            cur2.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s;", [f"zigbee2mqtt_{id}"])
            data = tuple(cur2)
            blinds_state_index = data.index(('state', 'text'))
            blinds_position_index = data.index(('position', 'integer'))
            blinds_motor_state_index = data.index(('motor_state', 'text'))
            blinds_running_index = data.index(('running', 'boolean'))

        # subscribe to the MQTT topics
        client.subscribe(topic_set)
        print(f"Subscribed to {topic_set}")
        topic_state = f"{base_topic}/{id}"
        client.subscribe(topic_state)
        print(f"Subscribed to {topic_state}")
        with _subscribed_lock:
            _subscribed_topics.add(topic_set)
            _subscribed_topics.add(topic_state)

        # publish to the /get topic to get the latest state of the device
        client.publish(f"{base_topic}/{id}/get", '{"state": ""}')


def discovery_refresh_loop(client):
    """Re-poll zigbee2mqtt registry every minute and subscribe to newly-added devices (§5.5)."""
    while not FLAG_EXIT:
        time.sleep(DISCOVERY_REFRESH_INTERVAL_S)
        try:
            cur1.execute("SELECT * FROM zigbee2mqtt;")
            _subscribe_devices(client, cur1)
        except Exception as err:
            logging.warning("Zigbee discovery refresh failed: %s", err)

def on_message(client, userdata, msg):
    try:
        # check if there is any data in the message payload
        if not msg.payload:
            print(f'Message from {msg.topic} has empty payload.')
            return

        global database_row, database_table_name
        global data, columns, values
        global id_index, type_index, base_topic_index
        
        # get the database table using the topic
        database_table_name = ("zigbee2mqtt" + msg.topic[msg.topic.find(f"/"):]).replace("/set","").replace("/","_")
        if not _safe_table_name(database_table_name):
            logging.warning("Refusing to query disallowed table: %s", database_table_name)
            return

        # get the latest data from the zigbee2mqtt device from the database (§5.1)
        cur1.execute(
            sql.SQL("SELECT * FROM {} ORDER BY timestamp DESC LIMIT 1").format(sql.Identifier(database_table_name))
        )
        last_inserted_row = cur1.fetchone()

        # input the timestamp for the received packet
        database_row.update({"timestamp":datetime.datetime.now(pytz.timezone(timezone)).strftime("%Y-%m-%d %X%z")})

        data = json.loads(msg.payload)
        # store the data to be inserted into the database, parameters are based on the device type
        cur1.execute("SELECT * FROM zigbee2mqtt WHERE id = %s;", [database_table_name.replace("zigbee2mqtt_", "")])
        device_type = (cur1.fetchone())[type_index]
        
        if device_type == "lights":
            if "state" in data:
                database_row.update({"state":data["state"]})
            else:
                database_row.update({"state":last_inserted_row[light_state_index]})
            if "brightness" in data:
                database_row.update({"brightness":data["brightness"]})
            else:
                database_row.update({"brightness":last_inserted_row[light_brightness_index]})
            if "color_temp" in data:
                database_row.update({"color_temp":data["color_temp"]})
            else:
                database_row.update({"color_temp":last_inserted_row[light_color_temp_index]})
        elif device_type == "switch":
            if "state" in data:
                database_row.update({"state":data["state"]})
            else:
                database_row.update({"state":last_inserted_row[switch_state_index]})
        elif device_type == "blinds":
            if "state" in data:
                database_row.update({"state":data["state"]})
            else:
                database_row.update({"state":last_inserted_row[blinds_state_index]})
            if "position" in data:
                database_row.update({"position":data["position"]})
            else:
                database_row.update({"position":last_inserted_row[blinds_position_index]})
            if "motor_state" in data:
                database_row.update({"motor_state":data["motor_state"]})
            else:
                database_row.update({"motor_state":last_inserted_row[blinds_motor_state_index]})
            if "running" in data:
                database_row.update({"running":data["running"]})
            else:
                database_row.update({"running":last_inserted_row[blinds_running_index]})
        
        # §5.1 -- parameterized INSERT; §8.2 ON CONFLICT for idempotency where
        # PK(timestamp) exists, plain INSERT fall-back otherwise.
        col_list = list(database_row.keys())
        val_list = list(database_row.values())
        cols_sql = sql.SQL(', ').join(map(sql.Identifier, col_list))
        placeholders = sql.SQL(', ').join(sql.Placeholder() * len(col_list))
        try:
            cur1.execute(
                sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (timestamp) DO NOTHING").format(
                    sql.Identifier(database_table_name), cols_sql, placeholders,
                ),
                val_list,
            )
        except psycopg2.errors.InvalidColumnReference:
            cur1.execute(
                sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(database_table_name), cols_sql, placeholders,
                ),
                val_list,
            )

        # print the topic and the data inserted into the database
        print("[{timestamp}]  Topic: {topic:<35}  |  Data: {data}".format(timestamp=database_row['timestamp'], topic=msg.topic, data=data))
        database_row.clear()
        return

    except Exception:

        # database error logging — parameterized so a crafted payload can't inject SQL.
        try:
            if _safe_table_name(database_table_name):
                cur1.execute(
                    sql.SQL("INSERT INTO error_logs ({}) VALUES (%s)").format(sql.Identifier(database_table_name)),
                    [str(database_row)],
                )
        except Exception:
            pass
        print("[{timestamp}]  Topic: {topic:<35}  |  Data: {data}".format(timestamp=database_row.get('timestamp', ''), topic="error_logs", data=str(database_row)))
        database_row.clear()
        return
        



if __name__ == "__main__":
    print("Zigbee2MQTT to Database code started!\n")

    logging.basicConfig(format='%(asctime)s - %(levelname)s: %(message)s', level=logging.DEBUG)
    client = mqtt_client.Client(client_id=BROKER_CLIENT_ID)
    client.username_pw_set(BROKER_USERNAME, BROKER_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.connect(BROKER_IP, BROKER_PORT, keepalive=120)
    # §5.5 — kick off periodic discovery so new Zigbee devices get subscribed without restart.
    threading.Thread(target=discovery_refresh_loop, args=(client,), daemon=True, name='zigbee_discovery_refresh').start()
    client.loop_forever()
