"use strict";

// ----- DOM handles ----------------------------------------------------
const PLOT_DIV       = document.getElementById("probability-plot");
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
const PLOT_FONT = '"JetBrains Mono", ui-monospace, monospace';

function makeLayout() {
    const shapes = [
        { type: "line", xref: "paper", yref: "y", x0: 0, x1: 1, y0: 0.5, y1: 0.5,
          line: { color: "rgba(255, 181, 71, 0.18)", width: 1, dash: "dot" } },
    ];
    if (threshold !== null) {
        shapes.push({
            type: "line", xref: "paper", yref: "y",
            x0: 0, x1: 1, y0: threshold, y1: threshold,
            line: { color: "rgba(239, 68, 68, 0.85)", width: 1.2, dash: "dash" },
        });
    }
    return {
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        font: { color: "#8a8278", family: PLOT_FONT, size: 10 },
        margin: { l: 38, r: 12, t: 8, b: 26 },
        xaxis: {
            gridcolor: "rgba(255, 181, 71, 0.06)",
            zerolinecolor: "rgba(255, 181, 71, 0.06)",
            color: "#5b554c",
            tickfont: { size: 9 },
            showgrid: true,
            tickformat: "%H:%M",
        },
        yaxis: {
            gridcolor: "rgba(255, 181, 71, 0.06)",
            zerolinecolor: "rgba(255, 181, 71, 0.06)",
            color: "#5b554c",
            range: [0, 1],
            tickvals: [0, 0.25, 0.5, 0.75, 1.0],
            ticktext: ["0.00", "0.25", "0.50", "0.75", "1.00"],
            tickfont: { size: 9 },
        },
        showlegend: false,
        shapes,
    };
}

function makeInitialTrace() {
    return {
        x: [], y: [],
        type: "scatter",
        mode: "lines",
        line: { color: "#ffb547", width: 2.0, shape: "spline", smoothing: 0.4 },
        fill: "tozeroy",
        fillcolor: "rgba(255, 181, 71, 0.10)",
        hovertemplate: "%{x}<br><b>p EMA-5 = %{y:.4f}</b><extra></extra>",
    };
}

Plotly.newPlot(PLOT_DIV, [makeInitialTrace()], makeLayout(), { responsive: true, displayModeBar: false });

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
    } else {
        ERROR_BAND.hidden = true;
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
            MODEL_RUN.textContent = shortRunId(inf.model_run_id);
            MODEL_RUN.title = inf.model_run_id || "";
            DATA_SOURCE.textContent = inf.data_source || "—";
            if (inf.last_error) {
                showError(inf.last_error);
            } else if (!inf.last_event || !inf.last_event.error) {
                showError(null);
            }
        }
        if (payload.rtsp) {
            const r = payload.rtsp;
            RTSP_URL.textContent = r.url_safe || "—";
            OVERLAY_RTSP.textContent = shortHost(r.url_safe || "");
            RTSP_STATUS.classList.remove("pending", "connected", "disconnected");
            RTSP_STATUS.classList.add(r.connected ? "connected" : "disconnected");
            REC_STATUS.textContent = r.connected ? "REC" : "OFFLINE";
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
                Plotly.relayout(PLOT_DIV, makeLayout());
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
        const xs = valid.map(ev => ev.timestamp);
        const ys = smoothProbabilitySeries(valid.map(ev => ev.probability));
        Plotly.react(PLOT_DIV, [{ ...makeInitialTrace(), x: xs, y: ys }], makeLayout());
        totalTicks = valid.length;
        SAMPLE_COUNT.textContent = String(totalTicks);
        if (valid.length > 0) {
            const last = valid[valid.length - 1];
            const displayedProbability = ys[ys.length - 1];
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
    Plotly.extendTraces(PLOT_DIV, { x: [[event.timestamp]], y: [[displayedProbability]] }, [0], MAX_POINTS);
    if (timestampMs !== null) latestPlottedTimestampMs = timestampMs;
    setDigits(displayedProbability);
    setOccupiedFlag(visualOccupied(displayedProbability), displayedProbability);
    setGroundTruth(event);
    LATEST_TS.textContent = event.timestamp;
    REFERENCE_TS.textContent = event.reference_time || event.timestamp;
    if (event.model_run_id) {
        MODEL_RUN.textContent = shortRunId(event.model_run_id);
        MODEL_RUN.title = event.model_run_id;
    }
    if (event.source_label) DATA_SOURCE.textContent = event.source_label;
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
        `${pad2(now.getUTCHours())}:${pad2(now.getUTCMinutes())}:${pad2(now.getUTCSeconds())}Z`;
    LAST_TICK_REL.textContent = formatRelative(lastTickInstant);
}

// ----- bootstrap ------------------------------------------------------
(async () => {
    await refreshHealth();
    await loadHistory();
    connectStream();
    setInterval(refreshHealth, 30000);
    setInterval(tickClock, 1000);
    tickClock();
})();
