import "./style.css";
import Plotly from "plotly.js-dist-min";

// ----- DOM handles ----------------------------------------------------
// const PLOT_DIV       = document.getElementById("probability-plot"); // [GRAPH-1 DISABLED]
const HIST_DIV       = document.getElementById("history-plot");
const MSE_DIV        = document.getElementById("mse-plot");
const MSE_CURRENT    = document.getElementById("mse-current");
const MODEL_RUN      = document.getElementById("model-run-id");
const DATA_SOURCE    = document.getElementById("data-source");
const RTSP_STATUS    = document.getElementById("rtsp-status");
const RTSP_URL       = document.getElementById("rtsp-url");
const REC_DOT        = document.getElementById("rec-dot");
const REC_STATUS     = document.getElementById("rec-status");
const FRAME_AGE      = document.getElementById("rtsp-frame-age");
const OVERLAY_RTSP   = document.getElementById("overlay-rtsp-host");
const OVERLAY_FPS    = document.getElementById("overlay-fps");
const UTC_CLOCK      = document.getElementById("utc-clock");
const DIGITS         = document.getElementById("digits");
const DIGITS_SUFFIX  = document.getElementById("digits-suffix");
const OCCUPIED_FLAG  = document.getElementById("occupied-flag");
const THRESHOLD_TAG  = document.getElementById("threshold-tag");
const THRESHOLD_VAL  = document.getElementById("threshold-value");
const ERROR_BAND     = document.getElementById("error-band");
const ERROR_TEXT     = document.getElementById("error-text");
const LATEST_TS      = document.getElementById("latest-ts");
const REFERENCE_TS   = document.getElementById("reference-ts");
const CFG_GAP        = document.getElementById("cfg-gap");
const CFG_AGE        = document.getElementById("cfg-age");
const TICKER         = document.getElementById("ticker");
const SAMPLE_COUNT   = document.getElementById("sample-count");
const LAST_TICK_REL  = document.getElementById("last-tick-rel");
const POINTER_PATH   = document.getElementById("pointer-path");
const GT_COUNT       = document.getElementById("ground-truth-count");
const GT_FLAG        = document.getElementById("ground-truth-flag");
const GT_META        = document.getElementById("ground-truth-meta");
const MASK_OVERLAY   = document.getElementById("mask-overlay");
const MASK_STATE     = document.getElementById("mask-state");

// ----- constants ------------------------------------------------------
const MAX_POINTS = 240;
const SMOOTHING_WINDOW_TICKS = 5;
const SMOOTHING_ALPHA = 2 / (SMOOTHING_WINDOW_TICKS + 1);
let threshold = null;
let lastTickInstant = null;
let totalTicks = 0;
let smoothedProbability = null;
let latestPlottedTimestampMs = null;
const TICKER_LINES = [];

// ----- plotly oscilloscope styling -----------------------------------
const PLOT_FONT = '"Google Sans Mono", "JetBrains Mono", ui-monospace, monospace';
// M3 teal primary & surface colours (mirrors CSS tokens)
const M3_PRIMARY       = "#4DD9AC";
const M3_OUTLINE       = "#889490";
const M3_OUTLINE_VAR   = "#3F4944";
const M3_ERROR         = "#FFB4AB";

// [GRAPH-1 DISABLED] — makeLayout / makeInitialTrace kept for reference
// function makeLayout() { ... }
// function makeInitialTrace() { ... }
// Plotly.newPlot(PLOT_DIV, [makeInitialTrace()], makeLayout(), { responsive: true, displayModeBar: false });

// ----- helpers --------------------------------------------------------
function shortRunId(id) {
    if (!id) return "—";
    const s = String(id);
    if (s.length <= 22) return s;
    return s.slice(0, 8) + "…" + s.slice(-8);
}
function shortHost(url) {
    if (!url) return "—";
    try { return url.replace(/^[a-z]+:\/\//, "").split("/")[0]; }
    catch { return url; }
}
function fmtAge(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "—";
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
    return `${(seconds / 3600).toFixed(1)}h`;
}
function pad2(n) { return String(n).padStart(2, "0"); }

function formatRelative(then) {
    if (!then) return "—";
    const diffSec = (Date.now() - then) / 1000;
    if (diffSec < 1.5) return "just now";
    if (diffSec < 60) return `${Math.round(diffSec)}s ago`;
    if (diffSec < 3600) return `${Math.round(diffSec / 60)}m ago`;
    return `${Math.round(diffSec / 3600)}h ago`;
}

function pushTicker(line) {
    TICKER_LINES.push(line);
    while (TICKER_LINES.length > 5) TICKER_LINES.shift();
    TICKER.textContent = TICKER_LINES.join("   ::   ");
}

function resetSmoothing() {
    smoothedProbability = null;
}

function smoothProbability(probability) {
    const p = Number(probability);
    if (!Number.isFinite(p)) return null;
    smoothedProbability = smoothedProbability === null
        ? p
        : (SMOOTHING_ALPHA * p) + ((1 - SMOOTHING_ALPHA) * smoothedProbability);
    return smoothedProbability;
}

function smoothProbabilitySeries(probabilities) {
    resetSmoothing();
    return probabilities
        .map(probability => smoothProbability(probability))
        .filter(probability => Number.isFinite(probability));
}

function visualOccupied(probability) {
    return threshold !== null && Number.isFinite(probability) ? probability >= threshold : null;
}

function eventTimestampMs(event) {
    const ms = Date.parse(event.timestamp);
    return Number.isFinite(ms) ? ms : null;
}

function mmwaveLookbackFraction(event) {
    const value = Number(event?.sensor_context?.mmwave_s5_occupied_fraction);
    return Number.isFinite(value) ? value : null;
}

function setDigits(probability) {
    DIGITS.textContent = probability.toFixed(4);
    DIGITS.classList.remove("flash");
    void DIGITS.offsetWidth;          // restart animation
    DIGITS.classList.add("flash");
    if (threshold !== null && probability >= threshold) {
        DIGITS.classList.add("alarm");
    } else {
        DIGITS.classList.remove("alarm");
    }
}

function setOccupiedFlag(occupied, probability) {
    if (occupied === true) {
        OCCUPIED_FLAG.dataset.state = "occupied";
        OCCUPIED_FLAG.textContent = "OCCUPIED";
    } else if (occupied === false) {
        OCCUPIED_FLAG.dataset.state = "vacant";
        OCCUPIED_FLAG.textContent = "VACANT";
    } else if (Number.isFinite(probability)) {
        OCCUPIED_FLAG.dataset.state = "idle";
        OCCUPIED_FLAG.textContent = probability >= 0.5 ? "ELEVATED" : "NOMINAL";
    } else {
        OCCUPIED_FLAG.dataset.state = "idle";
        OCCUPIED_FLAG.textContent = "IDLE";
    }
}

function setGroundTruth(event) {
    const count = Number(event.ground_truth_count);
    const hasLabel = Number.isFinite(count) && event.ground_truth_timestamp;
    if (!hasLabel) {
        GT_COUNT.textContent = "--";
        GT_FLAG.dataset.state = "idle";
        GT_FLAG.textContent = "NO LABEL";
        GT_META.textContent = "--";
        GT_META.title = "";
        return;
    }

    GT_COUNT.textContent = Number.isInteger(count) ? String(count) : count.toFixed(2);
    if (event.ground_truth_occupied === true) {
        GT_FLAG.dataset.state = "occupied";
        GT_FLAG.textContent = "OCCUPIED";
    } else {
        GT_FLAG.dataset.state = "vacant";
        GT_FLAG.textContent = "VACANT";
    }

    const age = Number(event.ground_truth_age_minutes);
    const ageText = Number.isFinite(age) ? `${age.toFixed(1)}m old` : "age n/a";
    const ts = String(event.ground_truth_timestamp);
    const timeText = ts.split("T")[1] || ts;
    GT_META.textContent = `${ageText} @ ${timeText}`;
    GT_META.title = ts;
}

function showError(text) {
    if (text) {
        ERROR_TEXT.textContent = text;
        ERROR_BAND.hidden = false;
        ERROR_BAND.classList.remove("hidden");
    } else {
        ERROR_BAND.hidden = true;
        ERROR_BAND.classList.add("hidden");
        ERROR_TEXT.textContent = "";
    }
}

function setMaskState(mask) {
    const available = Boolean(mask && mask.available && mask.url);
    MASK_STATE.dataset.state = available ? "active" : "missing";
    MASK_STATE.textContent = available ? "MASK ON" : "MASK OFF";
    MASK_STATE.title = mask && mask.path ? String(mask.path) : "";

    if (available) {
        if (!MASK_OVERLAY.getAttribute("src")) {
            MASK_OVERLAY.src = String(mask.url);
        }
        MASK_OVERLAY.hidden = false;
    } else {
        MASK_OVERLAY.hidden = true;
        MASK_OVERLAY.removeAttribute("src");
    }
}

MASK_OVERLAY.addEventListener("error", () => {
    MASK_OVERLAY.hidden = true;
    MASK_STATE.dataset.state = "missing";
    MASK_STATE.textContent = "MASK OFF";
});

// ----- /api/health polling -------------------------------------------
async function refreshHealth() {
    try {
        const response = await fetch("/api/health");
        if (!response.ok) return;
        const payload = await response.json();
        if (payload.inference) {
            const inf = payload.inference;
            //MODEL_RUN.textContent = shortRunId(inf.model_run_id);
            //MODEL_RUN.title = inf.model_run_id || "";
            //DATA_SOURCE.textContent = inf.data_source || "—";
            if (inf.last_error) {
                showError(inf.last_error);
            } else if (!inf.last_event || !inf.last_event.error) {
                showError(null);
            }
        }
        if (payload.rtsp) {
            const r = payload.rtsp;
            //RTSP_URL.textContent = r.url_safe || "—";
            OVERLAY_RTSP.textContent = shortHost(r.url_safe || "");
            //RTSP_STATUS.classList.remove("pending", "connected", "disconnected");
            //RTSP_STATUS.classList.add(r.connected ? "connected" : "disconnected");
            REC_STATUS.textContent = r.connected ? "LIVE" : "OFFLINE";
            REC_DOT.style.background = r.connected ? "var(--hot)" : "var(--text-faint)";
            const age = r.latest_frame_age_sec;
            FRAME_AGE.textContent = fmtAge(age);
            OVERLAY_FPS.textContent = (age != null && age > 0) ? `${(1 / age).toFixed(1)} fps` : "--";
        }
        if (payload.mask) {
            setMaskState(payload.mask);
        }
        if (payload.config) {
            const t = payload.config.threshold;
            const tn = (t === null || t === undefined) ? null : Number(t);
            if (tn !== threshold) {
                threshold = tn;
                if (tn !== null) {
                    THRESHOLD_VAL.textContent = tn.toFixed(2);
                    THRESHOLD_TAG.hidden = false;
                } else {
                    THRESHOLD_TAG.hidden = true;
                }
                // Plotly.relayout(PLOT_DIV, makeLayout()); // [GRAPH-1 DISABLED]
            }
            if (payload.config.max_gap_minutes != null) {
                CFG_GAP.textContent = `${Number(payload.config.max_gap_minutes).toFixed(0)}m`;
            }
            if (payload.config.max_age_minutes != null) {
                CFG_AGE.textContent = `${Number(payload.config.max_age_minutes).toFixed(0)}m`;
            } else {
                CFG_AGE.textContent = "off";
            }
            if (payload.config.production_pointer) {
                const p = String(payload.config.production_pointer);
                POINTER_PATH.textContent = p.length > 28 ? "…" + p.slice(-26) : p;
                POINTER_PATH.title = p;
            }
        }
    } catch (err) {
        console.warn("health refresh failed", err);
    }
}

// ----- /api/history initial load -------------------------------------
async function loadHistory() {
    try {
        const response = await fetch(`/api/history?n=${MAX_POINTS}`);
        if (!response.ok) return;
        const events = await response.json();
        const valid = events.filter(ev => ev.error == null && Number.isFinite(ev.probability));
        // [GRAPH-1 DISABLED] — smoothed series + PLOT_DIV react removed
        // const xs = valid.map(ev => ev.timestamp);
        // const ys = smoothProbabilitySeries(valid.map(ev => ev.probability));
        // Plotly.react(PLOT_DIV, [{ ...makeInitialTrace(), x: xs, y: ys }], makeLayout());
        totalTicks = valid.length;
        SAMPLE_COUNT.textContent = String(totalTicks);
        if (valid.length > 0) {
            const last = valid[valid.length - 1];
            const displayedProbability = Number(last.probability);
            latestPlottedTimestampMs = eventTimestampMs(last);
            setDigits(displayedProbability);
            setOccupiedFlag(visualOccupied(displayedProbability), displayedProbability);
            setGroundTruth(last);
            LATEST_TS.textContent = last.timestamp;
            REFERENCE_TS.textContent = last.reference_time || last.timestamp;
            lastTickInstant = Date.now();
            LAST_TICK_REL.textContent = "just now";
        }
    } catch (err) {
        console.warn("history load failed", err);
    }
}

// ----- SSE stream subscription ---------------------------------------
function appendEventToPlot(event) {
    if (event.error) {
        showError(event.error);
        pushTicker(`ERR ${event.error}`);
        return;
    }
    if (!Number.isFinite(event.probability)) {
        showError("non-finite probability");
        return;
    }
    const timestampMs = eventTimestampMs(event);
    if (timestampMs !== null && latestPlottedTimestampMs !== null && timestampMs <= latestPlottedTimestampMs) {
        return;
    }
    showError(null);
    const displayedProbability = smoothProbability(event.probability);
    if (!Number.isFinite(displayedProbability)) {
        showError("non-finite smoothed probability");
        return;
    }
    // [GRAPH-1 DISABLED] — live extendTraces removed
    // Plotly.extendTraces(PLOT_DIV, { x: [[event.timestamp]], y: [[displayedProbability]] }, [0], MAX_POINTS);
    if (timestampMs !== null) latestPlottedTimestampMs = timestampMs;
    setDigits(displayedProbability);
    setOccupiedFlag(visualOccupied(displayedProbability), displayedProbability);
    setGroundTruth(event);
    LATEST_TS.textContent = event.timestamp;
    REFERENCE_TS.textContent = event.reference_time || event.timestamp;
    /*if (event.model_run_id) {
        MODEL_RUN.textContent = shortRunId(event.model_run_id);
        MODEL_RUN.title = event.model_run_id;
    }*/
    //if (event.source_label) DATA_SOURCE.textContent = event.source_label;
    totalTicks += 1;
    SAMPLE_COUNT.textContent = String(totalTicks);
    lastTickInstant = Date.now();
    LAST_TICK_REL.textContent = "just now";
    const gtText = Number.isFinite(Number(event.ground_truth_count))
        ? ` cv=${Number(event.ground_truth_count).toFixed(0)}`
        : "";
    pushTicker(`tick @ ${event.timestamp.split("T")[1] || event.timestamp} → p=${displayedProbability.toFixed(4)}${gtText}`);
}

function connectStream() {
    const evt = new EventSource("/api/stream");
    evt.addEventListener("tick", (e) => {
        try {
            const event = JSON.parse(e.data);
            appendEventToPlot(event);
        } catch (err) {
            console.warn("bad tick payload", err);
        }
    });
    evt.onerror = () => {
        evt.close();
        setTimeout(connectStream, 2000);
    };
}

// ----- decorative clock ----------------------------------------------
function tickClock() {
    const now = new Date();
    UTC_CLOCK.textContent =
        `${pad2(now.getUTCHours()+8)}:${pad2(now.getUTCMinutes())}:${pad2(now.getUTCSeconds())}`;
    LAST_TICK_REL.textContent = formatRelative(lastTickInstant);
}

// ----- environment panel toggle --------------------------------------
const ENV_PANEL      = document.getElementById("env-panel");
const ENV_TOGGLE_BTN = document.getElementById("env-toggle-btn");
const ENV_TOGGLE_ICO = document.getElementById("env-toggle-icon");
const ENV_TOGGLE_LBL = document.getElementById("env-toggle-label");
const ENV_FRESH      = document.getElementById("env-fresh-badge");

// ----- sensor sparkline graphs ---------------------------------------
const MAX_ENV_POINTS = 60;

const SENSOR_PLOTS = {
    air1: {
        co2:  { el: "air1-graph-co2",  label: "CO₂ ppm",  color: "#4DD9AC", unit: "ppm", key: "co2" },
        temp: { el: "air1-graph-temp", label: "Temp °C",   color: "#FFBA4D", unit: "°C",  key: "temperature" },
    },
    msr2: {
        co2:  { el: "msr2-graph-co2",  label: "CO₂ ppm",  color: "#4DD9AC", unit: "ppm", key: "co2" },
        temp: { el: "msr2-graph-temp", label: "Temp °C",   color: "#FFBA4D", unit: "°C",  key: "temperature" },
    },
    sensibo: {
        temp: { el: "sensibo-graph-temp", label: "Temp °C", color: "#FFBA4D", unit: "°C", key: "temperature" },
        humidity: { el: "sensibo-graph-humidity", label: "Humidity %", color: "#84D5A1", unit: "%", key: "humidity" },
    },
    ag: {
        co2:  { el: "ag-graph-co2",  label: "CO₂ ppm",  color: "#4DD9AC", unit: "ppm", key: "co2" },
        temp: { el: "ag-graph-temp", label: "Temp °C",   color: "#FFBA4D", unit: "°C",  key: "temperature" },
    },
};

const envHistory = {};
for (const dev of Object.keys(SENSOR_PLOTS)) {
    envHistory[dev] = {};
    for (const metric of Object.keys(SENSOR_PLOTS[dev])) {
        envHistory[dev][metric] = { xs: [], ys: [] };
    }
}

const SPARK_FONT = '"Google Sans Mono", "JetBrains Mono", ui-monospace, monospace';
const SPARK_GRID = "#3F494460";
const SPARK_OUTLINE = "#889490";

function makeSparkLayout(unit) {
    return {
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor:  "rgba(0,0,0,0)",
        font: { color: SPARK_OUTLINE, family: SPARK_FONT, size: 8 },
        margin: { l: 32, r: 4, t: 4, b: 20 },
        xaxis: {
            gridcolor: SPARK_GRID, zerolinecolor: SPARK_GRID, color: SPARK_OUTLINE,
            tickfont: { size: 7 }, showgrid: false, tickformat: "%H:%M",
        },
        yaxis: {
            gridcolor: SPARK_GRID, zerolinecolor: SPARK_GRID, color: SPARK_OUTLINE,
            tickfont: { size: 7 }, showgrid: true, gridwidth: 0.5,
        },
        showlegend: false,
    };
}

function makeSparkTrace(color) {
    return {
        x: [], y: [],
        type: "scatter", mode: "lines",
        line: { color, width: 1.5, shape: "spline", smoothing: 0.4 },
        fill: "tozeroy",
        fillcolor: `${color}1A`,
        hovertemplate: "%{x}<br><b>%{y:.1f}</b><extra></extra>",
    };
}

let sparkPlotsInitialized = false;

function initSparkPlots() {
    if (sparkPlotsInitialized) return;
    sparkPlotsInitialized = true;
    for (const [dev, metrics] of Object.entries(SENSOR_PLOTS)) {
        for (const [, cfg] of Object.entries(metrics)) {
            const el = document.getElementById(cfg.el);
            if (!el) continue;
            Plotly.newPlot(el, [makeSparkTrace(cfg.color)], makeSparkLayout(cfg.unit), {
                responsive: true, displayModeBar: false, staticPlot: false,
            });
        }
    }
}

function updateSparkPlot(dev, metricKey, ts, value) {
    const cfg = SENSOR_PLOTS[dev]?.[metricKey];
    if (!cfg) return;
    const hist = envHistory[dev][metricKey];
    hist.xs.push(ts);
    hist.ys.push(value);
    if (hist.xs.length > MAX_ENV_POINTS) { hist.xs.shift(); hist.ys.shift(); }
    const el = document.getElementById(cfg.el);
    if (!el || !el._fullLayout) return;
    Plotly.react(el, [{ ...makeSparkTrace(cfg.color), x: hist.xs, y: hist.ys }], makeSparkLayout(cfg.unit));
}

let envPanelOpen = false;
ENV_TOGGLE_BTN.addEventListener("click", () => {
    envPanelOpen = !envPanelOpen;
    ENV_PANEL.classList.toggle("env-collapsed", !envPanelOpen);
    ENV_TOGGLE_ICO.textContent = envPanelOpen ? "expand_less" : "expand_more";
    ENV_TOGGLE_LBL.textContent = envPanelOpen ? "Hide Details" : "More Details";
    if (envPanelOpen) {
        initSparkPlots();
        setTimeout(() => {
            for (const metrics of Object.values(SENSOR_PLOTS))
                for (const cfg of Object.values(metrics)) {
                    const el = document.getElementById(cfg.el);
                    if (el) Plotly.Plots.resize(el);
                }
        }, 320);
    }
    setTimeout(() => {
        // Plotly.Plots.resize(PLOT_DIV); // [GRAPH-1 DISABLED]
        if (histChartsInitialized) {
            Plotly.Plots.resize(HIST_DIV);
            Plotly.Plots.resize(MSE_DIV);
        }
    }, 320);
});

// ----- environment data fetching -------------------------------------
const ENV_API_BASE = "/env-api";
const ENV_DEVICES = {
    air1:    { path: "air-1/889720" },
    msr2:    { path: "msr-2/89f464" },
    sensibo: { path: "sensibo/climate.back_right_sensibo_air" },
    ag:      { path: "ag-one/6f31cc" },
};

function fmtNum(v, digits = 1) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(digits) : "—";
}

function co2Quality(ppm) {
    if (!Number.isFinite(ppm)) return { label: "—", level: "" };
    if (ppm < 800)  return { label: "Good",     level: "good" };
    if (ppm < 1200) return { label: "Moderate", level: "moderate" };
    return              { label: "Poor",     level: "poor" };
}

function updateEnvFreshBadge() {
    ENV_FRESH.classList.remove("hidden");
    ENV_FRESH.classList.add("visible");
    clearTimeout(updateEnvFreshBadge._timer);
    updateEnvFreshBadge._timer = setTimeout(() => ENV_FRESH.classList.add("hidden"), 5000);
}

function applyAir1(d) {
    document.getElementById("air1-co2").textContent      = fmtNum(d.co2, 0);
    document.getElementById("air1-temp").textContent     = fmtNum(d.temperature);
    document.getElementById("air1-humidity").textContent = fmtNum(d.humidity);
    document.getElementById("air1-voc").textContent      = fmtNum(d.voc, 0);
    document.getElementById("air1-pm25").textContent     = fmtNum(d.pm_2_5);
    document.getElementById("air1-nox").textContent      = fmtNum(d.nox, 0);
    document.getElementById("air1-ts").textContent       = (d.timestamp || "").split("T")[1]?.slice(0,8) || "—";
    const ts = d.timestamp || new Date().toISOString();
    if (Number.isFinite(Number(d.co2)))         updateSparkPlot("air1", "co2",  ts, Number(d.co2));
    if (Number.isFinite(Number(d.temperature))) updateSparkPlot("air1", "temp", ts, Number(d.temperature));
}

function applyMsr2(d) {
    document.getElementById("msr2-co2").textContent   = fmtNum(d.co2, 0);
    document.getElementById("msr2-temp").textContent  = fmtNum(d.temperature, 1);
    document.getElementById("msr2-light").textContent = fmtNum(d.light, 3);
    document.getElementById("msr2-uv").textContent    = fmtNum(d.uv_index, 0);
    const motionEl = document.getElementById("msr2-motion");
    const detected = d.detection_target === "true" || d.detection_target === true;
    motionEl.textContent       = detected ? "DETECTED" : "CLEAR";
    motionEl.dataset.state     = String(detected);
    document.getElementById("msr2-ts").textContent = (d.timestamp || "").split("T")[1]?.slice(0,8) || "—";
    const ts = d.timestamp || new Date().toISOString();
    if (Number.isFinite(Number(d.co2)))         updateSparkPlot("msr2", "co2",  ts, Number(d.co2));
    if (Number.isFinite(Number(d.temperature))) updateSparkPlot("msr2", "temp", ts, Number(d.temperature));
}

function applySensibo(d) {
    document.getElementById("sensibo-temp").textContent     = fmtNum(d.temperature);
    document.getElementById("sensibo-humidity").textContent = fmtNum(d.humidity, 0);
    const modeEl = document.getElementById("sensibo-mode");
    const mode = String(d.hvac_mode || "off").toLowerCase();
    modeEl.textContent    = mode.toUpperCase();
    modeEl.dataset.mode   = mode;
    const sp = d.target_temperature;
    document.getElementById("sensibo-setpoint").textContent = sp != null ? fmtNum(sp) : "—";
    document.getElementById("sensibo-ts").textContent = (d.timestamp || "").split("T")[1]?.slice(0,8) || "—";
    const ts = d.timestamp || new Date().toISOString();
    if (Number.isFinite(Number(d.temperature))) updateSparkPlot("sensibo", "temp",     ts, Number(d.temperature));
    if (Number.isFinite(Number(d.humidity)))    updateSparkPlot("sensibo", "humidity", ts, Number(d.humidity));
}

function applyAg(d) {
    document.getElementById("ag-co2").textContent      = fmtNum(d.co2, 0);
    document.getElementById("ag-temp").textContent     = fmtNum(d.temperature);
    document.getElementById("ag-humidity").textContent = fmtNum(d.humidity);
    document.getElementById("ag-voc").textContent      = fmtNum(d.voc, 0);
    document.getElementById("ag-pm25").textContent     = fmtNum(d.pm_2_5);
    document.getElementById("ag-nox").textContent      = fmtNum(d.nox, 0);
    document.getElementById("ag-ts").textContent       = (d.timestamp || "").split("T")[1]?.slice(0,8) || "—";
    const ts = d.timestamp || new Date().toISOString();
    if (Number.isFinite(Number(d.co2)))         updateSparkPlot("ag", "co2",  ts, Number(d.co2));
    if (Number.isFinite(Number(d.temperature))) updateSparkPlot("ag", "temp", ts, Number(d.temperature));
}

// Accumulate latest readings for summary
const envLatest = {};

function updateSummary() {
    const co2Vals  = ["air1","msr2","ag"].map(k => Number(envLatest[k]?.co2)).filter(Number.isFinite);
    const tmpVals  = ["air1","msr2","sensibo","ag"].map(k => Number(envLatest[k]?.temperature)).filter(Number.isFinite);
    const avgCo2  = co2Vals.length  ? co2Vals.reduce((a,b)=>a+b,0)/co2Vals.length   : NaN;
    const avgTemp = tmpVals.length  ? tmpVals.reduce((a,b)=>a+b,0)/tmpVals.length    : NaN;
    document.getElementById("summary-co2").textContent  = Number.isFinite(avgCo2)  ? avgCo2.toFixed(0)  : "—";
    document.getElementById("summary-temp").textContent = Number.isFinite(avgTemp) ? avgTemp.toFixed(1) : "—";
    const { label, level } = co2Quality(avgCo2);
    const qualEl = document.getElementById("summary-co2-quality");
    qualEl.textContent    = label;
    qualEl.dataset.level  = level;
}

async function refreshEnv() {
    const results = await Promise.allSettled(
        Object.entries(ENV_DEVICES).map(([key, { path }]) =>
            fetch(`${ENV_API_BASE}/${path}`)
                .then(r => {
                    if (!r.ok) return Promise.reject(`HTTP ${r.status}`);
                    const ct = r.headers.get("content-type") || "";
                    if (!ct.includes("application/json")) return Promise.reject(`non-JSON response (${ct})`);
                    return r.json();
                })
                .then(data => ({ key, data: Array.isArray(data) ? data[0] : data }))
        )
    );
    for (const r of results) {
        if (r.status === "rejected") console.warn("env fetch failed:", r.reason);
    }
    let anyOk = false;
    for (const r of results) {
        if (r.status !== "fulfilled") continue;
        const { key, data } = r.value;
        if (!data) continue;
        anyOk = true;
        envLatest[key] = data;
        if (key === "air1")    applyAir1(data);
        if (key === "msr2")    applyMsr2(data);
        if (key === "sensibo") applySensibo(data);
        if (key === "ag")      applyAg(data);
    }
    if (anyOk) { updateSummary(); updateEnvFreshBadge(); }
}

// ----- Historical Predictions + MSE charts ---------------------------
const M3_TERTIARY    = "#84D5A1";
const M3_UNOCCUPIED   = "#ff4d3a";
const M3_WARN        = "#FFB4AB";
const M3_MMWAVE      = "#FFBA4D";

function makeHistLayout(nowTs) {
    const gridCol = `${M3_OUTLINE_VAR}60`;
    const shapes = [];
    if (nowTs) {
        shapes.push({
            type: "line", xref: "x", yref: "paper",
            x0: nowTs, x1: nowTs, y0: 0, y1: 1,
            line: { color: "#FF4444CC", width: 1.5, dash: "solid" },
        });
    }
    return {
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor:  "rgba(0,0,0,0)",
        font: { color: M3_OUTLINE, family: PLOT_FONT, size: 10 },
        margin: { l: 38, r: 12, t: 8, b: 26 },
        xaxis: {
            gridcolor: gridCol, zerolinecolor: gridCol, color: M3_OUTLINE,
            tickfont: { size: 9 }, showgrid: true, tickformat: "%H:%M:%S",
        },
        yaxis: {
            gridcolor: gridCol, zerolinecolor: gridCol, color: M3_OUTLINE,
            range: [0, 1],
            tickvals: [0, 0.25, 0.5, 0.75, 1.0],
            ticktext: ["0.00", "0.25", "0.50", "0.75", "1.00"],
            tickfont: { size: 9 },
            title: { text: "probability / fraction", font: { size: 9, color: M3_OUTLINE } },
        },
        showlegend: false,
        shapes,
    };
}

function makeMseLayout() {
    const gridCol = `${M3_OUTLINE_VAR}60`;
    return {
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor:  "rgba(0,0,0,0)",
        font: { color: M3_OUTLINE, family: PLOT_FONT, size: 10 },
        margin: { l: 38, r: 12, t: 8, b: 26 },
        xaxis: {
            gridcolor: gridCol, zerolinecolor: gridCol, color: M3_OUTLINE,
            tickfont: { size: 9 }, showgrid: true, tickformat: "%H:%M:%S",
        },
        yaxis: {
            gridcolor: gridCol, zerolinecolor: gridCol, color: M3_OUTLINE,
            tickfont: { size: 9 }, rangemode: "tozero",
            title: { text: "MSE", font: { size: 9, color: M3_OUTLINE } },
        },
        showlegend: false,
    };
}

let histChartsInitialized = false;

function initHistCharts() {
    if (histChartsInitialized) return;
    histChartsInitialized = true;

    const gtTrace = {
        x: [], y: [],
        type: "scatter", mode: "none", name: "CV GT",
        line: { shape: "vh" },
        fill: "tozeroy", fillcolor: `${M3_TERTIARY}40`,
        hovertemplate: "%{x|%H:%M:%S}<br><b>GT=%{y}</b><extra></extra>",
        showlegend: false,
    };
    const gtTrace2 = { ...gtTrace, fillcolor: `${M3_UNOCCUPIED}40` };
    const mmwaveTrace = {
        x: [], y: [],
        type: "scatter", mode: "lines", name: "mmWave lookback",
        line: { color: M3_MMWAVE, width: 1.7, dash: "dot", shape: "vh" },
        hovertemplate: "%{x|%H:%M:%S}<br><b>mmWave lookback=%{y:.1%}</b><extra></extra>",
        showlegend: false,
    };
    const predTrace = {
        x: [], y: [],
        type: "scatter", mode: "lines", name: "raw prediction",
        line: { color: M3_PRIMARY, width: 1.8, shape: "spline", smoothing: 0.4 },
        hovertemplate: "%{x|%H:%M:%S}<br><b>raw p=%{y:.4f}</b><extra></extra>",
        showlegend: false,
    };
    Plotly.newPlot(HIST_DIV, [gtTrace, gtTrace2, mmwaveTrace, predTrace], makeHistLayout(null), {
        responsive: true, displayModeBar: false,
    });

    // MSE trace
    const mseTrace = {
        x: [], y: [],
        type: "scatter", mode: "lines",
        line: { color: M3_WARN, width: 1.5, shape: "spline", smoothing: 0.3 },
        fill: "tozeroy",
        fillcolor: `${M3_WARN}22`,
        hovertemplate: "%{x|%H:%M:%S}<br><b>MSE=%{y:.6f}</b><extra></extra>",
    };
    Plotly.newPlot(MSE_DIV, [mseTrace], makeMseLayout(), {
        responsive: true, displayModeBar: false,
    });
}

async function refreshHistCharts() {
    initHistCharts();
    try {
        const resp = await fetch(`/api/history?n=${MAX_POINTS}`);
        if (!resp.ok) return;
        const events = await resp.json();

        // only events with both probability and CV ground truth
        const valid = events.filter(ev =>
            ev.error == null &&
            Number.isFinite(ev.probability) &&
            ev.ground_truth_timestamp != null
        );
        if (valid.length === 0) return;

        const xs        = valid.map(ev => ev.timestamp);
        const predYs    = valid.map(ev => Number(ev.probability));
        const gtYs      = valid.map(ev => ev.ground_truth_occupied === true ? 1 : 0);
        const mmwaveYs  = valid.map(mmwaveLookbackFraction);

        // MSE: squared error between predicted probability and GT binary
        const mseYs     = predYs.map((p, i) => (p - gtYs[i]) ** 2);
        const nowTs     = valid[valid.length - 1].timestamp;

        Plotly.react(HIST_DIV,
            [
                // GT occupancy background band — drawn first so prediction line sits on top
                { x: xs, y: gtYs,   type: "scatter", mode: "none", name: "CV GT",
                  line: { shape: "vh" },
                  fill: "tozeroy", fillcolor: `${M3_TERTIARY}40`,
                  hovertemplate: "%{x|%H:%M:%S}<br><b>GT=%{y}</b><extra></extra>",
                  showlegend: false },
                // GT occupancy background band (negative space) — fills area below 0 when GT=0, so vacant periods are visually distinct
                { x: xs, y: gtYs.map(y => y === 0 ? 1 : 0), type: "scatter", mode: "none", name: "CV GT vacant",
                  line: { shape: "vh" },
                  fill: "tozeroy", fillcolor: `${M3_UNOCCUPIED}40`,
                  hovertemplate: "%{x|%H:%M:%S}<br><b>GT=%{y}</b><extra></extra>",
                  showlegend: false },
                // mmWave occupancy fraction across the exact prediction lookback window
                { x: xs, y: mmwaveYs, type: "scatter", mode: "lines", name: "mmWave lookback",
                  line: { color: M3_MMWAVE, width: 1.7, dash: "dot", shape: "vh" },
                  hovertemplate: "%{x|%H:%M:%S}<br><b>mmWave lookback=%{y:.1%}</b><extra></extra>",
                  showlegend: false },
                // Prediction probability line
                { x: xs, y: predYs, type: "scatter", mode: "lines", name: "raw prediction",
                  line: { color: M3_PRIMARY, width: 1.8, shape: "spline", smoothing: 0.4 },
                  hovertemplate: "%{x|%H:%M:%S}<br><b>raw p=%{y:.4f}</b><extra></extra>",
                  showlegend: false },
            ],
            makeHistLayout(nowTs)
        );

        Plotly.react(MSE_DIV,
            [{ x: xs, y: mseYs,
               type: "scatter", mode: "lines",
               line: { color: M3_WARN, width: 1.5, shape: "spline", smoothing: 0.3 },
               fill: "tozeroy", fillcolor: `${M3_WARN}22`,
               hovertemplate: "%{x|%H:%M:%S}<br><b>MSE=%{y:.6f}</b><extra></extra>" }],
            makeMseLayout()
        );

        const avgMse = mseYs.reduce((a, b) => a + b, 0) / mseYs.length;
        MSE_CURRENT.textContent = `avg=${avgMse.toFixed(4)}`;
    } catch (err) {
        console.warn("hist chart refresh failed", err);
    }
}

// ----- split-pane drag ------------------------------------------------
(function initSplitter() {
    const handle   = document.getElementById("split-handle");
    const container = document.getElementById("main-split");
    const readout  = document.getElementById("readout-pane");
    const monitor_width = window.screen.width;
    if (!handle || !container || !readout) return;

    const MIN_PX = monitor_width * 0.25;
    const MAX_PX = monitor_width * 0.5;
    const STORAGE_KEY = "readout-width";

    function applyWidth(px) {
        const clamped = Math.max(MIN_PX, Math.min(MAX_PX, px));
        readout.style.width = `${clamped}px`;
        readout.style.flex  = "none";
        return clamped;
    }

    const saved = parseInt(localStorage.getItem(STORAGE_KEY), 10);
    applyWidth(!isNaN(saved) ? saved : monitor_width * 0.25);

    let startX = 0;
    let startW = 0;

    function onMove(e) {
        const clientX = e.touches ? e.touches[0].clientX : e.clientX;
        const delta = startX - clientX;
        const next = applyWidth(startW + delta);
        localStorage.setItem(STORAGE_KEY, String(next));
        // Plotly.Plots.resize(PLOT_DIV); // [GRAPH-1 DISABLED]
        if (histChartsInitialized) {
            Plotly.Plots.resize(HIST_DIV);
            Plotly.Plots.resize(MSE_DIV);
        }
    }

    function onUp() {
        handle.classList.remove("dragging");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
    }

    handle.addEventListener("mousedown", (e) => {
        e.preventDefault();
        startX = e.clientX;
        startW = readout.offsetWidth;
        handle.classList.add("dragging");
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });

    handle.addEventListener("touchstart", (e) => {
        startX = e.touches[0].clientX;
        startW = readout.offsetWidth;
        handle.classList.add("dragging");
        document.addEventListener("touchmove", onMove, { passive: true });
        document.addEventListener("touchend", onUp);
    }, { passive: true });
})();

// ----- bootstrap ------------------------------------------------------
(async () => {
    await refreshHealth();
    await loadHistory();
    connectStream();
    refreshEnv();
    refreshHistCharts();
    setInterval(refreshHealth,     30000);
    setInterval(refreshEnv,        10000);
    setInterval(refreshHistCharts, 10000);
    setInterval(tickClock, 1000);
    tickClock();
})();
