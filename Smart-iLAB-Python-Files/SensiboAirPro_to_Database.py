import logging
import re
import psycopg2
import json
import time
import datetime
import pytz
import os
timezone = "Asia/Singapore"

from dotenv import load_dotenv
from requests import get
from requests import post
from psycopg2 import sql
load_dotenv()

# §5.1 — gate writes to sensibo_<id> tables only.
_TABLE_NAME_RE = re.compile(r'^sensibo(_[a-z0-9_]+)?$')

def _safe_table_name(table_name: str) -> bool:
    return bool(_TABLE_NAME_RE.match(table_name))

# database variables to be used
database_row, database_table_name = {}, ""
data, columns, values = {}, {}, {}

# §5.3 — retry DB connect on startup so a brief Postgres unavailability doesn't kill the container.
def connect_db(max_attempts=30):
    last_err = None
    for attempt in range(max_attempts):
        try:
            return psycopg2.connect(host=os.getenv('DATABASE_IP'), dbname=os.getenv('DATABASE_USERNAME'), user=os.getenv('DATABASE_USERNAME'), password=os.getenv('DATABASE_PASSWORD'), port=os.getenv('DATABASE_PORT'))
        except psycopg2.OperationalError as err:
            last_err = err
            wait = min(2 ** attempt, 30)
            print(f"DB connection attempt {attempt + 1}/{max_attempts} failed: {err} — retrying in {wait}s")
            time.sleep(wait)
    raise last_err

# initialize connection to database and create cursor
conn = connect_db()
conn.autocommit = True
cur = conn.cursor()

# home assistant details and variables to be used
HOME_ASSISTANT_IP = os.getenv('HOME_ASSISTANT_URL').replace("http://", "")
TOKEN = os.getenv('HOME_ASSISTANT_TOKEN')
HEADERS = {"Authorization": f"Bearer {TOKEN}", "content-type": "application/json"}
url, response, data = "", "", ""
HOME_ASSISTANT_PORT = os.getenv('HOME_ASSISTANT_PORT')


def getAndInsertSensiboAirProData(deviceID: str):
    # §4.7-adjacent — deviceID flows into URL path; restrict to safe identifier chars.
    if not re.match(r'^[A-Za-z0-9._-]+$', deviceID):
        logging.warning("Refusing to query unsafe deviceID: %s", deviceID)
        return

    url = f"http://{HOME_ASSISTANT_IP}:{HOME_ASSISTANT_PORT}/api/states/{deviceID}"
    # Pre-declare so the except branch can reference it even if the try body throws early.
    database_table_name = "sensibo_" + deviceID.replace(".","_")
    if not _safe_table_name(database_table_name):
        logging.warning("Refusing to write to disallowed table: %s", database_table_name)
        return

    try:
        response = get(url=url, headers=HEADERS, timeout=10)

        data = json.loads(str(response.text))
        database_row.update({"timestamp":datetime.datetime.now(pytz.timezone(timezone)).strftime("%Y-%m-%d %X%z")})
        database_row.update({"temperature":data['attributes']['current_temperature']})
        database_row.update({"humidity":data['attributes']['current_humidity']})
        database_row.update({"hvac_mode":data['state']})
        database_row.update({"target_temperature":data['attributes']['temperature']})

        # §5.1 -- parameterized INSERT; §8.2 ON CONFLICT for idempotency where
        # PK(timestamp) exists, plain INSERT fall-back otherwise.
        col_list = list(database_row.keys())
        val_list = list(database_row.values())
        cols_sql = sql.SQL(', ').join(map(sql.Identifier, col_list))
        placeholders = sql.SQL(', ').join(sql.Placeholder() * len(col_list))
        try:
            cur.execute(
                sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (timestamp) DO NOTHING").format(
                    sql.Identifier(database_table_name), cols_sql, placeholders,
                ),
                val_list,
            )
        except psycopg2.errors.InvalidColumnReference:
            cur.execute(
                sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(database_table_name), cols_sql, placeholders,
                ),
                val_list,
            )

        print("[{timestamp}]  Device ID: {deviceID}  |  Data: {data}".format(timestamp=database_row['timestamp'], deviceID=deviceID, data=data))
        database_row.clear()
        return

    except Exception as err:

        try:
            cur.execute(
                sql.SQL("INSERT INTO error_logs ({}) VALUES (%s)").format(sql.Identifier(database_table_name)),
                [datetime.datetime.now(pytz.timezone(timezone)).strftime("%Y-%m-%d %X%z")],
            )
        except Exception:
            pass  # do not let a logging failure crash the bridge
        print("[{timestamp}]  Topic: {topic:<35}  |  Error: {err}".format(timestamp=datetime.datetime.now(pytz.timezone(timezone)).strftime("%Y-%m-%d %X%z"), topic="error_logs", err=str(err)))
        database_row.clear()
        return

DISCOVERY_REFRESH_INTERVAL_S = 60

def refresh_device_ids():
    """Re-poll the sensibo registry table (§5.5)."""
    try:
        cur.execute("SELECT * FROM sensibo")
        return [row[0] for row in cur.fetchall()]
    except Exception as err:
        print(f"Sensibo discovery refresh failed: {err}")
        return None

if __name__ == "__main__":
    print("Sensibo Air Pro to Database code started!")

    deviceIDs = refresh_device_ids() or []
    print("Sensibo Air Pro IDs:")
    for d in deviceIDs:
        print('-', d)
    print()

    last_refresh = time.time()
    while True:
        for deviceID in deviceIDs:
            getAndInsertSensiboAirProData(deviceID)
        print()
        time.sleep(10.0)
        # §5.5 — periodically re-discover devices added to the registry after startup.
        if time.time() - last_refresh > DISCOVERY_REFRESH_INTERVAL_S:
            new_ids = refresh_device_ids()
            if new_ids is not None:
                deviceIDs = new_ids
            last_refresh = time.time()
