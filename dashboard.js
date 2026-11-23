"use strict";

const API = {
  STREAM: "/api/stream",
  LATEST: "/api/latest",
  HISTORY: "/api/history",
  ALERTS: "/api/alerts",
  RELAY_SET: "/api/relay/set",
  RELAY_STATE: "/api/relay/state",
  HARDWARE_STATUS: "/api/hardware/status",
};

const MAX_CHART_POINTS = 60;
const ALERT_ITEM_HEIGHT = 38;
const MAX_RECONNECT_DELAY = 30000;

let chart = null;
let alertLog = [];
let lastTs = 0;
let sse = null;
let reconnectDelay = 1000;
let reconnectTimer = null;
let countdownTimer = null;
let pollingTimer = null;
let lastHardwareTelemetryTs = null;

const $ = (id) => document.getElementById(id);

const dom = {
  connDot: $("conn-dot"),
  connLabel: $("conn-label"),
  lastUpdate: $("last-update"),
  sourceTag: $("source-tag"),

  statusBanner: $("status-banner"),
  statusIcon: $("status-icon"),
  stageLabel: $("stage-label"),
  stageDesc: $("stage-desc"),
  confidenceVal: $("confidence-val"),

  tempValue: $("temp-value"),
  tempBar: $("temp-bar"),
  rainValue: $("rain-value"),
  rainBar: $("rain-bar"),
  waterValue: $("water-value"),
  waterBar: $("water-bar"),
  soilValue: $("soil-value"),
  soilBar: $("soil-bar"),

  led1: $("led1"),
  led2: $("led2"),
  led3: $("led3"),
  stage1Card: $("stage1-card"),
  stage2Card: $("stage2-card"),
  stage3Card: $("stage3-card"),
  stage1Badge: $("stage1-badge"),
  stage2Badge: $("stage2-badge"),
  stage3Badge: $("stage3-badge"),

  buzzerAlert: $("buzzer-alert"),
  alertList: $("alert-list"),

  tokenModal: $("token-modal"),
  tokenInput: $("token-input"),
  tokenSaveBtn: $("token-save-btn"),

  relay1Toggle: $("relay1-toggle"),
  relay2Toggle: $("relay2-toggle"),
  relay3Toggle: $("relay3-toggle"),
  relayApplyBtn: $("relay-apply-btn"),

  commandState: $("command-state"),
  reportedState: $("reported-state"),
  mismatchState: $("mismatch-state"),
  hardwareBadge: $("hardware-badge"),
  hardwarePing: $("hardware-ping"),
};

function getToken() {
  return localStorage.getItem("wms_token") || "";
}

function setToken(token) {
  localStorage.setItem("wms_token", token);
}

function showTokenModal() {
  dom.tokenModal.hidden = false;
  dom.tokenInput.value = getToken();
  dom.tokenInput.focus();
}

function hideTokenModal() {
  dom.tokenModal.hidden = true;
}

function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function fetchWithAuth(url, options = {}) {
  const headers = { ...(options.headers || {}), ...authHeaders() };
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401 && options.requireAuth) {
    showTokenModal();
    throw new Error("Unauthorized");
  }
  return res;
}

function registerServiceWorker() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  }
}

function initChart() {
  const ctx = document.getElementById("chartCanvas").getContext("2d");
  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "Temperature (°C)",
          data: [],
          borderColor: "#f0a500",
          backgroundColor: "rgba(240,165,0,0.08)",
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.35,
          yAxisID: "yTemp",
        },
        {
          label: "Rain Level",
          data: [],
          borderColor: "#00c8e0",
          backgroundColor: "rgba(0,200,224,0.08)",
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.35,
          yAxisID: "yRain",
        },
        {
          label: "Soil Moisture ADC",
          data: [],
          borderColor: "#9cf542",
          backgroundColor: "rgba(156,245,66,0.08)",
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.35,
          yAxisID: "ySoil",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 150 },
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#3d5a73" }, grid: { color: "#1c2a38" } },
        yTemp: { position: "left", ticks: { color: "#f0a500" }, grid: { color: "#1c2a38" } },
        yRain: { position: "right", ticks: { color: "#00c8e0" }, grid: { drawOnChartArea: false }, min: 0, max: 1023 },
        ySoil: { position: "right", ticks: { color: "#9cf542" }, grid: { drawOnChartArea: false }, min: 0, max: 4095 },
      },
    },
  });
}

function addChartPoint(payload) {
  if (!chart) return;

  const ts = new Date(payload.timestamp * 1000);
  chart.data.labels.push(ts.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
  chart.data.datasets[0].data.push(Number(payload.temperature || 0));
  chart.data.datasets[1].data.push(Number(payload.rain_level || 0));
  chart.data.datasets[2].data.push(Number(payload.soil_moisture || 0));

  if (chart.data.labels.length > MAX_CHART_POINTS) {
    chart.data.labels.shift();
    chart.data.datasets.forEach((ds) => ds.data.shift());
  }

  chart.update("none");
}

function setStage(n, active) {
  const card = dom[`stage${n}Card`];
  const badge = dom[`stage${n}Badge`];
  const badgeClasses = ["", "active-amber", "active-cyan", "active-red"];
  if (active) {
    card.classList.add("stage-active");
    badge.textContent = "ACTIVE";
    badge.className = `stage-badge ${badgeClasses[n]}`;
  } else {
    card.classList.remove("stage-active");
    badge.textContent = "INACTIVE";
    badge.className = "stage-badge inactive";
  }
}

function setLED(el, on, colorClass) {
  el.classList.toggle("led-on", Boolean(on));
  ["led-s1", "led-s2", "led-s3"].forEach((c) => el.classList.remove(c));
  el.classList.add(colorClass);
}

function updateSourceBadge(payload) {
  const now = Date.now() / 1000;
  if (payload.source === "esp32_hardware") {
    lastHardwareTelemetryTs = Number(payload.timestamp || now);
  }

  const stale = lastHardwareTelemetryTs !== null && now - lastHardwareTelemetryTs > 10;
  if (stale) {
    dom.hardwareBadge.textContent = "HARDWARE OFFLINE";
    dom.hardwareBadge.className = "mini-val hardware-offline";
    dom.sourceTag.textContent = "SIMULATION";
    return;
  }

  if (payload.source === "esp32_hardware") {
    dom.hardwareBadge.textContent = "ESP32 HARDWARE";
    dom.hardwareBadge.className = "mini-val hardware-online";
    dom.sourceTag.textContent = "ESP32 HARDWARE";
  } else {
    dom.hardwareBadge.textContent = "SIMULATION";
    dom.hardwareBadge.className = "mini-val hardware-sim";
    dom.sourceTag.textContent = "SIMULATION";
  }
}

function updateUI(p) {
  if (!p || !p.timestamp || p.timestamp === lastTs) return;
  lastTs = p.timestamp;

  dom.connDot.classList.add("live");
  dom.connLabel.textContent = "LIVE";
  dom.lastUpdate.textContent = new Date(p.timestamp * 1000).toLocaleTimeString();

  updateSourceBadge(p);

  const status = p.status || "NORMAL";
  const bannerCls = {
    NORMAL: "status-normal",
    WARNING: "status-warning",
    CRITICAL: "status-critical",
  };

  dom.statusBanner.className = `status-banner ${bannerCls[status] || "status-normal"}`;
  dom.stageLabel.textContent = status;
  dom.stageDesc.textContent = status === "CRITICAL"
    ? "Critical alert - immediate attention required"
    : status === "WARNING"
      ? "Elevated conditions detected - monitoring active"
      : "All conditions within normal parameters";
  dom.statusIcon.textContent = status === "CRITICAL" ? "!" : status === "WARNING" ? "*" : "+";
  dom.confidenceVal.textContent = `${((p.confidence || 0) * 100).toFixed(0)}%`;

  const temp = Number(p.temperature || 0);
  const rain = Number(p.rain_level || 0);
  const soil = Number(p.soil_moisture || 0);
  const water = Boolean(p.water_detected);

  dom.tempValue.textContent = temp.toFixed(1);
  dom.tempBar.style.width = `${Math.min(100, (temp / 55) * 100)}%`;

  dom.rainValue.textContent = String(rain);
  dom.rainBar.style.width = `${Math.min(100, (rain / 1023) * 100)}%`;

  dom.waterValue.textContent = water ? "DETECTED" : "CLEAR";
  dom.waterBar.style.width = water ? "100%" : "5%";

  dom.soilValue.textContent = String(soil);
  dom.soilBar.style.width = `${Math.min(100, (soil / 4095) * 100)}%`;
  dom.soilBar.style.background = soil < 2000 ? "var(--amber)" : "var(--green)";

  setStage(1, p.led1 || status === "WARNING");
  setStage(2, p.led2 || status === "WARNING");
  setStage(3, p.led3 || status === "CRITICAL");

  setLED(dom.led1, p.led1, "led-s1");
  setLED(dom.led2, p.led2, "led-s2");
  setLED(dom.led3, p.led3, "led-s3");

  dom.buzzerAlert.classList.toggle("visible", Boolean(p.buzzer));

  addChartPoint(p);

  if (status !== "NORMAL") {
    alertLog.unshift({
      time: new Date(p.timestamp * 1000).toLocaleTimeString(),
      status,
      temp: temp.toFixed(1),
      rain,
    });
    if (alertLog.length > 500) alertLog.pop();
    renderAlertListVirtualized();
  }
}

function renderAlertListVirtualized() {
  const list = dom.alertList;
  if (!alertLog.length) {
    list.innerHTML = "<div class='alert-empty'>NO ALERTS IN SESSION</div>";
    return;
  }

  const viewportHeight = list.clientHeight || 220;
  const visibleCount = Math.ceil(viewportHeight / ALERT_ITEM_HEIGHT) + 5;
  const scrollTop = list.scrollTop;
  const start = Math.floor(scrollTop / ALERT_ITEM_HEIGHT);
  const end = Math.min(alertLog.length, start + visibleCount);

  const topPad = start * ALERT_ITEM_HEIGHT;
  const bottomPad = Math.max(0, (alertLog.length - end) * ALERT_ITEM_HEIGHT);

  const rows = alertLog.slice(start, end).map((a) => `
    <div class="alert-item ${a.status}" style="height:${ALERT_ITEM_HEIGHT}px;">
      <span class="a-dot"></span>
      <span class="a-status">${a.status}</span>
      <span class="a-detail">${a.temp}C · Rain ${a.rain}</span>
      <span class="a-time">${a.time}</span>
    </div>
  `);

  list.innerHTML = `<div style="height:${topPad}px"></div>${rows.join("")}<div style="height:${bottomPad}px"></div>`;
}

function relayMismatchText(commanded, reported) {
  const mismatch = ["r1", "r2", "r3"].some((k) => Number(commanded[k] || 0) !== Number(reported[k] || 0));
  dom.mismatchState.textContent = mismatch ? "MISMATCH" : "SYNCED";
  dom.mismatchState.style.color = mismatch ? "var(--amber)" : "var(--green)";
}

async function refreshRelayState() {
  try {
    const res = await fetch(API.RELAY_STATE);
    const data = await res.json();

    const commanded = data.commanded || { r1: 0, r2: 0, r3: 0 };
    const reported = data.reported || { r1: 0, r2: 0, r3: 0 };

    dom.relay1Toggle.checked = Number(commanded.r1) === 1;
    dom.relay2Toggle.checked = Number(commanded.r2) === 1;
    dom.relay3Toggle.checked = Number(commanded.r3) === 1;

    dom.commandState.textContent = `r1=${commanded.r1},r2=${commanded.r2},r3=${commanded.r3}`;
    dom.reportedState.textContent = `r1=${reported.r1},r2=${reported.r2},r3=${reported.r3}`;
    relayMismatchText(commanded, reported);
  } catch (_) {
    dom.commandState.textContent = "unavailable";
    dom.reportedState.textContent = "unavailable";
  }
}

async function applyRelayCommands() {
  const payload = {
    r1: dom.relay1Toggle.checked ? 1 : 0,
    r2: dom.relay2Toggle.checked ? 1 : 0,
    r3: dom.relay3Toggle.checked ? 1 : 0,
    source: "dashboard",
  };

  try {
    await fetchWithAuth(API.RELAY_SET, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      requireAuth: true,
    });
    await refreshRelayState();
  } catch (_) {
    dom.mismatchState.textContent = "UPDATE FAILED";
    dom.mismatchState.style.color = "var(--red)";
  }
}

async function refreshHardwareStatus() {
  try {
    const res = await fetch(API.HARDWARE_STATUS);
    const data = await res.json();
    dom.hardwarePing.textContent = data.last_hardware_ping_ts
      ? new Date(data.last_hardware_ping_ts * 1000).toLocaleTimeString()
      : "never";

    if (data.last_hardware_telemetry_ts) {
      lastHardwareTelemetryTs = Number(data.last_hardware_telemetry_ts);
    }
  } catch (_) {
    dom.hardwarePing.textContent = "unavailable";
  }
}

async function fetchHistory() {
  const res = await fetch(API.HISTORY);
  const data = await res.json();
  if (Array.isArray(data.history)) {
    data.history.forEach((p) => addChartPoint(p));
  }
}

async function fetchAlerts() {
  const res = await fetch(API.ALERTS);
  const data = await res.json();
  if (Array.isArray(data.alerts)) {
    alertLog = data.alerts.slice().reverse().map((a) => ({
      time: new Date(a.time * 1000).toLocaleTimeString(),
      status: a.status,
      temp: Number(a.temperature || 0).toFixed(1),
      rain: Number(a.rain_level || 0),
    }));
    renderAlertListVirtualized();
  }
}

async function pollLatest() {
  try {
    const res = await fetch(API.LATEST);
    const data = await res.json();
    updateUI(data);
  } catch (_) {
    dom.connDot.classList.remove("live");
    dom.connLabel.textContent = "DISCONNECTED";
  }
}

function stopPolling() {
  if (pollingTimer) {
    clearInterval(pollingTimer);
    pollingTimer = null;
  }
}

function startPolling() {
  stopPolling();
  pollLatest();
  pollingTimer = setInterval(async () => {
    await pollLatest();
    await refreshRelayState();
    await refreshHardwareStatus();
  }, 2000);
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (countdownTimer) clearInterval(countdownTimer);

  let seconds = Math.ceil(reconnectDelay / 1000);
  dom.connLabel.textContent = `RETRY IN ${seconds}s`;

  countdownTimer = setInterval(() => {
    seconds -= 1;
    dom.connLabel.textContent = `RETRY IN ${Math.max(0, seconds)}s`;
  }, 1000);

  reconnectTimer = setTimeout(() => {
    if (countdownTimer) clearInterval(countdownTimer);
    connectSSE();
  }, reconnectDelay);

  reconnectDelay = Math.min(MAX_RECONNECT_DELAY, reconnectDelay * 2);
}

function connectSSE() {
  if (sse) {
    sse.close();
    sse = null;
  }

  stopPolling();
  dom.connDot.classList.remove("live");
  dom.connLabel.textContent = "CONNECTING";

  sse = new EventSource(API.STREAM);

  sse.onopen = () => {
    reconnectDelay = 1000;
    dom.connDot.classList.add("live");
    dom.connLabel.textContent = "LIVE";
  };

  sse.onmessage = async (event) => {
    try {
      updateUI(JSON.parse(event.data));
      await refreshRelayState();
      await refreshHardwareStatus();
    } catch (_) {}
  };

  sse.onerror = () => {
    dom.connDot.classList.remove("live");
    dom.connLabel.textContent = "DISCONNECTED";
    if (sse) {
      sse.close();
      sse = null;
    }
    if (reconnectDelay >= MAX_RECONNECT_DELAY) {
      startPolling();
    }
    scheduleReconnect();
  };
}

function wireTokenModal() {
  dom.tokenSaveBtn.addEventListener("click", async () => {
    const token = dom.tokenInput.value.trim();
    if (!token) return;
    setToken(token);
    hideTokenModal();
    connectSSE();
    await fetchHistory();
    await fetchAlerts();
    await refreshRelayState();
    await refreshHardwareStatus();
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  registerServiceWorker();
  initChart();
  wireTokenModal();

  dom.alertList.addEventListener("scroll", renderAlertListVirtualized);
  dom.relayApplyBtn.addEventListener("click", applyRelayCommands);

  try {
    await fetchHistory();
    await fetchAlerts();
    await refreshRelayState();
    await refreshHardwareStatus();
  } catch (_) {}

  connectSSE();
});
