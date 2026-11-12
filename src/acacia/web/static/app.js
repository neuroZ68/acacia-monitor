"use strict";

// All server text is inserted with textContent, never as HTML.
const $ = (id) => document.getElementById(id);
const canvas = $("view");
const ctx = canvas.getContext("2d");

const COLORS = {
  ON: "#e5484d", AMBIGUOUS: "#f5a524", OFF_COUCH: "#46a758", WEAK: "#8b8d98",
  person: "#7aa7e6", cushion: "#6fbf9b", context: "#b9a7e8",
};
const HANDLE_PX = 11;

let snapshot = null;
let health = null;
let bitmap = null;
let calibration = { polygons: [], status: "missing", detail: "" };
let lastCalibrationRevision = -1;
let socket = null;
let disconnected = true;

// Editor state. Draft polygons exist only until Save or Cancel.
let draft = null;
let selected = null;
let drawing = null;
let dragging = null;

function setText(id, text) { $(id).textContent = text; }
function fmt(value, digits = 2) { return value === null || value === undefined ? "n/a" : Number(value).toFixed(digits); }
function stat(s, unit = "ms") {
  if (!s) return "n/a";
  const tail = s.p95 !== undefined ? `, p95 ${s.p95}` : "";
  return `median ${s.median}${tail}, max ${s.max} ${unit} (n=${s.n})`;
}

function kv(root, pairs) {
  root.replaceChildren();
  for (const [key, value] of pairs) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    root.append(dt, dd);
  }
}

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

// ---- live socket ---------------------------------------------------------

function connect() {
  socket = new WebSocket(`ws://${location.host}/ws`);
  socket.binaryType = "arraybuffer";
  socket.onopen = () => { disconnected = false; };
  socket.onclose = () => {
    disconnected = true;
    renderStatus();
    setTimeout(connect, 1500);
  };
  socket.onmessage = onMessage;
}

async function onMessage(message) {
  const view = new DataView(message.data);
  const headerLength = view.getUint32(0);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(message.data, 4, headerLength)));
  snapshot = header;
  if (header.health) health = header.health;
  const jpegLength = message.data.byteLength - 4 - headerLength;
  if (jpegLength > 0) {
    const blob = new Blob([new Uint8Array(message.data, 4 + headerLength, jpegLength)], { type: "image/jpeg" });
    const next = await createImageBitmap(blob);
    if (bitmap) bitmap.close();
    bitmap = next;
    if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
    }
  }
  draw();
  renderStatus();
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({
      frame: jpegLength > 0 && header.frame ? header.frame.seq : null,
      evidence: header.evidence_frame_seq,
    }));
  }
  if (header.calibration.revision !== lastCalibrationRevision) {
    lastCalibrationRevision = header.calibration.revision;
    loadCalibration();
  }
}

// ---- drawing -------------------------------------------------------------

function polygonPath(points) {
  ctx.beginPath();
  points.forEach(([x, y], i) => {
    const px = x * canvas.width, py = y * canvas.height;
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  });
}

function scale() { return canvas.width / canvas.getBoundingClientRect().width || 1; }

function drawBox(box, color, label, width = 2) {
  const [x1, y1, x2, y2] = box;
  const px = x1 * canvas.width, py = y1 * canvas.height;
  const pw = (x2 - x1) * canvas.width, ph = (y2 - y1) * canvas.height;
  const s = scale();
  ctx.lineWidth = width * s;
  ctx.strokeStyle = color;
  ctx.strokeRect(px, py, pw, ph);
  ctx.font = `600 ${13 * s}px -apple-system, sans-serif`;
  const textWidth = ctx.measureText(label).width + 8 * s;
  const tagHeight = 18 * s;
  const tagY = py >= tagHeight ? py - tagHeight : py;
  ctx.fillStyle = color;
  ctx.fillRect(px, tagY, textWidth, tagHeight);
  ctx.fillStyle = "#101312";
  ctx.fillText(label, px + 4 * s, tagY + 14 * s);
}

function draw() {
  if (bitmap) ctx.drawImage(bitmap, 0, 0);
  else { ctx.fillStyle = "#000"; ctx.fillRect(0, 0, canvas.width, canvas.height); }
  const s = scale();

  const polygons = draft || calibration.polygons || [];
  for (const polygon of polygons) {
    polygonPath(polygon.points);
    ctx.closePath();
    const isSelected = draft && selected === polygon.id;
    ctx.globalAlpha = polygon.enabled ? (isSelected ? 0.3 : 0.16) : 0.05;
    ctx.fillStyle = COLORS.cushion;
    ctx.fill();
    ctx.globalAlpha = polygon.enabled ? 1 : 0.4;
    ctx.lineWidth = 2 * s;
    ctx.setLineDash(polygon.enabled ? [] : [6 * s, 6 * s]);
    ctx.strokeStyle = COLORS.cushion;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    if (draft) drawHandles(polygon, isSelected);
  }
  if (drawing) {
    polygonPath(drawing.points);
    ctx.lineWidth = 2 * s;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
    for (const [x, y] of drawing.points) dot(x, y, 5 * s, "#fff");
  }

  if (!snapshot || draft) return;
  for (const item of snapshot.context) drawBox(item.box, COLORS.context, `${item.label} ${fmt(item.confidence)}`, 1);
  for (const person of snapshot.persons) drawBox(person.box, COLORS.person, `person ${fmt(person.confidence)}`, 1);
  for (const dog of snapshot.unclassified_dogs) drawBox(dog.box, COLORS.WEAK, `dog ${fmt(dog.confidence)} | not classified`);
  const dogs = snapshot.evidence ? snapshot.evidence.dogs : [];
  for (const dog of dogs) {
    drawBox(dog.box, COLORS[dog.label], `dog ${fmt(dog.confidence)} | support ${fmt(dog.support)}`);
    dot((dog.box[0] + dog.box[2]) / 2, dog.box[3], 5 * s, dog.foot_in ? COLORS.cushion : "#fff");
  }
}

function dot(x, y, radius, color) {
  ctx.beginPath();
  ctx.arc(x * canvas.width, y * canvas.height, radius, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#101312";
  ctx.stroke();
}

function drawHandles(polygon, isSelected) {
  if (!isSelected) return;
  const s = scale();
  polygon.points.forEach(([x, y], i) => {
    dot(x, y, 6 * s, "#fff");
    const [nx, ny] = polygon.points[(i + 1) % polygon.points.length];
    dot((x + nx) / 2, (y + ny) / 2, 4 * s, COLORS.cushion);
  });
}

// ---- status panels -------------------------------------------------------

const REASONS = {
  startup: "Starting up",
  no_video_yet: "No video yet",
  no_credentials: "No camera credentials stored",
  auth_failed: "Camera rejected the credentials",
  stale_stream: "Video stopped updating",
  stream_disconnected: "Camera disconnected",
  replaying_buffered_frames: "Discarding delayed frames",
  stream_warming_up: "Stream recovering",
  calibration_missing: "Draw cushion polygons to begin",
  calibration_invalid: "Calibration invalid: redraw cushions",
  calibration_unverified: "Waiting to verify calibration",
  detector_unavailable: "Detector failed to load",
  awaiting_inference: "Waiting for first inference",
  inference_failed_or_stalled: "Inference failed or stalled",
  ambiguous_support: "Dog near cushions, support unclear",
  cushions_obstructed: "Cushions hidden",
  occluded_while_on: "Dog hidden while on couch",
  dog_lost_on_couch: "Dog vanished on couch, no exit seen",
  dog_unaccounted: "Dog not accounted for",
  confirming_clear: "Confirming couch is clear",
  awaiting_evidence: "Waiting for fresh evidence",
  awaiting_confirmation: "Possible ON, confirming",
  candidate_unresolved: "Candidate unresolved",
  candidate_cancelled: "Candidate cancelled",
  confirmed: "Dog on couch",
  clear: "Couch clear",
};

function chip(id, text, level) {
  const element = $(id);
  element.textContent = text;
  element.className = `chip ${level}`;
}

function renderStatus() {
  const banner = $("banner");
  if (disconnected) {
    $("state").textContent = "UNKNOWN";
    $("state").className = "state s-unknown";
    setText("reason", "UI lost contact with the app");
    setText("lastKnown", "");
    banner.textContent = "Not connected to the Acacia app";
    banner.hidden = false;
    return;
  }
  if (!snapshot) return;
  const st = snapshot.state;
  $("state").textContent = st.state;
  $("state").className = `state s-${st.state.toLowerCase()}`;
  setText("reason", REASONS[st.reason] || st.reason);
  // Last-known state is diagnostic history, shown apart from the current state.
  setText("lastKnown", st.state === "UNKNOWN" && st.last_known
    ? `Last known ${st.last_known}, ${st.last_known_age_s}s ago (not current)` : "");

  const frame = snapshot.frame;
  const stale = !frame || frame.age_ms > 1500;
  banner.hidden = !stale || !!draft;
  if (stale) banner.textContent = frame ? `No fresh video for ${(frame.age_ms / 1000).toFixed(1)}s. Image below is old.` : "No video yet";

  const cal = snapshot.calibration;
  chip("chipCal", `Calibration: ${cal.status}`, cal.status === "valid" ? "good" : "bad");
  if (health) {
    const phase = health.ingest.phase;
    chip("chipStream", `Stream: ${phase}${frame && !stale ? `, ${frame.age_ms} ms old` : ""}`,
      phase === "streaming" && !stale ? "good" : "bad");
    const det = health.detector;
    chip("chipDet", det.error ? "Detector: failed" : `${det.model} on ${det.provider || "..."}`, det.error ? "bad" : "good");
    renderHealth();
  }
  if (health && health.cue && activeTab === "cue") renderCue(health.cue);
  renderEvidence();
}

function renderEvidence() {
  const st = snapshot.state;
  const ev = snapshot.evidence;
  kv($("evidenceSummary"), [
    ["Evidence", ev ? ev.evidence : "none (not fresh)"],
    ["Evidence age", snapshot.evidence_age_s === null ? "n/a" : `${snapshot.evidence_age_s} s`],
    ["View", snapshot.scene ? snapshot.scene.mode : "n/a"],
    ["Cushions visible", ev ? `${Math.round(ev.visible_fraction * 100)}%` : "n/a"],
    ["People in view", ev ? ev.person_count : "n/a"],
    ["Dog unaccounted for", st.dog_unaccounted ? "yes" : "no"],
    ["Episode open", st.episode_open ? "yes" : "no"],
  ]);
  $("onTimer").value = st.on_confirm_s ? Math.min(1, st.on_timer_s / st.on_confirm_s) : 0;
  setText("onTimerText", `${st.on_timer_s} / ${st.on_confirm_s} s`);
  $("clearTimer").value = st.clear_target_s ? Math.min(1, st.clear_timer_s / st.clear_target_s) : 0;
  setText("clearTimerText", `${st.clear_timer_s} / ${st.clear_target_s} s`);

  const list = $("dogList");
  list.replaceChildren();
  const dogs = ev ? ev.dogs : [];
  if (!dogs.length) {
    const none = document.createElement("p");
    none.className = "note";
    const unclassified = snapshot.unclassified_dogs.length;
    none.textContent = unclassified
      ? `${unclassified} dog box(es) detected but not classified. See the reason at the top.`
      : "No dog detected in the latest frame.";
    list.append(none);
  }
  const css = { ON: "on", AMBIGUOUS: "amb", OFF_COUCH: "off", WEAK: "weak" };
  for (const dog of dogs) {
    const card = document.createElement("div");
    card.className = `dog ${css[dog.label]}`;
    const title = document.createElement("strong");
    title.textContent = dog.label;
    const details = document.createElement("dl");
    details.className = "kv";
    kv(details, [
      ["Detector confidence", fmt(dog.confidence)],
      ["Couch support", fmt(dog.support)],
      ["Bottom edge on cushion", fmt(dog.bottom_support)],
      ["Lower body on cushion", fmt(dog.lower_overlap)],
      ["Whole box on cushion", fmt(dog.box_overlap)],
      ["Footpoint on cushion", dog.foot_in ? "yes" : "no"],
      ["Footpoint trusted", dog.foot_unreliable ? `no: ${dog.foot_unreliable}` : "yes"],
    ]);
    card.append(title, details);
    list.append(card);
  }
}

function renderHealth() {
  const ingest = health.ingest;
  const stream = ingest.stream;
  const timing = health.timing;
  const resources = health.resources;
  kv($("healthList"), [
    ["Ingest", ingest.phase],
    ["Reconnects", ingest.reconnects],
    ["Next retry", ingest.next_retry_s === null ? "n/a" : `${ingest.next_retry_s} s`],
    ["Last error", ingest.last_error || "none"],
    ["Codec", stream ? `${stream.codec} ${stream.width}x${stream.height} ${stream.pixel_format}` : "n/a"],
    ["Declared fps", stream ? fmt(stream.declared_fps, 1) : "n/a"],
    ["Transport", stream ? stream.transport : "n/a"],
    ["Audio tracks opened", stream ? stream.audio_tracks_opened : "n/a"],
    ["Decoded fps", timing.decoded_fps],
    ["Inference fps", timing.inference_fps],
    ["UI fps", timing.ui_fps],
    ["Frames overwritten", health.dropped_frames],
    ["Stale frames skipped", timing.dropped_stale],
    ["Decode errors", ingest.decode_errors],
    ["Inference failures", timing.inference_failures],
    ["Frame age at inference", stat(timing.frame_age_at_inference_ms)],
    ["Inference", stat(timing.inference_ms)],
    ["Decode to decision", stat(timing.decode_to_decision_ms)],
    ["Decision to UI ack", stat(timing.decision_to_ui_ack_ms)],
    ["Frame to UI ack", stat(timing.frame_to_ui_ack_ms)],
    ["Backlog", snapshot.frame ? `${snapshot.frame.backlog_ms} ms` : "n/a"],
    ["Process CPU", `${resources.process_cpu_percent_of_one_core}% of one core`],
    ["Memory", `${resources.rss_mb} MB`],
    ["Model", `${health.detector.model} on ${health.detector.provider}`],
    ["Detector error", health.detector.error || "none"],
    ["Audio ingest", health.privacy.audio_ingest ? "ON" : "off"],
    ["Image retention", health.privacy.image_retention ? "ON" : "off"],
    ["Bound to", health.privacy.bind],
  ]);
}

// ---- events and truth ----------------------------------------------------

async function refreshEvents() {
  const tier = $("eventTier").value;
  const rows = await api(`/api/events?limit=150${tier ? `&tier=${tier}` : ""}`);
  const list = $("eventList");
  list.replaceChildren();
  for (const row of rows) {
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.textContent = row.ts_local;
    const tag = document.createElement("span");
    tag.className = `tag ${row.tier}`;
    tag.textContent = row.tier;
    const text = document.createElement("span");
    const arrow = row.state_from && row.state_from !== row.state_to ? ` ${row.state_from} to ${row.state_to}` : "";
    text.textContent = `${row.kind}${arrow}${row.reason ? ` (${row.reason})` : ""}`;
    item.append(time, tag, text);
    list.append(item);
  }
}

async function refreshTruth() {
  const rows = await api("/api/truth");
  const list = $("truthList");
  list.replaceChildren();
  for (const row of rows) {
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.textContent = row.ts_local;
    const text = document.createElement("span");
    text.textContent = `${row.kind}${row.note ? `: ${row.note}` : ""}`;
    item.append(time, text);
    list.append(item);
  }
}

for (const button of document.querySelectorAll("[data-mark]")) {
  button.addEventListener("click", async () => {
    const note = $("markNote").value;
    await api("/api/truth", "POST", { kind: button.dataset.mark, note });
    $("markNote").value = "";
    refreshTruth();
  });
}

// ---- calibration editor --------------------------------------------------

async function loadCalibration() {
  calibration = await api("/api/calibration");
  kv($("calSummary"), [
    ["Status", calibration.status],
    ["Detail", calibration.detail],
    ["Revision", calibration.revision],
    ["Saved", calibration.saved_at || "never"],
    ["Drawn on", calibration.view ? `${calibration.view.source_width}x${calibration.view.source_height}, ${calibration.view.stream_path}, rotation ${calibration.view.rotation_deg}` : "n/a"],
  ]);
  if (!draft) renderPolygons();
  draw();
}

function setEditing(on) {
  $("calActions").hidden = on;
  $("calEditActions").hidden = !on;
  canvas.classList.toggle("editing", on);
  $("editHint").hidden = !on;
  $("calError").hidden = true;
  updateHint();
}

function updateHint() {
  $("editHint").textContent = drawing
    ? "Click to add corners. Click the first corner or press Enter to finish. Esc cancels."
    : "Select a polygon, then drag corners. Drag a small dot to add a corner. Shift-click a corner to remove it.";
}

function renderPolygons() {
  const list = $("polyList");
  list.replaceChildren();
  const polygons = draft || calibration.polygons || [];
  for (const polygon of polygons) {
    const item = document.createElement("li");
    item.classList.toggle("selected", !!draft && polygon.id === selected);
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = polygon.enabled;
    enabled.disabled = !draft;
    enabled.title = "Enabled";
    enabled.setAttribute("aria-label", `Enable ${polygon.name}`);
    enabled.onchange = () => { polygon.enabled = enabled.checked; draw(); };
    const name = document.createElement("input");
    name.type = "text";
    name.value = polygon.name;
    name.disabled = !draft;
    name.maxLength = 60;
    name.setAttribute("aria-label", "Polygon name");
    name.oninput = () => { polygon.name = name.value; };
    name.onfocus = () => { selected = polygon.id; draw(); };
    item.append(enabled, name);
    if (draft) {
      const remove = document.createElement("button");
      remove.textContent = "Delete";
      remove.onclick = () => {
        draft = draft.filter((p) => p.id !== polygon.id);
        if (selected === polygon.id) selected = null;
        renderPolygons();
        draw();
      };
      item.append(remove);
    }
    list.append(item);
  }
}

function normalizedPoint(event) {
  const rect = canvas.getBoundingClientRect();
  return [
    Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
    Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
  ];
}

function pixelDistance(a, b) {
  const rect = canvas.getBoundingClientRect();
  return Math.hypot((a[0] - b[0]) * rect.width, (a[1] - b[1]) * rect.height);
}

function pointInPolygon([x, y], points) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const [xi, yi] = points[i], [xj, yj] = points[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function finishDrawing() {
  if (!drawing) return;
  if (drawing.points.length >= 3) {
    const polygon = {
      id: Math.random().toString(16).slice(2, 10),
      name: `cushion ${draft.length + 1}`,
      enabled: true,
      points: drawing.points,
    };
    draft.push(polygon);
    selected = polygon.id;
  }
  drawing = null;
  updateHint();
  renderPolygons();
  draw();
}

canvas.addEventListener("pointerdown", (event) => {
  if (!draft) return;
  const point = normalizedPoint(event);
  if (drawing) {
    if (drawing.points.length >= 3 && pixelDistance(point, drawing.points[0]) < HANDLE_PX) finishDrawing();
    else { drawing.points.push(point); draw(); }
    return;
  }
  const polygon = draft.find((p) => p.id === selected);
  if (polygon) {
    const vertex = polygon.points.findIndex((v) => pixelDistance(point, v) < HANDLE_PX);
    if (vertex >= 0) {
      if (event.shiftKey) {
        if (polygon.points.length > 3) polygon.points.splice(vertex, 1);
      } else {
        dragging = { polygon, index: vertex };
        canvas.setPointerCapture(event.pointerId);
      }
      draw();
      return;
    }
    for (let i = 0; i < polygon.points.length; i++) {
      const a = polygon.points[i], b = polygon.points[(i + 1) % polygon.points.length];
      if (pixelDistance(point, [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]) < HANDLE_PX) {
        polygon.points.splice(i + 1, 0, point);
        dragging = { polygon, index: i + 1 };
        canvas.setPointerCapture(event.pointerId);
        draw();
        return;
      }
    }
  }
  const hit = [...draft].reverse().find((p) => pointInPolygon(point, p.points));
  selected = hit ? hit.id : null;
  renderPolygons();
  draw();
});

canvas.addEventListener("pointermove", (event) => {
  if (!dragging) return;
  dragging.polygon.points[dragging.index] = normalizedPoint(event);
  draw();
});

canvas.addEventListener("pointerup", () => { dragging = null; });

window.addEventListener("keydown", (event) => {
  if (!drawing) return;
  if (event.key === "Enter") finishDrawing();
  if (event.key === "Escape") { drawing = null; updateHint(); draw(); }
});

$("calEdit").onclick = () => {
  draft = structuredClone(calibration.polygons || []);
  selected = draft.length ? draft[0].id : null;
  setEditing(true);
  renderPolygons();
  draw();
};
$("calAdd").onclick = () => { drawing = { points: [] }; selected = null; updateHint(); draw(); };
$("calCancel").onclick = () => {
  draft = null; drawing = null; selected = null;
  setEditing(false);
  renderPolygons();
  draw();
};
$("calSave").onclick = async () => {
  try {
    calibration = await api("/api/calibration", "PUT", { polygons: draft });
    draft = null; drawing = null; selected = null;
    setEditing(false);
    await loadCalibration();
  } catch (error) {
    $("calError").textContent = error.message;
    $("calError").hidden = false;
  }
};
$("calMoved").onclick = async () => {
  await api("/api/calibration/invalidate", "POST", { reason: "camera moved (marked in UI)" });
  await loadCalibration();
};

// ---- cue -----------------------------------------------------------------

function renderCue(cue) {
  $("cueArm").hidden = cue.armed;
  $("cueDisarm").hidden = !cue.armed;
  kv($("cueStatus"), [
    ["Status", cue.armed ? "ARMED: will play" : "Disarmed: logging only"],
    ["Ready to arm", cue.can_arm ? "yes" : "no: see alexa/README.md"],
    ["Played today", `${cue.sent_today} of ${cue.max_per_day}`],
    ["Cooldown", `${cue.cooldown_s} s`],
    ["Quiet hours", cue.quiet_hours],
    ["Silent with people in view", cue.silent_when_people ? "yes" : "no"],
    ["Last decision", cue.last_decision || "none yet"],
  ]);
}

async function setCueArmed(armed) {
  const error = $("cueError");
  error.hidden = true;
  try {
    renderCue(await api("/api/cue", "POST", { armed }));
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  }
}

$("cueArm").onclick = () => setCueArmed(true);
$("cueDisarm").onclick = () => setCueArmed(false);

// ---- settings ------------------------------------------------------------

async function loadSettings() {
  const values = await api("/api/settings");
  const form = $("settingsForm");
  form.replaceChildren();
  for (const [key, value] of Object.entries(values)) {
    const label = document.createElement("label");
    label.textContent = key;
    label.htmlFor = `set-${key}`;
    const input = document.createElement("input");
    input.id = `set-${key}`;
    input.type = "number";
    input.step = "any";
    input.value = value;
    input.dataset.key = key;
    input.dataset.original = value;
    form.append(label, input);
  }
}

$("settingsForm").addEventListener("submit", (event) => event.preventDefault());
$("settingsSave").onclick = async () => {
  const changes = {};
  for (const input of $("settingsForm").querySelectorAll("input")) {
    if (input.value !== String(input.dataset.original)) changes[input.dataset.key] = Number(input.value);
  }
  const message = $("settingsMsg");
  message.hidden = false;
  if (!Object.keys(changes).length) { message.textContent = "Nothing changed."; return; }
  try {
    await api("/api/settings", "PATCH", changes);
    message.textContent = `Applied ${Object.keys(changes).length} change(s).`;
    await loadSettings();
  } catch (error) {
    message.textContent = error.message;
  }
};

$("retry").onclick = () => api("/api/retry", "POST", {});

// ---- tabs and timers -----------------------------------------------------

let activeTab = "evidence";
for (const button of document.querySelectorAll(".tabs button")) {
  button.addEventListener("click", () => {
    activeTab = button.dataset.tab;
    for (const other of document.querySelectorAll(".tabs button")) other.classList.toggle("active", other === button);
    for (const panel of document.querySelectorAll(".panel")) panel.hidden = panel.id !== `tab-${activeTab}`;
    if (activeTab === "events") refreshEvents();
    if (activeTab === "validate") refreshTruth();
    if (activeTab === "settings") loadSettings();
    if (activeTab === "cushions") loadCalibration();
    if (activeTab === "cue") api("/api/cue").then(renderCue);
  });
}
$("eventTier").onchange = refreshEvents;

setInterval(() => { if (activeTab === "events") refreshEvents().catch(() => {}); }, 2000);

function tickClock() {
  const now = new Date();
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  setText("clock", `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}.${pad(now.getMilliseconds(), 3)}`);
  requestAnimationFrame(tickClock);
}
tickClock();
connect();
