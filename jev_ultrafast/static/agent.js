const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
const escape = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let state = null;
let messageCount = -1;
// The server owns the clock. The client only interpolates between polls so the readout moves at
// 60fps, and snaps back to the server's number on every poll and at the end.
let anchor = { elapsed: 0, at: 0, running: false };
let errorText = "";
let streaming = false;
let logsOpen = false;
let logKey = "";

function showError(text) {
  errorText = text;
  $("error").textContent = text;
  $("error").hidden = !text;
}

/* ---------- clock ---------- */

function paintClock() {
  const ms = anchor.running ? anchor.elapsed + (performance.now() - anchor.at) : anchor.elapsed;
  $("clock").textContent = (ms / 1000).toFixed(2).padStart(5, "0");
  requestAnimationFrame(paintClock);
}
requestAnimationFrame(paintClock);

/* ---------- live browser feed ----------
   One multipart connection the browser decodes itself, rather than a new request per frame.
   Frames arrive as fast as the tab paints them, so the pane plays instead of flickering. */

function startFeed() {
  if (streaming) return;
  streaming = true;
  const feed = $("feed");
  feed.onload = () => {
    feed.hidden = false;
    $("viewport-empty").hidden = true;
  };
  feed.onerror = () => {
    streaming = false;
  };
  feed.src = `/api/stream.mjpg?t=${Date.now()}`;
}

function restartFeed() {
  streaming = false;
  $("feed").removeAttribute("src");
  $("feed").hidden = true;
  $("viewport-empty").hidden = false;
  startFeed();
}

/* ---------- rendering ---------- */

function renderMessages() {
  if (state.messages.length === messageCount) return;
  messageCount = state.messages.length;
  $("log").innerHTML = state.messages
    .map(
      (m) =>
        `<div class="msg ${m.role}"><span class="who">${m.role === "you" ? "YOU" : "AGENT ONE"}</span>` +
        `<div class="bubble">${escape(m.text)}</div></div>`,
    )
    .join("");
  $("log").scrollTop = $("log").scrollHeight;
}

function renderRail() {
  $("rail").innerHTML = state.steps
    .map(
      (s, i) =>
        `<li data-status="${s.status}"><span class="dot">${s.status === "done" ? "&#10003;" : i + 1}</span>` +
        `<small>${escape(s.label)}</small></li>`,
    )
    .join("");
  const done = state.steps.filter((s) => s.status === "done").length;
  const span = state.steps.length - 1;
  // The fill stops at the centre of the last finished node, so the line tracks the checks.
  $("rail-fill").style.width = `${(Math.max(0, done - 0.5) / span) * 100}%`;
}

function renderHeadline() {
  const trip = state.itinerary;
  if (!trip) {
    $("headline").innerHTML = "One sentence.<br />One seat.";
    return;
  }
  const seconds = (state.elapsed_ms / 1000).toFixed(2);
  const tail = state.phase === "done" ? ` In ${seconds} seconds.` : "";
  $("headline").innerHTML =
    `${escape(trip.origin_field)} <span class="arrow">&rarr;</span> ${escape(trip.destination_field)}.${escape(tail)}`;
  const party = trip.party > 1 ? `${trip.party} adults` : "one adult";
  const kind = trip.trip_type === "round_trip" ? "round trip" : "one way";
  $("subline").textContent = `${trip.pretty} · ${party} · economy · ${kind}`;
}

/** The drawer is the whole argument: what Jev decided, how long each call took, and what was
    simply the web being slow. */
function renderLogs() {
  const rows = state.timeline || [];
  const key = `${rows.length}:${logsOpen}`;
  if (key === logKey) return;
  logKey = key;
  $("logs").hidden = !logsOpen;
  $("logs-toggle").setAttribute("aria-expanded", String(logsOpen));
  $("logs-caret").innerHTML = logsOpen ? "&#9662;" : "&#9656;";
  if (!logsOpen) return;

  const sum = (who) => rows.filter((r) => r.who === who).reduce((a, r) => a + (r.ms || 0), 0);
  const jev = sum("jev");
  const llm = sum("llm");
  const wall = state.elapsed_ms || 0;
  const waiting = Math.max(0, wall - jev - llm);
  $("logs-totals").innerHTML =
    `<span><b>${rows.filter((r) => r.who === "jev").length}</b> Jev calls <b>${(jev / 1000).toFixed(2)}s</b></span>` +
    `<span><b>${rows.filter((r) => r.who === "llm").length}</b> model calls <b>${(llm / 1000).toFixed(2)}s</b></span>` +
    `<span>waiting on the web <b>${(waiting / 1000).toFixed(2)}s</b></span>` +
    `<span>total <b>${(wall / 1000).toFixed(2)}s</b></span>`;

  $("logs-body").innerHTML = rows
    .map(
      (r) =>
        `<div class="logrow" data-who="${r.who}">` +
        `<span class="log-t">${((r.t || 0) / 1000).toFixed(2)}s</span>` +
        `<span class="log-who">${r.who === "jev" ? "JEV" : r.who === "llm" ? "MODEL" : "PAGE"}</span>` +
        `<span class="log-what">${escape(r.what)}</span>` +
        `<span class="log-ms">${r.ms != null ? r.ms + " ms" : ""}</span>` +
        `<span class="log-conf">${r.conf != null ? Math.round(r.conf * 100) + "%" : ""}</span>` +
        `</div>`,
    )
    .join("");
  $("logs-body").scrollTop = $("logs-body").scrollHeight;
}

function render() {
  if (!state) return;
  const running = !!state.running;
  $("phase").textContent = (state.phase || "idle").toUpperCase();
  $("phase").dataset.phase = state.phase || "idle";
  $("live-tag").textContent = running ? "LIVE" : state.feed ? "HELD" : "IDLE";
  $("live-tag").dataset.on = running ? "1" : "0";
  if (state.feed) startFeed();

  anchor = { elapsed: state.elapsed_ms || 0, at: performance.now(), running };

  renderMessages();
  renderRail();
  renderHeadline();
  renderLogs();

  $("last-action").textContent = state.last_action || "No action yet";
  $("last-probability").textContent =
    state.last_probability != null ? `${Math.round(state.last_probability * 100)}% confident` : "";

  const stats = state.stats || {};
  $("stats").hidden = !stats.decisions;
  if (stats.decisions) {
    $("s-actions").textContent = stats.actions;
    $("s-decisions").textContent = stats.decisions;
    $("s-median").textContent = `${stats.median_ms} ms`;
    $("s-text").textContent = stats.text_calls;
    $("s-cost").textContent = `$${(stats.cost_usd || 0).toFixed(4)}`;
  }
  if (stats.url) $("viewport-url").textContent = stats.url;

  $("send").disabled = running;
  $("command").disabled = running;
  $("stop").hidden = !running;

  if (state.phase === "error" && state.error) showError(state.error);
  else if (errorText && running) showError("");
}

/* ---------- transport ---------- */

async function poll() {
  try {
    state = await fetch("/api/state").then((r) => r.json());
    render();
  } catch {
    $("phase").textContent = "OFFLINE";
  }
  setTimeout(poll, state?.running ? 120 : 600);
}

async function post(action, body) {
  const response = await fetch(`/api/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify(body || {}),
  });
  // The server mints a fresh token each start, so a tab left open across a restart holds a dead
  // one. Reload rather than leaving the page looking broken.
  if (response.status === 403) {
    showError("This tab was open across a server restart. Reloading…");
    setTimeout(() => location.reload(), 900);
    throw Error("Reloading");
  }
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  state = data;
  render();
}

$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const command = $("command").value.trim();
  if (!command) return;
  showError("");
  messageCount = -1;
  try {
    await post("run", { command });
    $("command").value = "";
    restartFeed();
  } catch (error) {
    if (error.message !== "Reloading") showError(error.message);
  }
});

$("command").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("task-form").requestSubmit();
  }
});

$("stop").addEventListener("click", () => post("stop").catch(() => {}));

function toggleLogs(open) {
  logsOpen = open;
  logKey = "";
  if (state) renderLogs();
}
$("logs-toggle").addEventListener("click", () => toggleLogs(!logsOpen));
$("logs-close").addEventListener("click", () => toggleLogs(false));

// The real window sits minimised so it never covers the recording. This hands it back.
$("open-browser").addEventListener("click", () =>
  post("window", { visible: true }).catch((error) => {
    if (error.message !== "Reloading") showError(error.message);
  }),
);

poll();
