'use strict';
// Index JS
// Author: SSL - IoT 1
// University of the Philippines - Diliman Electrical and Electronics Engineering Institute

// ------- START NodeJS/Express Setup ------ //
const fs = require("fs/promises");
const express = require("express");
const cors = require("cors");
const helmet = require("helmet");
const rateLimit = require("express-rate-limit");
const { v4: uuidv4 } = require("uuid");
require('dotenv').config();
const mqtt = require("mqtt");
const url = `${process.env.MQTT_IP}:${process.env.MQTT_PORT}`;

const app = express();
app.use(helmet());
app.use(cors({origin: process.env.ALLOWED_ORIGINS ? process.env.ALLOWED_ORIGINS.split(',').map(s => s.trim()).filter(Boolean) : false}));
app.use(express.json({limit: '10kb'}));
app.use((req, res, next) => {
    req.setTimeout(15000, () => { if (!res.headersSent) res.status(503).json({error: 'Request timeout'}); });
    next();
});

const authLimiter = rateLimit({windowMs: 15 * 60 * 1000, max: 100, standardHeaders: true, legacyHeaders: false});
const writeLimiter = rateLimit({windowMs: 60 * 1000, max: 60, standardHeaders: true, legacyHeaders: false});
app.use(['/access', '/users'], authLimiter);
app.use((req, res, next) => {
    if (req.method === 'POST' || req.method === 'PUT' || req.method === 'DELETE') return writeLimiter(req, res, next);
    next();
});
// -------- END NodeJS/Express Setup ------- //


// -- START PostgreSQL Connection Options -- //
const {Pool} = require('pg');
const format = require('pg-format');

// Pool auto-reconnects on transient failures and serves concurrent queries.
// Keep the variable name `client` so the rest of the file's .query() calls work unchanged.
const client = new Pool({
    host: process.env.DATABASE_IP,
    user: process.env.DATABASE_USERNAME,
    port: process.env.DATABASE_PORT,
    password: process.env.DATABASE_PASSWORD,
    database: process.env.DATABASE_NAME,
    max: 20,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 10000,
});
client.on('error', (err) => server_Log(`PG pool error — ${sanitizePgError(err)}`));
// --- END PostgreSQL Connection Options --- //

// Allow-list of column names per device-table prefix, used by GET_avg to
// reject column-enumeration attacks via the sensData query parameter (§4.3).
const SENSOR_COLUMNS = {
    apollo_air_1:        ['co2','pressure','temperature','humidity','nox','voc','pm_1_0','pm_2_5','pm_4_0','pm_10_0','brightness'],
    apollo_msr_2:        ['co2','pressure','temperature','light','uv_index','detection_distance','moving_distance','still_distance','brightness','zone_1_occupancy','zone_2_occupancy','zone_3_occupancy','radar_zone_1_occupancy','radar_zone_2_occupancy','radar_zone_3_occupancy'],
    athom_smart_plug_v2: ['current','energy','power','total_daily_energy','total_energy','voltage'],
    airgradient_one:     ['co2','temperature','humidity','nox','voc','pm_0_3','pm_1_0','pm_2_5','pm_10_0','brightness'],
    sensibo:             ['temperature','humidity','target_temperature'],
    zigbee2mqtt:         ['brightness','position'],
};


// ----- START MQTT Connection Options ----- //
const options = {
    // Clean session
    clean: true,
    connectTimeout: 4000,
    // Authentication
    clientId: process.env.MQTT_CLIENT_ID,
    username: process.env.MQTT_USERNAME,
    password: process.env.MQTT_PASSWORD,
    reconnectPeriod: process.env.MQTT_RECONNECT_PERIOD,
}

const mqttclient = mqtt.connect(url, options);
// ------ END MQTT Connection Options ------ //



// --- START Standardized Function Calls --- //

// Server logging

async function server_Log(logs) {
    try{
        const date = new Date();
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0'); // Month is 0-indexed
        const day = String(date.getDate()).padStart(2, '0');
        const hours = String(date.getHours()).padStart(2, '0');
        const minutes = String(date.getMinutes()).padStart(2, '0');
        const seconds = String(date.getSeconds()).padStart(2, '0');
        const mseconds = String(date.getMilliseconds()).padStart(3, '0');

        console.log(`[${year}-${month}-${day} ${hours}:${minutes}:${seconds}:${mseconds}] ${logs}`);
        return;
    }catch(err){
        console.log(`Internal Server Error: An unexpected error occurred in the server logging function\n${err && err.message ? err.message : err}`);
        return '';
    }
}

// Sanitize PG errors for logs — keep SQLSTATE + message, drop detail/hint/position which can leak schema.
function sanitizePgError(err) {
    if (!err) return 'unknown error';
    if (err.code) return `[${err.code}] ${err.message || ''}`;
    return err.message || String(err);
}

// Auth cache — 5 min TTL on api_key → {user_name, access_level}
const AUTH_CACHE_TTL_MS = 5 * 60 * 1000;
const authCache = new Map();
function authCacheGet(api_key) {
    const v = authCache.get(api_key);
    if (!v) return null;
    if (Date.now() > v.expiresAt) { authCache.delete(api_key); return null; }
    return v;
}
function authCacheSet(api_key, user_name, access_level) {
    authCache.set(api_key, {user_name, access_level, expiresAt: Date.now() + AUTH_CACHE_TTL_MS});
}
function authCacheInvalidate(api_key) { authCache.delete(api_key); }

// ____ SECURITY START ____

async function USER_is_available(user_name) {
    try{
        const queryResult = await client.query('SELECT * FROM users WHERE user_name = $1;', [user_name]);
        return !(queryResult.rowCount === 0);
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred in USER_is_available function\n${err}`);
        return false;
    }
}

async function KEY_is_available(api_key) {
    if (authCacheGet(api_key)) return true;
    try{
        const queryResult = await client.query('SELECT * FROM users WHERE api_key = $1;', [api_key]);
        return !(queryResult.rowCount === 0);
    }catch(err){
        server_Log(`Internal Server Error: KEY_is_available — ${sanitizePgError(err)}`);
        return false;
    }
}

async function RETURN_access_level(api_key) {
    const cached = authCacheGet(api_key);
    if (cached) return cached.access_level;
    const result = await client.query('SELECT user_name, access_level FROM users WHERE api_key = $1;', [api_key]);
    if (result.rowCount === 0) throw new Error('api_key not found');
    const {user_name, access_level} = result.rows[0];
    authCacheSet(api_key, user_name, access_level);
    return access_level;
}

async function RETURN_user_name(api_key) {
    const cached = authCacheGet(api_key);
    if (cached) return cached.user_name;
    try{
        const result = await client.query('SELECT user_name, access_level FROM users WHERE api_key = $1;', [api_key]);
        if (result.rowCount === 0) return '';
        const {user_name, access_level} = result.rows[0];
        authCacheSet(api_key, user_name, access_level);
        return user_name;
    }catch(err){
        server_Log(`Internal Server Error: RETURN_user_name — ${sanitizePgError(err)}`);
        return '';
    }
}

async function SECURITY_CHECK(res, req, api_key, array) {
    try{
        let to_verify = await KEY_is_available(api_key);
        if(!to_verify){
            server_Log(`Not Found: API Key does not exist`);
            res.status(401).json({ error: `API Key does not exist: Ensure your API key is valid and correctly provided.`});
            return false;
        }

        const access_level = await RETURN_access_level(api_key);

        if(array.includes(access_level)){
            return true;
        }

        server_Log(`Forbidden Request: User does not have access to this endpoint`);
        res.status(403).json({ error: `Forbidden Request: User does not have access to this endpoint`});
        return false;
    }catch(err){
        server_Log(`Internal Server Error: SECURITY_CHECK — ${sanitizePgError(err)}`);
        if (!res.headersSent) res.status(500).json({error: 'Internal Server Error'});
        return false;
    }
}
// ____ SECURITY END ____

// ____ TRANSACTIONS START ____
async function getCurrentTimestamp() {
    try{
        const date = new Date();
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0'); // Month is 0-indexed
        const day = String(date.getDate()).padStart(2, '0');
        const hours = String(date.getHours()).padStart(2, '0');
        const minutes = String(date.getMinutes()).padStart(2, '0');
        const seconds = String(date.getSeconds()).padStart(2, '0');

        return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred in getCurrentTimestamp function\n${err}`);
        return '';
    }
}

async function UPDATE_transactions(api_key, type, uri, success) {
    try{
        const user_name = await RETURN_user_name(api_key);
        const ts = await getCurrentTimestamp();
        // Digital_Twin used to be skipped here; now logged so the audit trail has no blind spot.
        client.query(`INSERT INTO transactions (timestamp, user_name, type, uri, success) VALUES ($1, $2, $3, $4, $5);`, [ts, user_name, type, uri, success], (err) => {
            if(err){
                server_Log(`ERROR: Unsuccessfully logged transaction — ${sanitizePgError(err)}`);
            }
        })
    }catch(err){
        server_Log(`Internal Server Error: UPDATE_transactions — ${sanitizePgError(err)}`);
    }
}
// ____ TRANSACTIONS END ____

// ____ ENDPOINTS START ____
// Check if an ID is available in the database
async function ID_is_available(table, deviceID){
    try{
        const queryResult = await client.query(format('SELECT * FROM %I WHERE id = $1;', table), [deviceID]);
        return !(queryResult.rowCount === 0);
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred in ID_is_available function\n${err}`);
        return false;
    }
}

// GET IDs
async function GET_ids(res, req, deviceName, api_key, type, uri){
    try{
        if(await SECURITY_CHECK(res, req, api_key, [0,1,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const queryResult = await client.query(`SELECT * FROM ${deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_")};`);

        if(queryResult.rowCount){
            let ids = [];
            for (let index = 0; index < queryResult.rowCount; index++) {
                ids.push(queryResult.rows[index]["id"]);
            }
            server_Log(`Successfully returned all available ${deviceName} IDs`);
            UPDATE_transactions(api_key, type, uri, true);
            res.json(ids);
        }else{
            server_Log(`There are no ${deviceName} IDs to return`);
            UPDATE_transactions(api_key, type, uri, false);
            res.json([]);
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
}

// GET most recent/historical data
async function GET_data(res, req, deviceName, api_key, type, uri){
    try{
        if(await SECURITY_CHECK(res, req, api_key, [0,1,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const deviceID = req.params.id;
        // Optional arguments; will be NULL if not provided
        const timeStart = req.query.time_start;
        const timeEnd = req.query.time_end;

        // Step 1: Check if an deviceName with ID: deviceID is available
        if(await ID_is_available(`${deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_")}`, deviceID) === false){
            server_Log(`Not Found: ${deviceName} with ID: ${deviceID} does not exist`);
            UPDATE_transactions(api_key, type, uri, false);
            return res.status(404).json({error: `Not Found: ${deviceName} with ID: ${deviceID} does not exist`});
        }

        // Step 2: Return data based on parameter values
        if(!timeStart && !timeEnd){
            // [A] If NO optional parameter values were given
            const queryText = format('SELECT * FROM %I ORDER BY timestamp DESC LIMIT 1;', `${deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_")}_${deviceID.replaceAll(".","_")}`);
            try {
                const data = await client.query(queryText);
                server_Log(`Successfully returned most recent data from ${deviceName} with ID: ${deviceID}`);
                UPDATE_transactions(api_key, type, uri, true);
                return res.json(data.rows[0]);
            } catch (err) {
                server_Log(`Internal Server Error: Unable to get most recent data from ${deviceName} with ID: ${deviceID} — ${sanitizePgError(err)}`);
                return res.status(500).send(`Internal Server Error: Unable to get most recent data from ${deviceName} with ID: ${deviceID}`);
            }
        }else if(timeStart && timeEnd){
            // [B] If optional parameter values for timeStart and timeEnd were given
            const queryText = format('SELECT * FROM %I WHERE timestamp BETWEEN $1 AND $2;', `${deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_")}_${deviceID.replaceAll(".","_")}`);
            try {
                const data = await client.query(queryText, [timeStart, timeEnd]);
                server_Log(`Successfully returned historical data (${timeStart} to ${timeEnd}) from ${deviceName} with ID: ${deviceID}`);
                UPDATE_transactions(api_key, type, uri, true);
                return res.json(data.rows);
            } catch (err) {
                server_Log(`Bad Request: Invalid arguments in historical data GET — ${sanitizePgError(err)}`);
                return res.status(400).json({error: `Bad Request: Invalid arguments in historical data GET request for ${deviceName} with ID: ${deviceID}`});
            }
        }else{
            // [C] If optional parameter values are INCOMPLETE (Error 400)
            server_Log(`Bad Request: Incomplete parameters in GET request for ${deviceName} with ID: ${deviceID}`);
            UPDATE_transactions(api_key, type, uri, false);
            return res.status(400).json({error: `Bad Request: Incomplete parameters in GET request for ${deviceName} with ID: ${deviceID}`});
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
}

// GET average of historical data
async function GET_avg(res, req, deviceName, api_key, type, uri){
    try{
        if(await SECURITY_CHECK(res, req, api_key, [0,1,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const deviceID = req.params.id;
        const specData = req.query.sensData;
        // Optional arguments; will be NULL if not provided
        const timeStart = req.query.time_start;
        const timeEnd = req.query.time_end;

        const tablePrefix = deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_");

        // Step 1a: Validate specData against the per-device allow-list (§4.3).
        if (specData && !(SENSOR_COLUMNS[tablePrefix] || []).includes(specData)) {
            server_Log(`Bad Request: invalid sensData '${specData}' for ${deviceName}`);
            UPDATE_transactions(api_key, type, uri, false);
            return res.status(400).json({error: `Bad Request: invalid sensData for ${deviceName}`});
        }

        // Step 1: Check if an deviceName with ID: deviceID is available
        if(await ID_is_available(tablePrefix, deviceID) === false){
            server_Log(`Not Found: ${deviceName} with ID: ${deviceID} does not exist`);
            UPDATE_transactions(api_key, type, uri, false);
            return res.status(404).json({error: `Not Found: ${deviceName} with ID: ${deviceID} does not exist`});
        }

        // Step 2: Return data based on parameter values
        if(!timeStart && !timeEnd && specData){
            // [A] If NO optional parameter values were given
            const queryText = format(`SELECT average(time_weight('Linear', timestamp, %I)) as time_weighted_average FROM %I WHERE timestamp > NOW() - INTERVAL '1 day';`, `${specData}`, `${deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_")}_${deviceID.replaceAll(".","_")}`);
            try {
                const data = await client.query(queryText);
                if (data.rowCount === 0) {
                    return res.status(404).json({error: `No data for ${deviceName} with ID: ${deviceID}`});
                }
                server_Log(`Successfully returned average historical ${specData} data (last 24 hours) from ${deviceName} with ID: ${deviceID}`);
                UPDATE_transactions(api_key, type, uri, true);
                return res.json(data.rows[0]);
            } catch (err) {
                server_Log(`Internal Server Error: ${sanitizePgError(err)}`);
                return res.status(500).send(`Internal Server Error: Unable to get average historical ${specData} data from ${deviceName} with ID: ${deviceID}`);
            }
        }else if(timeStart && timeEnd && specData){
            // [B] If optional parameter values for timeStart and timeEnd were given
            const queryText = format(`SELECT average(time_weight('Linear', timestamp, %I)) as time_weighted_average FROM %I WHERE timestamp BETWEEN $1 AND $2;`, `${specData}`, `${deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_")}_${deviceID}`);
            try {
                const data = await client.query(queryText, [timeStart, timeEnd]);
                if (data.rowCount === 0) {
                    return res.status(404).json({error: `No data in the requested range`});
                }
                server_Log(`Successfully returned average historical ${specData} data (${timeStart} to ${timeEnd}) from ${deviceName} with ID: ${deviceID}`);
                UPDATE_transactions(api_key, type, uri, true);
                return res.json(data.rows);
            } catch (err) {
                server_Log(`Bad Request: ${sanitizePgError(err)}`);
                return res.status(400).json({error: `Bad Request: Invalid arguments in average historical ${specData} data GET request for ${deviceName} with ID: ${deviceID}`});
            }
        }else{
            // [C] If optional parameter values are INCOMPLETE (Error 400)
            server_Log(`Bad Request: Incomplete parameters in GET average historical data request for ${deviceName} with ID: ${deviceID}`);
            UPDATE_transactions(api_key, type, uri, false);
            return res.status(400).json({error: `Bad Request: Incomplete parameters in GET average historical data request for ${deviceName} with ID: ${deviceID}`});
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
}

// POST LED light/strip
async function POST_light(res, req, deviceName, api_key, type, uri){
    try{
        if(await SECURITY_CHECK(res, req, api_key, [0,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const deviceID = req.params.id;
        // Optional arguments; will be NULL if not provided
        const lightState = req.query.state;
        const lightRed   = req.query.red;
        const lightGreen = req.query.green;
        const lightBlue  = req.query.blue;
        const lightBrightness = req.query.brightness;

        // Step 1: Check if an deviceName with ID: deviceID is available
        if(await ID_is_available(`${deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_")}`, deviceID) === false){
            server_Log(`Not Found: ${deviceName} with ID: ${deviceID} does not exist`);
            UPDATE_transactions(api_key, type, uri, false);
            return res.status(404).json({error: `Not Found: ${deviceName} with ID: ${deviceID} does not exist`});
        }

        // Step 2: Check if at least one optional parameter was given
        if(!lightState && !lightRed && !lightGreen && !lightBlue && !lightBrightness){
            server_Log(`Bad Request: Incomplete parameters in POST request for ${deviceName} with ID: ${deviceID}`);
            UPDATE_transactions(api_key, type, uri, false);
            return res.status(400).json({error: `Bad Request: Incomplete parameters in POST request for ${deviceName} with ID: ${deviceID}`});
        }

        // Step 3: Check if the given values are valid and build the JSON file to be published
        let toPublish = {};
        if(lightState){
            if(lightState !== "ON" && lightState !== "OFF"){
                server_Log(`Bad Request: Invalid 'state' parameter in POST request for ${deviceName} with ID: ${deviceID}. \n - Given value: ${lightState} \n - Possible values: "ON", "OFF"`);
                UPDATE_transactions(api_key, type, uri, false);
                return res.status(400).json({error: `Bad Request: Invalid 'state' parameter in POST request for ${deviceName} with ID: ${deviceID}. | Given value: ${lightState} | Possible values: "ON", "OFF"`});
            }
            toPublish['state'] = lightState;
        }
        if(lightRed){
            if(!Number.isFinite(+lightRed) || +lightRed < 0 || +lightRed > 1){
                server_Log(`Bad Request: Invalid 'red' parameter in POST request for ${deviceName} with ID: ${deviceID}. \n - Given value: ${lightRed} \n - Possible values: any value from 0-1`);
                UPDATE_transactions(api_key, type, uri, false);
                return res.status(400).json({error: `Bad Request: Invalid 'red' parameter in POST request for ${deviceName} with ID: ${deviceID}. | Given value: ${lightRed} | Possible values: any value from 0-1`});
            }
            toPublish['r'] = lightRed;
        }
        if(lightGreen){
            if(!Number.isFinite(+lightGreen) || +lightGreen < 0 || +lightGreen > 1){
                server_Log(`Bad Request: Invalid 'green' parameter in POST request for ${deviceName} with ID: ${deviceID}. \n - Given value: ${lightGreen} \n - Possible values: any value from 0-1`);
                UPDATE_transactions(api_key, type, uri, false);
                return res.status(400).json({error: `Bad Request: Invalid 'green' parameter in POST request for ${deviceName} with ID: ${deviceID}. | Given value: ${lightGreen} | Possible values: any value from 0-1`});
            }
            toPublish['g'] = lightGreen;
        }
        if(lightBlue){
            if(!Number.isFinite(+lightBlue) || +lightBlue < 0 || +lightBlue > 1){
                server_Log(`Bad Request: Invalid 'blue' parameter in POST request for ${deviceName} with ID: ${deviceID}. \n - Given value: ${lightBlue} \n - Possible values: any value from 0-1`);
                UPDATE_transactions(api_key, type, uri, false);
                return res.status(400).json({error: `Bad Request: Invalid 'blue' parameter in POST request for ${deviceName} with ID: ${deviceID}. | Given value: ${lightBlue} | Possible values: any value from 0-1`});
            }
            toPublish['b'] = lightBlue;
        }
        if(lightBrightness){
            if(!Number.isFinite(+lightBrightness) || +lightBrightness < 0 || +lightBrightness > 1){
                server_Log(`Bad Request: Invalid 'brightness' parameter in POST request for ${deviceName} with ID: ${deviceID}. \n - Given value: ${lightBrightness} \n - Possible values: any value from 0-1`);
                UPDATE_transactions(api_key, type, uri, false);
                return res.status(400).json({error: `Bad Request: Invalid 'brightness' parameter in POST request for ${deviceName} with ID: ${deviceID}. | Given value: ${lightBrightness} | Possible values: any value from 0-1`});
            }
            toPublish['brightness'] = lightBrightness;
        }

        // Step 4: Publish the JSON file to the correct MQTT topic to set the LED light/strip
        mqttclient.publish(`${deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_")}_${deviceID}/light`, JSON.stringify(toPublish), { qos: 2 }, (err) => {
            if (err) {
                server_Log(`Internal Server Error: MQTT Connection Failed`);
                UPDATE_transactions(api_key, type, uri, false);
                return res.status(500).json({error: `Internal Server Error: MQTT Connection Failed`});
            }else{
                server_Log(`POST request to ${deviceName} with ID: ${deviceID} OK`);
                server_Log(` - MQTT Topic: ${deviceName.toLowerCase().replaceAll("-","_").replaceAll(" ","_")}_${deviceID}/light`);
                server_Log(` - JSON File: ${JSON.stringify(toPublish)}`);
                UPDATE_transactions(api_key, type, uri, true);
                return res.status(200).send(`POST request to ${deviceName} with ID: ${deviceID} OK`);
            }
        });
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }

}
// ____ ENDPOINTS END ____

// ---- END Standardized Function Calls ---- //


// ----------- START Define REST Endpoints ---------- //

// Public health probe — no auth, used by Docker healthcheck and external monitoring.
// Returns 200 if the PG pool can serve a trivial query, 503 otherwise.
app.get('/healthz', async (req, res) => {
    try {
        await client.query('SELECT 1');
        return res.status(200).json({status: 'ok'});
    } catch (err) {
        server_Log(`healthz: db unreachable — ${sanitizePgError(err)}`);
        return res.status(503).json({status: 'db_unreachable'});
    }
});

// START Digital Twin Endpoints -------------------------- //

// (#1) GET "/access/:api_key" -- Return access_level of api_key
app.get("/access/:api_key", async (req,res)=>{
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        server_Log(`Successfully returned access level`);
        UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);

        const api_key = req.params.api_key;
        let to_verify = await KEY_is_available(api_key);
        if(to_verify){ // check if API Key is valid
            let access_level = await RETURN_access_level(api_key);
            res.json(access_level);
        }else{
            res.json(-1); // RETURN -1 if API Key is NOT Valid
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// END Digital Twin Endpoints -------------------------- //


// START User Management Endpoints -------------------------- //

// (#1) GET "/users" -- Return List of Users
app.get("/users", async (req,res)=>{
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        try {
            const response = await client.query('SELECT user_name FROM users;');
            const arr_user_names = response.rows.map(r => r.user_name);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
            server_Log(`Successfully returned list of usernames`);
            return res.json(arr_user_names);
        } catch (err) {
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            server_Log(`Database Error Occurred while listing users — ${sanitizePgError(err)}`);
            return res.status(500).json({ error: `Database Error Occurred.` });
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// (#2) POST "/user/{user_name}" -- Create new user
app.post("/users/:user_name", async (req,res)=>{
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const user_name = req.params.user_name;
        const access_level = req.query.access_level;
        let api_key = uuidv4(); // generate API key

        // Check if user is available
        let to_check = await USER_is_available(user_name); // Call ID checker function
        if (to_check === true) {
            server_Log(`ERROR: ${user_name} is already taken`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(409).json({ error: `${user_name} is already taken: Please choose a different username.` });
        }

        // Check if access_level is defined
        if(access_level === undefined){
            server_Log(`ERROR: Request Incomplete Parameters`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({ error: `Missing required parameters: Ensure all required fields are provided.` });
        }

        // Strict access_level validation (§4.6) — must be integer 0, 1, or 2.
        const access_level_num = Number(access_level);
        if (!Number.isInteger(access_level_num) || access_level_num < 0 || access_level_num > 2) {
            server_Log(`ERROR: Invalid access_level`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({ error: `access_level must be 0, 1, or 2` });
        }

        try {
            await client.query(
                'INSERT INTO users (user_name, api_key, access_level) VALUES ($1, $2, $3)',
                [user_name, api_key, access_level_num]
            );
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
            server_Log(`SUCCESSFULLY create new user ${user_name}`);
            return res.status(200).send();
        } catch (err) {
            server_Log(`ERROR: Unsuccessfully in creating new user — ${sanitizePgError(err)}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(500).json({ error: `Database error occurred while creating the user.` });
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// (#3) GET "/users/:user_name}" -- Return Data of Specific User (with query)
app.get("/users/:user_name", async (req,res)=>{
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        // Check if user is available
        const user_name = req.params.user_name;
        let to_check = await USER_is_available(user_name); // Call ID checker function
        if (to_check === false) {
            server_Log(`ERROR: User with the username ${user_name} is unavailable`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({ error: `Not Found: User ${user_name} not available` });
        }

        try {
            const response = await client.query('SELECT user_name, api_key, access_level FROM users WHERE user_name = $1;', [user_name]);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
            server_Log(`SUCCESSFULLY return data of user ${user_name}`);
            return res.json(response.rows[0]);
        } catch (err) {
            server_Log(`ERROR: GET /users/:user_name — ${sanitizePgError(err)}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(500).json({ error: `Database error occurred.` });
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }

})

// (#4) PUT "/user" -- Edit Access Level of User
app.put("/users/:user_name", async (req,res)=>{
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const user_name = req.params.user_name;
        const access_level = req.query.access_level;

        // Check if user is available
        let to_check = await USER_is_available(user_name); // Call ID checker function
        if (to_check === false) {
            server_Log(`ERROR: User with the username ${user_name} is unavailable`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({ error: `Not Found: User ${user_name} not available` });
        }

        // Strict access_level validation (§4.6) — must be integer 0, 1, or 2.
        if (access_level === undefined) {
            server_Log(`ERROR: Request Incomplete Parameters`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({ error: `Missing required parameters: Ensure all required fields are provided.` });
        }
        const access_level_num = Number(access_level);
        if (!Number.isInteger(access_level_num) || access_level_num < 0 || access_level_num > 2) {
            server_Log(`ERROR: Invalid access_level`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({ error: `access_level must be 0, 1, or 2` });
        }
        // Invalidate auth cache so the change takes effect immediately rather than on TTL expiry.
        authCache.clear();

        try {
            await client.query('UPDATE users SET access_level = $1 WHERE user_name = $2;', [access_level_num, user_name]);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
            server_Log(`SUCCESSFULLY changed access level of user ${user_name} to ${access_level_num}`);
            return res.status(200).send();
        } catch (err) {
            server_Log(`ERROR: Unsuccessfully modified user ${user_name}'s access level — ${sanitizePgError(err)}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(500).json({ error: `Database error occurred.` });
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// (#5) DELETE "/user" -- Delete Specific User
app.delete("/users/:user_name", async (req,res)=>{
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const user_name = req.params.user_name;

        // Check if user is available
        let to_check = await USER_is_available(user_name); // Call ID checker function
        if (to_check === false) {
            server_Log(`ERROR: User with the username '${user_name}' is unavailable`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({ error: `Not Found: User '${user_name}' not available` });
        }

        try {
            await client.query('DELETE FROM users WHERE user_name = $1;', [user_name]);
            authCache.clear();
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
            server_Log(`SUCCESSFULLY deleted user ${user_name}`);
            return res.status(200).send();
        } catch (err) {
            server_Log(`ERROR: Unsuccessfully deleted user ${user_name} — ${sanitizePgError(err)}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(500).json({ error: `Database error occurred.` });
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// END User Management Endpoints -------------------------- //


// START Transactions Endpoints -------------------------- //

// (#1) "/transactions/?time_start&time_end" Return Last 20 Transactions by Default, Can give timestamp range
app.get("/transactions", async (req,res)=>{
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        // Optional arguments; will be NULL if not provided
        // Format: yyyy-mm-dd hh:mm:ss (hh in 24-hour cycle)
        const time_start = req.query.time_start;
        const time_end = req.query.time_end;

        // Step 1: Return data based on parameter values
        // [A] If NO optional parameters
        if (!time_start && !time_end) {
            try {
                const data = await client.query('SELECT * FROM transactions ORDER BY timestamp DESC LIMIT 10;');
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
                server_Log('SUCCESSFULLY returned most recent transactions');
                return res.json(data.rows);
            } catch (err) {
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                server_Log(`ERROR: Getting most recent transactions — ${sanitizePgError(err)}`);
                return res.status(500).json({error: 'Database error occurred.'});
            }
        } else if (time_start && time_end) {
            try {
                const data = await client.query('SELECT * FROM transactions WHERE timestamp BETWEEN $1 AND $2;', [time_start, time_end]);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
                server_Log('SUCCESSFULLY returned transactions');
                return res.json(data.rows);
            } catch (err) {
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                server_Log(`ERROR: Getting transactions — ${sanitizePgError(err)}`);
                return res.status(500).json({error: 'Database error occurred.'});
            }
        } else {
            // [C] Error 400: Optional parameters are INCOMPLETE
            server_Log("ERROR: Incomplete parameters in transactions request");
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({ error: 'Invalid request: Missing arguments in request' });
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// END Transactions Endpoints -------------------------- //


// START Apollo AIR-1 Endpoints ------------------------ //

// (#1) "/air-1" GET all available Apollo AIR-1 IDs
app.get("/air-1", async (req, res) => {
    return GET_ids(res, req, "Apollo AIR-1", req.header("x-api-key"), req.method, req.originalUrl);
})

// (#2) "/air-1/{id}" and "/air-1/{id}&options" GET the most recent/historical data of a specific Apollo AIR-1
app.get("/air-1/:id", async (req, res) => {
    return GET_data(res, req, "Apollo AIR-1", req.header("x-api-key"), req.method, req.originalUrl);
})

// (#3) "/air-1/{id}/light" POST the state of the LED light of a specific Apollo AIR-1
app.post("/air-1/:id/light", async (req, res) => {
    return POST_light(res, req, "Apollo AIR-1", req.header("x-api-key"), req.method, req.originalUrl);
})

// (#4) "/air-1/{id}/avg&options" GET the average historical data of a specific sensor data of a specific Apollo AIR-1
app.get("/air-1/:id/avg", async (req, res) => {
    return GET_avg(res, req, "Apollo AIR-1", req.header("x-api-key"), req.method, req.originalUrl);
})

// END Apollo AIR-1 Endpoints -------------------------- //


// START Apollo MSR-2 Endpoints ------------------------ //

// (#1) "/msr-2" GET all available Apollo MSR-2 IDs
app.get("/msr-2", async (req, res) => {
    return GET_ids(res, req, "Apollo MSR-2", req.header("x-api-key"), req.method, req.originalUrl)
})

// (#2) "/msr-2/{id}" and "/msr-2/{id}&options" GET the most recent/historical data of a specific Apollo MSR-2
app.get("/msr-2/:id", async (req, res) => {
    return GET_data(res, req, "Apollo MSR-2", req.header("x-api-key"), req.method, req.originalUrl);
})

// (#3) "/msr-2/{id}/light" POST the state of the LED light of a specific Apollo MSR-2
app.post("/msr-2/:id/light", async (req, res) => {
    return POST_light(res, req, "Apollo MSR-2", req.header("x-api-key"), req.method, req.originalUrl);
})

// (#4) "/msr-2/{id}/buzzer" POST the rtttl string to be played on the buzzer of a specific Apollo MSR-2
app.post("/msr-2/:id/buzzer", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const deviceID = req.params.id;
        // Optional argument; will be NULL if not provided
        const mtttl_string = req.query.mtttl_string;

        // Step 1: Check if an Apollo MSR-2 with ID: deviceID is available
        if(await ID_is_available('apollo_msr_2', deviceID) === false){
            server_Log(`Not Found: Apollo MSR-2 with ID: ${deviceID} does not exist`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({error: `Not Found: Apollo MSR-2 with ID: ${deviceID} does not exist`});
        }

        // Step 2: Check if the string to play was given
        if(!mtttl_string){
            server_Log(`Bad Request: Incomplete parameters in POST request for Apollo MSR-2 with ID: ${deviceID}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({error: `Bad Request: Incomplete parameters in POST request for Apollo MSR-2 with ID: ${deviceID}`});
        }

        // Step 3: Build and publish the JSON file to the correct MQTT topic to play the buzzer
        let toPublish = {'mtttl_string' : `${mtttl_string}`};
        // Guarantee the message with MQTT QOS2 and return an error request if there is an issue sending the message
        mqttclient.publish(`apollo_msr_2_${deviceID}/buzzer`, JSON.stringify(toPublish), { qos: 2 }, (err) => {
            if (err) {
                server_Log(`Internal Server Error: MQTT Connection Failed`);
                return res.status(500).json({error: `Internal Server Error: MQTT Connection Failed`});
            }else{
                server_Log(`POST request to Apollo MSR-2 with ID: ${deviceID} OK`);
                server_Log(` - MQTT Topic: apollo_msr_2_${deviceID}/buzzer`);
                server_Log(` - JSON File: ${JSON.stringify(toPublish)}`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
                return res.status(200).send(`POST request to Apollo MSR-2 with ID: ${deviceID} OK`);
            }
        });
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// (#5) "/msr-2/{id}/avg&options" GET the average historical data of a specific sensor data of a specific Apollo MSR-2
app.get("/msr-2/:id/avg", async (req, res) => {
    return GET_avg(res, req, "Apollo MSR-2", req.header("x-api-key"), req.method, req.originalUrl);
})

// END Apollo MSR-2 Endpoints -------------------------- //


// START Athom Smart Plug v2 Endpoints ----------------- //

// (#1) "/smart-plug-v2" GET all available Athom Smart Plug v2 IDs
app.get("/smart-plug-v2", async (req, res) => {
    return GET_ids(res, req, "Athom Smart Plug v2", req.header("x-api-key"), req.method, req.originalUrl)
})

// (#2) "/smart-plug-v2/{id}" and "/smart-plug-v2/{id}&options" GET the most recent/historical data of specific Athom Smart Plug v2
app.get("/smart-plug-v2/:id", async (req, res) => {
    return GET_data(res, req, "Athom Smart Plug v2", req.header("x-api-key"), req.method, req.originalUrl);
})

// (#3) "/smart-plug-v2/{id}/relay" POST relay of specific Athom Smart Plug v2
app.post("/smart-plug-v2/:id/relay", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const deviceID = req.params.id;
        // Optional argument; will be NULL if not provided
        const relayState = req.query.state;

        // Step 1: Check if an Athom Smart Plug v2 with ID: deviceID is available
        if(await ID_is_available('athom_smart_plug_v2', deviceID) === false){
            server_Log(`Not Found: Athom Smart Plug v2 with ID: ${deviceID} does not exist`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({error: `Not Found: Athom Smart Plug v2 with ID: ${deviceID} does not exist`});
        }

        // Step 2: Check if relayState was given and is a valid value
        if(!relayState){
            server_Log(`Bad Request: Incomplete parameters in POST request for Athom Smart Plug v2 with ID: ${deviceID}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({error: `Bad Request: Incomplete parameters in POST request for Athom Smart Plug v2 with ID: ${deviceID}`});
        }else if(relayState !== "On" && relayState !== "Off"){
            server_Log(`Bad Request: Invalid 'state' parameter in POST request for Athom Smart Plug v2 with ID: ${deviceID}. \n - Given value: ${relayState} \n - Possible values: "On", "Off"`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({error: `Bad Request: Invalid 'state' parameter in POST request for Athom Smart Plug v2 with ID: ${deviceID}. | Given value: ${relayState} | Possible values: "On", "Off"`});
        }

        // Step 3: Build and publish the JSON file to the correct MQTT topic to set the relay
        let toPublish = {'state' : `${relayState}`};
        // Guarantee the message with MQTT QOS2 and return an error request if there is an issue sending the message
        mqttclient.publish(`athom_smart_plug_v2_${deviceID}/relay`, JSON.stringify(toPublish), { qos: 2 }, (err) => {
            if (err) {
                server_Log(`Internal Server Error: MQTT Connection Failed`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(500).json({error: `Internal Server Error: MQTT Connection Failed`});
            }else{
                server_Log(`POST request to Athom Smart Plug v2 with ID: ${deviceID} OK`);
                server_Log(` - MQTT Topic: athom_smart_plug_v2_${deviceID}/relay`);
                server_Log(` - JSON File: ${JSON.stringify(toPublish)}`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
                return res.status(200).send(`POST request to Athom Smart Plug v2 with ID: ${deviceID} OK`);
            }
        });
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }

})

// (#4) "/smart-plug-v2/{id}/avg&options" GET the average historical data of a specific sensor data of a specific Athom Smart Plug v2
app.get("/smart-plug-v2/:id/avg", async (req, res) => {
    return GET_avg(res, req, "Athom Smart Plug v2", req.header("x-api-key"), req.method, req.originalUrl);
})

// END Athom Smart Plug v2 Endpoints ------------------- //


// START AirGradient One Endpoints --------------------- //

// (#1) "/ag-one" GET all available AirGradient One IDs
app.get("/ag-one", async (req, res) => {
    return GET_ids(res, req, "AirGradient One", req.header("x-api-key"), req.method, req.originalUrl)
})

// (#2) "/ag-one/{id}" and "/ag-one/{id}&options" GET the most recent/historical data of a specific AirGradient One
app.get("/ag-one/:id", async (req, res) => {
    return GET_data(res, req, "AirGradient One", req.header("x-api-key"), req.method, req.originalUrl);
})

// (#3) "/ag-one/{id}/light" POST the state of the LED strip of a specific AirGradient One
app.post("/ag-one/:id/light", async (req, res) => {
    return POST_light(res, req, "AirGradient One", req.header("x-api-key"), req.method, req.originalUrl);
})

// (#4) "/ag-one/{id}/avg&options" GET the average historical data of a specific sensor data of a specific Athom Smart Plug v2
app.get("/ag-one/:id/avg", async (req, res) => {
    return GET_avg(res, req, "AirGradient One", req.header("x-api-key"), req.method, req.originalUrl);
})

// END AirGradient One Endpoints ----------------------- //


// START Zigbee2MQTT Endpoints ------------------------- //

// (#1) "/zigbee2mqtt" GET all available Zigbee2MQTT device IDs and group IDs
app.get("/zigbee2mqtt", async (req, res) => {
    return GET_ids(res, req, "Zigbee2MQTT", req.header("x-api-key"), req.method, req.originalUrl)
})

// (#2) "/zigbee2mqtt/{id}/get" GET the most recent/historical state of a specific Zigbee2MQTT device
app.get("/zigbee2mqtt/:id", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0,1,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        // Step 0: Check if the ID belongs to a group
        let queryText = 'SELECT * FROM zigbee2mqtt WHERE id = $1;';
        let queryValues = [req.params.id];
        let queryResult = await client.query(queryText, queryValues);

        if(!queryResult.rowCount) {
            server_Log(`Not found: Zigbee2MQTT with ID: ${req.params.id} does not exist`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({error: `Not found: Zigbee2MQTT with ID: ${req.params.id} does not exist`});
        }else if(queryResult.rows[0]['type'] === "group"){
            server_Log(`Bad Request: ID: ${req.params.id} belongs to a Zigbee2MQTT group. Data requests are not available for groups`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({error: `Bad Request: ID: ${req.params.id} belongs to a Zigbee2MQTT group. Data requests are not available for groups`});
        }

        return GET_data(res, req, "Zigbee2MQTT", specific_api_key, req.method, req.originalUrl);
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// (#3) "/zigbee2mqtt/{id}/set" POST the state of a specific Zigbee2MQTT device or group
app.post("/zigbee2mqtt/:id", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const entityID = req.params.id;
        // Optional arguments; will be NULL if not provided
        // for groups and lights:
        const lightState = req.query.light_state;
        const lightBrightness = req.query.light_brightness;
        const lightColorTemperature = req.query.light_color_temperature;
        // for switches:
        const switchState = req.query.switch_state;
        // for blinds:
        const blindsState = req.query.blinds_state;
        const blindsPosition = req.query.blinds_position;

        // Step 1: Check if an Zigbee2MQTT device or group with ID: entity_id is available
        let baseTopic = "";
        let deviceType = "";
        let queryResult = await client.query('SELECT * FROM zigbee2mqtt WHERE id = $1;', [entityID]);
        if(!queryResult.rowCount) {
            server_Log(`Not Found: Zigbee2MQTT with ID: ${entityID} does not exist`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({error: `Not Found: Zigbee2MQTT with ID: ${entityID} does not exist`});
        }else{
            // If entity_id is found, store its base topic
            baseTopic = queryResult.rows[0]["base_topic"];
            deviceType = queryResult.rows[0]["type"];
        }

        let toPublish = {};
        // each type of device has a corresponding json file
        if(deviceType === "group" || deviceType === "lights"){
            // Step 2: Check if at least one optional parameter was given
            if(!lightState && !lightBrightness && !lightColorTemperature){
                server_Log(`Bad Request: Incomplete parameters in POST request for Zigbee2MQTT with ID: ${entityID}`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(400).json({error: `Bad Request: Incomplete parameters in POST request for Zigbee2MQTT with ID: ${entityID}`});
            }

            // Step 3: Check if the given values are valid and build the json file to be published
            if(lightState){
                if(lightState !== "ON" && lightState !== "OFF"){
                    server_Log(`Bad Request: Invalid 'light_state' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. \n - Given value: ${lightState} \n - Possible values: "ON", "OFF"`);
                    UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                    return res.status(400).json({error: `Bad Request: Invalid 'state' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. | Given value: ${lightState} | Possible values: "ON", "OFF"`});
                }
                toPublish['state'] = lightState;
            }
            if(lightBrightness){
                if(lightBrightness < 0 || lightBrightness > 254){
                    server_Log(`Bad Request: Invalid 'light_brightness' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. \n - Given value: ${lightBrightness} \n - Possible values: any integer from 0 to 254`);
                    UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                    return res.status(400).json({error: `Bad Request: Invalid 'brightness' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. | Given value: ${lightBrightness} | Possible values: any integer from 0 to 254`});
                }
                toPublish['brightness'] = lightBrightness;
            }
            if(lightColorTemperature){
                if(lightColorTemperature < 153 || lightColorTemperature > 500){
                    server_Log(`Bad Request: Invalid 'light_color_temperature' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. \n - Given value: ${lightColorTemperature} \n - Possible values: any integer from 153 to 500`);
                    UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                    return res.status(400).json({error: `Bad Request: Invalid 'color_temperature' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. | Given value: ${lightColorTemperature} | Possible values: any integer from 153 to 500`});
                }
                toPublish['color_temp'] = lightColorTemperature;
            }
        }else if(deviceType === "switch"){
            // Step 2-3: Check if the optional parameter was given and check if the given value is valid then build the json file to be published
            if(!switchState){
                server_Log(`Bad Request: Incomplete parameters in POST request for Zigbee2MQTT with ID: ${entityID}`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(400).json({error: `Bad Request: Incomplete parameters in POST request for Zigbee2MQTT with ID: ${entityID}`});
            }else{
                if(switchState !== "ON" && switchState !== "OFF" && switchState !== "TOGGLE"){
                    server_Log(`Bad Request: Invalid 'blinds_state' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. \n - Given value: ${blindsState} \n - Possible values: "ON", "OFF", "TOGGLE"`);
                    UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                    return res.status(400).json({error: `ad Request: Invalid 'blinds_state' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. \n - Given value: ${blindsState} \n - Possible values: "ON", "OFF", "TOGGLE"`});
                }
                toPublish['state'] = switchState;
            }
        }else if(deviceType === "blinds"){
            // Step 2: Check if at least one optional parameter was given
            if(!blindsState && !blindsPosition) {
                server_Log(`Bad Request: Incomplete parameters in POST request for Zigbee2MQTT with ID: ${entityID}`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(400).json({error: `Bad Request: Incomplete parameters in POST request for Zigbee2MQTT with ID: ${entityID}`});
            }

            // Step 3: Check if the given values are valid and build the json file to be published
            if(blindsState) {
                if (blindsState !== "OPEN" && blindsState !== "CLOSE" && blindsState !== "STOP") {
                    server_Log(`Bad Request: Invalid 'blinds_state' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. \n - Given value: ${blindsState} \n - Possible values: "OPEN", "CLOSE", "STOP"`);
                    UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                    return res.status(400).json({error: `Bad Request: Invalid 'blinds_state' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. | Given value: ${blindsState} | - Possible values: "OPEN", "CLOSE", "STOP"`});
                }
                toPublish['state'] = blindsState;
            }
            if(blindsPosition) {
                if (!Number.isFinite(+blindsPosition) || +blindsPosition < 0 || +blindsPosition > 100) {
                    server_Log(`Bad Request: Invalid 'blinds_position' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. \n - Given value: ${blindsPosition} \n - Possible values: any integer from 0 to 100`);
                    UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                    return res.status(400).json({error: `Bad Request: Invalid 'blinds_position' parameter in POST request for Zigbee2MQTT with ID: ${entityID}. \n - Given value: ${blindsPosition} \n - Possible values: any integer from 0 to 100`});
                }
                toPublish['position'] = blindsPosition;
            }
        }

        // Step 4: Publish the json file to the correct MQTT topic to set the light
        // Guarantee the message with MQTT QOS2 and return an error request if there is an issue sending the message
        mqttclient.publish(`${baseTopic}/${entityID}/set`, JSON.stringify(toPublish), { qos: 2 }, (err) => {
            if (err) {
                server_Log(`Internal Server Error: MQTT Connection Failed`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
                return res.status(500).json({error: `Internal Server Error: MQTT Connection Failed`});
            }else{
                server_Log(`POST request to Zigbee2MQTT with ID: ${entityID} OK`);
                server_Log(` - MQTT Topic: ${baseTopic}/${entityID}/set`);
                server_Log(` - JSON File: ${JSON.stringify(toPublish)}`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
                return res.status(200).send(`POST request to Zigbee2MQTT with ID: ${entityID} OK`);
            }
        });
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }

})

// END Zigbee2MQTT Endpoints --------------------------- //


// START Sensibo Endpoints ----------------------------- //

// (#1) "/sensibo" GET all available Sensibo Air Pro IDs
app.get("/sensibo", async (req, res) => {
    return GET_ids(res, req, "Sensibo", req.header("x-api-key"), req.method, req.originalUrl)
})

// (#2) "/sensibo/{id}" and "/sensibo/{id}&options" GET the [most recent/historical] data of a specific Sensibo Air Pro
app.get("/sensibo/:id", async (req, res) => {
    return GET_data(res, req, "Sensibo", req.header("x-api-key"), req.method, req.originalUrl);
})

// (#3) "/sensibo/{id}/hvac" POST the state of a Sensibo Air Pro's HVAC
app.post("/sensibo/:id/hvac", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const deviceID = req.params.id;
        // Optional arguments; will be NULL if not provided
        const hvacMode = req.query.hvac_mode
        const targetTemperature = req.query.target_temperature

        // Step 1: Check if an Sensibo with ID: deviceID is available
        if(await ID_is_available('sensibo', deviceID) === false){
            server_Log(`Not Found: Sensibo with ID: ${deviceID} does not exist`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({error: `Not Found: Sensibo with ID: ${deviceID} does not exist`});
        }

        // Step 2: Check if at least one optional parameter is given
        if(!hvacMode && !targetTemperature){
            server_Log(`Bad Request: Incomplete parameters in POST request for Sensibo with ID: ${deviceID}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({error: `Bad Request: Incomplete parameters in POST request for Sensibo with ID: ${deviceID}`});
        }

        // Step 3: POST based on parameter values and check if they are valid
        if(hvacMode){
            if(hvacMode !== "off" && hvacMode !== "heat" && hvacMode !== "cool"){
                server_Log(`Bad Request: Invalid 'hvac_mode' parameter in POST request for Sensibo with ID: ${deviceID} \n - Given value: ${hvacMode} \n - Possible values: "off", "heat", "cool"`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(404).json({error: `Bad Request: Invalid 'hvac_mode' parameter in POST request for Sensibo with ID: ${deviceID} | Given value: ${hvacMode} | Possible values: "off", "heat", "cool"`});
            }
            // §4.7 — validate deviceID against a strict pattern so it cannot inject into URLs/JSON.
            if (!/^[A-Za-z0-9._-]+$/.test(deviceID)) {
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(400).json({error: `Invalid deviceID format`});
            }
            const parameters = {"entity_id": deviceID, "hvac_mode": hvacMode};
            const r1 = await fetch(`${process.env.HOME_ASSISTANT_URL}:${process.env.HOME_ASSISTANT_PORT}/api/services/climate/set_hvac_mode`, {method: 'POST', body: JSON.stringify(parameters), headers: {"Authorization":`Bearer ${process.env.HOME_ASSISTANT_TOKEN}`, "content-type":"application/json"}});
            if (!r1.ok) {
                server_Log(`Home Assistant set_hvac_mode failed: HTTP ${r1.status}`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(502).json({error: `Upstream Home Assistant call failed`});
            }
        }
        if(targetTemperature){
            if(!Number.isFinite(+targetTemperature) || +targetTemperature < 10 || +targetTemperature > 35){
                server_Log(`Bad Request: Invalid 'target_temperature' parameter in POST request for Sensibo with ID: ${deviceID} \n - Given value: ${targetTemperature} \n - Possible values: any value from 10 to 35`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(400).json({error: `Bad Request: Invalid 'target_temperature' parameter in POST request for Sensibo with ID: ${deviceID} | Given value: ${targetTemperature} | Possible values: any value from 10 to 35`});
            }
            if (!/^[A-Za-z0-9._-]+$/.test(deviceID)) {
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(400).json({error: `Invalid deviceID format`});
            }
            const parameters = {"entity_id": deviceID, "temperature": targetTemperature};
            const r2 = await fetch(`${process.env.HOME_ASSISTANT_URL}:${process.env.HOME_ASSISTANT_PORT}/api/services/climate/set_temperature`, {method: 'POST', body: JSON.stringify(parameters), headers: {"Authorization":`Bearer ${process.env.HOME_ASSISTANT_TOKEN}`, "content-type":"application/json"}});
            if (!r2.ok) {
                server_Log(`Home Assistant set_temperature failed: HTTP ${r2.status}`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(502).json({error: `Upstream Home Assistant call failed`});
            }
        }

        // Step 4: Return status 200 OK
        server_Log(`POST request to Sensibo with ID: ${deviceID} OK`);
        UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
        if(hvacMode){server_Log(` - Given HVAC mode: ${hvacMode}`);}
        if(targetTemperature){server_Log(` - Given target temperature: ${targetTemperature}`);}
        return res.status(200).send(`POST request to Sensibo with ID: ${deviceID} OK`);
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// END Sensibo Endpoints ------------------------------- //


// START Groups Endpoints ------------------------------ //

// (#1) "/groups" GET/POST/PUT/DELETE groups (mapping of devices to tables)
// (1a) GET: Return all group IDs
app.get("/groups", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0,1,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const queryResult = await client.query('SELECT * FROM groups;');

        if(queryResult.rowCount){
            let data = {};
            for(let row of queryResult.rows) {
                data[row["id"]] = {};
                if(row["apollo_air_1_ids"]){
                    data[row["id"]]["apollo_air_1_ids"] = row["apollo_air_1_ids"];
                }
                if(row["apollo_msr_2_ids"]){
                    data[row["id"]]["apollo_msr_2_ids"] = row["apollo_msr_2_ids"];
                }
                if(row["athom_smart_plug_v2_ids"]){
                    data[row["id"]]["athom_smart_plug_v2_ids"] = row["athom_smart_plug_v2_ids"];
                }
                if(row["zigbee2mqtt_ids"]){
                    data[row["id"]]["zigbee2mqtt_ids"] = row["zigbee2mqtt_ids"];
                }
            }
            server_Log("Successfully returned all group IDs");
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
            res.json(data);
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// (1b) POST: Add a new group. FOR HIGHEST PRIVILEGE ONLY
app.post("/groups", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const id = req.query.id;
        // Optional arguments; will be NULL if not provided
        let apollo_air_1_ids = req.query.apollo_air_1_ids;
        let apollo_msr_2_ids = req.query.apollo_msr_2_ids;
        let athom_smart_plug_v2_ids = req.query.athom_smart_plug_v2_ids;
        let zigbee2mqtt_ids = req.query.zigbee2mqtt_ids;

        // Step 1: Check if the id is already taken
        let queryText = 'SELECT * FROM groups WHERE id = $1;';
        let queryValues = [id];
        let queryResult = await client.query(queryText, queryValues);

        if(queryResult.rows.length){
            server_Log(`Bad Request: There is already a group with ID: ${id}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({error: `Bad Request: There is already a group with ID: ${id}`});
        }

        // Step 2: Validate each device id against its registry table.
        // §4.2 — column names are now an allow-list, values are parameterized.
        const GROUP_COLUMN_BY_NAME = {
            'Apollo AIR-1':        'apollo_air_1_ids',
            'Apollo MSR-2':        'apollo_msr_2_ids',
            'Athom Smart Plug v2': 'athom_smart_plug_v2_ids',
            'Zigbee2MQTT':         'zigbee2mqtt_ids',
        };
        const REGISTRY_TABLE_BY_NAME = {
            'Apollo AIR-1':        'apollo_air_1',
            'Apollo MSR-2':        'apollo_msr_2',
            'Athom Smart Plug v2': 'athom_smart_plug_v2',
            'Zigbee2MQTT':         'zigbee2mqtt',
        };
        const inputs = [
            ['Apollo AIR-1',        apollo_air_1_ids],
            ['Apollo MSR-2',        apollo_msr_2_ids],
            ['Athom Smart Plug v2', athom_smart_plug_v2_ids],
            ['Zigbee2MQTT',         zigbee2mqtt_ids],
        ];

        const columns = ['id'];
        const params = [id];

        for (let [deviceName, ids] of inputs) {
            if (!ids) continue;
            if (typeof ids === 'string') ids = [ids];

            for (const deviceID of ids) {
                const id_is_available = await ID_is_available(REGISTRY_TABLE_BY_NAME[deviceName], deviceID);
                if (!id_is_available) {
                    server_Log(`Not Found: ${deviceName} with ID: ${deviceID} does not exist`);
                    UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                    return res.status(404).json({error: `Not Found: ${deviceName} with ID: ${deviceID} does not exist`});
                }
                if (deviceName === 'Zigbee2MQTT') {
                    const check = await client.query('SELECT 1 FROM zigbee2mqtt WHERE id = $1 AND type != $2;', [deviceID, 'group']);
                    if (!check.rowCount) {
                        server_Log(`Bad Request: ${deviceName} with ID: ${deviceID} cannot be added to a group`);
                        UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                        return res.status(400).json({error: `Bad Request: ${deviceName} with ID: ${deviceID} cannot be added to a group`});
                    }
                }
            }

            columns.push(GROUP_COLUMN_BY_NAME[deviceName]);
            params.push(ids);  // pg driver handles JS arrays as TEXT[]
        }

        // Step 3: Insert with parameterized values. Column list is built from an allow-list.
        const placeholders = params.map((_, i) => `$${i + 1}`).join(', ');
        const queryTextInsert = `INSERT INTO groups (${columns.map(c => `"${c}"`).join(', ')}) VALUES (${placeholders});`;
        try {
            await client.query(queryTextInsert, params);
        } catch (err) {
            server_Log(`Bad Request: Bad arguments in POST /groups — ${sanitizePgError(err)}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({error: `Bad Request: Bad arguments in POST request`});
        }

        server_Log(`Successfully crated a new group with ID: ${id}`);
        if(apollo_air_1_ids){server_Log(` - Apollo AIR-1's: ${apollo_air_1_ids}`);}
        if(apollo_msr_2_ids){server_Log(` - Apollo MSR-2's: ${apollo_msr_2_ids}`);}
        if(athom_smart_plug_v2_ids){server_Log(` - Athom Smart Plug v2's: ${athom_smart_plug_v2_ids}`);}
        if(zigbee2mqtt_ids){server_Log(` - Zigbee2MQTT's: ${zigbee2mqtt_ids}`);}
        UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
        return res.status(200).send(`Successfully crated a new group with ID: ${id}`);
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// (1c) PUT: Change the members of a group. FOR HIGHEST PRIVILEGE ONLY
app.put("/groups", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const id = req.query.id;
        // Optional arguments; will be NULL if not provided
        let apollo_air_1_ids = req.query.apollo_air_1_ids;
        let apollo_msr_2_ids = req.query.apollo_msr_2_ids;
        let athom_smart_plug_v2_ids = req.query.athom_smart_plug_v2_ids;
        let zigbee2mqtt_ids = req.query.zigbee2mqtt_ids;

        // Step 1: Check if a group with ID: id is available
        let queryText = 'SELECT * FROM groups WHERE id = $1;';
        let queryValues = [id];
        let queryResult = await client.query(queryText, queryValues);

        if(!queryResult.rows.length){
            server_Log(`Not Found: Group with ID: ${id} does not exist`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({error: `Not Found: Group with ID: ${id} does not exist`});
        }

        // Step 2: Check if at least one optional parameter is given
        if(!apollo_air_1_ids && !apollo_msr_2_ids && !athom_smart_plug_v2_ids && !zigbee2mqtt_ids){
            server_Log(`ERROR: Incomplete parameters in PUT request`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(400).json({error: `Invalid request: Incomplete parameters in PUT request`});
        }

        // Step 3: Validate device IDs against their registries (§4.2).
        const GROUP_COLUMN_BY_NAME_PUT = {
            'Apollo AIR-1':        'apollo_air_1_ids',
            'Apollo MSR-2':        'apollo_msr_2_ids',
            'Athom Smart Plug v2': 'athom_smart_plug_v2_ids',
            'Zigbee2MQTT':         'zigbee2mqtt_ids',
        };
        const REGISTRY_TABLE_BY_NAME_PUT = {
            'Apollo AIR-1':        'apollo_air_1',
            'Apollo MSR-2':        'apollo_msr_2',
            'Athom Smart Plug v2': 'athom_smart_plug_v2',
            'Zigbee2MQTT':         'zigbee2mqtt',
        };
        const inputsPut = [
            ['Apollo AIR-1',        apollo_air_1_ids],
            ['Apollo MSR-2',        apollo_msr_2_ids],
            ['Athom Smart Plug v2', athom_smart_plug_v2_ids],
            ['Zigbee2MQTT',         zigbee2mqtt_ids],
        ];
        // setOps: list of {column, value} where value is null (REMOVE MEMBERS) or a JS array.
        const setOps = [];

        for (let [deviceName, ids] of inputsPut) {
            if (!ids) continue;

            if (ids === '[REMOVE MEMBERS]') {
                setOps.push({column: GROUP_COLUMN_BY_NAME_PUT[deviceName], value: null});
                continue;
            }
            if (typeof ids === 'string') ids = [ids];

            for (const deviceID of ids) {
                const id_is_available = await ID_is_available(REGISTRY_TABLE_BY_NAME_PUT[deviceName], deviceID);
                if (!id_is_available) {
                    server_Log(`Not Found: ${deviceName} with ID: ${deviceID} does not exist`);
                    UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                    return res.status(404).json({error: `Not Found: ${deviceName} with ID: ${deviceID} does not exist`});
                }
                if (deviceName === 'Zigbee2MQTT') {
                    const check = await client.query('SELECT 1 FROM zigbee2mqtt WHERE id = $1 AND type != $2;', [deviceID, 'group']);
                    if (!check.rowCount) {
                        server_Log(`Bad Request: ${deviceName} with ID: ${deviceID} cannot be added to a group`);
                        UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                        return res.status(400).json({error: `Bad Request: ${deviceName} with ID: ${deviceID} cannot be added to a group`});
                    }
                }
            }

            setOps.push({column: GROUP_COLUMN_BY_NAME_PUT[deviceName], value: ids});
        }

        // Step 4: Edit the group's details using a single parameterized UPDATE.
        if (setOps.length) {
            const setClauses = setOps.map((op, i) => `"${op.column}" = $${i + 1}`);
            const params = setOps.map(op => op.value);
            params.push(id);
            const queryTextUpdate = `UPDATE groups SET ${setClauses.join(', ')} WHERE id = $${params.length};`;
            try {
                await client.query(queryTextUpdate, params);
            } catch (err) {
                server_Log(`ERROR: Bad arguments in PUT /groups — ${sanitizePgError(err)}`);
                UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
                return res.status(400).json({error: `Bad arguments in PUT request`});
            }
        }

        server_Log(`Successfully edited members of group with ID: ${id}`);
        if(apollo_air_1_ids){server_Log(` - Apollo AIR-1's: ${apollo_air_1_ids}`);}
        if(apollo_msr_2_ids){server_Log(` - Apollo MSR-2's: ${apollo_msr_2_ids}`);}
        if(athom_smart_plug_v2_ids){server_Log(` - Athom Smart Plug v2's: ${athom_smart_plug_v2_ids}`);}
        if(zigbee2mqtt_ids){server_Log(` - Zigbee2MQTT's: ${zigbee2mqtt_ids}`);}
        UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
        return res.status(200).send(`Successfully edited members of group with ID: ${id}`);
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// (1d) DELETE: Delete a group. FOR HIGHEST PRIVILEGE ONLY
app.delete("/groups", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const id = req.query.id;

        // Step 1: Check if a group with ID: id is available
        let queryText = 'SELECT * FROM groups WHERE id = $1;';
        let queryValues = [id];
        let queryResult = await client.query(queryText, queryValues);

        if(!queryResult.rows.length){
            server_Log(`Not Found: Group with ID: ${id} does not exist`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({error: `Not Found: Group with ID: ${id} does not exist`});
        }

        // Step 2: DELETE the group from the table
        try {
            await client.query('DELETE FROM groups WHERE id = $1;', [id]);
            server_Log(`Successfully deleted group with ID: ${id}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
            return res.status(200).send(`Successfully deleted group with ID: ${id}`);
        } catch (err) {
            server_Log(`Failed to delete group ${id} — ${sanitizePgError(err)}`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(500).json({error: 'Database error occurred.'});
        }
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// (#2) "/groups/{id}" GET all current data from all devices in a specific group
app.get("/groups/:id", async (req, res) => {
    try{
        let specific_api_key = req.header("x-api-key"); //Extract API Key from Header
        if(await SECURITY_CHECK(res, req, specific_api_key, [0,1,2]) === false){ //______ SECURITY CONDITIONAL
            return;
        }

        const id = req.params.id;

        // Step 1: Check if a group with ID: id is available. If id is available, get the group members
        const groupResult = await client.query('SELECT * FROM groups WHERE id = $1;', [id]);
        if (!groupResult.rows.length) {
            server_Log(`Not Found: Group with ID: ${id} does not exist`);
            UPDATE_transactions(specific_api_key, req.method, req.originalUrl, false);
            return res.status(404).json({error: `Not Found: Group with ID: ${id} does not exist`});
        }
        const groupMembers = groupResult.rows[0];
        delete groupMembers.id;

        // Step 2: Get the most recent data from the group's members
        const groupData = {};
        for (const [columnKey, ids] of Object.entries(groupMembers)) {
            if (!ids) continue;
            for (const deviceID of ids) {
                const tableName = `${columnKey.replace("_ids", "")}_${deviceID}`;
                const queryText = format('SELECT * FROM %I ORDER BY timestamp DESC LIMIT 1;', tableName);
                try {
                    const r = await client.query(queryText);
                    groupData[tableName] = r.rows[0];
                } catch (err) {
                    server_Log(`Group fetch failed for ${tableName} — ${sanitizePgError(err)}`);
                    groupData[tableName] = null;
                }
            }
        }

        // Step 3: Pass the data to the GET request
        server_Log(`Successfully returned most recent data for all devices in the group with id: ${id}`);
        UPDATE_transactions(specific_api_key, req.method, req.originalUrl, true);
        res.json(groupData);
    }catch(err){
        server_Log(`Internal Server Error: An unexpected error occurred\n${err}`);
        return res.status(500).json({error: `Internal Server Error: An unexpected error occurred`});
    }
})

// END Groups Endpoints -------------------------------- //


// ------------ END Define REST Endpoints ----------- //



const PORT = process.env.HOST_PORT || 3000;
app.listen(PORT, () => console.log(`SSL IoT 1 Server Hosted at port ${PORT}`));
