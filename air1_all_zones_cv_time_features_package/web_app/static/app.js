const MAX_POINTS = 240;
const ZONE_COUNT = 15;

const CAMERAS = {
    cam1: {
        label: "cam1",
        host: "10.158.71.241",
        zones: [4, 9, 10, 11, 12, 13, 14, 15],
        excluded: [16],
    },
    cam2: {
        label: "cam2",
        host: "10.158.71.240",
        zones: [1, 2, 3, 5, 6, 7, 8],
        excluded: [],
    },
};

const ZONE_TO_CAMERA = {};
Object.entries(CAMERAS).forEach(([cameraId, meta]) => {
    meta.zones.forEach((zoneId) => {
        ZONE_TO_CAMERA[zoneId] = cameraId;
    });
});

const el = (id) => document.getElementById(id);

const PLOT_DIV = el("probability-plot");
const MODEL_RUN = el("model-run-id");
const DATA_SOURCE = el("data-source");
const CLOCK = el("utc-clock");
const POINTER_PATH = el("pointer-path");
const SAMPLE_COUNT = el("sample-count");
const THRESHOLD_TAG = el("threshold-tag");
const THRESHOLD_VALUE = el("threshold-value");
const DIGITS = el("digits");
const AGGREGATE_PROB = el("aggregate-prob");
const ACTIVE_ZONE_COUNT = el("active-zone-count");
const OCCUPIED_FLAG = el("occupied-flag");
const GT_COUNT = el("ground-truth-count");
const GT_FLAG = el("ground-truth-flag");
const GT_META = el("ground-truth-meta");
const LABELED_ZONE_COUNT = el("labeled-zone-count");
const ZONE_GROUPS = el("zone-groups");
const ERROR_BAND = el("error-band");
const ERROR_TEXT = el("error-text");
const LATEST_TS = el("latest-ts");
const LAST_TICK = el("last-tick-rel");
const REFERENCE_TS = el("reference-ts");
const CFG_GAP = el("cfg-gap");
const CFG_AGE = el("cfg-age");
const TICKER = el("ticker");

const DETAIL = {
    zone: el("detail-zone"),
    camera: el("detail-camera"),
    probability: el("detail-probability"),
    state: el("detail-state"),
    cvCount: el("detail-cv-count"),
    cvState: el("detail-cv-state"),
    cvAge: el("detail-cv-age"),
    cvTs: el("detail-cv-ts"),
};

let threshold = null;
let latestEventTime = null;
let latestEvent = null;
let selectedZone = 1;
let plotReady = false;

function shortRunId(value) {
    if (!value) return "--";
    const text = String(value);
    return text.length > 14 ? `${text.slice(0, 8)}...${text.slice(-4)}` : text;
}

function numberOrNull(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

function formatProbability(value) {
    const parsed = numberOrNull(value);
    return parsed === null ? "--" : parsed.toFixed(4);
}

function formatShortProbability(value) {
    const parsed = numberOrNull(value);
    return parsed === null ? "--" : parsed.toFixed(3);
}

function formatTimestamp(value) {
    if (!value) return "--";
    return String(value).replace("T", " ");
}

function probabilityFromEvent(event) {
    const direct = numberOrNull(event.probability);
    if (direct !== null) return direct;
    const probs = Object.values(event.zone_probabilities || {}).map(numberOrNull).filter((v) => v !== null);
    return probs.length ? Math.max(...probs) : null;
}

function meanProbability(event) {
    const direct = numberOrNull(event.aggregate_probability);
    if (direct !== null) return direct;
    const probs = Object.values(event.zone_probabilities || {}).map(numberOrNull).filter((v) => v !== null);
    if (!probs.length) return null;
    return probs.reduce((sum, value) => sum + value, 0) / probs.length;
}

function zoneProbability(event, zoneId) {
    if (!event || !event.zone_probabilities) return null;
    return numberOrNull(event.zone_probabilities[String(zoneId)] ?? event.zone_probabilities[zoneId]);
}

function zoneGroundTruth(event, zoneId) {
    const labels = event?.ground_truth_by_zone || {};
    return labels[String(zoneId)] || labels[zoneId] || {};
}

function hasZoneGroundTruth(label) {
    return Boolean(
        label
        && (numberOrNull(label.count) !== null || label.occupied !== null || label.timestamp)
    );
}

function zoneState(probability, occupiedZones, zoneId) {
    if (probability === null) return "missing";
    if (threshold !== null) {
        return new Set((occupiedZones || []).map((value) => String(value))).has(String(zoneId))
            ? "active"
            : "nominal";
    }
    return probability >= 0.5 ? "active" : "nominal";
}

function setError(message) {
    if (!message) {
        ERROR_BAND.hidden = true;
        ERROR_TEXT.textContent = "";
        return;
    }
    ERROR_TEXT.textContent = message;
    ERROR_BAND.hidden = false;
}

function pushTicker(text) {
    TICKER.textContent = text;
}

function setClock() {
    CLOCK.textContent = new Date().toLocaleTimeString([], { hour12: false });
}
setClock();
setInterval(setClock, 1000);

function buildZoneGroups() {
    ZONE_GROUPS.innerHTML = "";
    Object.entries(CAMERAS).forEach(([cameraId, meta]) => {
        const group = document.createElement("section");
        group.className = "zone-group";
        group.dataset.camera = cameraId;

        const head = document.createElement("div");
        head.className = "zone-group__head";
        head.innerHTML = `
            <span>${meta.label}</span>
            <b>${meta.host}</b>
            <i>tables ${meta.zones.join(", ")}${meta.excluded.length ? `; excluded ${meta.excluded.join(", ")}` : ""}</i>
        `;
        group.appendChild(head);

        const list = document.createElement("div");
        list.className = "zone-list";
        meta.zones.forEach((zoneId) => {
            const row = document.createElement("button");
            row.type = "button";
            row.className = "zone-row";
            row.dataset.zone = String(zoneId);
            row.dataset.camera = cameraId;
            row.dataset.state = "missing";
            row.innerHTML = `
                <span class="zone-row__id">Z${String(zoneId).padStart(2, "0")}</span>
                <span class="zone-row__bar"><i></i></span>
                <span class="zone-row__value">--</span>
                <span class="zone-row__cv">CV --</span>
            `;
            row.addEventListener("click", () => selectZone(zoneId));
            list.appendChild(row);
        });
        group.appendChild(list);
        ZONE_GROUPS.appendChild(group);
    });
}

function selectZone(zoneId) {
    selectedZone = Number(zoneId);
    document.querySelectorAll(".zone-row").forEach((row) => {
        row.classList.toggle("is-selected", Number(row.dataset.zone) === selectedZone);
    });
    updateZoneDetail();
}

function updateZoneGrid(event = latestEvent) {
    if (!event) return;
    const occupiedZones = event.occupied_zones || [];
    let labeledCount = 0;
    for (let zoneId = 1; zoneId <= ZONE_COUNT; zoneId += 1) {
        const row = ZONE_GROUPS.querySelector(`[data-zone="${zoneId}"]`);
        if (!row) continue;
        const probability = zoneProbability(event, zoneId);
        const label = zoneGroundTruth(event, zoneId);
        const width = probability === null ? 0 : Math.max(0, Math.min(100, probability * 100));
        const hasLabel = hasZoneGroundTruth(label);
        if (hasLabel) labeledCount += 1;

        row.querySelector(".zone-row__bar i").style.width = `${width}%`;
        row.querySelector(".zone-row__value").textContent = formatShortProbability(probability);
        row.querySelector(".zone-row__cv").textContent = cvLabelText(label);
        row.dataset.state = zoneState(probability, occupiedZones, zoneId);
        row.dataset.cv = hasLabel ? "labeled" : "missing";
    }
    LABELED_ZONE_COUNT.textContent = `${labeledCount} / ${ZONE_COUNT}`;
    updateZoneDetail();
}

function cvLabelText(label) {
    if (!hasZoneGroundTruth(label)) return "CV --";
    const count = numberOrNull(label.count);
    if (label.occupied === true) return count === null ? "CV occ" : `CV ${count.toFixed(0)} occ`;
    if (label.occupied === false) return count === null ? "CV clear" : `CV ${count.toFixed(0)} clear`;
    return count === null ? "CV label" : `CV ${count.toFixed(0)}`;
}

function setFlag(probability, occupied) {
    if (occupied === true) {
        OCCUPIED_FLAG.dataset.state = "occupied";
        OCCUPIED_FLAG.textContent = "occupied";
    } else if (occupied === false) {
        OCCUPIED_FLAG.dataset.state = "vacant";
        OCCUPIED_FLAG.textContent = "clear";
    } else if (probability !== null) {
        OCCUPIED_FLAG.dataset.state = probability >= 0.5 ? "elevated" : "idle";
        OCCUPIED_FLAG.textContent = probability >= 0.5 ? "elevated" : "idle";
    } else {
        OCCUPIED_FLAG.dataset.state = "idle";
        OCCUPIED_FLAG.textContent = "idle";
    }
}

function setGroundTruth(event) {
    const count = numberOrNull(event.ground_truth_count);
    const hasLabel = count !== null && event.ground_truth_timestamp;
    GT_COUNT.textContent = hasLabel ? count.toFixed(0) : "--";
    if (event.ground_truth_occupied === true) {
        GT_FLAG.dataset.state = "occupied";
        GT_FLAG.textContent = "occupied";
    } else if (event.ground_truth_occupied === false) {
        GT_FLAG.dataset.state = "vacant";
        GT_FLAG.textContent = "clear";
    } else {
        GT_FLAG.dataset.state = "idle";
        GT_FLAG.textContent = "no label";
    }
    const age = numberOrNull(event.ground_truth_age_minutes);
    const stamp = event.ground_truth_timestamp || "--";
    GT_META.textContent = age === null ? formatTimestamp(stamp) : `age ${age.toFixed(1)} min`;
}

function updateZoneDetail() {
    const event = latestEvent || {};
    const cameraId = ZONE_TO_CAMERA[selectedZone] || "--";
    const camera = CAMERAS[cameraId] || {};
    const probability = zoneProbability(event, selectedZone);
    const label = zoneGroundTruth(event, selectedZone);
    const state = zoneState(probability, event.occupied_zones || [], selectedZone);
    const count = numberOrNull(label.count);
    const age = numberOrNull(label.age_minutes);

    DETAIL.zone.textContent = `Z${String(selectedZone).padStart(2, "0")}`;
    DETAIL.camera.textContent = cameraId === "--" ? "--" : `${cameraId} (${camera.host})`;
    DETAIL.probability.textContent = formatProbability(probability);
    DETAIL.state.textContent = state === "active" ? "above threshold" : state === "nominal" ? "below threshold" : "no probability";
    DETAIL.cvCount.textContent = count === null ? "--" : count.toFixed(0);
    DETAIL.cvState.textContent = label.occupied === true ? "occupied" : label.occupied === false ? "clear" : "no label";
    DETAIL.cvAge.textContent = age === null ? "--" : `${age.toFixed(1)} min`;
    DETAIL.cvTs.textContent = formatTimestamp(label.timestamp);
}

function updateRelativeTick() {
    if (!latestEventTime) {
        LAST_TICK.textContent = "--";
        return;
    }
    const elapsed = Math.max(0, (Date.now() - latestEventTime.getTime()) / 1000);
    LAST_TICK.textContent = elapsed < 60 ? `${elapsed.toFixed(0)}s` : `${(elapsed / 60).toFixed(1)}m`;
}
setInterval(updateRelativeTick, 1000);

function renderEvent(event, { appendPlot = true } = {}) {
    if (!event) return;
    latestEvent = event;
    if (event.error) {
        setError(event.error);
        pushTicker(`error: ${event.error}`);
        updateZoneGrid(event);
        return;
    }
    setError(null);
    const probability = probabilityFromEvent(event);
    const mean = meanProbability(event);
    if (probability === null) return;

    DIGITS.textContent = probability.toFixed(4);
    AGGREGATE_PROB.textContent = mean === null ? "--" : mean.toFixed(4);
    const occupiedZones = event.occupied_zones || [];
    ACTIVE_ZONE_COUNT.textContent = `${occupiedZones.length} / ${event.zone_count || ZONE_COUNT}`;
    setFlag(probability, event.occupied);
    setGroundTruth(event);
    updateZoneGrid(event);

    LATEST_TS.textContent = formatTimestamp(event.timestamp);
    REFERENCE_TS.textContent = formatTimestamp(event.reference_time || event.timestamp);
    if (event.model_run_id) {
        MODEL_RUN.textContent = shortRunId(event.model_run_id);
        MODEL_RUN.title = event.model_run_id;
    }
    if (event.source_label) DATA_SOURCE.textContent = event.source_label;
    latestEventTime = event.timestamp ? new Date(event.timestamp) : new Date();
    updateRelativeTick();

    if (appendPlot && plotReady) {
        Plotly.extendTraces(
            PLOT_DIV,
            {
                x: [[event.timestamp], [event.timestamp], [event.timestamp]],
                y: [[probability], [mean ?? probability], [occupiedZones.length]],
            },
            [0, 1, 2],
            MAX_POINTS,
        );
        SAMPLE_COUNT.textContent = String(Number(SAMPLE_COUNT.textContent || "0") + 1);
    }
    pushTicker(`tick ${String(event.timestamp || "").split("T")[1] || event.timestamp} - max=${probability.toFixed(3)} mean=${(mean ?? probability).toFixed(3)} zones=${occupiedZones.length}`);
}

function initPlot(events = []) {
    const clean = events.filter((event) => !event.error && probabilityFromEvent(event) !== null);
    SAMPLE_COUNT.textContent = String(clean.length);
    if (!window.Plotly) {
        if (clean.length) renderEvent(clean[clean.length - 1], { appendPlot: false });
        return;
    }
    const x = clean.map((event) => event.timestamp);
    const yMax = clean.map((event) => probabilityFromEvent(event));
    const yMean = clean.map((event) => meanProbability(event) ?? probabilityFromEvent(event));
    const yZones = clean.map((event) => (event.occupied_zones || []).length);
    Plotly.newPlot(PLOT_DIV, [
        {
            x,
            y: yMax,
            name: "max probability",
            type: "scatter",
            mode: "lines",
            line: { color: "#bc3f31", width: 2 },
        },
        {
            x,
            y: yMean,
            name: "mean probability",
            type: "scatter",
            mode: "lines",
            line: { color: "#27745f", width: 2 },
        },
        {
            x,
            y: yZones,
            name: "zones above threshold",
            type: "scatter",
            mode: "lines",
            yaxis: "y2",
            line: { color: "#315f9f", width: 2, dash: "dot" },
        },
    ], {
        margin: { l: 42, r: 42, t: 10, b: 32 },
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        font: { color: "#20242a", family: "Consolas, 'Courier New', monospace", size: 11 },
        xaxis: { gridcolor: "rgba(32,36,42,0.10)", zeroline: false },
        yaxis: { range: [0, 1], gridcolor: "rgba(32,36,42,0.10)", zeroline: false },
        yaxis2: { range: [0, ZONE_COUNT], overlaying: "y", side: "right", zeroline: false, gridcolor: "rgba(0,0,0,0)" },
        legend: { orientation: "h", x: 0, y: 1.15 },
    }, {
        displayModeBar: false,
        responsive: true,
    });
    plotReady = true;
    if (clean.length) renderEvent(clean[clean.length - 1], { appendPlot: false });
}

async function loadHistory() {
    try {
        const response = await fetch(`/api/history?n=${MAX_POINTS}`);
        if (!response.ok) return initPlot([]);
        const events = await response.json();
        initPlot(Array.isArray(events) ? events : []);
    } catch (err) {
        console.warn("history load failed", err);
        initPlot([]);
    }
}

function setCameraHealth(cameraId, status = {}) {
    const dot = el(`${cameraId}-status`);
    const url = el(`${cameraId}-url`);
    const age = el(`${cameraId}-frame-age`);
    const stream = el(`${cameraId}-stream-state`);
    const connected = Boolean(status.connected);
    if (dot) dot.className = `status-dot ${connected ? "connected" : "disconnected"}`;
    if (url) {
        url.textContent = status.url_safe || status.host || CAMERAS[cameraId]?.host || "--";
        url.title = url.textContent;
    }
    const frameAge = numberOrNull(status.latest_frame_age_sec);
    if (age) age.textContent = frameAge === null ? "--" : `${frameAge.toFixed(1)}s`;
    if (stream) stream.textContent = connected ? "YOLO stream live" : "YOLO stream offline";
}

async function loadHealth() {
    try {
        const response = await fetch("/api/health");
        if (!response.ok) return;
        const payload = await response.json();
        const inf = payload.inference || {};
        const cfg = payload.config || {};
        const rtspByCamera = payload.rtsp_by_camera || { cam1: payload.rtsp || {} };
        MODEL_RUN.textContent = shortRunId(inf.model_run_id);
        MODEL_RUN.title = inf.model_run_id || "";
        DATA_SOURCE.textContent = inf.data_source || "--";
        POINTER_PATH.textContent = cfg.production_pointer || "production_run.txt";
        threshold = numberOrNull(cfg.threshold);
        if (threshold !== null) {
            THRESHOLD_TAG.hidden = false;
            THRESHOLD_VALUE.textContent = threshold.toFixed(2);
        } else {
            THRESHOLD_TAG.hidden = true;
        }
        CFG_GAP.textContent = cfg.max_gap_minutes == null ? "--" : `${cfg.max_gap_minutes}m`;
        CFG_AGE.textContent = cfg.max_age_minutes == null ? "off" : `${cfg.max_age_minutes}m`;
        Object.keys(CAMERAS).forEach((cameraId) => setCameraHealth(cameraId, rtspByCamera[cameraId] || {}));
        if (inf.last_error) {
            setError(inf.last_error);
        } else if (!inf.last_event || !inf.last_event.error) {
            setError(null);
        }
    } catch (err) {
        setError(`health check failed: ${err}`);
    }
}

function connectStream() {
    const source = new EventSource("/api/stream");
    source.addEventListener("tick", (message) => {
        try {
            renderEvent(JSON.parse(message.data));
        } catch (err) {
            setError(`bad stream payload: ${err}`);
        }
    });
    source.onerror = () => {
        pushTicker("stream reconnecting");
    };
}

buildZoneGroups();
selectZone(1);
loadHistory();
loadHealth();
setInterval(loadHealth, 5000);
connectStream();
