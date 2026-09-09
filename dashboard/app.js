const $ = (id) => document.getElementById(id);
const cls = (status) => `status ${(status || "").replace(/[^a-z0-9_-]/gi, "")}`;
const text = (value) => {
  if (value === null || value === undefined || value === "") {
    return "n/a";
  }
  return value;
};
const escapeMap = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};
const html = (value) =>
  String(text(value)).replace(/[&<>"']/g, (char) => escapeMap[char]);
const size = (bytes) => {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1048576) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / 1048576).toFixed(1)} MB`;
};
let latestAccounts = [];
let latestEvents = [];
let latestService = null;
let selectedService = "http";
let selectedLogFilter = "all";
let expandedAccount = null;
let eventStream = null;
const pendingPolls = new Set();

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(`${url}: ${detail}`);
  }
  return response.json();
}

async function postJson(url) {
  const response = await fetch(url, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(`${url}: ${detail}`);
  }
  return response.json();
}

function renderKeyValues(settings) {
  const entries = Object.entries(settings || {});
  if (!entries.length) {
    return '<div class="config-empty">No settings</div>';
  }
  return `<dl class="config-list">${entries
    .map(
      ([key, value]) =>
        `<div><dt>${html(key)}</dt><dd>${html(value)}</dd></div>`
    )
    .join("")}</dl>`;
}

function renderOutputPills(outputs) {
  if (!outputs.length) {
    return '<span class="muted">none</span>';
  }
  return outputs
    .map(
      (output) =>
        `<span class="output-pill">${html(output.name)}:${html(
          output.type
        )}</span>`
    )
    .join("");
}

function renderOutputConfigCards(account) {
  if (!account.outputs.length) {
    return '<div class="config-empty">No output plugins configured.</div>';
  }
  return account.outputs
    .map(
      (output) => `
        <div class="output-card">
          <div class="selected-subtitle">${html(output.name)}:${html(
        output.type
      )}</div>
          ${renderKeyValues(output.settings)}
        </div>`
    )
    .join("");
}

function renderAccountDetailRow(account) {
  if (expandedAccount !== account.name) {
    return "";
  }
  return `<tr class="account-detail-row">
    <td colspan="7">
      <div class="account-detail-panel">
        <div>
          <div class="plugin-label">Output configuration</div>
          <div class="selected-title"><code>${html(account.name)}</code></div>
        </div>
        <div class="output-card-grid">${renderOutputConfigCards(account)}</div>
      </div>
    </td>
  </tr>`;
}

function renderAccounts(accounts) {
  const mqttRunning = Boolean(latestService && latestService.mqtt.running);
  $("accounts").innerHTML = accounts.length
    ? accounts
        .map(
          (account) => {
            const pollPending = pendingPolls.has(account.name);
            const pollLabel = pollPending ? "Sending" : "Poll";
            return `
            <tr><td data-label="Account"><code>${html(
              account.name
            )}</code></td><td data-label="Source">${html(
              account.source
            )}</td><td data-label="Outputs">${renderOutputPills(
              account.outputs
            )}</td><td data-label="Last Run">${html(
              account.state.last_run
            )}</td><td data-label="Seen">${html(
              account.state.seen_count
            )}</td><td data-label="Poll"><button type="button" class="poll-trigger" data-account="${html(
              account.name
            )}" ${
              mqttRunning && !pollPending ? "" : "disabled"
            }>${html(pollLabel)}</button></td><td data-label="Details"><button type="button" class="detail-toggle${
              expandedAccount === account.name ? " is-selected" : ""
            }" data-account="${html(account.name)}">${
              expandedAccount === account.name ? "Hide" : "Show"
            }</button></td></tr>
            ${renderAccountDetailRow(account)}`;
          }
        )
        .join("")
    : '<tr><td colspan="7" class="muted">No configured accounts returned by the API.</td></tr>';
}

function renderServiceBadges(service) {
  $("httpBadge").textContent = `:${text(service.http.port)}`;
  if (!service.mqtt.configured) {
    $("mqttBadge").textContent = "off";
  } else if (service.mqtt.running) {
    $("mqttBadge").textContent = "running";
  } else {
    $("mqttBadge").textContent = "configured";
  }
}

function renderServiceDetails() {
  if (!latestService) {
    $("serviceDetails").innerHTML = '<div class="muted">Loading service details.</div>';
    return;
  }
  const details =
    selectedService === "mqtt" ? latestService.mqtt : latestService.http;
  $("serviceDetails").innerHTML = `
    <div class="selected-subtitle">${html(selectedService.toUpperCase())}</div>
    ${renderKeyValues(details)}
  `;
}

function eventIsError(event) {
  return ["aborted", "error", "timeout"].includes(event.status);
}

function lastPollFrom(status) {
  if (status.last_poll) {
    return status.last_poll;
  }
  return status.recent_events
    .slice()
    .reverse()
    .find((event) => event.event === "poll.finished");
}

function formatDetailKey(key) {
  return key.replace(/_/g, " ");
}

function renderEventDetails(event) {
  const details = Object.entries(event.details || {}).filter(
    ([key]) => key !== "error_message"
  );
  if (!details.length) {
    return "";
  }
  return `<span class="log-details">${details
    .map(([key, value]) => `${html(formatDetailKey(key))}: ${html(value)}`)
    .join(" · ")}</span>`;
}

function mergeEvents(events) {
  const byId = new Map();
  [...latestEvents, ...events].forEach((event) => byId.set(event.id, event));
  latestEvents = [...byId.values()]
    .sort((left, right) => Number(left.id) - Number(right.id))
    .slice(-200);
}

function renderLog() {
  const events =
    selectedLogFilter === "error"
      ? latestEvents.filter(eventIsError)
      : latestEvents;
  $("events").innerHTML = events.length
    ? events
        .slice()
        .reverse()
        .map(
          (event) =>
            `<div class="log-line ${eventIsError(event) ? "is-error" : ""}">
              <span class="log-time">${html(event.timestamp)}</span>
              <span class="${cls(event.status)}">${html(event.status)}</span>
              <strong>${html(event.event)}</strong>
              ${event.account ? `<code>${html(event.account)}</code>` : ""}
              ${
                event.source
                  ? `<span class="log-source">${html(event.source)}</span>`
                  : ""
              }
              ${
                event.message
                  ? `<span class="log-message">${html(event.message)}</span>`
                  : ""
              }
              ${renderEventDetails(event)}
            </div>`
        )
        .join("")
    : '<div class="muted">No log entries yet.</div>';
}

function connectEventStream() {
  if (eventStream || !window.EventSource) {
    if (!window.EventSource) {
      $("logLiveState").textContent = "polling";
    }
    return;
  }
  const lastId = latestEvents.length ? latestEvents[latestEvents.length - 1].id : 0;
  eventStream = new EventSource(`/api/events/stream?after=${lastId}`);
  eventStream.onopen = () => {
    $("logLiveState").textContent = "live";
    $("logLiveState").classList.add("is-live");
  };
  eventStream.onerror = () => {
    $("logLiveState").textContent = "reconnecting";
    $("logLiveState").classList.remove("is-live");
  };
  eventStream.addEventListener("runtime", (message) => {
    mergeEvents([JSON.parse(message.data)]);
    renderLog();
  });
}

function renderRuntime(status) {
  const runtimeItems = [
    ...status.active_polls.map(
      (poll) =>
        `<div><span class="${cls("running")}">running</span> <code>${html(
          poll.account
        )}</code><div class="muted">since ${html(poll.started_at)}</div></div>`
    ),
    ...status.pending_inputs.map(
      (input) =>
        `<div><span class="${cls("waiting")}">waiting</span> <code>${html(
          input.account
        )}</code><div class="muted">${html(input.kind)}: ${input.fields
          .map(html)
          .join(", ")}</div></div>`
    ),
  ];
  $("runtime").innerHTML = runtimeItems.length
    ? runtimeItems.join("")
    : '<div class="muted">No active polls or pending inputs.</div>';
}

function renderDocuments(documents, emptyMessage) {
  $("documents").innerHTML = documents.length
    ? documents
        .map(
          (document) =>
            `<tr><td data-label="Account"><code>${html(
              document.account
            )}</code></td><td data-label="File">${html(
              document.filename
            )}</td><td data-label="Size">${html(
              size(document.size_bytes)
            )}</td></tr>`
        )
        .join("")
    : `<tr><td colspan="3" class="muted">${html(emptyMessage)}</td></tr>`;
}

function renderPlugins(plugins) {
  const sourceItems = plugins.sources.map(
    (source) => `<span class="plugin-pill source">${html(source)}</span>`
  );
  const outputItems = plugins.outputs.map(
    (output) => `<span class="plugin-pill output">${html(output)}</span>`
  );
  $("plugins").innerHTML = `
    <div>
      <div class="plugin-label">Sources</div>
      <div class="plugin-pills">${sourceItems.join("")}</div>
    </div>
    <div>
      <div class="plugin-label">Outputs</div>
      <div class="plugin-pills">${outputItems.join("")}</div>
    </div>
  `;
}

async function triggerPoll(accountName) {
  pendingPolls.add(accountName);
  renderAccounts(latestAccounts);
  try {
    const result = await postJson(
      `/api/accounts/${encodeURIComponent(accountName)}/poll`
    );
    $("updated").textContent = `Poll trigger published for ${result.account}`;
    await loadData();
  } catch (error) {
    $("updated").textContent = `Trigger failed: ${error.message}`;
    await loadData();
  } finally {
    pendingPolls.delete(accountName);
    renderAccounts(latestAccounts);
  }
}

async function loadData() {
  try {
    const [accounts, status, plugins, service] = await Promise.all([
      fetchJson("/api/accounts"),
      fetchJson("/api/status"),
      fetchJson("/api/plugins"),
      fetchJson("/api/service"),
    ]);
    latestAccounts = accounts;
    latestService = service;
    const lastPoll = lastPollFrom(status);
    const lastPollDocuments =
      lastPoll && Array.isArray(lastPoll.documents) ? lastPoll.documents : [];
    $("accountCount").textContent = accounts.length;
    $("activeCount").textContent = status.active_polls.length;
    $("inputCount").textContent = status.pending_inputs.length;
    $("documentCount").textContent =
      lastPoll && lastPoll.new_documents !== undefined ? lastPoll.new_documents : 0;
    $("documentsTitle").textContent = "Last Crawl Source Documents";
    $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    mergeEvents(status.recent_events);
    renderAccounts(accounts);
    renderRuntime(status);
    renderDocuments(
      lastPollDocuments,
      lastPoll
        ? "The last crawl did not receive any new source documents."
        : "No crawl document history recorded yet."
    );
    renderLog();
    renderPlugins(plugins);
    renderServiceBadges(service);
    renderServiceDetails();
    connectEventStream();
    if (expandedAccount && !accounts.some((account) => account.name === expandedAccount)) {
      expandedAccount = null;
      renderAccounts(accounts);
    }
  } catch (error) {
    $("updated").textContent = `API error: ${error.message}`;
    $("runtime").innerHTML = `<div class="error">${html(error.message)}</div>`;
  }
}

$("refresh").addEventListener("click", loadData);
document.addEventListener("click", (event) => {
  const target =
    event.target instanceof Element ? event.target.closest("button") : null;
  if (!target) {
    return;
  }
  if (target.classList.contains("detail-toggle")) {
    expandedAccount =
      expandedAccount === target.dataset.account ? null : target.dataset.account;
    renderAccounts(latestAccounts);
    return;
  }
  if (target.classList.contains("poll-trigger")) {
    const account = target.dataset.account;
    if (account && !target.disabled) {
      triggerPoll(account);
    }
    return;
  }
  if (target.classList.contains("service-tab")) {
    selectedService = target.dataset.service || "http";
    document
      .querySelectorAll(".service-tab")
      .forEach((chip) => chip.classList.toggle("is-selected", chip === target));
    renderServiceDetails();
    return;
  }
  if (target.classList.contains("log-filter")) {
    selectedLogFilter = target.dataset.logFilter || "all";
    document
      .querySelectorAll(".log-filter")
      .forEach((chip) => chip.classList.toggle("is-selected", chip === target));
    renderLog();
  }
});
loadData();
setInterval(loadData, 10000);
