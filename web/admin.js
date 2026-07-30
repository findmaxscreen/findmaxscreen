/* Admin page — talks to the local serve.py API.
 *
 * This is the only code in the project that calls POST /api/sync, which shells
 * out to sync.py.  serve.py refuses any client that is not 127.0.0.1, and
 * export.py never copies this file, so the endpoint has no public surface.
 */

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options) {
  const resp = await fetch(path, options);
  const payload = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
  if (!resp.ok || payload.error) throw new Error(payload.error || `HTTP ${resp.status}`);
  return payload;
}

function fact(term, value) {
  return [el("dt", null, term), el("dd", null, value)];
}

async function loadMeta() {
  try {
    const meta = await api("/api/meta");
    const s = meta.stats;
    const rev = meta.revision;
    const dl = $("meta");
    dl.replaceChildren(
      ...fact("Venues", fmt.format(s.venues)),
      ...fact("Countries", fmt.format(s.countries)),
      ...fact("15/70 mm film", fmt.format(s.film70)),
      ...fact("Dome", fmt.format(s.dome)),
      ...fact("De-listed", fmt.format(s.removed)),
      ...fact("Wiki revision", rev ? String(rev.revid) : "—"),
      ...fact("Wiki edited", rev ? new Date(rev.wiki_timestamp).toLocaleString() : "—"),
      ...fact("Last synced", rev ? new Date(rev.fetched_at).toLocaleString() : "—"),
    );
  } catch (err) {
    $("meta").replaceChildren(el("dd", "error", err.message));
  }
}

function renderFindings(findings) {
  const box = $("validation");
  box.replaceChildren();
  if (!findings) {
    box.append(el("p", null, "Run a refresh to see validation results."));
    return;
  }
  if (!findings.length) {
    const ok = el("p", "ok");
    ok.append(document.createTextNode("All checks passed."));
    box.append(ok);
    return;
  }
  const list = el("ul", "findings");
  for (const f of findings) {
    const item = el("li", f.severity === "ERROR" ? "bad" : "warn");
    item.append(el("strong", null, `${f.severity} ${f.check} (${f.count})`));
    item.append(el("p", null, f.explanation));
    if (f.examples?.length) {
      item.append(el("p", "examples", f.examples.join(" · ")));
    }
    list.append(item);
  }
  box.append(list);
}

async function loadChanges() {
  try {
    const { changes } = await api("/api/changes");
    const box = $("changes");
    if (!changes.length) {
      box.replaceChildren(el("p", null, "No changes recorded yet."));
      return;
    }
    const table = el("table", "changes");
    const head = el("tr");
    for (const h of ["When", "Venue", "Field", "From", "To"]) {
      head.append(el("th", null, h));
    }
    table.append(head);
    for (const c of changes) {
      const row = el("tr");
      row.append(el("td", null, new Date(c.changed_at).toLocaleString()));
      row.append(el("td", null, `${c.name} (${c.city}, ${c.country})`));
      row.append(el("td", null, c.field));
      row.append(el("td", "was", c.old_value ?? "—"));
      row.append(el("td", null, c.new_value ?? "—"));
      table.append(row);
    }
    box.replaceChildren(table);
  } catch (err) {
    $("changes").replaceChildren(el("p", "error", err.message));
  }
}

async function refresh() {
  const button = $("refresh");
  const status = $("syncstatus");
  const log = $("synclog");
  button.disabled = true;
  status.textContent = "Reading the wiki…";
  log.hidden = true;

  try {
    const result = await api("/api/sync", { method: "POST" });

    if (result.status === "current") {
      status.textContent = "Already up to date.";
    } else if (result.status === "refused") {
      status.textContent =
        `Refused: parsed ${result.venues}, was ${result.previous}. Nothing written.`;
    } else {
      status.textContent = `+${result.added} added · ${result.removed} removed`
        + ` · ${result.changed} changed.`;
      if (result.added || result.removed || result.changed) {
        status.textContent += " Run ./export.py to publish.";
      }
    }

    if (result.log?.length) {
      log.textContent = result.log.join("\n");
      log.hidden = false;
    }
    renderFindings(result.findings || []);
    await Promise.all([loadMeta(), loadChanges()]);
  } catch (err) {
    status.textContent = `Sync failed: ${err.message}`;
  } finally {
    button.disabled = false;
  }
}

$("refresh").addEventListener("click", refresh);
renderFindings(null);
loadMeta();
loadChanges();
