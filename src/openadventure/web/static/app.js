import { ApiError, api } from "/static/api.js";
import {
  formatDate,
  node,
  renderInspector,
  renderMarkdown,
  safeMediaUrl,
  stateCounts,
} from "/static/render.js";

const $ = (id) => document.getElementById(id);

const dom = {
  libraryView: $("library-view"),
  playView: $("play-view"),
  libraryStatus: $("library-status"),
  campaignGrid: $("campaign-grid"),
  emptyLibrary: $("empty-library"),
  newCampaignButton: $("new-campaign-button"),
  backButton: $("back-button"),
  campaignTitle: $("campaign-title"),
  campaignSubtitle: $("campaign-subtitle"),
  campaignModeBadge: $("campaign-mode-badge"),
  connectionChip: $("connection-chip"),
  connectionLabel: $("connection-label"),
  connectionNotice: $("connection-notice"),
  connectionNoticeTitle: $("connection-notice-title"),
  connectionNoticeMessage: $("connection-notice-message"),
  connectionSettingsButton: $("connection-settings-button"),
  settingsButton: $("settings-button"),
  transcriptScroll: $("transcript-scroll"),
  transcript: $("transcript"),
  storyEmpty: $("story-empty"),
  jumpLatest: $("jump-latest"),
  turnStatus: $("turn-status"),
  turnStatusText: $("turn-status-text"),
  composerForm: $("composer-form"),
  composerInput: $("composer-input"),
  sendButton: $("send-button"),
  cancelButton: $("cancel-turn-button"),
  composerHint: $("composer-hint"),
  quietControl: $("quiet-control"),
  quietCheckbox: $("quiet-checkbox"),
  slashMenu: $("slash-menu"),
  inspector: $("inspector"),
  inspectorContent: $("inspector-content"),
  inspectorToggle: $("inspector-toggle"),
  inspectorClose: $("inspector-close"),
  inspectorOverlay: $("inspector-overlay"),
  partyCount: $("party-count"),
  clockCount: $("clock-count"),
  createDialog: $("create-dialog"),
  createForm: $("create-form"),
  createName: $("create-name"),
  createPremise: $("create-premise"),
  createError: $("create-error"),
  createSubmit: $("create-submit"),
  sourceOptions: $("source-options"),
  moduleOptions: $("module-options"),
  settingsDialog: $("settings-dialog"),
  settingsForm: $("settings-form"),
  settingsMode: $("settings-mode"),
  settingsEffort: $("settings-effort"),
  settingsThinking: $("settings-thinking"),
  settingsVerbosity: $("settings-verbosity"),
  settingsContext: $("settings-context"),
  settingsConnection: $("settings-connection"),
  settingsError: $("settings-error"),
  settingsSubmit: $("settings-submit"),
  rollDialog: $("roll-dialog"),
  rollForm: $("roll-form"),
  rollExpression: $("roll-expression"),
  rollError: $("roll-error"),
  rollSubmit: $("roll-submit"),
  toastRegion: $("toast-region"),
  globalStatus: $("global-status"),
};

const shortcuts = [
  { command: "/roll", template: "/roll d20", description: "Roll dice locally" },
  { command: "/undo", template: "/undo", description: "Take back the last turn" },
  { command: "/retry", template: "/retry", description: "Retry the last turn" },
  { command: "/recap", template: "/recap", description: "Recall the story so far" },
  { command: "/compact", template: "/compact", description: "Update the rolling story summary" },
  { command: "/btw", template: "/btw ", description: "Ask an off-record rules question" },
  { command: "/sudo", template: "/sudo ", description: "Guide the GM out of character" },
];

const store = {
  bootstrap: { campaigns: [], books: [] },
  slug: null,
  campaign: null,
  settings: {},
  provider: null,
  history: [],
  gameState: {},
  busy: false,
  messageKind: "normal",
  inspectorTab: localStorage.getItem("openadventure.inspectorTab") || "party",
  streamController: null,
  activeAssistant: null,
  toolCards: new Map(),
  slashIndex: 0,
  visibleShortcuts: [],
  pollTimer: null,
  pollInFlight: false,
  pollHadError: false,
};

function errorMessage(error) {
  if (error instanceof ApiError) return error.message;
  if (error?.name === "AbortError") return "The request was cancelled.";
  return error?.message || "Something went wrong.";
}

function toast(message, kind = "info") {
  if (!message) return;
  const item = node("div", `toast${kind === "error" ? " is-error" : ""}`, message);
  dom.toastRegion.append(item);
  window.setTimeout(() => item.remove(), kind === "error" ? 6500 : 4200);
}

function announce(message) {
  dom.globalStatus.textContent = "";
  window.requestAnimationFrame(() => {
    dom.globalStatus.textContent = message;
  });
}

function setTurnStatus(message, state = "ready") {
  dom.turnStatusText.textContent = message;
  dom.turnStatus.classList.toggle("is-ready", state === "ready");
  dom.turnStatus.classList.toggle("is-working", state === "working");
  dom.turnStatus.classList.toggle("is-error", state === "error");
}

function setBusy(busy, label = "The GM is thinking…") {
  store.busy = busy;
  dom.composerInput.disabled = busy;
  dom.sendButton.hidden = busy;
  dom.cancelButton.hidden = !busy;
  document.querySelectorAll(".quick-action").forEach((button) => {
    button.disabled = busy;
  });
  if (busy) {
    setTurnStatus(label, "working");
    stopEventPolling();
  } else {
    setTurnStatus(connectionInfo().connected === false ? "GM connection required" : "Ready", connectionInfo().connected === false ? "error" : "ready");
    startEventPolling();
  }
}

function isNearTranscriptEnd() {
  const remaining =
    dom.transcriptScroll.scrollHeight - dom.transcriptScroll.scrollTop - dom.transcriptScroll.clientHeight;
  return remaining < 120;
}

function scrollToLatest(behavior = "smooth") {
  dom.transcriptScroll.scrollTo({ top: dom.transcriptScroll.scrollHeight, behavior });
  dom.jumpLatest.hidden = true;
}

function appendTranscript(element, { forceScroll = false } = {}) {
  const shouldScroll = forceScroll || isNearTranscriptEnd();
  if (dom.storyEmpty.isConnected) dom.storyEmpty.remove();
  dom.transcript.append(element);
  if (shouldScroll) {
    window.requestAnimationFrame(() => scrollToLatest(forceScroll ? "auto" : "smooth"));
  } else {
    dom.jumpLatest.hidden = false;
  }
}

function messageMeta(label, time = "") {
  const meta = node("div", "message-meta");
  meta.append(node("span", "message-label", label));
  if (time) meta.append(node("span", "", time));
  return meta;
}

function userMessage(text, kind = "normal", label = null) {
  const article = node("article", `message message-user${kind === "aside" ? " is-aside" : ""}${kind === "steer" ? " is-steer" : ""}`);
  const kindLabel = label || (kind === "aside" ? "Aside" : kind === "steer" ? "GM guidance" : "You");
  article.append(messageMeta(kindLabel));
  article.append(node("div", "message-body", text));
  return article;
}

function assistantMessage(label = "Game Master", text = "") {
  const article = node("article", "message message-assistant");
  article.append(messageMeta(label));
  const body = node("div", "message-body");
  renderMarkdown(body, text);
  article.append(body);
  return { article, body, text };
}

function systemMessage(message, { error = false, label = "Table" } = {}) {
  const article = node("article", `message message-system${error ? " is-error" : ""}`);
  article.append(messageMeta(label));
  article.append(node("div", "message-body", message));
  return article;
}

function readableHistoryTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);
}

function historyRole(entry) {
  if (entry.role === "user" || entry.type === "user_message") return "user";
  if (entry.role === "assistant" || entry.role === "gm" || entry.type === "gm_message") return "assistant";
  return "system";
}

function historyText(entry) {
  return entry.text ?? entry.data?.text ?? entry.message ?? entry.summary ?? "";
}

function renderHistory(history = store.history) {
  store.history = Array.isArray(history) ? history : [];
  dom.transcript.replaceChildren();
  let visible = 0;

  for (const entry of store.history) {
    if (!entry || (entry.private && store.campaign?.mode === "gm" && entry.type !== "roll")) continue;
    if (entry.type === "roll") {
      dom.transcript.append(createRollCard(entry));
      visible += 1;
      continue;
    }
    const text = historyText(entry);
    if (!text) continue;
    const role = historyRole(entry);
    if (role === "user") {
      const kind = entry.kind || (entry.sudo ? "steer" : "normal");
      const item = userMessage(text, kind);
      item.querySelector(".message-meta").append(node("span", "", readableHistoryTime(entry.ts)));
      dom.transcript.append(item);
      visible += 1;
    } else if (role === "assistant") {
      const item = assistantMessage("Game Master", text);
      item.article.querySelector(".message-meta").append(node("span", "", readableHistoryTime(entry.ts)));
      dom.transcript.append(item.article);
      visible += 1;
    } else if (["action_message", "module_transition", "engine_error"].includes(entry.type)) {
      dom.transcript.append(systemMessage(text, { error: entry.type === "engine_error" }));
      visible += 1;
    }
  }

  if (!visible) dom.transcript.append(dom.storyEmpty);
  dom.jumpLatest.hidden = true;
  window.requestAnimationFrame(() => scrollToLatest("auto"));
}

function beginAssistant(label = "Game Master") {
  if (store.activeAssistant) finishAssistant();
  store.activeAssistant = assistantMessage(label);
  store.activeAssistant.body.classList.add("streaming-caret");
  appendTranscript(store.activeAssistant.article, { forceScroll: true });
}

function updateAssistant(delta) {
  if (!store.activeAssistant) beginAssistant();
  store.activeAssistant.text += delta || "";
  renderMarkdown(store.activeAssistant.body, store.activeAssistant.text);
  store.activeAssistant.body.classList.add("streaming-caret");
  if (isNearTranscriptEnd()) window.requestAnimationFrame(() => scrollToLatest());
}

function finishAssistant() {
  if (!store.activeAssistant) return;
  store.activeAssistant.body.classList.remove("streaming-caret");
  store.activeAssistant = null;
}

function friendlyToolName(value) {
  if (!value) return "table tool";
  return String(value)
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function toolCard(event, pending = false) {
  const card = node("article", `event-card tool-card${event.ok === false ? " is-error" : ""}${event.ok === true ? " is-success" : ""}`);
  const title = node("div", "event-card-title");
  title.append(node("span", "", pending ? "⚙" : event.ok === false ? "×" : "✓"));
  const name = store.campaign?.mode === "gm" ? friendlyToolName(event.name) : friendlyToolName(event.name);
  title.append(node("span", "", pending ? `${name}…` : name));
  card.append(title);
  if (store.campaign?.mode === "assistant") {
    const detail = event.result_summary || event.args_summary || "";
    if (detail) card.append(node("p", "event-card-detail", detail));
  }
  return card;
}

function createRollCard(event) {
  const privateRoll = event.private && store.campaign?.mode === "gm";
  const card = node("article", "dice-card");
  card.append(node("div", "dice-total", privateRoll ? "?" : event.total ?? "?"));
  const copy = node("div", "dice-copy");
  copy.append(node("strong", "", privateRoll ? "Secret roll" : event.expression || "Dice roll"));
  const details = [];
  if (!privateRoll && event.detail) details.push(event.detail);
  if (!privateRoll && event.reason) details.push(event.reason);
  if (details.length) copy.append(node("p", "", details.join(" · ")));
  if (!privateRoll && event.outcome) copy.append(node("span", "dice-outcome", event.outcome));
  card.append(copy);
  return card;
}

function renderRoll(event) {
  appendTranscript(createRollCard(event));
}

function renderImage(event) {
  const url = safeMediaUrl(event.path || event.url);
  if (!url) return;
  const caption = event.caption || "Generated scene image";
  const figure = node("figure", "media-card");
  const image = node("img");
  image.src = url;
  image.alt = caption;
  image.loading = "lazy";
  image.addEventListener("load", () => {
    if (isNearTranscriptEnd()) scrollToLatest();
  });
  figure.append(image, node("figcaption", "", caption));
  appendTranscript(figure);
}

function renderMusic(event) {
  const card = node("section", "media-card");
  const caption = node("div", "media-caption", event.type === "music_stopped" ? "Music stopped" : `Now playing: ${event.mood || event.track || "campaign ambience"}`);
  card.append(caption);
  const url = safeMediaUrl(event.url || event.path || event.track);
  if (url && event.type !== "music_stopped") {
    const audio = node("audio");
    audio.controls = true;
    audio.loop = true;
    audio.src = url;
    card.append(audio);
    audio.play().catch(() => {
      // Browsers may require the player to press play after a user gesture.
    });
  }
  appendTranscript(card);
}

function renderEngineError(event) {
  const card = systemMessage(event.message || "The GM encountered an error.", {
    error: true,
    label: "OpenAdventure",
  });
  const actions = node("div", "event-actions");
  if (event.suggest_retry) {
    const retry = node("button", "", "Retry turn");
    retry.type = "button";
    retry.addEventListener("click", () => runRetry());
    actions.append(retry);
  }
  if (event.suggest_model || connectionInfo().connected === false) {
    const settings = node("button", "", "Open settings");
    settings.type = "button";
    settings.addEventListener("click", openSettings);
    actions.append(settings);
  }
  if (actions.childElementCount) card.append(actions);
  appendTranscript(card);
  setTurnStatus("The turn could not finish", "error");
}

function renderModuleTransition(event) {
  const message = event.active_title
    ? `Completed ${event.completed_title || event.completed}. Now playing: ${event.active_title}.`
    : `Completed ${event.completed_title || event.completed}. The campaign arc is complete.`;
  appendTranscript(node("div", "module-banner", message));
}

function handleEngineEvent(rawEvent, { background = false } = {}) {
  if (!rawEvent) return;
  const event = rawEvent.event && rawEvent.type === "event" ? rawEvent.event : rawEvent;
  if (!event?.type) return;

  switch (event.type) {
    case "state_snapshot":
      applyState(event.state || {});
      break;
    case "history_snapshot":
      renderHistory(event.history || []);
      break;
    case "action_message":
      if (event.message) appendTranscript(systemMessage(event.message));
      break;
    case "turn_started":
      beginAssistant();
      setTurnStatus("The GM is thinking…", "working");
      break;
    case "assistant_text_delta":
      updateAssistant(event.text || "");
      setTurnStatus("The GM is responding…", "working");
      break;
    case "debug_chatter":
      if (store.campaign?.mode === "assistant" && event.text) {
        appendTranscript(systemMessage(event.text, { label: event.reason || "GM notes" }));
      }
      break;
    case "tool_started": {
      setTurnStatus("The GM is consulting the campaign…", "working");
      if (store.campaign?.mode === "assistant") {
        const card = toolCard(event, true);
        store.toolCards.set(event.call_id, card);
        appendTranscript(card);
      }
      break;
    }
    case "tool_finished": {
      if (event.private && store.campaign?.mode === "gm") break;
      const existing = store.toolCards.get(event.call_id);
      const card = toolCard(event, false);
      if (existing?.isConnected) existing.replaceWith(card);
      else appendTranscript(card);
      store.toolCards.delete(event.call_id);
      break;
    }
    case "roll_result":
      renderRoll(event);
      break;
    case "state_changed":
      if (!event.private && event.summary) setTurnStatus(event.summary, "working");
      break;
    case "module_transition":
      renderModuleTransition(event);
      break;
    case "background_task_started":
      setTurnStatus(event.label || "Background work started…", "working");
      break;
    case "background_task_finished":
      if (!event.ok) {
        appendTranscript(systemMessage(event.message || "A background task failed.", { error: true }));
      } else if (background) {
        setTurnStatus(event.message || "Background work complete", "ready");
      }
      break;
    case "image_generated":
    case "show_image":
      renderImage(event);
      break;
    case "music_started":
    case "music_stopped":
      renderMusic(event);
      break;
    case "compaction_started":
      appendTranscript(systemMessage("The chronicler is updating the story so far…", { label: "Chronicler" }));
      setTurnStatus("The chronicler is working…", "working");
      break;
    case "compaction_progress":
      setTurnStatus("The chronicler is still working…", "working");
      break;
    case "compaction_finished":
      appendTranscript(systemMessage("The story summary is up to date.", { label: "Chronicler" }));
      break;
    case "engine_error":
      renderEngineError(event);
      break;
    case "turn_completed":
      finishAssistant();
      setTurnStatus("Turn complete", "ready");
      break;
    default:
      break;
  }
}

function normalizePayload(payload) {
  const campaign = payload?.campaign || payload?.meta || null;
  return {
    campaign,
    settings: payload?.settings || campaign?.settings || {},
    provider: payload?.provider || payload?.connection || null,
    history: Array.isArray(payload?.history) ? payload.history : [],
    state: payload?.state || {},
  };
}

function applyPayload(payload, { renderTranscript = true } = {}) {
  const normalized = normalizePayload(payload);
  if (!normalized.campaign) throw new Error("The campaign payload is missing campaign details.");
  store.campaign = normalized.campaign;
  store.slug = normalized.campaign.slug || store.slug;
  store.settings = normalized.settings;
  store.provider = normalized.provider;
  store.history = normalized.history;
  store.gameState = normalized.state;
  updateCampaignHeader();
  updateConnection();
  applyState(normalized.state);
  if (renderTranscript) renderHistory(normalized.history);
}

function applyState(state) {
  store.gameState = state || {};
  if (store.gameState.meta && typeof store.gameState.meta === "object") {
    store.campaign = { ...(store.campaign || {}), ...store.gameState.meta };
    updateCampaignHeader();
  }
  const counts = stateCounts(store.gameState, store.campaign?.mode || "gm");
  dom.partyCount.textContent = String(counts.party);
  dom.clockCount.textContent = String(counts.clocks);
  renderInspector(
    dom.inspectorContent,
    store.inspectorTab,
    store.gameState,
    store.campaign?.mode || "gm",
  );
}

function providerEnvironmentName(name) {
  const normalized = String(name || "").toLowerCase();
  if (normalized.includes("gemini") || normalized.includes("google")) return "GEMINI_API_KEY";
  if (normalized.includes("openai") || normalized.includes("gpt")) return "OPENAI_API_KEY";
  return "ANTHROPIC_API_KEY";
}

function connectionInfo() {
  const value = store.provider;
  if (typeof value === "boolean") return { connected: value, name: "AI provider", message: "" };
  if (!value || typeof value !== "object") return { connected: null, name: "AI provider", message: "" };
  const explicit = value.connected ?? value.ready ?? value.configured ?? value.has_api_key ?? value.has_key;
  const name = value.name || value.provider || value.backend || "AI provider";
  const connected = explicit === undefined ? null : Boolean(explicit);
  return {
    connected,
    name,
    model: value.model || store.settings?.model || "",
    message: value.message || value.error || "",
  };
}

function updateConnection() {
  const connection = connectionInfo();
  dom.connectionChip.classList.remove("is-ready", "is-error", "is-working");
  if (connection.connected === false) {
    dom.connectionChip.classList.add("is-error");
    dom.connectionLabel.textContent = "GM offline";
    dom.connectionNotice.hidden = false;
    dom.connectionNoticeTitle.textContent = `${connection.name} is not connected`;
    dom.connectionNoticeMessage.textContent =
      connection.message ||
      `Set ${providerEnvironmentName(connection.name)} in the server environment, then restart OpenAdventure.`;
  } else {
    dom.connectionChip.classList.add(connection.connected === true ? "is-ready" : "is-working");
    dom.connectionLabel.textContent = connection.model || connection.name || "Connected";
    dom.connectionNotice.hidden = true;
  }
  if (!store.busy) {
    setTurnStatus(connection.connected === false ? "GM connection required" : "Ready", connection.connected === false ? "error" : "ready");
  }
}

function updateCampaignHeader() {
  const campaign = store.campaign || {};
  dom.campaignTitle.textContent = campaign.name || campaign.slug || "Campaign";
  dom.campaignModeBadge.textContent = campaign.mode === "assistant" ? "GM assistant" : "GM mode";
  const activeModule = Array.isArray(campaign.modules)
    ? campaign.modules.find((module) => module.slug === campaign.active_module)
    : null;
  const settingModel = store.settings?.model || campaign.settings?.model;
  dom.campaignSubtitle.textContent = [
    activeModule?.title || campaign.active_module,
    settingModel,
    campaign.system_source,
  ]
    .filter(Boolean)
    .join(" · ") || "OpenAdventure campaign";
  document.title = `${campaign.name || "Campaign"} | OpenAdventure`;
}

function bookTitle(book) {
  return book?.title || book?.name || book?.slug || "Untitled book";
}

function renderBookOptions() {
  const books = Array.isArray(store.bootstrap.books) ? store.bootstrap.books : [];
  const sources = books.filter((book) => !book.type || book.type === "source");
  const modules = books.filter((book) => !book.type || book.type === "module");

  const fill = (container, rows, inputName, className) => {
    container.replaceChildren();
    if (!rows.length) {
      container.append(node("p", "book-options-empty", "No matching books have been ingested yet."));
      return;
    }
    for (const book of rows) {
      const label = node("label", "book-option");
      const input = node("input");
      input.type = "checkbox";
      input.name = inputName;
      input.value = book.slug;
      input.className = className;
      label.append(input, node("span", "", bookTitle(book)));
      container.append(label);
    }
  };

  fill(dom.sourceOptions, sources, "sources", "source-book");
  fill(dom.moduleOptions, modules, "modules", "module-book");
}

function renderCampaigns() {
  const campaigns = Array.isArray(store.bootstrap.campaigns) ? store.bootstrap.campaigns : [];
  dom.campaignGrid.replaceChildren();
  dom.libraryStatus.hidden = true;
  dom.emptyLibrary.hidden = campaigns.length > 0;
  dom.campaignGrid.hidden = campaigns.length === 0;

  for (const campaign of campaigns) {
    const card = node("button", "campaign-card");
    card.type = "button";
    card.dataset.slug = campaign.slug;
    const top = node("div", "campaign-card-top");
    top.append(node("span", "badge", campaign.mode === "assistant" ? "GM assistant" : "AI Game Master"));
    if (campaign.has_prior_play) top.append(node("span", "badge", "In progress"));
    card.append(top);
    card.append(node("h2", "", campaign.name || campaign.slug));
    const description =
      campaign.premise ||
      (campaign.active_module ? `Now playing ${campaign.active_module}` : "A new story waiting for the party.");
    card.append(node("p", "campaign-card-description", description));
    const footer = node("div", "campaign-card-footer");
    footer.append(node("span", "", formatDate(campaign.created_at)));
    footer.append(node("span", "campaign-card-action", campaign.has_prior_play ? "Continue →" : "Begin →"));
    card.append(footer);
    card.addEventListener("click", () => openCampaign(campaign.slug));
    dom.campaignGrid.append(card);
  }
  renderBookOptions();
}

async function loadBootstrap() {
  dom.libraryStatus.hidden = false;
  dom.libraryStatus.classList.remove("is-error");
  dom.libraryStatus.textContent = "Loading campaigns…";
  try {
    const payload = await api.bootstrap();
    store.bootstrap = {
      campaigns: Array.isArray(payload?.campaigns) ? payload.campaigns : [],
      books: Array.isArray(payload?.books) ? payload.books : [],
    };
    renderCampaigns();
  } catch (error) {
    dom.libraryStatus.hidden = false;
    dom.libraryStatus.classList.add("is-error");
    dom.libraryStatus.textContent = errorMessage(error);
  }
}

function campaignHash(slug) {
  return `#campaign/${encodeURIComponent(slug)}`;
}

async function openCampaign(slug, suppliedPayload = null, { updateUrl = true } = {}) {
  if (!slug) return;
  stopEventPolling();
  store.slug = slug;
  dom.libraryView.hidden = true;
  dom.playView.hidden = false;
  dom.campaignTitle.textContent = "Opening campaign…";
  dom.campaignSubtitle.textContent = "Preparing your table";
  dom.transcript.replaceChildren(systemMessage("Opening the campaign…"));
  setTurnStatus("Opening campaign…", "working");

  try {
    const payload = suppliedPayload || (await api.campaign(slug));
    applyPayload(payload);
    if (updateUrl && window.location.hash !== campaignHash(store.slug)) {
      window.history.pushState({ slug: store.slug }, "", campaignHash(store.slug));
    }
    startEventPolling();
  } catch (error) {
    appendTranscript(systemMessage(errorMessage(error), { error: true, label: "OpenAdventure" }));
    setTurnStatus("Campaign could not open", "error");
  }
}

function showLibrary({ updateUrl = true } = {}) {
  if (store.busy) cancelTurn();
  stopEventPolling();
  closeInspector();
  store.slug = null;
  store.campaign = null;
  store.activeAssistant = null;
  dom.playView.hidden = true;
  dom.libraryView.hidden = false;
  document.title = "OpenAdventure";
  if (updateUrl && window.location.hash) window.history.pushState({}, "", window.location.pathname);
  loadBootstrap();
}

async function executeStream(label, streamCall) {
  if (store.busy) {
    toast("A turn is already in progress.");
    return;
  }
  const controller = new AbortController();
  store.streamController = controller;
  store.toolCards.clear();
  setBusy(true, label);
  try {
    await streamCall((event) => handleEngineEvent(event), controller.signal);
  } catch (error) {
    if (error?.name !== "AbortError") {
      renderEngineError({ message: errorMessage(error), suggest_retry: true });
    }
  } finally {
    finishAssistant();
    store.streamController = null;
    setBusy(false);
    window.requestAnimationFrame(() => dom.composerInput.focus());
  }
}

async function sendTurn(text, kind = store.messageKind, quiet = false) {
  const clean = String(text || "").trim();
  if (!clean) return;
  if (connectionInfo().connected === false) {
    toast("Connect an AI provider before starting a GM turn.", "error");
    openSettings();
    return;
  }
  appendTranscript(userMessage(clean, kind), { forceScroll: true });
  dom.composerInput.value = "";
  resizeComposer();
  closeSlashMenu();
  await executeStream("The GM is thinking…", (onEvent, signal) =>
    api.turn(
      store.slug,
      {
        text: clean,
        kind,
        quiet: kind === "aside" ? true : Boolean(quiet),
      },
      onEvent,
      { signal },
    ),
  );
}

async function cancelTurn() {
  if (!store.busy || !store.slug) return;
  try {
    const result = await api.cancel(store.slug);
    toast(result.cancelled ? "Cancelling turn…" : "The turn is already finishing.");
  } catch {
    // If the cancellation request itself fails, close the local stream so the
    // interface still returns control. A successful request must stay open for
    // the server's final action message and state snapshot.
    store.streamController?.abort();
    toast("Turn cancelled locally.");
  }
}

async function runRoll(expression = "") {
  if (store.busy) {
    toast("Wait for the current turn to finish.");
    return false;
  }
  const clean = expression.trim();
  if (!clean) {
    openRoll();
    return false;
  }
  setBusy(true, "Rolling dice…");
  try {
    const payload = await api.roll(store.slug, clean);
    handleEngineEvent(payload.event || payload.roll || payload);
    if (payload.state) applyState(payload.state);
    return true;
  } catch (error) {
    toast(errorMessage(error), "error");
    return false;
  } finally {
    setBusy(false);
  }
}

async function runUndo(count = 1) {
  if (store.busy) {
    toast("Wait for the current turn to finish.");
    return;
  }
  const normalizedCount = Math.max(1, Math.min(30, Number(count) || 1));
  setBusy(true, "Rewinding the story…");
  try {
    const payload = await api.undo(store.slug, normalizedCount);
    if (payload.history) renderHistory(payload.history);
    if (payload.state) applyState(payload.state);
    if (payload.message) appendTranscript(systemMessage(payload.message));
    toast(payload.message || `${normalizedCount} turn${normalizedCount === 1 ? "" : "s"} undone.`);
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    setBusy(false);
  }
}

async function runRetry() {
  await executeStream("Preparing the last turn again…", (onEvent, signal) =>
    api.retry(store.slug, onEvent, { signal }),
  );
}

async function runCompact() {
  await executeStream("The chronicler is updating the story…", (onEvent, signal) =>
    api.compact(store.slug, onEvent, { signal }),
  );
}

async function runRecap() {
  if (store.busy) {
    toast("Wait for the current turn to finish.");
    return;
  }
  setBusy(true, "Recalling the story so far…");
  try {
    const payload = await api.recap(store.slug);
    const text = payload?.text || "Nothing has happened yet.";
    const recap = assistantMessage("Previously", text);
    appendTranscript(recap.article, { forceScroll: true });
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    setBusy(false);
  }
}

function parseSudo(raw) {
  let text = raw.trim();
  let quiet = false;
  if (/^(?:-q|--quiet)(?:\s+|$)/.test(text)) {
    quiet = true;
    text = text.replace(/^(?:-q|--quiet)(?:\s+|$)/, "").trim();
  }
  return { text, quiet };
}

async function handleShortcut(text) {
  const [rawCommand, ...rest] = text.trim().split(/\s+/);
  const command = rawCommand.toLowerCase();
  const args = text.trim().slice(rawCommand.length).trim();
  switch (command) {
    case "/roll":
      if (args) await runRoll(args);
      else openRoll();
      return true;
    case "/undo":
      await runUndo(args ? Number.parseInt(args, 10) : 1);
      return true;
    case "/retry":
      await runRetry();
      return true;
    case "/recap":
      await runRecap();
      return true;
    case "/compact":
      await runCompact();
      return true;
    case "/btw":
      if (args) await sendTurn(args, "aside", true);
      else {
        setMessageKind("aside");
        dom.composerInput.value = "";
        dom.composerInput.focus();
        toast("Aside mode selected. Ask a question off the record.");
      }
      return true;
    case "/sudo": {
      const parsed = parseSudo(args);
      if (parsed.text) await sendTurn(parsed.text, "steer", parsed.quiet);
      else {
        setMessageKind("steer");
        dom.quietCheckbox.checked = parsed.quiet;
        dom.composerInput.value = "";
        dom.composerInput.focus();
        toast("GM guidance mode selected.");
      }
      return true;
    }
    default:
      toast(`Unknown browser shortcut ${command}. Type / to see the available commands.`, "error");
      return true;
  }
}

function setMessageKind(kind) {
  if (!["normal", "aside", "steer"].includes(kind)) return;
  store.messageKind = kind;
  document.querySelectorAll("[data-message-kind]").forEach((button) => {
    const selected = button.dataset.messageKind === kind;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-checked", String(selected));
  });
  dom.quietControl.hidden = kind !== "steer";
  dom.composerInput.placeholder =
    kind === "aside"
      ? "Ask the GM something off the record…"
      : kind === "steer"
        ? "Give the GM an out-of-character instruction…"
        : "What do you do?";
  dom.composerHint.textContent =
    kind === "aside"
      ? "This read-only aside is not saved to the campaign story."
      : kind === "steer"
        ? "Guide the GM directly. Off-the-record guidance can still change campaign state."
        : "Enter to send, Shift+Enter for a new line. Type / for shortcuts.";
}

function resizeComposer() {
  dom.composerInput.style.height = "auto";
  dom.composerInput.style.height = `${Math.min(dom.composerInput.scrollHeight, 170)}px`;
}

function matchingShortcuts(text) {
  const query = text.trim().toLowerCase();
  if (!query.startsWith("/") || query.includes(" ")) return [];
  return shortcuts.filter((item) => item.command.startsWith(query));
}

function renderSlashMenu() {
  const matches = matchingShortcuts(dom.composerInput.value);
  store.visibleShortcuts = matches;
  store.slashIndex = Math.max(0, Math.min(store.slashIndex, matches.length - 1));
  dom.slashMenu.replaceChildren();
  if (!matches.length) {
    dom.slashMenu.hidden = true;
    return;
  }
  matches.forEach((item, index) => {
    const button = node("button", `slash-option${index === store.slashIndex ? " is-active" : ""}`);
    button.type = "button";
    button.role = "option";
    button.setAttribute("aria-selected", String(index === store.slashIndex));
    button.append(node("code", "", item.command), node("span", "", item.description));
    button.addEventListener("mousedown", (event) => event.preventDefault());
    button.addEventListener("click", () => selectShortcut(index));
    dom.slashMenu.append(button);
  });
  dom.slashMenu.hidden = false;
}

function selectShortcut(index = store.slashIndex) {
  const shortcut = store.visibleShortcuts[index];
  if (!shortcut) return;
  dom.composerInput.value = shortcut.template;
  dom.composerInput.focus();
  dom.composerInput.setSelectionRange(dom.composerInput.value.length, dom.composerInput.value.length);
  closeSlashMenu();
  resizeComposer();
}

function closeSlashMenu() {
  dom.slashMenu.hidden = true;
  store.visibleShortcuts = [];
  store.slashIndex = 0;
}

function openCreate() {
  dom.createError.hidden = true;
  dom.createForm.reset();
  renderBookOptions();
  dom.createDialog.showModal();
  window.setTimeout(() => dom.createName.focus(), 0);
}

function openRoll() {
  dom.rollError.hidden = true;
  dom.rollDialog.showModal();
  window.setTimeout(() => {
    dom.rollExpression.focus();
    dom.rollExpression.select();
  }, 0);
}

function openSettings() {
  if (!store.campaign) return;
  const settings = store.settings || store.campaign.settings || {};
  dom.settingsMode.value = store.campaign.mode || "gm";
  dom.settingsEffort.value = settings.effort || "high";
  dom.settingsThinking.checked = Boolean(settings.thinking);
  dom.settingsVerbosity.value = settings.verbosity || "medium";
  dom.settingsContext.value = settings.context_budget || "";
  dom.settingsError.hidden = true;

  const connection = connectionInfo();
  dom.settingsConnection.classList.toggle("is-error", connection.connected === false);
  dom.settingsConnection.textContent =
    connection.connected === false
      ? connection.message || `${connection.name} needs ${providerEnvironmentName(connection.name)} in the server environment.`
      : `${connection.name}${connection.model ? ` · ${connection.model}` : ""} is connected.`;
  dom.settingsDialog.showModal();
}

function closeInspector() {
  dom.inspector.classList.remove("is-open");
  dom.inspectorOverlay.hidden = true;
  dom.inspectorToggle.setAttribute("aria-expanded", "false");
}

function openInspector() {
  dom.inspector.classList.add("is-open");
  dom.inspectorOverlay.hidden = false;
  dom.inspectorToggle.setAttribute("aria-expanded", "true");
  window.setTimeout(() => dom.inspectorClose.focus(), 0);
}

function setInspectorTab(tab) {
  if (!["party", "scene", "encounter", "clocks"].includes(tab)) return;
  store.inspectorTab = tab;
  localStorage.setItem("openadventure.inspectorTab", tab);
  document.querySelectorAll("[data-inspector-tab]").forEach((button) => {
    const selected = button.dataset.inspectorTab === tab;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  renderInspector(dom.inspectorContent, tab, store.gameState, store.campaign?.mode || "gm");
}

async function pollEvents() {
  if (!store.slug || store.pollInFlight || store.busy || document.hidden) return;
  store.pollInFlight = true;
  try {
    const payload = await api.events(store.slug);
    const recovered = store.pollHadError;
    store.pollHadError = false;
    if (recovered) updateConnection();
    const events = Array.isArray(payload?.events) ? payload.events : [];
    for (const event of events) handleEngineEvent(event, { background: true });
    if (payload?.state) applyState(payload.state);
  } catch (error) {
    if (!store.pollHadError) {
      store.pollHadError = true;
      dom.connectionChip.classList.remove("is-ready", "is-error");
      dom.connectionChip.classList.add("is-working");
      dom.connectionLabel.textContent = "Reconnecting";
      console.warn("Background event polling paused after an error:", error);
    }
  } finally {
    store.pollInFlight = false;
  }
}

function startEventPolling() {
  if (!store.slug || store.pollTimer || document.hidden || store.busy) return;
  pollEvents();
  store.pollTimer = window.setInterval(pollEvents, 2000);
}

function stopEventPolling() {
  if (store.pollTimer) window.clearInterval(store.pollTimer);
  store.pollTimer = null;
}

async function routeFromLocation() {
  const match = window.location.hash.match(/^#campaign\/(.+)$/);
  if (match) {
    let slug;
    try {
      slug = decodeURIComponent(match[1]);
    } catch {
      slug = match[1];
    }
    if (slug !== store.slug || dom.playView.hidden) await openCampaign(slug, null, { updateUrl: false });
  } else if (!dom.libraryView.hidden) {
    await loadBootstrap();
  } else {
    showLibrary({ updateUrl: false });
  }
}

dom.newCampaignButton.addEventListener("click", openCreate);
document.querySelectorAll("[data-open-create]").forEach((button) => button.addEventListener("click", openCreate));
dom.backButton.addEventListener("click", () => showLibrary());
dom.settingsButton.addEventListener("click", openSettings);
dom.connectionSettingsButton.addEventListener("click", openSettings);
dom.inspectorToggle.addEventListener("click", () => {
  if (dom.inspector.classList.contains("is-open")) closeInspector();
  else openInspector();
});
dom.inspectorClose.addEventListener("click", closeInspector);
dom.inspectorOverlay.addEventListener("click", closeInspector);
dom.jumpLatest.addEventListener("click", () => scrollToLatest());
dom.cancelButton.addEventListener("click", cancelTurn);

document.querySelectorAll("[data-message-kind]").forEach((button) => {
  button.addEventListener("click", () => setMessageKind(button.dataset.messageKind));
});

document.querySelectorAll("[data-inspector-tab]").forEach((button) => {
  button.addEventListener("click", () => setInspectorTab(button.dataset.inspectorTab));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const tabs = ["party", "scene", "encounter", "clocks"];
    const current = tabs.indexOf(store.inspectorTab);
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const next = tabs[(current + direction + tabs.length) % tabs.length];
    setInspectorTab(next);
    $(`${next}-tab`)?.focus();
  });
});

document.querySelectorAll(".quick-action").forEach((button) => {
  button.addEventListener("click", () => {
    switch (button.dataset.action) {
      case "roll":
        openRoll();
        break;
      case "undo":
        runUndo();
        break;
      case "retry":
        runRetry();
        break;
      case "recap":
        runRecap();
        break;
      case "compact":
        runCompact();
        break;
      default:
        break;
    }
  });
});

dom.composerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (store.busy) return;
  const text = dom.composerInput.value.trim();
  if (!text) return;
  if (text.startsWith("/")) {
    await handleShortcut(text);
    dom.composerInput.value = "";
    resizeComposer();
    closeSlashMenu();
    return;
  }
  await sendTurn(text, store.messageKind, dom.quietCheckbox.checked);
});

dom.composerInput.addEventListener("input", () => {
  resizeComposer();
  store.slashIndex = 0;
  renderSlashMenu();
});

dom.composerInput.addEventListener("keydown", (event) => {
  if (!dom.slashMenu.hidden && ["ArrowDown", "ArrowUp"].includes(event.key)) {
    event.preventDefault();
    const direction = event.key === "ArrowDown" ? 1 : -1;
    store.slashIndex =
      (store.slashIndex + direction + store.visibleShortcuts.length) % store.visibleShortcuts.length;
    renderSlashMenu();
    return;
  }
  if (!dom.slashMenu.hidden && event.key === "Tab") {
    event.preventDefault();
    selectShortcut();
    return;
  }
  if (
    !dom.slashMenu.hidden &&
    event.key === "Enter" &&
    !event.shiftKey &&
    !dom.composerInput.value.trim().includes(" ")
  ) {
    event.preventDefault();
    selectShortcut();
    return;
  }
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    dom.composerForm.requestSubmit();
  }
  if (event.key === "Escape") closeSlashMenu();
});

dom.transcriptScroll.addEventListener("scroll", () => {
  if (isNearTranscriptEnd()) dom.jumpLatest.hidden = true;
});

dom.createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  dom.createError.hidden = true;
  const formData = new FormData(dom.createForm);
  const payload = {
    name: String(formData.get("name") || "").trim(),
    mode: String(formData.get("mode") || "gm"),
    premise: String(formData.get("premise") || "").trim() || null,
    sources: formData.getAll("sources").map(String),
    modules: formData.getAll("modules").map(String),
  };
  if (!payload.name) return;
  dom.createSubmit.disabled = true;
  dom.createSubmit.textContent = "Creating…";
  try {
    const response = await api.createCampaign(payload);
    dom.createDialog.close();
    const slug = response?.campaign?.slug || response?.slug;
    if (!slug) throw new Error("The server created a campaign without returning its slug.");
    await openCampaign(slug, response?.campaign && response?.history ? response : null);
  } catch (error) {
    dom.createError.textContent = errorMessage(error);
    dom.createError.hidden = false;
  } finally {
    dom.createSubmit.disabled = false;
    dom.createSubmit.textContent = "Create campaign";
  }
});

dom.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  dom.settingsError.hidden = true;
  const contextBudget = Number.parseInt(dom.settingsContext.value, 10);
  const payload = {
    mode: dom.settingsMode.value,
    effort: dom.settingsEffort.value,
    thinking: dom.settingsThinking.checked,
    verbosity: dom.settingsVerbosity.value,
  };
  if (Number.isFinite(contextBudget)) payload.context_budget = contextBudget;
  dom.settingsSubmit.disabled = true;
  dom.settingsSubmit.textContent = "Saving…";
  try {
    const response = await api.updateSettings(store.slug, payload);
    applyPayload(response, { renderTranscript: false });
    dom.settingsDialog.close();
    toast("Campaign settings updated.");
  } catch (error) {
    dom.settingsError.textContent = errorMessage(error);
    dom.settingsError.hidden = false;
  } finally {
    dom.settingsSubmit.disabled = false;
    dom.settingsSubmit.textContent = "Save settings";
  }
});

dom.rollForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  dom.rollError.hidden = true;
  dom.rollSubmit.disabled = true;
  dom.rollSubmit.textContent = "Rolling…";
  const succeeded = await runRoll(dom.rollExpression.value);
  if (succeeded) dom.rollDialog.close();
  else if (!store.busy) {
    dom.rollError.textContent = "That dice expression could not be rolled.";
    dom.rollError.hidden = false;
  }
  dom.rollSubmit.disabled = false;
  dom.rollSubmit.textContent = "Roll dice";
});

document.querySelectorAll("[data-roll-example]").forEach((button) => {
  button.addEventListener("click", () => {
    dom.rollExpression.value = button.dataset.rollExample;
    dom.rollExpression.focus();
  });
});

document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog")?.close());
});

document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    const rect = dialog.getBoundingClientRect();
    const outside =
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom;
    if (outside) dialog.close();
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && store.busy && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    cancelTurn();
    return;
  }
  const activeTag = document.activeElement?.tagName;
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(activeTag);
  if (event.key === "/" && !typing && !dom.playView.hidden) {
    event.preventDefault();
    dom.composerInput.focus();
    dom.composerInput.value = "/";
    resizeComposer();
    renderSlashMenu();
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k" && !dom.playView.hidden) {
    event.preventDefault();
    dom.composerInput.focus();
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopEventPolling();
  else startEventPolling();
});

window.addEventListener("popstate", routeFromLocation);
window.addEventListener("beforeunload", stopEventPolling);

setInspectorTab(store.inspectorTab);
setMessageKind("normal");
routeFromLocation();
