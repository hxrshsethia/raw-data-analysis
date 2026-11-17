"use strict";

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat();
const state = { dataset: null, filters: {}, included: [], sort: null, descending: false, page: 1, size: 25 };
const valuePickers = new Map();
const valueLabels = new Map();
let files = [];
let importing = false;
let gridRequest = 0;
let filterTimer;
let chartRequest = 0;
let modalRequest = 0;
let editing = null;
let detailRow = null;
let saving = false;
let analysisReady = false;
let analysisTimer;
let analysisTicket = 0;
let lastExplore = null;
const view = { date: null, group: 0, compare: null, measure: null, period: "month", metric: "n", type: "area", rankPage: 0, groupPage: 0, workMetric: "n" };
let route = ["overview", "workbench", "records", "catalog"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("data-explorer") : null;

function node(tag, label) {
  const element = document.createElement(tag);
  if (label !== undefined) element.textContent = label;
  return element;
}

function button(label, handler) {
  const element = node("button", label);
  element.type = "button";
  element.addEventListener("click", handler);
  return element;
}

function errorAt(id, message = "") {
  $(id).textContent = message;
  $(id).hidden = !message;
}

async function request(url, payload) {
  const options = payload === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    };
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed. Try again.");
  return result;
}

function openImport() {
  $("replace-note").hidden = !state.dataset;
  $("import-progress").hidden = true;
  delete $("import-progress").dataset.state;
  $("import-dialog").showModal();
}

function updateFiles() {
  $("file-list").hidden = importing;
  $("files").closest(".picker").hidden = importing;
  $("submit-import").disabled = importing || !files.length;
  $("file-list").replaceChildren(...files.map((file, index) => {
    const item = node("li");
    const remove = button("Remove", () => {
      files.splice(index, 1);
      errorAt("import-error");
      updateFiles();
    });
    remove.setAttribute("aria-label", `Remove ${file.name}`);
    remove.disabled = importing;
    item.append(node("span", file.name), remove);
    return item;
  }));
}

function importProgress(phase, current, counts, fraction = null) {
  $("import-progress").hidden = false;
  $("import-phase").textContent = phase;
  $("import-current").textContent = current;
  $("import-counts").textContent = counts;
  $("import-bar").setAttribute("aria-label", phase);
  if (fraction === null) {
    $("import-bar").removeAttribute("value");
    $("import-percent").textContent = "";
  } else {
    const percentage = Math.max(0, Math.min(100, fraction * 100));
    $("import-bar").value = percentage;
    $("import-percent").textContent = `${Math.floor(percentage)}%`;
  }
}

function processingProgress(progress) {
  const phases = {
    inspecting: "Inspecting files…", waiting: "Waiting for the database…",
    processing: "Reading and validating CSVs…", publishing: "Saving table…", complete: "Import complete"
  };
  if (!phases[progress.phase]) return;
  const total = progress.total_sources;
  const completed = progress.completed_sources || 0;
  const files = total === null || total === undefined
    ? `${number.format(progress.inspected_files || 0)} files inspected`
    : `${number.format(completed)} of ${number.format(total)} CSV files complete`;
  const counts = `${files} · ${number.format(progress.rows || 0)} rows processed`;
  const fraction = progress.phase === "processing" && progress.total_bytes > 0
    ? progress.processed_bytes / progress.total_bytes
    : progress.phase === "complete" ? 1 : null;
  importProgress(phases[progress.phase], progress.current || "", counts, fraction);
}

function uploadFile(batch, file, onProgress) {
  return new Promise((resolve, reject) => {
    const upload = new XMLHttpRequest();
    upload.open("POST", "/api/upload");
    upload.setRequestHeader("X-Import-Batch", batch);
    upload.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    upload.upload.onprogress = (event) => onProgress(event.loaded);
    upload.onerror = () => reject(new Error(`${file.name}: upload interrupted. Retry the batch.`));
    upload.onabort = () => reject(new Error(`${file.name}: upload cancelled. Retry the batch.`));
    upload.onload = () => {
      try {
        const result = JSON.parse(upload.responseText);
        if (upload.status < 200 || upload.status >= 300) {
          reject(new Error(result.error || `${file.name}: upload failed. Try again.`));
        } else resolve(result);
      } catch {
        reject(new Error(`${file.name}: the server response could not be read. Retry the batch.`));
      }
    };
    upload.send(file);
  });
}

async function submitImport(event) {
  event.preventDefault();
  if (importing || !files.length) return;
  errorAt("import-error");
  delete $("import-progress").dataset.state;
  importing = true;
  $("files").disabled = true;
  $("close-import").disabled = true;
  updateFiles();
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  let completedBytes = 0;
  let batch;
  let polling = false;
  let pollTimer;

  async function poll() {
    if (!polling) return;
    try {
      const progress = await request(`/api/progress/${batch}`);
      if (polling) processingProgress(progress);
    } catch {
      // The import response reports completion or any final error.
    } finally {
      if (polling) pollTimer = setTimeout(poll, 400);
    }
  }

  try {
    importProgress("Preparing upload…", "", `0 of ${files.length} files uploaded`);
    const created = await request("/api/batch", { files: files.length, bytes: totalBytes });
    batch = created.batch;
    for (const [index, file] of files.entries()) {
      $("submit-import").textContent = `Uploading ${index + 1} of ${files.length}…`;
      const update = (loaded) => {
        const transferred = completedBytes + Math.min(loaded, file.size);
        const counts = `${number.format(index)} of ${number.format(files.length)} files uploaded · ${number.format(Math.floor(transferred / 1024))} of ${number.format(Math.ceil(totalBytes / 1024))} KiB sent`;
        importProgress("Uploading files…", file.name, counts, totalBytes ? transferred / totalBytes : null);
      };
      update(0);
      await uploadFile(batch, file, update);
      completedBytes += file.size;
      importProgress("Uploading files…", file.name,
        `${number.format(index + 1)} of ${number.format(files.length)} files uploaded`,
        totalBytes ? completedBytes / totalBytes : null);
    }
    $("submit-import").textContent = "Importing…";
    importProgress("Inspecting files…", "Reading CSV and ZIP directories",
      `${number.format(files.length)} files uploaded`);
    const pending = request("/api/import", { batch });
    if (created.supports_progress) {
      polling = true;
      poll();
    }
    const dataset = await pending;
    polling = false;
    clearTimeout(pollTimer);
    importProgress("Import complete", "",
      `${number.format(dataset.sources)} CSV files · ${number.format(dataset.total)} rows imported`, 1);
    files = [];
    $("import-dialog").close();
    showDataset(dataset);
    channel?.postMessage({type:"changed"});
  } catch (error) {
    polling = false;
    clearTimeout(pollTimer);
    $("import-phase").textContent = "Import stopped";
    $("import-bar").removeAttribute("value");
    $("import-percent").textContent = "";
    $("import-progress").dataset.state = "error";
    errorAt("import-error", error.message);
  } finally {
    polling = false;
    clearTimeout(pollTimer);
    if (batch) {
      try { await request("/api/cancel-batch", { batch }); }
      catch { /* The server removes staged files after import or shutdown. */ }
    }
    importing = false;
    $("files").disabled = false;
    $("close-import").disabled = false;
    $("submit-import").textContent = "Import table";
    updateFiles();
  }
}

function toggleFilters(show) {
  $("filter-panel").hidden = !show;
  $("toggle-filters").setAttribute("aria-expanded", String(show));
}

function filterLabel(group, text, control, key, index) {
  const id = `filter-${index}-${key}`;
  control.id = id;
  const label = node("label", text);
  label.htmlFor = id;
  control.addEventListener("input", () => {
    setFilter(index, key, control.value);
  });
  control.value = state.filters[index]?.[key] ?? "";
  group.append(label, control);
}

function setFilter(index, key, value) {
  state.filters[index] ??= {};
  state.filters[index][key] = value;
  state.page = 1;
  view.rankPage = view.groupPage = 0;
  updateFilterCount();
  renderChips();
  clearTimeout(filterTimer);
  gridRequest++;
  chartRequest++;
  filterTimer = setTimeout(loadGrid, 180);
}

function selectOptions(items) {
  const select = node("select");
  select.append(...items.map(([value, label]) => {
    const option = node("option", label);
    option.value = value;
    return option;
  }));
  return select;
}

function valuePicker(index, name) {
  const details = node("details");
  const summary = node("summary", "Browse values");
  const mode = selectOptions([["include", "Include selected"], ["exclude", "Exclude selected"]]);
  mode.setAttribute("aria-label", `Selection mode for ${name}`);
  mode.value = state.filters[index]?.mode || "include";
  mode.onchange = () => setFilter(index, "mode", mode.value);
  const search = node("input");
  search.type = "search";
  search.placeholder = "Search all values";
  search.setAttribute("aria-label", `Search values in ${name}`);
  const status = node("p");
  status.className = "value-status";
  status.setAttribute("role", "status");
  const list = node("ul");
  list.className = "value-choices";
  const selections = node("div");
  selections.className = "value-selection";
  const nav = node("div");
  nav.className = "value-nav";
  const pageLabel = node("span");
  pageLabel.className = "value-page";
  let page = 1;
  let ticket = 0;
  let searchTimer;

  function renderSelected() {
    const keys = state.filters[index]?.values || [];
    summary.textContent = keys.length ? `Browse values (${number.format(keys.length)} selected)` : "Browse values";
    selections.replaceChildren(...keys.slice(0, 4).map((key) => {
      const label = valueLabels.get(key) ?? key.slice(2);
      const remove = button(`${label} ×`, () => updateSelection(key, false));
      remove.setAttribute("aria-label", `Remove selected value ${label}`);
      remove.title = label;
      return remove;
    }));
    if (keys.length > 4) selections.append(node("span", `+${number.format(keys.length - 4)}`));
    if (keys.length > 1) selections.append(button("Clear selected", () => {
      setFilter(index, "values", []);
      renderSelected();
      list.querySelectorAll("input").forEach((input) => { input.checked = false; });
    }));
  }

  function updateSelection(key, checked) {
    const keys = new Set(state.filters[index]?.values || []);
    if (checked) keys.add(key);
    else keys.delete(key);
    setFilter(index, "values", Array.from(keys));
    renderSelected();
    for (const input of list.querySelectorAll("input")) {
      if (input.value === key) input.checked = checked;
    }
  }

  const previous = button("‹", () => { page--; load(); });
  previous.setAttribute("aria-label", `Previous values for ${name}`);
  const next = button("›", () => { page++; load(); });
  next.setAttribute("aria-label", `Next values for ${name}`);
  nav.append(previous, pageLabel, next);

  async function load() {
    clearTimeout(searchTimer);
    const token = ++ticket;
    const dataset = state.dataset;
    previous.disabled = true;
    next.disabled = true;
    list.replaceChildren();
    nav.hidden = true;
    status.textContent = "Finding values…";
    try {
      const result = await request("/api/values", {
        version: dataset.version, column: index, search: search.value, valuePage: page
      });
      if (token !== ticket || dataset !== state.dataset || !details.isConnected) return;
      status.textContent = `${number.format(result.missing)} missing in the full table${result.values.length ? "" : " · No matching values"}`;
      const keys = new Set(state.filters[index]?.values || []);
      list.replaceChildren(...result.values.map(([key, label, count]) => {
        const item = node("li");
        const wrapper = node("label");
        const input = node("input");
        input.type = "checkbox";
        input.value = key;
        input.checked = keys.has(key);
        input.onchange = () => {
          if (input.checked) valueLabels.set(key, label);
          updateSelection(key, input.checked);
        };
        const value = node("span", label);
        value.className = "value-label";
        value.title = label;
        const countText = node("span", number.format(count));
        countText.className = "value-count";
        countText.setAttribute("aria-label", `${number.format(count)} records`);
        wrapper.append(input, value, countText);
        item.append(wrapper);
        return item;
      }));
      pageLabel.textContent = `Page ${number.format(page)}`;
      previous.disabled = page <= 1;
      next.disabled = !result.more;
      nav.hidden = page === 1 && !result.more;
    } catch (error) {
      if (token === ticket && dataset === state.dataset) status.textContent = error.message;
    }
  }

  search.oninput = () => {
    page = 1;
    ticket++;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(load, 180);
  };
  details.ontoggle = () => {
    if (details.open) load();
  };
  details.append(summary, mode, search, selections, status, list, nav);
  renderSelected();
  return { details, search, load: () => { page = 1; if (details.open) load(); } };
}

function renderFilters({ preservePickers = false } = {}) {
  const openSearches = new Map();
  if (preservePickers) {
    for (const [index, picker] of valuePickers) {
      if (picker.details.open) openSearches.set(index, picker.search.value);
    }
  }
  valuePickers.clear();
  $("filters").replaceChildren(...state.dataset.columns.map((column, index) => {
    const group = node("fieldset");
    group.className = "filter";
    group.append(node("legend", column.name));
    if (column.type === "category") {
      const select = node("select");
      const any = node("option", "Any value");
      any.value = "";
      select.append(any, ...column.options.map((value) => {
        const option = node("option", value);
        option.value = value;
        return option;
      }));
      filterLabel(group, "Value", select, "category", index);
    } else if (column.type === "text") {
      const input = node("input");
      input.type = "search";
      filterLabel(group, "Contains", input, "text", index);
    } else {
      for (const [key, text] of [["min", "From"], ["max", "To"]]) {
        const input = node("input");
        input.type = column.type === "date" ? "date" : "number";
        if (input.type === "number") input.step = "any";
        filterLabel(group, text, input, key, index);
      }
    }
    filterLabel(group, "Missing values", selectOptions([
      ["", "Any"], ["only", "Missing only"], ["present", "Present only"]
    ]), "missing", index);
    const picker = valuePicker(index, column.name);
    if (openSearches.has(index)) {
      picker.search.value = openSearches.get(index);
      picker.details.open = true;
    }
    valuePickers.set(index, picker);
    group.append(picker.details);
    return group;
  }));
}

function updateFilterCount() {
  const count = Object.values(state.filters).filter((filters) => (
    Object.entries(filters).some(([key, value]) => key !== "mode" && (Array.isArray(value) ? value.length : value))
  )).length;
  $("toggle-filters").textContent = count ? `Filters (${count})` : "Filters";
  $("clear-filters").hidden = !count;
}

function renderHeaders() {
  const header = node("tr");
  const record = node("th", "#");
  record.scope = "col";
  record.title = "Record number";
  header.append(record);
  state.included.forEach((index) => {
    const column = state.dataset.columns[index];
    const cell = node("th");
    const control = button(column.name, () => {
      state.descending = state.sort === index && !state.descending;
      state.sort = index;
      state.page = 1;
      updateSort();
      loadGrid();
    });
    control.title = column.name;
    cell.scope = "col";
    cell.append(control);
    header.append(cell);
  });
  $("records").tHead.replaceChildren(header);
  updateSort();
}

function updateSort() {
  state.included.forEach((index, position) => {
    const column = state.dataset.columns[index];
    const cell = $("records").tHead.rows[0].cells[position + 1];
    const sorted = state.sort === index;
    if (sorted) cell.setAttribute("aria-sort", state.descending ? "descending" : "ascending");
    else cell.removeAttribute("aria-sort");
    cell.firstChild.textContent = column.name + (sorted ? (state.descending ? " ↓" : " ↑") : "");
  });
}

function renderRecord(row) {
  detailRow = row;
  $("record-title").textContent = `Record ${number.format(row[0])}`;
  $("record-fields").replaceChildren(...state.dataset.columns.flatMap((column, index) => {
    const value = row[index + 1];
    const heading = node("dt");
    const edit = button("Edit", () => showEditor(row[0], index, value));
    edit.setAttribute("aria-label", `Edit ${column.name}`);
    heading.append(node("span", column.name), edit);
    const description = node("dd", value);
    if (value === "") description.append(node("em", "Missing"));
    return [heading, description];
  }));
}

async function openRecord(id) {
  const dataset = state.dataset;
  const token = ++modalRequest;
  errorAt("error");
  try {
    const row = await request("/api/record", { id, version: dataset.version });
    if (state.dataset !== dataset || token !== modalRequest || document.querySelector("dialog[open]")) return;
    renderRecord(row);
    $("record-dialog").showModal();
    $("record-dialog").scrollTop = 0;
  } catch (error) {
    if (state.dataset === dataset && token === modalRequest) errorAt("error", error.message);
  }
}

async function openCell(id, column) {
  const dataset = state.dataset;
  const token = ++modalRequest;
  errorAt("error");
  try {
    const [value] = await request("/api/record", { id, column, version: dataset.version });
    if (state.dataset !== dataset || token !== modalRequest || document.querySelector("dialog[open]")) return;
    showEditor(id, column, value);
  } catch (error) {
    if (state.dataset === dataset && token === modalRequest) errorAt("error", error.message);
  }
}

function showEditor(id, column, value) {
  $("edit-title").textContent = `Edit ${state.dataset.columns[column].name} · Record ${number.format(id)}`;
  $("edit-missing").checked = value === "";
  $("edit-value").value = value;
  editing = { id, column, original: value, initialText: $("edit-value").value };
  $("edit-value").disabled = $("edit-missing").checked;
  errorAt("edit-error");
  $("edit-dialog").showModal();
  $(value === "" ? "edit-missing" : "edit-value").focus();
}

function setSaving(active) {
  saving = active;
  $("edit-value").disabled = active || $("edit-missing").checked;
  $("edit-missing").disabled = active;
  $("close-edit").disabled = active;
  $("save-cell").disabled = active;
  $("save-cell").textContent = active ? "Saving…" : "Save cell";
}

async function saveCell(event) {
  event.preventDefault();
  if (!editing || saving) return;
  const { id, column, original, initialText } = editing;
  const text = $("edit-value").value;
  const value = $("edit-missing").checked ? "" : (text === initialText ? original : text);
  const dataset = state.dataset;
  errorAt("edit-error");
  setSaving(true);
  try {
    const updated = await request("/api/edit", { id, column, value, version: dataset.version });
    if (state.dataset !== dataset) return;
    state.dataset = updated;
    channel?.postMessage({ type: "changed" });
    if ($("record-dialog").open && detailRow?.[0] === id) {
      detailRow[column + 1] = value;
      renderRecord(detailRow);
    }
    const previous = dataset.columns[column];
    const current = updated.columns[column];
    let cleared = false;
    const filters = state.filters[column] || {};
    if (previous.type !== current.type) {
      for (const key of ["min", "max", "category", "text"]) {
        if (filters[key]) cleared = true;
        delete filters[key];
      }
    } else if (filters.category && !current.options.includes(filters.category)) {
      delete filters.category;
      cleared = true;
    }
    if (JSON.stringify(previous) !== JSON.stringify(current)) renderFilters({ preservePickers: true });
    else for (const picker of valuePickers.values()) picker.load();
    updateFilterCount();
    $("edit-dialog").close();
    $("edit-status").textContent = cleared
      ? "Cell saved. The quick filter for the edited column was cleared because its available values or type changed."
      : "Cell saved.";
    renderExplorerControls(false);
    loadGrid();
  } catch (error) {
    errorAt("edit-error", error.message);
  } finally {
    setSaving(false);
  }
}

function renderRows(rows) {
  $("records").tBodies[0].replaceChildren(...rows.map(([id, ...values]) => {
    const row = node("tr");
    const record = node("td");
    const control = button(number.format(id), () => openRecord(id));
    control.setAttribute("aria-label", `Open record ${id}`);
    record.append(control);
    row.append(record, ...values.map((value, position) => {
      const index = state.included[position];
      const cell = node("td");
      const edit = button(value === "" ? "Missing" : value, () => openCell(id, index));
      edit.className = value === "" ? "cell-edit empty-value" : "cell-edit";
      edit.setAttribute("aria-label", `Edit ${state.dataset.columns[index].name}, record ${id}${value === "" ? ", missing" : ""}`);
      cell.append(edit);
      return cell;
    }));
    return row;
  }));
}

async function loadGrid() {
  clearTimeout(filterTimer);
  const token = ++gridRequest;
  const { dataset, ...query } = state;
  if (!dataset) return;
  errorAt("error");
  try {
    const result = await request("/api/grid", { ...query, version: dataset.version });
    if (token !== gridRequest) return;
    state.page = result.page;
    renderRows(result.rows);
    $("no-results").hidden = Boolean(result.rows.length);
    $("no-results").textContent = dataset.total
      ? "No records match. Adjust your filters to see more rows." : "The imported files contain headers only.";
    $("result-count").textContent = `${number.format(result.total)} of ${number.format(dataset.total)} records`;
    $("page-status").textContent = `Page ${number.format(result.page)} of ${number.format(result.pages)}`;
    $("previous").disabled = result.page <= 1;
    $("next").disabled = result.page >= result.pages;
    $("records").closest(".scroll").scrollTop = 0;
    loadExplore();
  } catch (error) {
    if (token === gridRequest) errorAt("error", error.message);
  }
}

function showDataset(dataset) {
  gridRequest++;
  chartRequest++;
  modalRequest++;
  const included = dataset ? dataset.columns.map((_, index) => index) : [];
  Object.assign(state, { dataset, included, filters: {}, sort: null, descending: false, page: 1 });
  valueLabels.clear();
  $("edit-status").textContent = "";
  analysisReady = false;
  lastExplore = null;
  analysisTicket++;
  clearTimeout(analysisTimer);
  $("empty").hidden = Boolean(dataset);
  $("workspace").hidden = !dataset;
  $("analysis-status").hidden = !dataset;
  if (!dataset) {
    $("description").textContent = "Import files to start browsing your data.";
    return;
  }
  $("description").textContent = `${number.format(dataset.total)} rows · ${number.format(dataset.columns.length)} columns · ${number.format(dataset.sources)} CSV ${dataset.sources === 1 ? "source" : "sources"}`;
  Object.assign(view, {date: null, group: 0, compare: null, measure: null, metric:"n", rankPage:0, groupPage:0, workMetric:"n"});
  toggleFilters(false);
  updateFilterCount();
  renderChips();
  renderFilters();
  renderHeaders();
  renderRows([]);
  renderExplorerControls(true);
  renderCatalog();
  go(route);
  loadGrid();
  prepareAnalysis();
}

function openColumns() {
  const included = new Set(state.included);
  $("column-options").replaceChildren(...state.dataset.columns.map((column, index) => {
    const label = node("label");
    label.className = "check";
    const input = node("input");
    input.type = "checkbox";
    input.value = index;
    input.checked = included.has(index);
    input.onchange = updateColumnAction;
    label.append(input, node("span", column.name));
    return label;
  }));
  updateColumnAction();
  $("columns-dialog").showModal();
}

function updateColumnAction() {
  $("apply-columns").disabled = !$("column-options").querySelector("input:checked");
}

function applyColumns() {
  state.included = Array.from($("column-options").querySelectorAll("input:checked"), (input) => Number(input.value));
  if (!state.included.includes(state.sort)) {
    state.sort = null;
    state.descending = false;
  }
  state.page = 1;
  $("columns-dialog").close();
  renderHeaders();
  renderRows([]);
  renderExplorerControls(false);
  loadGrid();
}

function go(next) {
  route = ["overview","workbench","records","catalog"].includes(next) ? next : "overview";
  const titles = {overview:"Overview",workbench:"Workbench",records:"Records",catalog:"Data catalog"};
  $("page-title").textContent = titles[route];
  for (const name of Object.keys(titles)) $(name+"-page").hidden = route !== name;
  document.querySelectorAll(".sidebar nav a").forEach(link => {
    if (link.hash === "#"+route) link.setAttribute("aria-current","page");
    else link.removeAttribute("aria-current");
  });
  $("open-columns").hidden = route !== "records";
  if (state.dataset) {
    if (route === "records") loadGrid();
    else if (route === "catalog") renderCatalog();
    else loadExplore();
  }
}

function renderChips() {
  const chips = [];
  for (const [rawIndex, filter] of Object.entries(state.filters)) {
    const active = Object.entries(filter).filter(([key,value]) => key !== "mode" && (Array.isArray(value) ? value.length : value));
    if (!active.length) continue;
    const index = Number(rawIndex);
    const label = state.dataset.columns[index].name;
    const descriptions = active.map(([key,value]) => {
      if (key === "values") return `${filter.mode === "exclude" ? "exclude" : "include"} ${value.length} values`;
      if (key === "missing") return value === "only" ? "missing" : "present";
      return `${key} ${value}`;
    });
    const chip = button(label+" · "+descriptions.join(", ")+" ×", () => {
      delete state.filters[index];
      state.page = 1;
      view.rankPage = view.groupPage = 0;
      renderFilters({preservePickers:true});
      updateFilterCount();
      renderChips();
      loadGrid();
    });
    chip.title = "Clear filter for "+label;
    chips.push(chip);
  }
  $("selection-chips").replaceChildren(...chips);
}

function renderExplorerControls(reset) {
  const columns = state.dataset.columns;
  const all = columns.map((column,index) => [String(index),column.name]);
  const dates = all.filter(([index]) => columns[Number(index)].type === "date");
  const numbers = all.filter(([index]) => columns[Number(index)].type === "number");
  if (reset || view.date === null || columns[view.date]?.type !== "date") view.date = dates.length ? Number(dates[0][0]) : null;
  if (reset) view.group = Math.max(0,columns.findIndex(column => column.type === "category"));
  if (view.measure !== null && columns[view.measure]?.type !== "number") {
    view.measure = null;
    view.metric = "n";
    view.workMetric = "n";
  }

  function options(id, items, selected) {
    const select = selectOptions(items);
    $(id).replaceChildren(...select.options);
    $(id).value = selected === null ? "" : String(selected);
    $(id).disabled = !analysisReady || !items.length;
  }
  options("trend-date",dates.length ? dates : [["","No date field"]],view.date);
  options("overview-group",all,view.group);
  options("work-group",all,view.group);
  options("work-compare",[["","None"],...all.filter(([index]) => Number(index) !== view.group)],view.compare);
  options("trend-measure",[["","Choose a numeric measure"],...numbers],view.measure);
  options("work-measure",[["","Records only"],...numbers],view.measure);
  $("trend-period").value = view.period;
  $("trend-metric").value = view.metric;
  $("trend-type").value = view.type;
  $("work-metric").value = view.workMetric;
  $("work-metric").disabled = !analysisReady || !numbers.length;
  $("trend-period").disabled = !analysisReady || view.date === null;
  $("trend-type").disabled = !analysisReady || view.date === null;
  $("trend-metric").disabled = !analysisReady || !numbers.length;
}

async function prepareAnalysis() {
  const ticket = ++analysisTicket;
  const dataset = state.dataset;
  try {
    const status = await request("/api/analysis-status");
    if (ticket !== analysisTicket || state.dataset !== dataset) return;
    if (status.phase === "ready") {
      analysisReady = true;
      state.dataset.columns = status.columns;
      $("analysis-status").hidden = true;
      renderFilters({preservePickers:true});
      renderHeaders();
      renderExplorerControls(true);
      renderCatalog();
      loadGrid();
      return;
    }
    $("analysis-status").hidden = false;
    $("analysis-message").textContent = (status.message || "Preparing analysis") +
      (status.phase === "copying" ? ` · ${number.format(status.rows)} of ${number.format(status.total)} rows` : "");
    if (status.phase === "copying" && status.total) $("analysis-progress").value = status.rows/status.total*100;
    else $("analysis-progress").removeAttribute("value");
    if (status.phase !== "error") analysisTimer = setTimeout(prepareAnalysis,1000);
  } catch (error) {
    if (ticket !== analysisTicket) return;
    $("analysis-message").textContent = error.message;
    analysisTimer = setTimeout(prepareAnalysis,2000);
  }
}

function metricName() {
  return {n:"Records",sum:"Total",avg:"Average",min:"Minimum",max:"Maximum"}[view.metric];
}

function renderOverview(data) {
  const summary = data.summary;
  const cards = [
    ["Matching records",number.format(summary.n)],
    ["Share of table",new Intl.NumberFormat(undefined,{style:"percent",maximumFractionDigits:1}).format(data.total ? summary.n/data.total : 0)],
    [view.measure === null ? "CSV sources" : "Average · "+state.dataset.columns[view.measure].name,
      view.measure === null ? number.format(state.dataset.sources) : summary.avg === null ? "—" : new Intl.NumberFormat(undefined,{maximumFractionDigits:2}).format(summary.avg)],
    [view.date === null ? "Included columns" : "Period range", view.date === null ? number.format(state.included.length) :
      data.trend.length ? data.trend[0].start+" – "+data.trend.at(-1).end : "—"]
  ];
  $("summary-metrics").replaceChildren(...cards.map(([label,value]) => {
    const card=node("div");card.className="kpi";card.append(node("span",label),node("strong",value));return card;
  }));
  if (view.date === null) {
    $("trend-render").replaceChildren(node("p","No ISO date or timestamp column is available. Use Workbench to compare groups."));
  } else {
    ExplorerCharts.time($("trend-render"),data.trend,{
      metric:view.metric,type:view.type,period:view.period,metricLabel:metricName(),
      label:metricName()+" by "+view.period,
      onSelect:item=>{
        const filter=state.filters[view.date] || {};
        state.filters[view.date]={...filter,min:item.start,max:item.end};
        applyLinkedSelection();
      }
    });
  }
  const rows = data.ranking.rows;
  const maximum = Math.max(1,...rows.map(row => row.n));
  $("ranking-bars").replaceChildren(...rows.map(row => {
    const control=button("",()=>selectGroup(data.ranking.columns,row));
    control.className="bar-row";
    const label=row.values.map(value=>value === "" ? "Missing" : value).join(" / ");
    control.title=label;
    const track=node("span");track.className="rank-track";
    const fill=node("i");fill.style.width=(row.n/maximum*100)+"%";track.append(fill);
    control.append(node("span",label),node("strong",number.format(row.n)),track);
    return control;
  }));
  if (!rows.length) $("ranking-bars").append(node("p","No matching groups."));
  $("rank-page").textContent="Page "+number.format(view.rankPage+1);
  $("rank-previous").disabled=view.rankPage===0;
  $("rank-next").disabled=!data.ranking.more;
}

function renderWorkbench(data) {
  const indices=data.ranking.columns;
  const heading=node("tr");
  heading.append(...indices.map(index=>node("th",state.dataset.columns[index].name)),node("th","Records"));
  if(view.measure!==null) heading.append(...["Total","Average","Minimum","Maximum"].map(label=>node("th",label)));
  $("group-table").tHead.replaceChildren(heading);
  $("group-table").tBodies[0].replaceChildren(...data.ranking.rows.map(item=>{
    const row=node("tr");
    item.values.forEach(value=>{
      const cell=node("td");
      const control=button(value===""?"Missing":value,()=>selectGroup(indices,item,true));
      control.className="group-link";cell.append(control);row.append(cell);
    });
    row.append(node("td",number.format(item.n)));
    if(view.measure!==null) for(const key of ["sum","avg","min","max"]){
      row.append(node("td",item[key]===null?"—":new Intl.NumberFormat(undefined,{maximumFractionDigits:4}).format(item[key])));
    }
    return row;
  }));
  $("work-status").textContent=number.format(data.summary.n)+" matching records · all rows contribute to groups";
  $("group-page").textContent="Page "+number.format(view.groupPage+1);
  $("group-previous").disabled=view.groupPage===0;
  $("group-next").disabled=!data.ranking.more;
}

function applyLinkedSelection() {
  state.page=1;view.rankPage=view.groupPage=0;
  renderFilters({preservePickers:true});
  updateFilterCount();renderChips();loadGrid();
}

async function selectGroup(indices, row, records = false) {
  for (const [position,index] of indices.entries()) {
    const value=row.values[position];
    if(value==="") state.filters[index]={missing:"only"};
    else {
      let key;
      if(value.length<=120) key="v:"+value;
      else {
        const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(value));
        key="h:"+Array.from(new Uint8Array(digest),byte=>byte.toString(16).padStart(2,"0")).join("");
      }
      valueLabels.set(key,value.slice(0,120));
      state.filters[index]={values:[key],mode:"include"};
    }
  }
  applyLinkedSelection();
  if(records) location.hash="records";
}

async function loadExplore() {
  if(!state.dataset || !analysisReady || !["overview","workbench"].includes(route)) return;
  const token=++chartRequest;
  const payload={version:state.dataset.version,filters:state.filters,date:view.date,period:view.period,
    group:view.group,compare:route==="workbench"?view.compare:null,measure:view.measure,
    groupPage:route==="workbench"?view.groupPage:view.rankPage,groupSize:route==="workbench"?50:8,rankMetric:route==="workbench"?view.workMetric:"n"};
  $("explore-status").textContent="Updating charts…";
  $("work-status").textContent="Updating groups…";
  try {
    const data=await request("/api/explore",payload);
    if(token!==chartRequest)return;
    lastExplore=data;
    $("explore-status").textContent="";
    if(route==="overview")renderOverview(data);else renderWorkbench(data);
  } catch(error){
    if(token!==chartRequest)return;
    $("explore-status").textContent=error.message;
    $("work-status").textContent=error.message;
  }
}

function renderCatalog() {
  const dataset=state.dataset;
  if(!dataset)return;
  const inventory=dataset.inventory || [];
  if(!inventory.length){
    $("source-inventory").replaceChildren(node("p",number.format(dataset.sources)+" CSV sources. Filenames were not retained by the earlier import version."));
  } else {
    const list=node("ul");
    for(const file of inventory)list.append(node("li",file.name+" · "+number.format(file.rows)+" rows"));
    $("source-inventory").replaceChildren(list);
  }
  $("field-catalog").tBodies[0].replaceChildren(...dataset.columns.map((column,index)=>{
    const row=node("tr");
    row.append(node("td",column.name),node("td",{date:"Date / timestamp",number:"Number",category:"Category",text:"Text"}[column.type]),
      node("td",dataset.missing ? number.format(dataset.missing[index]) : "—"));
    return row;
  }));
}

async function downloadCsv(scope, source = null) {
  const controls = [$("download-master"), $("download-filtered"), $("download-groups"), $("clear-export")];
  if ($("clear-dialog").open) controls.push($("confirm-clear"));
  controls.forEach((control) => { control.disabled = true; });
  const clicked = source || (scope === "groups" ? $("download-groups") : scope === "master" ? $("download-master") : $("download-filtered"));
  const label = clicked.textContent;
  clicked.textContent = "Preparing CSV…";
  const errorTarget = $("clear-dialog").open ? "clear-error" : $("download-dialog").open ? "download-error" : "error";
  errorAt(errorTarget);
  try {
    const payload = scope === "master" ? { scope } : {
      scope, filters: state.filters, included: state.included, sort: state.sort, descending: state.descending,
      exploration:{group:view.group,compare:view.compare,measure:view.measure,rankMetric:view.workMetric}
    };
    const result = await request("/api/export", { ...payload, version: state.dataset.version });
    const link = node("a");
    link.href = `/api/download/${result.token}`;
    link.download = result.filename;
    document.body.append(link);
    link.click();
    link.remove();
    if ($("download-dialog").open) $("download-dialog").close();
  } catch (error) {
    errorAt(errorTarget, error.message);
  } finally {
    controls.forEach((control) => { control.disabled = false; });
    clicked.textContent = label;
  }
}

$("choose-files").onclick = openImport;
$("open-import").onclick = openImport;
$("close-import").onclick = () => $("import-dialog").close();
$("import-dialog").addEventListener("cancel", (event) => {
  if (importing) event.preventDefault();
});
$("close-record").onclick = () => $("record-dialog").close();
$("files").addEventListener("change", (event) => {
  for (const file of event.target.files) files.push(file);
  event.target.value = "";
  errorAt("import-error");
  updateFiles();
});
$("import-form").addEventListener("submit", submitImport);
$("toggle-filters").onclick = () => toggleFilters($("filter-panel").hidden);
$("clear-filters").onclick = () => {
  state.filters = {};
  state.page = 1;
  valueLabels.clear();
  chartRequest++;
  renderFilters({ preservePickers: true });
  updateFilterCount();
  renderChips();
  view.rankPage=view.groupPage=0;
  $("toggle-filters").focus();
  loadGrid();
};
$("page-size").onchange = (event) => {
  state.size = Number(event.target.value);
  state.page = 1;
  loadGrid();
};
$("previous").onclick = () => { state.page--; loadGrid(); };
$("next").onclick = () => { state.page++; loadGrid(); };

$("edit-form").addEventListener("submit", saveCell);
$("edit-missing").onchange = () => {
  $("edit-value").disabled = $("edit-missing").checked;
  if (!$("edit-missing").checked) $("edit-value").focus();
};
$("close-edit").onclick = () => $("edit-dialog").close();
$("edit-dialog").addEventListener("cancel", (event) => {
  if (saving) event.preventDefault();
});
$("edit-dialog").addEventListener("close", () => { editing = null; });
$("open-columns").onclick = openColumns;
$("close-columns").onclick = () => $("columns-dialog").close();
$("show-all-columns").onclick = () => {
  $("column-options").querySelectorAll("input").forEach((input) => { input.checked = true; });
  updateColumnAction();
};
$("apply-columns").onclick = applyColumns;

$("open-download").onclick = () => {
  errorAt("download-error");
  $("download-dialog").showModal();
};
$("close-download").onclick = () => $("download-dialog").close();
$("download-master").onclick = () => downloadCsv("master");
$("download-filtered").onclick = () => downloadCsv("filtered");

window.addEventListener("hashchange",()=>go(location.hash.slice(1)));
$("trend-date").onchange=event=>{view.date=event.target.value===""?null:Number(event.target.value);loadExplore();};
$("trend-period").onchange=event=>{view.period=event.target.value;loadExplore();};
$("trend-type").onchange=event=>{view.type=event.target.value;if(lastExplore)renderOverview(lastExplore);};
$("trend-metric").onchange=event=>{
  view.metric=event.target.value;
  if(view.metric!=="n"&&view.measure===null){
    const index=state.dataset.columns.findIndex(column=>column.type==="number");
    view.measure=index<0?null:index;renderExplorerControls(false);loadExplore();
  } else if(lastExplore)renderOverview(lastExplore);
};
for(const id of ["trend-measure","work-measure"])$(id).onchange=event=>{
  view.measure=event.target.value===""?null:Number(event.target.value);
  if(view.measure===null){view.metric="n";view.workMetric="n";}
  renderExplorerControls(false);loadExplore();
};
for(const id of ["overview-group","work-group"])$(id).onchange=event=>{
  view.group=Number(event.target.value);view.rankPage=view.groupPage=0;
  if(view.compare===view.group)view.compare=null;
  renderExplorerControls(false);loadExplore();
};
$("work-metric").onchange=event=>{
  view.workMetric=event.target.value;
  if(view.workMetric!=="n"&&view.measure===null){
    const index=state.dataset.columns.findIndex(column=>column.type==="number");
    view.measure=index<0?null:index;
  }
  view.groupPage=0;
  renderExplorerControls(false);
  loadExplore();
};
$("work-compare").onchange=event=>{view.compare=event.target.value===""?null:Number(event.target.value);view.groupPage=0;loadExplore();};
$("rank-previous").onclick=()=>{view.rankPage--;loadExplore();};
$("rank-next").onclick=()=>{view.rankPage++;loadExplore();};
$("group-previous").onclick=()=>{view.groupPage--;loadExplore();};
$("group-next").onclick=()=>{view.groupPage++;loadExplore();};
$("download-groups").onclick=()=>downloadCsv("groups");
$("clear-dataset").onclick=()=>{errorAt("clear-error");$("clear-dialog").showModal();};
$("close-clear").onclick=()=>$("clear-dialog").close();
$("clear-export").onclick=()=>downloadCsv("master",$("clear-export"));
$("confirm-clear").onclick=async()=>{
  $("confirm-clear").disabled=true;
  errorAt("clear-error");
  try{
    const outcome=await request("/api/clear",{version:state.dataset.version});
    $("clear-dialog").close();
    channel?.postMessage({type:"cleared"});
    showDataset(null);
    if(outcome.warning)errorAt("error",outcome.warning);
  }catch(error){errorAt("clear-error",error.message);}
  finally{$("confirm-clear").disabled=false;}
};
if(channel)channel.onmessage=async event=>{
  if(!["changed","cleared"].includes(event.data?.type))return;
  for(const dialog of document.querySelectorAll("dialog[open]"))dialog.close();
  try{
    const dataset=await request("/api/dataset");
    showDataset(dataset);
    errorAt("error",event.data.type==="cleared"?"The dataset was cleared in another tab.":"The table changed in another tab. The current version is loaded.");
  }catch(error){errorAt("error",error.message);}
};

go(route);
request("/api/dataset").then(showDataset).catch((error) => errorAt("error", error.message));
