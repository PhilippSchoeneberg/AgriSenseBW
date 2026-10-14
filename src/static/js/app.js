/* AgriSense BW dashboard: tabs, alerts table, livestock map and crop chart. */
"use strict";

// ------------------------------------------------------------- language
// All texts come from data/translations.csv; the server puts them, already in the
// page's language, into <script id="i18n-data"> (see base.html and src/i18n.py).
const I18N = JSON.parse(document.getElementById("i18n-data").textContent);
const LANG = document.documentElement.lang || "en";

// t("alerts.count_many", { n: 5 }) -> "5 alerts, most urgent first." (or the Setswana)
function t(key, values = {}) {
  const text = I18N[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (match, name) => (name in values ? values[name] : match));
}

// An alert's text in the page's language: reason_tn in Setswana, falling back to English.
const loc = (alert, field) => (LANG !== "en" && alert[`${field}_${LANG}`]) || alert[field];

// The language switch in the top bar remembers the choice in a cookie and reloads.
for (const button of document.querySelectorAll("[data-lang]")) {
  button.addEventListener("click", () => {
    document.cookie = `lang=${button.dataset.lang}; path=/; max-age=31536000; samesite=lax`;
    const url = new URL(location.href);
    if (url.searchParams.has("lang")) {   // ?lang= would override the cookie
      url.searchParams.delete("lang");
      location.replace(url);
    } else {
      location.reload();
    }
  });
}

const FEVER_C = 39.5;
// "Sorghum Alert" brand colours: primary ink, accent blue, sorghum grain brown.
const COLORS = {
  ink: "#1D2B36", ink2: "#3A4651", grid: "#DCE2E7", high: "#D03B3B",
  muted: "#AEB9C3",
  risk: { High: "#D03B3B", Medium: "#FAB219", Low: "#0CA30C" },  // same as the risk badges
  series: ["#3A86C8", "#A8641F"],  // the animal or site in focus, and its comparison
};

Chart.defaults.font.family = '"Space Grotesk", system-ui, -apple-system, "Segoe UI", sans-serif';
Chart.defaults.font.size = 15;
Chart.defaults.color = COLORS.ink2;

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (text) => String(text ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

const shortDate = (iso) => `${Number(iso.slice(8, 10))} ${t(`month.${Number(iso.slice(5, 7))}`)}`;
const fmtDate = (iso) => `${shortDate(iso)} ${iso.slice(0, 4)}`;

// The coloured risk label; `label` replaces the risk word (used for the collar states).
function badge(risk, label = t(`risk.${risk}`)) {
  return `<span class="badge risk-${esc(risk)}">${esc(label)}</span>`;
}

// ------------------------------------------------------------------ tabs
const tabs = [...document.querySelectorAll('[role="tab"]')];
const onShow = { livestock: () => initLivestock(), crops: () => initCrops(), sensors: () => startSensors() };

function selectTab(tab) {
  for (const t of tabs) {
    const selected = t === tab;
    t.setAttribute("aria-selected", selected);
    t.tabIndex = selected ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !selected;
  }
  const name = tab.id.replace("tab-", "");
  try { localStorage.setItem("agrisense-tab", name); } catch { /* storage unavailable */ }
  if (name !== "sensors") stopSensors();
  onShow[name]?.();
}

tabs.forEach((tab, i) => {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (!step) return;
    const next = tabs[(i + step + tabs.length) % tabs.length];
    next.focus();
    selectTab(next);
  });
});

// ---------------------------------------------------------------- alerts
const filters = $("#alert-filters");
let alertRows = [];

function advice(alert) {
  if (LANG === "tn") {
    return alert.advice_tn
      ? esc(alert.advice_tn)
      : `<span class="pending">${esc(t("advice.pending"))}</span> ${esc(alert.advice_en)}`;
  }
  return esc(alert.advice_en);
}

function when(alert) {
  if (alert.type === "crop") return `${esc(t("alerts.season_ending"))}<br>${fmtDate(alert.end_date)}`;
  if (alert.start_date === alert.end_date) return fmtDate(alert.start_date);
  return `${fmtDate(alert.start_date)} –<br>${fmtDate(alert.end_date)}<br><small>${esc(t("alerts.days", { n: alert.days }))}</small>`;
}

function renderAlerts() {
  const body = $("#alert-rows");
  body.innerHTML = alertRows.map((a) => `
    <tr class="${a.for_whom !== "Farmers" ? "vets" : ""} ${a.status === "resolved" ? "done" : ""}">
      <td>${badge(a.risk_level)}</td>
      <td>
        <span class="subject">${esc(loc(a, "subject"))} · ${esc(t(`cat.${a.category}`))}</span>
        ${a.for_whom !== "Farmers" ? `<span class="tag">${esc(t("alerts.for", { who: t(`for_whom.${a.for_whom}`) }))}</span> ` : ""}
        ${esc(loc(a, "reason"))}
        <details><summary>${esc(t("alerts.details"))}</summary>${esc(loc(a, "details"))}<br><small>ID ${esc(a.alert_id)}</small></details>
      </td>
      <td>${advice(a)}</td>
      <td>${when(a)}</td>
      <td>
        <select class="status-select" id="status-${esc(a.alert_id)}" data-id="${esc(a.alert_id)}"
                aria-label="${esc(t("alerts.status_of", { subject: loc(a, "subject") }))}">
          ${["open", "acknowledged", "resolved"].map((s) =>
            `<option value="${s}"${s === a.status ? " selected" : ""}>${esc(t(`status.${s}`))}</option>`).join("")}
        </select>
      </td>
    </tr>`).join("");
  const n = alertRows.length;
  $("#alert-count").textContent = n === 0 ? t("alerts.none")
    : n === 1 ? t("alerts.count_one") : t("alerts.count_many", { n });
}

async function loadAlerts() {
  const params = new URLSearchParams();
  for (const name of ["type", "risk", "status"]) {
    const value = filters.elements[name].value;
    if (value) params.set(name, value);
  }
  alertRows = await api(`/api/alerts?${params}`);
  renderAlerts();
}

async function refreshTiles() {
  const summary = await api("/api/summary");
  for (const [risk, n] of Object.entries(summary.open_by_risk)) {
    const tile = $(`[data-open="${risk}"]`);
    if (tile) tile.textContent = n;
  }
}

filters.addEventListener("change", loadAlerts);

$("#alert-rows").addEventListener("change", async (e) => {
  const select = e.target.closest(".status-select");
  if (!select) return;
  select.disabled = true;
  try {
    await api(`/api/alerts/${encodeURIComponent(select.dataset.id)}/status`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: select.value }),
    });
    await Promise.all([loadAlerts(), refreshTiles()]);
  } catch (err) {
    alert(t("alerts.save_failed", { error: err.message }));
    select.disabled = false;
  }
});

// ------------------------------------------------------------- livestock
let map, markerLayer, trackLayer, tempChart, activityChart, selectedAnimal;
let positions = { date: null, animals: [] };
const daySelect = $("#day");
const shownRisks = new Set(["High", "Medium", "Low"]);
const tracks = new Map();  // animal_id -> daily readings, for drawing paths
const markers = new Map(); // animal_id -> { marker, animal } currently on the map
const FOCUS_ZOOM = 15;     // zoom level when an animal is chosen
let focused = false;       // true while the map is zoomed in on the chosen animal

// An animal's risk, as for the crop sites: its most serious alert so far, or Low
// when it has none.
const riskOf = (a) => (a.alerts.length ? a.risk_level : "Low");

// Map control: zoom back out so that every animal is in view.
const ShowAllControl = L.Control.extend({
  options: { position: "topright" },
  onAdd() {
    const box = L.DomUtil.create("div", "leaflet-bar show-all");
    box.innerHTML = `<button type="button">${esc(t("livestock.show_all"))}</button>`;
    L.DomEvent.disableClickPropagation(box);
    box.querySelector("button").addEventListener("click", showAllAnimals);
    return box;
  },
});

function allAnimalsBounds() {
  return L.latLngBounds(positions.animals.map((a) => [a.lat, a.lon]));
}

const NO_ANIMAL = `<p class="hint">${esc(t("livestock.choose"))}</p>`;

// "Show all animals": zoom out and deselect, back to the start view.
function showAllAnimals() {
  focused = false;
  clearAnimal();
  flying();
  map.flyToBounds(allAnimalsBounds(), { padding: [40, 40], duration: 0.6 });
}

// During a flight Leaflet stretches the path layer and only redraws it at the end,
// so the straying path is faded out until the map has arrived.
function flying() {
  map.getContainer().classList.add("flying");
  map.once("moveend", () => map.getContainer().classList.remove("flying"));
}

// Dots grow a little as you zoom in and shrink as you zoom out, smoothly, every frame.
function dotScale(zoom) {
  return Math.min(1.7, Math.max(0.85, 1 + (zoom - 11) * 0.12)).toFixed(3);
}
function scaleDots(zoom) {
  map.getContainer().style.setProperty("--dot-scale", dotScale(zoom));
}

function clearAnimal() {
  selectedAnimal = null;
  tempChart?.destroy();
  activityChart?.destroy();
  tempChart = activityChart = null;
  $("#animal-detail").innerHTML = NO_ANIMAL;
  markSelection();
}

// One meaning per dot: where the animal was on the chosen day, coloured by its risk.
// The dots are HTML markers (styled in style.css), so they keep their size during the
// zoom animation instead of being stretched with the map. High is drawn on top.
const DOT_LAYER = { Low: 0, Medium: 100, High: 200 };
function dotIcon(risk) {
  return L.divIcon({ className: `dot-marker dot-${risk}`, html: "<span></span>",
                     iconSize: [24, 24], tooltipAnchor: [12, 0] });
}

// Show the current selection on the existing dots and list buttons. Markers are not
// rebuilt here: this runs inside their click handlers.
function markSelection() {
  for (const { marker, animal } of markers.values()) {
    const chosen = animal.animal_id === selectedAnimal;
    marker.getElement()?.classList.toggle("chosen", chosen);
    marker.setZIndexOffset(chosen ? 1000 : DOT_LAYER[riskOf(animal)]);
  }
  for (const chip of document.querySelectorAll("[data-animal]")) {
    chip.setAttribute("aria-pressed", chip.dataset.animal === selectedAnimal);
  }
}

// Clicking the chosen animal again deselects it, like the "Show all animals" button.
function toggleAnimal(id) {
  if (id === selectedAnimal) showAllAnimals(); else showAnimal(id);
}

function focusAnimal(id) {
  const a = positions.animals.find((x) => x.animal_id === id);
  if (!map || !a) return;
  focused = true;
  flying();
  map.flyTo([a.lat, a.lon], Math.max(map.getZoom(), FOCUS_ZOOM), { duration: 0.8 });
}

function initLivestock() {
  if (map) { map.invalidateSize(); return; }
  map = L.map("map", { scrollWheelZoom: true });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  trackLayer = L.layerGroup().addTo(map);
  markerLayer = L.layerGroup().addTo(map);
  new ShowAllControl().addTo(map);
  map.on("zoom", () => scaleDots(map.getZoom()));   // every frame of a flight
  map.on("zoomanim", (e) => scaleDots(e.zoom));     // mouse-wheel and +/- zoom
  daySelect.addEventListener("change", () => loadPositions(false));
  loadPositions(true);
}

async function loadPositions(fit) {
  positions = await api(`/api/livestock/positions?date=${daySelect.value}`);
  const { animals } = positions;
  if (fit) map.fitBounds(allAnimalsBounds(), { padding: [40, 40] });

  // Paths are drawn for animals that are possibly lost or stolen.
  for (const a of animals.filter((x) => x.alerts.some((id) => id.endsWith("-STRAY")))) {
    if (!tracks.has(a.animal_id)) {
      tracks.set(a.animal_id, (await api(`/api/livestock/${encodeURIComponent(a.animal_id)}`)).days);
    }
  }
  for (const risk of ["High", "Medium", "Low"]) {
    $(`[data-count="${risk}"]`).textContent = `(${animals.filter((a) => riskOf(a) === risk).length})`;
  }
  renderMap();
  // On another day, follow the chosen animal if the map is zoomed in on it.
  const chosen = animals.find((a) => a.animal_id === selectedAnimal);
  if (focused && chosen) map.panTo([chosen.lat, chosen.lon]);
}

function renderMap() {
  const { date, animals } = positions;
  const visible = animals.filter((a) => shownRisks.has(riskOf(a)));
  markerLayer.clearLayers();
  trackLayer.clearLayers();

  for (const a of visible.filter((x) => tracks.has(x.animal_id))) {
    const path = tracks.get(a.animal_id).filter((d) => d.date <= date).map((d) => [d.lat, d.lon]);
    L.polyline(path, { color: COLORS.high, weight: 3, dashArray: "8 6" }).addTo(trackLayer);
  }

  // Low first, so High animals are drawn on top; the chosen animal last of all.
  markers.clear();
  for (const a of visible) {
    const risk = riskOf(a);
    const title = t("livestock.dot_title", { animal: a.animal_id, herd: a.herd_id, risk: t(`risk.${risk}`) });
    const marker = L.marker([a.lat, a.lon], { icon: dotIcon(risk), title,
                                              zIndexOffset: DOT_LAYER[risk] })
      .on("click", () => toggleAnimal(a.animal_id));
    if (risk === "High") {
      // interactive: a click on the name label selects the animal, like a click on its dot
      marker.bindTooltip(a.animal_id, { permanent: true, interactive: true, direction: "right", offset: [6, 0], className: "map-label" });
      // Leaflet closes a permanent label when it is clicked; keep it on screen.
      marker.on("tooltipclose", () => setTimeout(() => { if (map.hasLayer(marker)) marker.openTooltip(); }, 0));
    }
    marker.addTo(markerLayer);
    if (marker.getTooltip()?.options.permanent) marker.openTooltip();
    markers.set(a.animal_id, { marker, animal: a });
  }
  scaleDots(map.getZoom());
  renderChips(visible);
  markSelection();
}

$(".risk-filter").addEventListener("change", () => {
  shownRisks.clear();
  for (const box of document.querySelectorAll('input[name="show-risk"]:checked')) shownRisks.add(box.value);
  renderMap();
});

function renderChips(animals) {
  const groups = ["High", "Medium", "Low"].map((risk) => ({
    risk, animals: animals.filter((a) => riskOf(a) === risk).sort((x, y) => x.animal_id.localeCompare(y.animal_id)),
  })).filter((g) => g.animals.length);
  $("#flagged-list").innerHTML = groups.length ? groups.map((g) => `
    <h3 class="chip-group">${badge(g.risk)} <span>${esc(g.animals.length === 1 ? t("livestock.animals_one")
      : t("livestock.animals_many", { n: g.animals.length }))}</span></h3>
    <div class="chips">${g.animals.map((a) => `
      <button type="button" class="chip" data-animal="${esc(a.animal_id)}"
              aria-pressed="${a.animal_id === selectedAnimal}">
        <span class="dot ${esc(g.risk)}" aria-hidden="true"></span>${esc(a.animal_id)}
        <span class="visually-hidden">(${esc(t(`risk.${g.risk}`))})</span>
      </button>`).join("")}</div>`).join("")
    : `<p class="hint">${esc(t("livestock.none_shown"))}</p>`;
}

$("#flagged-list").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-animal]");
  if (chip) toggleAnimal(chip.dataset.animal);
});

function lineChart(canvas, datasets, yTitle, extra = {}) {
  return new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { type: "category", grid: { display: false }, ticks: { maxTicksLimit: 6, maxRotation: 0 } },
        y: { title: { display: true, text: yTitle }, grid: { color: COLORS.grid }, ...extra.y },
      },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 24, boxHeight: 3 } } },
      elements: { point: { radius: 0, hoverRadius: 5 }, line: { borderWidth: 2, borderCapStyle: "round" } },
    },
  });
}

async function showAnimal(id) {
  selectedAnimal = id;
  markSelection();
  focusAnimal(id);
  const a = await api(`/api/livestock/${encodeURIComponent(id)}`);
  if (selectedAnimal !== id) return;  // deselected or another animal chosen meanwhile
  const today = a.days.find((d) => d.date === daySelect.value) ?? a.days.at(-1);
  $("#animal-detail").innerHTML = `
    <h2>${esc(a.animal_id)} <small>${esc(t("livestock.herd", { herd: a.herd_id }))}</small></h2>
    <div class="animal-facts">
      <div><span>${esc(t("livestock.body_temp_on", { date: fmtDate(today.date) }))}</span><strong>${today.body_temp_c.toFixed(1)} °C</strong></div>
      <div><span>${esc(t("livestock.activity_vs_herd"))}</span><strong>${today.activity_level.toFixed(0)}</strong> (${today.herd_activity.toFixed(0)})</div>
    </div>
    <p class="chart-title">${esc(t("livestock.chart_temp"))}</p>
    <div class="small-chart"><canvas id="temp-chart" aria-label="${esc(t("livestock.chart_temp_aria"))}"></canvas></div>
    <p class="chart-title">${esc(t("livestock.chart_activity"))}</p>
    <div class="small-chart"><canvas id="activity-chart" aria-label="${esc(t("livestock.chart_activity_aria"))}"></canvas></div>
    <ul class="animal-alerts">${a.alerts.map((al) =>
      `<li>${badge(al.risk_level)} ${esc(loc(al, "reason"))}</li>`).join("") || `<li>${badge("Low")} ${esc(t("livestock.no_alerts"))}</li>`}</ul>`;

  tempChart?.destroy();
  activityChart?.destroy();
  const labels = a.days.map((d) => shortDate(d.date));
  tempChart = lineChart($("#temp-chart"), [
    { label: a.animal_id, data: a.days.map((d, i) => ({ x: labels[i], y: d.body_temp_c })),
      borderColor: COLORS.series[0], backgroundColor: COLORS.series[0] },
    { label: t("livestock.fever_line", { fever: FEVER_C }), data: labels.map((x) => ({ x, y: FEVER_C })),
      borderColor: COLORS.high, backgroundColor: COLORS.high, borderDash: [6, 4], borderWidth: 2 },
  ], "°C", { y: { suggestedMin: 37.5, suggestedMax: 41 } });
  activityChart = lineChart($("#activity-chart"), [
    { label: a.animal_id, data: a.days.map((d, i) => ({ x: labels[i], y: d.activity_level })),
      borderColor: COLORS.series[0], backgroundColor: COLORS.series[0] },
    { label: t("livestock.rest_of_herd"), data: a.days.map((d, i) => ({ x: labels[i], y: d.herd_activity })),
      borderColor: COLORS.series[1], backgroundColor: COLORS.series[1] },
  ], t("livestock.activity"), { y: { beginAtZero: true } });
}

// ----------------------------------------------------------------- crops
// Six overlapping lines are unreadable on a projector, so one site is highlighted
// at a time and the others stay in the background in grey.
let cropChart, cropSites = [];

function highlightSite(site) {
  for (const ds of cropChart.data.datasets) {
    if (!ds.site) continue;
    const on = ds.site === site;
    ds.borderColor = ds.backgroundColor = on ? COLORS.series[0] : COLORS.muted;
    ds.borderWidth = on ? 3 : 1.5;
    ds.order = on ? 0 : 2;
  }
  cropChart.data.datasets[0].label = site;
  cropChart.update();
  for (const chip of document.querySelectorAll("[data-site]")) {
    chip.setAttribute("aria-pressed", chip.dataset.site === site);
  }
}

async function initCrops() {
  if (cropChart) return;
  const c = await api("/api/crops");
  const years = [...new Set(c.seasons.map((s) => s.date.slice(0, 4)))].sort();
  const forecast = [...c.alerts].sort((x, y) => x.pct_of_average - y.pct_of_average);
  cropSites = c.sites;
  const datasets = [
    // Legend entries only: the highlighted site, the other sites and the average.
    { label: "", data: [], borderColor: COLORS.series[0], backgroundColor: COLORS.series[0] },
    { label: t("crops.other_sites"), data: [], borderColor: COLORS.muted, backgroundColor: COLORS.muted },
    ...c.sites.map((site) => ({
      site, label: site,
      data: c.seasons.filter((s) => s.location === site).map((s) => ({ x: s.date.slice(0, 4), y: s.yield_t_ha })),
    })),
    { label: t("crops.district_average", { avg: c.regional_average_t_ha.toFixed(2) }), order: 1,
      data: years.map((x) => ({ x, y: c.regional_average_t_ha })),
      borderColor: COLORS.ink, backgroundColor: COLORS.ink, borderDash: [6, 4] },
  ];
  cropChart = lineChart($("#yield-chart"), datasets, t("crops.yield_axis"), { y: { beginAtZero: true } });
  cropChart.options.scales.x.ticks.maxTicksLimit = 12;
  cropChart.options.plugins.legend.labels.filter = (item, data) => !data.datasets[item.datasetIndex].site;
  cropChart.options.plugins.tooltip = {
    filter: (item) => !item.dataset.site || item.dataset.borderWidth === 3,
  };

  $("#site-chips").innerHTML = forecast.map((a) => `
    <button type="button" class="chip" data-site="${esc(a.location)}" aria-pressed="false">
      ${esc(a.location)}</button>`).join("");
  $("#site-chips").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-site]");
    if (chip) highlightSite(chip.dataset.site);
  });
  highlightSite(forecast[0]?.location ?? c.sites[0]);

  $("#crop-rows").innerHTML = forecast.map((a) => `<tr>
      <td>${esc(a.location)}</td>
      <td class="num">${a.predicted_t_ha?.toFixed(2) ?? "–"}</td>
      <td class="num">${a.pct_of_average ?? "–"}%</td>
      <td>${badge(a.risk_level)}</td></tr>`).join("");
}

// ------------------------------------------------------------------ start
loadAlerts();
// --------------------------------------------------------------- sensors
// Live readings from the hardware prototype, refreshed every few seconds while the tab is open.
const SENSOR_REFRESH_MS = 3000;
let sensorTimer = null;
const sensorCharts = new Map();  // animal_id -> Chart

function startSensors() {
  if (sensorTimer) return;
  loadSensors();
  sensorTimer = setInterval(loadSensors, SENSOR_REFRESH_MS);
}

function stopSensors() {
  clearInterval(sensorTimer);
  sensorTimer = null;
}

function duration(seconds) {
  if (seconds < 60) return t("time.s", { n: seconds });
  if (seconds < 3600) return t("time.min", { n: Math.round(seconds / 60) });
  return t("time.h", { n: Math.round(seconds / 3600) });
}
const ago = (seconds) => t("time.ago", { time: duration(seconds) });

const localTime = (t) => new Date(t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
const COLLAR_RISK = { high_temperature: "High", not_seen: "Medium", ok: "Low" };  // badge colour per state

async function loadSensors() {
  let live;
  try {
    live = await api("/api/sensors/live?minutes=10");
  } catch (err) {
    $("#sensor-status").textContent = t("sensors.load_failed", { error: err.message });
    return;
  }
  const gw = live.gateways[0];
  $("#sensor-status").textContent = !gw
    ? t("sensors.no_data")
    : gw.seconds_ago > 30
      ? t("sensors.gateway_quiet", { id: gw.gateway_id, time: duration(gw.seconds_ago) })
      : t("sensors.gateway_ok", { id: gw.gateway_id, ago: ago(gw.seconds_ago) });
  renderRain(live.rain);
  renderCollars(live);
}

function renderRain(rain) {
  const card = $("#rain-card");
  if (!rain) {
    card.innerHTML = `<h2>${esc(t("sensors.rain"))}</h2><p class="hint">${esc(t("sensors.no_rain"))}</p>`;
    return;
  }
  card.innerHTML = `
    <h2>${esc(t("sensors.rain"))}</h2>
    <p class="rain-state">${esc(t(rain.rain_wet ? "sensors.raining" : "sensors.dry"))}</p>
    <div class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${rain.rain_level}"
         aria-label="${esc(t("sensors.wetness_aria"))}"><span style="width:${rain.rain_level}%"></span></div>
    <p class="hint">${esc(t("sensors.wetness", { n: rain.rain_level, ago: ago(rain.seconds_ago) }))}</p>`;
}

function renderCollars(live) {
  const box = $("#collar-cards");
  if (!live.collars.length) {
    box.innerHTML = `<div class="chart-card"><h2>${esc(t("sensors.collars"))}</h2><p class="hint">${esc(t("sensors.no_collar"))}</p></div>`;
    sensorCharts.forEach((c) => c.destroy());
    sensorCharts.clear();
    return;
  }
  // Build the cards once per collar, then only update their values and charts.
  for (const c of live.collars) {
    let card = box.querySelector(`[data-collar="${CSS.escape(c.animal_id)}"]`);
    if (!card) {
      if (!box.querySelector("[data-collar]")) box.innerHTML = "";
      card = document.createElement("div");
      card.className = "chart-card collar-card";
      card.dataset.collar = c.animal_id;
      card.innerHTML = `
        <div class="collar-top"><h2>${esc(t("sensors.collar", { id: c.animal_id }))}</h2><span class="collar-badge"></span></div>
        <div class="animal-facts">
          <div><span>${esc(t("sensors.skin_temp"))}</span><strong data-v="temp"></strong></div>
          <div><span>${esc(t("sensors.air_temp"))}</span><strong data-v="ambient"></strong></div>
          <div><span>${esc(t("sensors.activity"))}</span><strong data-v="activity"></strong></div>
          <div><span>${esc(t("sensors.last_heard"))}</span><strong data-v="seen"></strong></div>
        </div>
        <p class="chart-title">${esc(t("sensors.chart_title"))}</p>
        <div class="small-chart"><canvas aria-label="${esc(t("sensors.chart_aria", { id: c.animal_id }))}"></canvas></div>
        <p class="hint no-series" hidden>${esc(t("sensors.no_series"))}</p>`;
      box.append(card);
    }
    card.querySelector(".collar-badge").innerHTML = badge(COLLAR_RISK[c.status], t(`collar.${c.status}`));
    card.classList.toggle("stale", c.status === "not_seen");
    const fmt = (v, unit) => (v === null || v === undefined ? "–" : `${v.toFixed(1)} ${unit}`);
    card.querySelector('[data-v="temp"]').textContent = fmt(c.body_temp_c, "°C");
    card.querySelector('[data-v="ambient"]').textContent = fmt(c.ambient_c, "°C");
    card.querySelector('[data-v="activity"]').textContent = c.activity ?? t("sensors.no_sensor");
    card.querySelector('[data-v="seen"]').textContent = ago(c.seconds_ago);

    // Time axis = the last 10 minutes up to now, so the newest reading is always on the right.
    const end = Date.parse(live.now);
    const start = end - live.minutes * 60000;
    const temps = c.series.map((p) => ({ x: Date.parse(p.t), y: p.body_temp_c }));
    const limit = [{ x: start, y: live.high_temp_threshold_c }, { x: end, y: live.high_temp_threshold_c }];
    card.querySelector(".small-chart").hidden = !temps.length;
    card.querySelector(".no-series").hidden = temps.length > 0;
    let chart = sensorCharts.get(c.animal_id);
    if (!chart) {
      chart = lineChart(card.querySelector("canvas"), [
        { label: t("sensors.skin_temp"), data: temps, borderColor: COLORS.series[0], backgroundColor: COLORS.series[0] },
        { label: t("sensors.threshold", { t: live.high_temp_threshold_c }), data: limit,
          borderColor: COLORS.high, backgroundColor: COLORS.high, borderDash: [6, 4] },
      ], "°C", { y: { suggestedMin: 30, suggestedMax: 40 } });
      chart.options.scales.x = { type: "linear", grid: { display: false },
                                 ticks: { maxTicksLimit: 6, maxRotation: 0, callback: (v) => localTime(v) } };
      chart.options.plugins.tooltip = { callbacks: { title: (items) => localTime(items[0].parsed.x) } };
      sensorCharts.set(c.animal_id, chart);
    }
    chart.data.datasets[0].data = temps;
    chart.data.datasets[1].data = limit;
    Object.assign(chart.options.scales.x, { min: start, max: end });
    chart.update("none");
  }
}

// A link like /#livestock opens that tab; otherwise the last tab used, or Alerts.
let startTab = location.hash.slice(1);
try { startTab ||= localStorage.getItem("agrisense-tab"); } catch { /* storage unavailable */ }
startTab ||= "alerts";
selectTab(document.getElementById(`tab-${startTab}`) ?? tabs[0]);
