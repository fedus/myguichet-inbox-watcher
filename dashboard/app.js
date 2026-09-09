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

function renderOutputSettings(settings) {
  const entries = Object.entries(settings || {});
  if (!entries.length) {
    return '<div class="output-empty">No settings</div>';
  }
  return `<dl class="output-settings">${entries
    .map(
      ([key, value]) =>
        `<div><dt>${html(key)}</dt><dd>${html(value)}</dd></div>`
    )
    .join("")}</dl>`;
}

function renderOutputConfig(output) {
  return `<details class="output-config">
    <summary><span>${html(output.name)}:${html(output.type)}</span></summary>
    ${renderOutputSettings(output.settings)}
  </details>`;
}

function renderAccounts(accounts) {
  $("accounts").innerHTML = accounts.length
    ? accounts
        .map(
          (account) =>
            `<tr><td data-label="Account"><code>${html(
              account.name
            )}</code></td><td data-label="Source">${html(
              account.source
            )}</td><td data-label="Outputs">${account.outputs
              .map(renderOutputConfig)
              .join("")}</td><td data-label="Last Run">${html(
              account.state.last_run
            )}</td><td data-label="Seen">${html(
              account.state.seen_count
            )}</td></tr>`
        )
        .join("")
    : '<tr><td colspan="5" class="muted">No configured accounts returned by the API.</td></tr>';
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

function renderDocuments(documents) {
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
    : '<tr><td colspan="3" class="muted">No folder-output documents found.</td></tr>';
}

function renderEvents(events) {
  $("events").innerHTML = events.length
    ? events
        .slice()
        .reverse()
        .slice(0, 20)
        .map(
          (event) =>
            `<div class="event"><span class="${cls(event.status)}">${html(
              event.status
            )}</span> <strong>${html(event.event)}</strong> ${
              event.account ? `<code>${html(event.account)}</code>` : ""
            }<div class="muted">${html(event.timestamp)} ${html(
              event.message || ""
            )}</div></div>`
        )
        .join("")
    : '<div class="muted">No runtime events yet.</div>';
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

async function loadData() {
  try {
    const [accounts, status, documents, plugins] = await Promise.all([
      fetchJson("/api/accounts"),
      fetchJson("/api/status"),
      fetchJson("/api/documents?limit=20"),
      fetchJson("/api/plugins"),
    ]);
    $("accountCount").textContent = accounts.length;
    $("activeCount").textContent = status.active_polls.length;
    $("inputCount").textContent = status.pending_inputs.length;
    $("documentCount").textContent = documents.length;
    $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    renderAccounts(accounts);
    renderRuntime(status);
    renderDocuments(documents);
    renderEvents(status.recent_events);
    renderPlugins(plugins);
  } catch (error) {
    $("updated").textContent = `API error: ${error.message}`;
    $("runtime").innerHTML = `<div class="error">${html(error.message)}</div>`;
  }
}

$("refresh").addEventListener("click", loadData);
loadData();
setInterval(loadData, 10000);
