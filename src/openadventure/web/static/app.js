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
  libraryButton: $("library-button"),
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
  playLibraryButton: $("play-library-button"),
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
  settingsModel: $("settings-model"),
  settingsEffort: $("settings-effort"),
  settingsThinking: $("settings-thinking"),
  settingsVerbosity: $("settings-verbosity"),
  settingsContext: $("settings-context"),
  settingsTts: $("settings-tts"),
  settingsSfx: $("settings-sfx"),
  settingsImages: $("settings-images"),
  settingsImagesAuto: $("settings-images-auto"),
  settingsMusic: $("settings-music"),
  settingsMusicAuto: $("settings-music-auto"),
  settingsMusicVolume: $("settings-music-volume"),
  settingsMusicVolumeOutput: $("settings-music-volume-output"),
  mediaSettingsStatus: $("media-settings-status"),
  settingsCredentialsButton: $("settings-credentials-button"),
  settingsConnection: $("settings-connection"),
  settingsError: $("settings-error"),
  settingsSubmit: $("settings-submit"),
  credentialDialog: $("credential-dialog"),
  credentialForm: $("credential-form"),
  credentialTitle: $("credential-title"),
  credentialIntro: $("credential-intro"),
  credentialLabel: $("credential-label"),
  credentialKey: $("credential-key"),
  credentialNote: $("credential-note"),
  credentialError: $("credential-error"),
  credentialSubmit: $("credential-submit"),
  libraryDialog: $("library-dialog"),
  libraryBooks: $("library-books"),
  showIngestButton: $("show-ingest-button"),
  hideIngestButton: $("hide-ingest-button"),
  ingestPanel: $("ingest-panel"),
  ingestForm: $("ingest-form"),
  ingestDropZone: $("ingest-drop-zone"),
  ingestFile: $("ingest-file"),
  ingestFileLabel: $("ingest-file-label"),
  ingestType: $("ingest-type"),
  ingestName: $("ingest-name"),
  ingestPages: $("ingest-pages"),
  ingestError: $("ingest-error"),
  ingestSubmit: $("ingest-submit"),
  libraryJob: $("library-job"),
  libraryJobKicker: $("library-job-kicker"),
  libraryJobTitle: $("library-job-title"),
  libraryJobState: $("library-job-state"),
  libraryJobCancel: $("library-job-cancel"),
  libraryJobMessage: $("library-job-message"),
  libraryJobProgressWrap: $("library-job-progress-wrap"),
  libraryJobProgress: $("library-job-progress"),
  libraryJobProgressLabel: $("library-job-progress-label"),
  libraryJobActivity: $("library-job-activity"),
  libraryJobError: $("library-job-error"),
  jobGlyph: $("job-glyph"),
  campaignLibraryPanel: $("campaign-library-panel"),
  campaignLibraryForm: $("campaign-library-form"),
  campaignSourceOptions: $("campaign-source-options"),
  campaignModuleOptions: $("campaign-module-options"),
  campaignSystemSource: $("campaign-system-source"),
  campaignActiveModule: $("campaign-active-module"),
  campaignLibraryError: $("campaign-library-error"),
  campaignLibrarySubmit: $("campaign-library-submit"),
  rollDialog: $("roll-dialog"),
  rollForm: $("roll-form"),
  rollExpression: $("roll-expression"),
  rollError: $("roll-error"),
  rollSubmit: $("roll-submit"),
  toastRegion: $("toast-region"),
  globalStatus: $("global-status"),
  mediaDock: $("media-dock"),
  mediaDockKind: $("media-dock-kind"),
  mediaDockTitle: $("media-dock-title"),
  mediaDockStatus: $("media-dock-status"),
  mediaPlayButton: $("media-play-button"),
  mediaDockVolume: $("media-dock-volume"),
  foregroundAudioPlayer: $("foreground-audio-player"),
  musicPlayer: $("music-player"),
};

const shortcuts = [
  { command: "/roll", template: "/roll d20", description: "Roll dice locally" },
  { command: "/undo", template: "/undo", description: "Take back the last turn" },
  { command: "/retry", template: "/retry", description: "Retry the last turn" },
  { command: "/recap", template: "/recap", description: "Recall the story so far" },
  { command: "/compact", template: "/compact", description: "Update the rolling story summary" },
  { command: "/library", template: "/library", description: "Manage books for this campaign" },
  { command: "/ingest", template: "/ingest", description: "Add a rule source or adventure module" },
  { command: "/template", template: "/template ", description: "Derive a character sheet template" },
  { command: "/btw", template: "/btw ", description: "Ask an off-record rules question" },
  { command: "/sudo", template: "/sudo ", description: "Guide the GM out of character" },
];

const store = {
  bootstrap: { campaigns: [], books: [], models: [], utilityModel: "" },
  slug: null,
  campaign: null,
  settings: {},
  provider: null,
  media: {},
  history: [],
  gameState: {},
  usage: {},
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
  usageRefreshTimer: null,
  usageRefreshInFlight: false,
  libraryJobId: null,
  libraryJobTimer: null,
  libraryJobActivity: [],
  targetedTemplate: null,
  credentialRequest: null,
  dismissedCredentials: new Set(),
  audio: {
    active: null,
    currentClip: null,
    queue: [],
    foregroundVolumes: { narration: 1, sound_effect: 1 },
    musicTitle: "",
    waitingForGesture: false,
    volumeTimer: null,
  },
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

function appendTranscript(element, { forceScroll = false, before = null } = {}) {
  const shouldScroll = forceScroll || isNearTranscriptEnd();
  if (dom.storyEmpty.isConnected) dom.storyEmpty.remove();
  if (before?.parentElement === dom.transcript) dom.transcript.insertBefore(element, before);
  else dom.transcript.append(element);
  if (shouldScroll) {
    window.requestAnimationFrame(() => scrollToLatest(forceScroll ? "auto" : "smooth"));
  } else {
    dom.jumpLatest.hidden = false;
  }
}

function appendTurnEvent(element, options = {}) {
  const pendingResponse = store.activeAssistant?.article;
  appendTranscript(element, {
    ...options,
    before: pendingResponse?.isConnected ? pendingResponse : null,
  });
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
  appendTurnEvent(createRollCard(event));
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
  appendTurnEvent(figure);
}

function clampVolume(value, fallback = 0.2) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, Math.min(1, parsed)) : fallback;
}

function setMediaDock(kind, title, status = "") {
  dom.mediaDock.hidden = false;
  dom.mediaDockKind.textContent = kind;
  dom.mediaDockTitle.textContent = title || "Campaign audio";
  dom.mediaDockStatus.textContent = status;
  const player = activeMediaPlayer();
  const paused = !player || player.paused;
  dom.mediaDock.classList.toggle("is-paused", paused);
  dom.mediaPlayButton.textContent = paused ? "Play" : "Pause";
  dom.mediaPlayButton.setAttribute("aria-label", paused ? "Play audio" : "Pause audio");
}

function activeMediaPlayer() {
  switch (store.audio.active) {
    case "narration":
    case "sound_effect":
      return dom.foregroundAudioPlayer;
    case "music":
      return dom.musicPlayer;
    default:
      return null;
  }
}

function restoreMediaDock() {
  const clip = store.audio.currentClip;
  if (clip) {
    store.audio.active = clip.kind;
    const status = dom.foregroundAudioPlayer.paused
      ? store.audio.waitingForGesture
        ? "Press Play to hear this in your browser"
        : "Paused"
      : "";
    setMediaDock(clip.kind === "sound_effect" ? "Sound effect" : "Spoken narration", clip.title, status);
    return;
  }
  if (dom.musicPlayer.src) {
    store.audio.active = "music";
    setMediaDock(
      "Background music",
      store.audio.musicTitle || "Campaign ambience",
      dom.musicPlayer.paused ? "Paused" : "Looping",
    );
    return;
  }
  store.audio.active = null;
  dom.mediaDock.hidden = true;
}

function stopBrowserMedia() {
  for (const player of [dom.foregroundAudioPlayer, dom.musicPlayer]) {
    player.pause();
    player.removeAttribute("src");
    player.load();
  }
  store.audio.active = null;
  store.audio.currentClip = null;
  store.audio.queue.length = 0;
  store.audio.musicTitle = "";
  store.audio.waitingForGesture = false;
  dom.mediaDock.hidden = true;
}

async function playBrowserAudio(player, kind, title, status = "", guard = null) {
  const canOwnDock = () =>
    (!guard || guard()) && (kind !== "music" || !store.audio.currentClip);
  if (canOwnDock()) {
    store.audio.active = kind;
    setMediaDock(
      kind === "music"
        ? "Background music"
        : kind === "sound_effect"
          ? "Sound effect"
          : "Spoken narration",
      title,
      status,
    );
  }
  try {
    await player.play();
    if (!canOwnDock()) return;
    store.audio.waitingForGesture = false;
    setMediaDock(dom.mediaDockKind.textContent, title, status);
  } catch {
    if (!canOwnDock()) return;
    store.audio.waitingForGesture = true;
    setMediaDock(dom.mediaDockKind.textContent, title, "Press Play to hear this in your browser");
  }
}

function mediaUrl(event) {
  return safeMediaUrl(event.url || event.path || event.track || event.src);
}

function audioChannel(event) {
  const value = String(event.kind || event.channel || event.audio_type || "narration").toLowerCase();
  return value.includes("sfx") || value.includes("effect") ? "sound_effect" : "narration";
}

function handleAudioReady(event) {
  const url = mediaUrl(event);
  if (!url) return;
  const channel = audioChannel(event);
  const title =
    event.label ||
    event.caption ||
    event.text ||
    (channel === "sound_effect" ? "Scene sound" : "Game Master narration");
  store.audio.queue.push({
    kind: channel,
    title,
    url,
    volume: event.volume === undefined ? null : clampVolume(event.volume, 1),
  });
  playNextForegroundClip();
}

function playNextForegroundClip() {
  if (store.audio.currentClip || store.audio.queue.length === 0) return;
  const clip = store.audio.queue.shift();
  store.audio.currentClip = clip;
  dom.foregroundAudioPlayer.src = clip.url;
  dom.foregroundAudioPlayer.volume = clip.volume ?? store.audio.foregroundVolumes[clip.kind] ?? 1;
  playBrowserAudio(
    dom.foregroundAudioPlayer,
    clip.kind,
    clip.title,
    "",
    () => store.audio.currentClip === clip,
  );
}

function finishForegroundClip() {
  dom.foregroundAudioPlayer.pause();
  dom.foregroundAudioPlayer.removeAttribute("src");
  dom.foregroundAudioPlayer.load();
  store.audio.currentClip = null;
  store.audio.waitingForGesture = false;
  if (store.audio.queue.length > 0) {
    playNextForegroundClip();
  } else {
    restoreMediaDock();
  }
}

function handleAudioStopped() {
  store.audio.queue.length = 0;
  dom.foregroundAudioPlayer.pause();
  dom.foregroundAudioPlayer.removeAttribute("src");
  dom.foregroundAudioPlayer.load();
  store.audio.currentClip = null;
  store.audio.waitingForGesture = false;
  restoreMediaDock();
}

function renderMusic(event) {
  const stopped = event.type === "music_stopped";
  const title = event.mood || event.prompt || event.label || event.track_name || "campaign ambience";
  const card = node("section", "media-card");
  card.append(node("div", "media-caption", stopped ? "Music stopped" : `Now playing: ${title}`));
  appendTurnEvent(card);

  if (stopped) {
    dom.musicPlayer.pause();
    dom.musicPlayer.removeAttribute("src");
    dom.musicPlayer.load();
    store.audio.musicTitle = "";
    restoreMediaDock();
    return;
  }

  const url = mediaUrl(event);
  if (!url) return;
  store.audio.musicTitle = title;
  if (dom.musicPlayer.getAttribute("src") !== url) dom.musicPlayer.src = url;
  dom.musicPlayer.volume = clampVolume(event.volume, dom.mediaDockVolume.value || 0.2);
  dom.mediaDockVolume.value = String(dom.musicPlayer.volume);
  playBrowserAudio(dom.musicPlayer, "music", title, "Looping");
}

function handleMediaVolume(event) {
  const values = event.volumes && typeof event.volumes === "object" ? event.volumes : null;
  const channel = String(event.kind || event.channel || "music").toLowerCase();
  const direct = event.volume ?? event.value;
  if (values) {
    if (values.music !== undefined) dom.musicPlayer.volume = clampVolume(values.music);
    if (values.tts !== undefined || values.narration !== undefined) {
      setForegroundVolume("narration", values.tts ?? values.narration);
    }
    if (values.sfx !== undefined || values.sound_effects !== undefined) {
      setForegroundVolume("sound_effect", values.sfx ?? values.sound_effects);
    }
  } else if (channel.includes("music")) {
    dom.musicPlayer.volume = clampVolume(direct);
  } else if (channel.includes("sfx") || channel.includes("effect")) {
    setForegroundVolume("sound_effect", direct);
  } else {
    setForegroundVolume("narration", direct);
  }
  dom.mediaDockVolume.value = String(dom.musicPlayer.volume);
  if (store.media && typeof store.media === "object") {
    store.media.music_volume = dom.musicPlayer.volume;
  }
}

function setForegroundVolume(kind, value) {
  const volume = clampVolume(value, 1);
  store.audio.foregroundVolumes[kind] = volume;
  if (store.audio.currentClip?.kind === kind) {
    store.audio.currentClip.volume = volume;
    dom.foregroundAudioPlayer.volume = volume;
  }
}

function syncMediaPayload(media) {
  if (!media || typeof media !== "object") return;
  const volume = clampVolume(media.music_volume ?? media.volumes?.music, 0.2);
  dom.musicPlayer.volume = volume;
  dom.mediaDockVolume.value = String(volume);
  const current = media.now_playing || media.current_music || media.music_track;
  if (!current) return;
  const event = typeof current === "string" ? { url: current } : current;
  const url = mediaUrl(event);
  if (!url) return;
  const title = event.mood || event.prompt || event.label || event.title || "campaign ambience";
  store.audio.musicTitle = title;
  if (dom.musicPlayer.getAttribute("src") !== url) dom.musicPlayer.src = url;
  playBrowserAudio(dom.musicPlayer, "music", title, "Looping");
}

function restoreCampaignImages(media) {
  const images = Array.isArray(media?.restored_images) ? media.restored_images : [];
  for (const image of images) renderImage(image);
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
  appendTurnEvent(node("div", "module-banner", message));
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
      if (event.message) appendTurnEvent(systemMessage(event.message));
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
        appendTurnEvent(systemMessage(event.text, { label: event.reason || "GM notes" }));
      }
      break;
    case "tool_started": {
      setTurnStatus("The GM is consulting the campaign…", "working");
      if (store.campaign?.mode === "assistant") {
        const card = toolCard(event, true);
        store.toolCards.set(event.call_id, card);
        appendTurnEvent(card);
      }
      break;
    }
    case "tool_finished": {
      if (event.private && store.campaign?.mode === "gm") break;
      const existing = store.toolCards.get(event.call_id);
      const card = toolCard(event, false);
      if (existing?.isConnected) existing.replaceWith(card);
      else appendTurnEvent(card);
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
        appendTurnEvent(systemMessage(event.message || "A background task failed.", { error: true }));
      } else if (background) {
        setTurnStatus(event.message || "Background work complete", "ready");
      }
      break;
    case "image_generated":
    case "show_image":
      renderImage(event);
      break;
    case "audio_ready":
      handleAudioReady(event);
      break;
    case "audio_stopped":
      handleAudioStopped(event);
      break;
    case "music_started":
    case "music_stopped":
      renderMusic(event);
      break;
    case "media_volume":
      handleMediaVolume(event);
      break;
    case "compaction_started":
      appendTurnEvent(systemMessage("The chronicler is updating the story so far…", { label: "Chronicler" }));
      setTurnStatus("The chronicler is working…", "working");
      break;
    case "compaction_progress":
      setTurnStatus("The chronicler is still working…", "working");
      break;
    case "compaction_finished":
      appendTurnEvent(systemMessage("The story summary is up to date.", { label: "Chronicler" }));
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
  const rawMedia = payload?.media;
  const media = rawMedia && typeof rawMedia === "object"
    ? { ...(rawMedia.settings || {}), ...rawMedia }
    : {
        tts_enabled: campaign?.tts_enabled,
        sound_effects_enabled: campaign?.sound_effects_enabled,
        images_enabled: campaign?.images_enabled,
        images_auto: campaign?.images_auto,
        music_enabled: campaign?.music_enabled,
        music_auto: campaign?.music_auto,
        music_volume: campaign?.music_volume,
      };
  return {
    campaign,
    settings: payload?.settings || campaign?.settings || {},
    provider: payload?.provider || payload?.connection || null,
    media,
    history: Array.isArray(payload?.history) ? payload.history : [],
    state: payload?.state || {},
    usage: payload?.usage || payload?.state?.usage || {},
  };
}

function applyPayload(payload, { renderTranscript = true } = {}) {
  const normalized = normalizePayload(payload);
  if (!normalized.campaign) throw new Error("The campaign payload is missing campaign details.");
  store.campaign = normalized.campaign;
  store.slug = normalized.campaign.slug || store.slug;
  store.settings = normalized.settings;
  store.provider = normalized.provider;
  store.media = normalized.media || {};
  syncMediaPayload(store.media);
  store.history = normalized.history;
  store.usage = normalized.usage;
  store.gameState = { ...normalized.state, usage: store.usage };
  updateCampaignHeader();
  updateConnection();
  applyState(store.gameState);
  if (renderTranscript) {
    renderHistory(normalized.history);
    restoreCampaignImages(store.media);
  }
  queueCredentialPrompt();
}

function applyState(state) {
  const nextState = state && typeof state === "object" ? state : {};
  if (nextState.usage && typeof nextState.usage === "object") {
    store.usage = nextState.usage;
  }
  store.gameState = { ...nextState, usage: store.usage };
  if (store.gameState.media && typeof store.gameState.media === "object") {
    store.media = { ...store.media, ...store.gameState.media };
  }
  if (store.gameState.meta && typeof store.gameState.meta === "object") {
    store.campaign = { ...(store.campaign || {}), ...store.gameState.meta };
    for (const key of [
      "tts_enabled",
      "sound_effects_enabled",
      "images_enabled",
      "images_auto",
      "music_enabled",
      "music_auto",
      "music_volume",
    ]) {
      if (key in store.gameState.meta) store.media[key] = store.gameState.meta[key];
    }
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

const credentialServices = {
  anthropic: {
    title: "Connect Anthropic",
    label: "Anthropic API key",
    intro: "Your selected model uses Anthropic. Add its API key to start the Game Master.",
  },
  gemini: {
    title: "Connect Google AI",
    label: "Google AI API key",
    intro: "Your selected Gemini model needs a Google AI API key before the Game Master can respond.",
  },
  openai: {
    title: "Connect OpenAI",
    label: "OpenAI API key",
    intro: "Your selected model uses OpenAI. Add its API key to start the Game Master.",
  },
  google: {
    title: "Enable scene images",
    label: "Google AI API key",
    intro: "Scene images use Google AI. Add a key to generate illustrations at this table.",
  },
  elevenlabs: {
    title: "Enable browser audio",
    label: "ElevenLabs API key",
    intro: "Narration, sound effects, and music use ElevenLabs. Add a key to generate audio at this table.",
  },
};

function credentialServiceForProvider(name) {
  const value = String(name || "").toLowerCase();
  if (value.includes("gemini") || value.includes("google")) return "gemini";
  if (value.includes("openai") || value.includes("gpt")) return "openai";
  if (value.includes("anthropic") || value.includes("claude")) return "anthropic";
  return null;
}

function backendNeedsApiKey(backend) {
  return Boolean(
    backend &&
      backend.ready === false &&
      /(?:API_KEY|api key|google_api_key|elevenlabs_api_key)/i.test(String(backend.hint || "")),
  );
}

function missingCredentialRequests() {
  if (!store.campaign?.slug) return [];
  const requested = [];
  const connection = connectionInfo();
  if (connection.connected === false) {
    const service = credentialServiceForProvider(connection.name);
    if (service) requested.push(service);
  }

  const enabled = store.media?.enabled || {};
  const backends = store.media?.backends || {};
  if (enabled.images && backendNeedsApiKey(backends.images)) requested.push("google");
  const audioNeedsKey =
    (enabled.narration && backendNeedsApiKey(backends.narration)) ||
    (enabled.sound_effects && backendNeedsApiKey(backends.sound_effects)) ||
    (enabled.music && backendNeedsApiKey(backends.music));
  if (audioNeedsKey) requested.push("elevenlabs");
  return [...new Set(requested)];
}

function credentialDismissalKey(service) {
  return `${store.slug || "local"}:${service}`;
}

function openCredentialPrompt(service, { force = false } = {}) {
  const details = credentialServices[service];
  if (!details || !store.slug || dom.credentialDialog.open) return false;
  const dismissalKey = credentialDismissalKey(service);
  if (!force && store.dismissedCredentials.has(dismissalKey)) return false;
  store.credentialRequest = service;
  dom.credentialForm.reset();
  dom.credentialTitle.textContent = details.title;
  dom.credentialLabel.textContent = details.label;
  dom.credentialIntro.textContent = details.intro;
  dom.credentialNote.textContent = "Saved only to this computer's local .env file and never returned by OpenAdventure.";
  dom.credentialError.hidden = true;
  dom.credentialSubmit.disabled = false;
  dom.credentialSubmit.textContent = "Save API key";
  dom.credentialDialog.showModal();
  window.setTimeout(() => dom.credentialKey.focus(), 0);
  return true;
}

function promptForMissingCredentials({ force = false } = {}) {
  if (store.credentialRequest || dom.credentialDialog.open) return;
  const service = missingCredentialRequests().find(
    (item) => force || !store.dismissedCredentials.has(credentialDismissalKey(item)),
  );
  if (service) openCredentialPrompt(service, { force });
}

function queueCredentialPrompt() {
  window.setTimeout(() => promptForMissingCredentials(), 0);
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
      `Add your ${providerEnvironmentName(connection.name)} in campaign settings to connect it now.`;
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

function modelId(model) {
  return typeof model === "string" ? model : model?.id || model?.model || "";
}

function modelLabel(model) {
  if (typeof model === "string") return model;
  const id = modelId(model);
  const name = model?.display_name || model?.name || id;
  const provider = model?.provider || model?.backend;
  return provider ? `${name} (${provider})` : name;
}

function populateModelSelect(select, current = "") {
  const models = Array.isArray(store.bootstrap.models) ? store.bootstrap.models : [];
  select.replaceChildren();
  for (const model of models) {
    const id = modelId(model);
    if (!id) continue;
    const option = node("option", "", modelLabel(model));
    option.value = id;
    select.append(option);
  }
  const selected = current || store.bootstrap.utilityModel || models.map(modelId).find(Boolean) || "";
  if (selected && !Array.from(select.options).some((option) => option.value === selected)) {
    const option = node("option", "", selected);
    option.value = selected;
    select.prepend(option);
  }
  select.value = selected;
  select.disabled = select.options.length === 0;
}

function syncModelSettingsControls() {
  const selected = (store.bootstrap.models || []).find(
    (model) => modelId(model) === dom.settingsModel.value,
  );
  dom.settingsEffort.disabled = selected?.supports_effort === false;
  dom.settingsThinking.disabled = selected?.supports_thinking === false;
  if (Number.isFinite(Number(selected?.context_window))) {
    dom.settingsContext.max = String(selected.context_window);
  } else {
    dom.settingsContext.max = "1000000";
  }
}

function templateDetails(book) {
  const value = book?.template;
  if (!value) return { ready: false, fields: 0, resources: 0 };
  if (value === true || value === "ready" || value === "complete") {
    return { ready: true, fields: 0, resources: 0 };
  }
  if (typeof value !== "object") return { ready: false, fields: 0, resources: 0 };
  const status = String(value.status || "").toLowerCase();
  const ready = Boolean(
    value.present ||
      value.exists ||
      value.ready ||
      ["ready", "complete", "completed", "succeeded"].includes(status) ||
      value.path ||
      Array.isArray(value.fields),
  );
  const count = (raw, list) => {
    if (Number.isFinite(Number(raw))) return Number(raw);
    return Array.isArray(list) ? list.length : 0;
  };
  return {
    ready,
    fields: count(value.field_count ?? value.fields_count ?? value.fields, value.fields),
    resources: count(
      value.resource_count ?? value.resources_count ?? value.resources,
      value.resources,
    ),
  };
}

function libraryBookType(book) {
  if (book?.type === "module") return { label: "Adventure", className: "is-module" };
  if (book?.type === "source") return { label: "Rules", className: "" };
  return { label: "Legacy", className: "is-legacy" };
}

function renderLibraryBooks(targetSlug = store.targetedTemplate) {
  const books = Array.isArray(store.bootstrap.books) ? store.bootstrap.books : [];
  dom.libraryBooks.replaceChildren();
  if (!books.length) {
    dom.libraryBooks.append(
      node("div", "library-empty", "No books yet. Add a rule source or adventure module to begin."),
    );
    return;
  }

  for (const book of books) {
    const type = libraryBookType(book);
    const template = templateDetails(book);
    const card = node(
      "article",
      `library-book-card${targetSlug === book.slug ? " is-targeted" : ""}`,
    );
    card.dataset.bookSlug = book.slug;
    const top = node("div", "book-card-top");
    top.append(node("span", `book-type-badge ${type.className}`.trim(), type.label));
    if (template.ready) top.append(node("span", "template-badge", "Template ready"));
    card.append(top, node("h4", "", bookTitle(book)));

    const meta = node("div", "book-card-meta");
    meta.append(node("span", "", book.section_count ? `${book.section_count} sections` : book.slug));
    if (book.pages) meta.append(node("span", "", `Pages ${book.pages}`));
    if (template.ready && (template.fields || template.resources)) {
      meta.append(node("span", "", `${template.fields} fields, ${template.resources} resources`));
    }
    card.append(meta);
    if (book.warning) card.append(node("p", "book-card-note", book.warning));

    if (book.type !== "module") {
      const actions = node("div", "book-card-actions");
      const select = node("select", "book-template-model");
      select.setAttribute("aria-label", `Model for ${bookTitle(book)} template`);
      populateModelSelect(
        select,
        store.campaign?.settings?.model || store.settings?.model || store.bootstrap.utilityModel,
      );
      const button = node("button", "button button-quiet", template.ready ? "Regenerate" : "Build template");
      button.type = "button";
      button.disabled = Boolean(store.libraryJobId) || select.disabled;
      button.addEventListener("click", () => startTemplateJob(book, select.value));
      actions.append(select, button);
      card.append(actions);
    }
    dom.libraryBooks.append(card);
  }

  if (targetSlug) {
    const target = dom.libraryBooks.querySelector(`[data-book-slug="${CSS.escape(targetSlug)}"]`);
    window.setTimeout(() => {
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
      target?.querySelector("button")?.focus();
    }, 0);
  }
}

function orderedBooksForCampaign(kind) {
  const books = (store.bootstrap.books || []).filter(
    (book) => !book.type || book.type === kind,
  );
  const attached = kind === "source"
    ? store.campaign?.sources || []
    : (store.campaign?.modules || []).map((module) => module.slug || module);
  const rank = new Map(attached.map((slug, index) => [slug, index]));
  return [...books].sort((left, right) => {
    const leftRank = rank.has(left.slug) ? rank.get(left.slug) : Number.MAX_SAFE_INTEGER;
    const rightRank = rank.has(right.slug) ? rank.get(right.slug) : Number.MAX_SAFE_INTEGER;
    return leftRank - rightRank || bookTitle(left).localeCompare(bookTitle(right));
  });
}

function checkedValues(container) {
  return Array.from(container.querySelectorAll('input[type="checkbox"]:checked')).map(
    (input) => input.value,
  );
}

function syncCampaignLibrarySelects(useCampaignDefaults = false) {
  const selectedSources = checkedValues(dom.campaignSourceOptions);
  const selectedModules = checkedValues(dom.campaignModuleOptions);
  const previousSystem = useCampaignDefaults
    ? store.campaign?.system_source || ""
    : dom.campaignSystemSource.value || store.campaign?.system_source || "";
  const previousActive = useCampaignDefaults
    ? store.campaign?.active_module || ""
    : dom.campaignActiveModule.value || store.campaign?.active_module || "";

  const fill = (select, values, preferred, emptyLabel) => {
    select.replaceChildren();
    const empty = node("option", "", emptyLabel);
    empty.value = "";
    select.append(empty);
    for (const slug of values) {
      const book = (store.bootstrap.books || []).find((entry) => entry.slug === slug);
      const option = node("option", "", bookTitle(book || { slug }));
      option.value = slug;
      select.append(option);
    }
    select.value = values.includes(preferred) ? preferred : values[0] || "";
    select.disabled = values.length === 0;
  };
  fill(dom.campaignSystemSource, selectedSources, previousSystem, "No system source");
  fill(dom.campaignActiveModule, selectedModules, previousActive, "No active module");
}

function renderCampaignLibrary() {
  const hasCampaign = Boolean(store.campaign?.slug);
  dom.campaignLibraryPanel.hidden = !hasCampaign;
  if (!hasCampaign) return;

  const sourceSet = new Set(store.campaign.sources || []);
  const moduleSet = new Set(
    (store.campaign.modules || []).map((module) => module.slug || module),
  );
  const fill = (container, books, selected) => {
    container.replaceChildren();
    if (!books.length) {
      container.append(node("p", "book-options-empty", "No matching books in the library."));
      return;
    }
    for (const book of books) {
      const label = node("label", "book-option");
      const input = node("input");
      input.type = "checkbox";
      input.value = book.slug;
      input.checked = selected.has(book.slug);
      input.addEventListener("change", () => syncCampaignLibrarySelects(false));
      label.append(input, node("span", "", bookTitle(book)));
      container.append(label);
    }
  };
  fill(dom.campaignSourceOptions, orderedBooksForCampaign("source"), sourceSet);
  fill(dom.campaignModuleOptions, orderedBooksForCampaign("module"), moduleSet);
  syncCampaignLibrarySelects(true);
}

function jobIdentifier(payload) {
  return payload?.job_id || payload?.id || payload?.job?.id || payload?.job?.job_id || null;
}

function normalizedJob(payload) {
  const job = payload?.job && typeof payload.job === "object" ? payload.job : payload || {};
  const progress = job.progress && typeof job.progress === "object" ? job.progress : {};
  return {
    ...job,
    phase: progress.phase || progress.stage || job.phase || job.stage || "Working",
    message: progress.message || progress.detail || job.message || job.detail || "Work is underway.",
    completed: progress.completed ?? progress.current ?? job.completed ?? job.current,
    total: progress.total ?? job.total,
    round: progress.round ?? job.round,
    maxRounds: progress.max_rounds ?? progress.maxRounds ?? job.max_rounds ?? job.maxRounds,
    percent: progress.percent ?? job.percent,
  };
}

function terminalJobState(status) {
  return ["complete", "completed", "done", "success", "succeeded", "failed", "error", "cancelled", "canceled"].includes(
    String(status || "").toLowerCase(),
  );
}

function successfulJobState(status) {
  return ["complete", "completed", "done", "success", "succeeded"].includes(
    String(status || "").toLowerCase(),
  );
}

function appendJobActivity(message) {
  const clean = String(message || "").trim();
  if (!clean || store.libraryJobActivity.at(-1) === clean) return;
  store.libraryJobActivity.push(clean);
  store.libraryJobActivity = store.libraryJobActivity.slice(-6);
  dom.libraryJobActivity.replaceChildren(
    ...store.libraryJobActivity.map((entry) => node("li", "", entry)),
  );
  dom.libraryJobActivity.scrollTop = dom.libraryJobActivity.scrollHeight;
}

function renderLibraryJob(payload, { kind = "ingest", title = "Library job" } = {}) {
  const job = normalizedJob(payload);
  const status = String(job.status || job.state || "running").toLowerCase();
  const finished = terminalJobState(status);
  const succeeded = successfulJobState(status);
  dom.libraryJob.hidden = false;
  dom.libraryJob.classList.toggle("is-complete", succeeded);
  dom.libraryJob.classList.toggle("is-failed", finished && !succeeded);
  dom.libraryJobKicker.textContent = kind === "template" ? "Research expedition" : "Building the library";
  dom.libraryJobTitle.textContent = job.title || job.label || title;
  dom.libraryJobState.textContent = succeeded ? "Ready" : finished ? status : status === "queued" ? "Queued" : "Working";
  dom.libraryJobCancel.hidden = finished || !job.cancellable;
  dom.libraryJobCancel.disabled = finished;
  dom.libraryJobMessage.textContent = job.message;
  dom.jobGlyph.textContent = succeeded ? "OK" : kind === "template" ? "T" : "OA";

  const completed = Number(job.completed);
  const total = Number(job.total);
  if (Number.isFinite(completed) && Number.isFinite(total) && total > 0) {
    dom.libraryJobProgress.value = Math.max(0, Math.min(total, completed));
    dom.libraryJobProgress.max = total;
    const percent = Math.round((completed / total) * 100);
    dom.libraryJobProgressLabel.textContent = `${completed} of ${total} (${percent}%)`;
  } else if (Number.isFinite(Number(job.round)) && Number.isFinite(Number(job.maxRounds))) {
    dom.libraryJobProgress.value = Number(job.round);
    dom.libraryJobProgress.max = Number(job.maxRounds);
    dom.libraryJobProgressLabel.textContent = `Round ${job.round} of ${job.maxRounds}`;
  } else if (Number.isFinite(Number(job.percent))) {
    dom.libraryJobProgress.value = Math.max(0, Math.min(100, Number(job.percent)));
    dom.libraryJobProgress.max = 100;
    dom.libraryJobProgressLabel.textContent = `${Math.round(Number(job.percent))}%`;
  } else {
    dom.libraryJobProgress.removeAttribute("value");
    dom.libraryJobProgressLabel.textContent = finished ? "Finished" : job.phase;
  }
  if (Array.isArray(job.events)) {
    store.libraryJobActivity = job.events.slice(-6).map((item) => {
      const phase = item?.phase || job.phase;
      const message = item?.message || "";
      return phase === message ? message : `${phase}: ${message}`;
    });
    dom.libraryJobActivity.replaceChildren(
      ...store.libraryJobActivity.map((entry) => node("li", "", entry)),
    );
    dom.libraryJobActivity.scrollTop = dom.libraryJobActivity.scrollHeight;
  } else {
    appendJobActivity(job.phase === job.message ? job.message : `${job.phase}: ${job.message}`);
  }
  const error = job.error || (finished && !succeeded ? job.message : "");
  dom.libraryJobError.textContent = error;
  dom.libraryJobError.hidden = !error;
}

function stopLibraryJobPolling() {
  if (store.libraryJobTimer) window.clearTimeout(store.libraryJobTimer);
  store.libraryJobTimer = null;
}

async function pollLibraryJob(jobId, context) {
  if (!jobId || store.libraryJobId !== jobId) return;
  try {
    const payload = await api.libraryJob(jobId);
    renderLibraryJob(payload, context);
    const job = normalizedJob(payload);
    const status = job.status || job.state;
    if (terminalJobState(status)) {
      store.libraryJobId = null;
      stopLibraryJobPolling();
      await loadBootstrap();
      renderLibraryBooks();
      renderCampaignLibrary();
      if (successfulJobState(status)) {
        if (context.kind === "ingest") {
          dom.ingestForm.reset();
          dom.ingestFileLabel.textContent = "Choose a PDF, Markdown, or text file";
        }
        toast(context.kind === "template" ? "Character template ready." : "Book added to the library.");
      }
      return;
    }
  } catch (error) {
    renderLibraryJob(
      { status: "failed", message: errorMessage(error), error: errorMessage(error) },
      context,
    );
    store.libraryJobId = null;
    stopLibraryJobPolling();
    return;
  }
  store.libraryJobTimer = window.setTimeout(() => pollLibraryJob(jobId, context), 900);
}

function watchLibraryJob(payload, context) {
  const jobId = jobIdentifier(payload);
  if (!jobId) throw new Error("The server started work without returning a job id.");
  stopLibraryJobPolling();
  store.libraryJobId = jobId;
  store.libraryJobActivity = [];
  renderLibraryJob(
    { status: "queued", phase: "Queued", message: "The workbench is ready." },
    context,
  );
  renderLibraryBooks();
  pollLibraryJob(jobId, context);
}

async function startTemplateJob(book, model) {
  if (store.libraryJobId) {
    toast("Another library job is already running.");
    return;
  }
  const existing = templateDetails(book).ready;
  const overwrite = existing
    ? window.confirm(`Replace the existing character template for ${bookTitle(book)}?`)
    : false;
  if (existing && !overwrite) return;
  try {
    const payload = await api.startTemplate(book.slug, { model, overwrite });
    watchLibraryJob(payload, {
      kind: "template",
      title: `Researching ${bookTitle(book)}`,
    });
    dom.libraryJob.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    toast(errorMessage(error), "error");
  }
}

async function openLibrary({ focusIngest = false, templateSlug = null, bookType = null } = {}) {
  await loadBootstrap();
  try {
    const overview = await api.library();
    if (Array.isArray(overview?.books)) store.bootstrap.books = overview.books;
    if (Array.isArray(overview?.models)) store.bootstrap.models = overview.models;
    if (overview?.utility_model) store.bootstrap.utilityModel = overview.utility_model;
    const activeJob = (overview?.jobs || []).find((job) =>
      ["queued", "running"].includes(String(job?.status || "").toLowerCase()),
    );
    if (activeJob && !store.libraryJobId) {
      store.libraryJobId = jobIdentifier(activeJob);
      store.libraryJobActivity = [];
      const context = {
        kind: activeJob.kind || "ingest",
        title: activeJob.label || "Library job",
      };
      renderLibraryJob(activeJob, context);
      pollLibraryJob(store.libraryJobId, context);
    }
  } catch {
    // Bootstrap already has enough data to manage the library. The overview is
    // an enhancement that reconnects a refreshed page to an active long job.
  }
  store.targetedTemplate = templateSlug;
  renderLibraryBooks(templateSlug);
  if (templateSlug && !(store.bootstrap.books || []).some((book) => book.slug === templateSlug)) {
    toast(`No library book named ${templateSlug}.`, "error");
  }
  renderCampaignLibrary();
  dom.ingestError.hidden = true;
  if (bookType) dom.ingestType.value = bookType;
  dom.ingestPanel.hidden = !focusIngest;
  if (!dom.libraryDialog.open) dom.libraryDialog.showModal();
  window.setTimeout(() => {
    if (focusIngest) dom.ingestFile.focus();
    else if (!templateSlug) dom.showIngestButton.focus();
  }, 0);
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
      models: Array.isArray(payload?.models) ? payload.models : [],
      utilityModel: payload?.utility_model || payload?.utilityModel || "",
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
  const previousSlug = store.slug;
  stopEventPolling();
  stopUsageRefresh();
  if (previousSlug && previousSlug !== slug) stopBrowserMedia();
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
    syncUsageRefresh();
  } catch (error) {
    appendTranscript(systemMessage(errorMessage(error), { error: true, label: "OpenAdventure" }));
    setTurnStatus("Campaign could not open", "error");
  }
}

function showLibrary({ updateUrl = true } = {}) {
  if (store.busy) cancelTurn();
  stopEventPolling();
  stopUsageRefresh();
  stopBrowserMedia();
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
    case "/library":
      await openLibrary();
      return true;
    case "/ingest":
      await openLibrary({
        focusIngest: true,
        bookType: /(?:^|\s)--module(?:\s|$)/.test(args) ? "module" : "source",
      });
      return true;
    case "/template": {
      const target = args.split(/\s+/).find((part) => part && !part.startsWith("--")) || null;
      await openLibrary({ templateSlug: target });
      if (!target) toast("Choose a rules source, then select Build template.");
      return true;
    }
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

function mediaSetting(key, fallback = false) {
  const enabledKeys = {
    tts_enabled: "narration",
    sound_effects_enabled: "sound_effects",
    images_enabled: "images",
    music_enabled: "music",
  };
  const automaticKeys = {
    images_auto: "images",
    music_auto: "music",
  };
  const value =
    store.media?.[key] ??
    (enabledKeys[key] ? store.media?.enabled?.[enabledKeys[key]] : undefined) ??
    (automaticKeys[key] ? store.media?.automatic?.[automaticKeys[key]] : undefined) ??
    store.campaign?.[key] ??
    store.campaign?.settings?.[key];
  return value === undefined || value === null ? fallback : value;
}

function syncMediaSettingsControls() {
  dom.settingsImagesAuto.disabled = !dom.settingsImages.checked;
  dom.settingsMusicAuto.disabled = !dom.settingsMusic.checked;
  dom.settingsMusicVolume.disabled = !dom.settingsMusic.checked;
  const percent = Math.round(clampVolume(dom.settingsMusicVolume.value) * 100);
  dom.settingsMusicVolumeOutput.value = `${percent}%`;
  dom.settingsMusicVolumeOutput.textContent = `${percent}%`;

  const backends = store.media?.backends || {};
  const unavailable = Object.entries(backends)
    .filter(([, value]) => value && value.ready === false)
    .map(([name]) => name.replaceAll("_", " "));
  dom.mediaSettingsStatus.textContent = unavailable.length
    ? `Needs server configuration: ${unavailable.join(", ")}.`
    : "Available media backends are ready.";
  dom.mediaSettingsStatus.classList.toggle("is-warning", unavailable.length > 0);
  const missing = missingCredentialRequests();
  dom.settingsCredentialsButton.hidden = missing.length === 0;
  dom.settingsCredentialsButton.textContent =
    missing.length > 1 ? "Add missing API keys" : "Add missing API key";
}

function openSettings() {
  if (!store.campaign) return;
  const settings = store.settings || store.campaign.settings || {};
  dom.settingsMode.value = store.campaign.mode || "gm";
  populateModelSelect(dom.settingsModel, settings.model || store.campaign.settings?.model || "");
  syncModelSettingsControls();
  dom.settingsEffort.value = settings.effort || "high";
  dom.settingsThinking.checked = Boolean(settings.thinking);
  dom.settingsVerbosity.value = settings.verbosity || "medium";
  dom.settingsContext.value = settings.context_budget || "";
  dom.settingsTts.checked = Boolean(mediaSetting("tts_enabled"));
  dom.settingsSfx.checked = Boolean(mediaSetting("sound_effects_enabled"));
  dom.settingsImages.checked = Boolean(mediaSetting("images_enabled"));
  dom.settingsImagesAuto.checked = Boolean(mediaSetting("images_auto"));
  dom.settingsMusic.checked = Boolean(mediaSetting("music_enabled"));
  dom.settingsMusicAuto.checked = Boolean(mediaSetting("music_auto"));
  dom.settingsMusicVolume.value = String(clampVolume(mediaSetting("music_volume", 0.2)));
  syncMediaSettingsControls();
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
  dom.inspector.inert = true;
  dom.inspectorOverlay.hidden = true;
  dom.inspectorToggle.setAttribute("aria-expanded", "false");
  syncUsageRefresh();
}

function openInspector() {
  dom.inspector.inert = false;
  dom.inspector.classList.add("is-open");
  dom.inspectorOverlay.hidden = false;
  dom.inspectorToggle.setAttribute("aria-expanded", "true");
  syncUsageRefresh();
  window.setTimeout(() => dom.inspectorClose.focus(), 0);
}

function setInspectorTab(tab) {
  if (!["party", "scene", "encounter", "clocks", "usage"].includes(tab)) return;
  store.inspectorTab = tab;
  localStorage.setItem("openadventure.inspectorTab", tab);
  document.querySelectorAll("[data-inspector-tab]").forEach((button) => {
    const selected = button.dataset.inspectorTab === tab;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  renderInspector(dom.inspectorContent, tab, store.gameState, store.campaign?.mode || "gm");
  syncUsageRefresh();
}

function stopUsageRefresh() {
  if (store.usageRefreshTimer) window.clearInterval(store.usageRefreshTimer);
  store.usageRefreshTimer = null;
}

function shouldRefreshUsage() {
  return (
    Boolean(store.slug) &&
    store.inspectorTab === "usage" &&
    dom.inspector.classList.contains("is-open") &&
    !document.hidden
  );
}

async function refreshUsage() {
  if (!shouldRefreshUsage() || store.usageRefreshInFlight) return;
  const slug = store.slug;
  store.usageRefreshInFlight = true;
  try {
    const payload = await api.usage(slug);
    const usage = payload?.usage;
    if (!usage || typeof usage !== "object" || store.slug !== slug) return;
    store.usage = usage;
    store.gameState = { ...store.gameState, usage };
    renderInspector(dom.inspectorContent, store.inspectorTab, store.gameState, store.campaign?.mode || "gm");
  } catch (error) {
    // The regular campaign poll owns connection status. A failed optional
    // refresh should leave the last usable report visible.
    console.warn("Usage refresh failed:", error);
  } finally {
    store.usageRefreshInFlight = false;
  }
}

function syncUsageRefresh() {
  if (!shouldRefreshUsage()) {
    stopUsageRefresh();
    return;
  }
  refreshUsage();
  if (!store.usageRefreshTimer) {
    store.usageRefreshTimer = window.setInterval(refreshUsage, 2000);
  }
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
    if (!store.bootstrap.models.length) await loadBootstrap();
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
dom.libraryButton.addEventListener("click", () => openLibrary());
dom.playLibraryButton.addEventListener("click", () => openLibrary());
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

dom.showIngestButton.addEventListener("click", () => {
  dom.ingestPanel.hidden = false;
  window.setTimeout(() => dom.ingestFile.focus(), 0);
});

dom.hideIngestButton.addEventListener("click", () => {
  dom.ingestPanel.hidden = true;
});

dom.libraryJobCancel.addEventListener("click", async () => {
  if (!store.libraryJobId) return;
  dom.libraryJobCancel.disabled = true;
  try {
    const payload = await api.cancelLibraryJob(store.libraryJobId);
    toast(payload?.cancelled ? "Stopping template generation..." : "This job is already finishing.");
  } catch (error) {
    toast(errorMessage(error), "error");
    dom.libraryJobCancel.disabled = false;
  }
});

dom.ingestFile.addEventListener("change", () => {
  const file = dom.ingestFile.files?.[0];
  dom.ingestFileLabel.textContent = file ? file.name : "Choose a PDF, Markdown, or text file";
});

for (const type of ["dragenter", "dragover"]) {
  dom.ingestDropZone.addEventListener(type, (event) => {
    event.preventDefault();
    dom.ingestDropZone.classList.add("is-dragging");
  });
}

for (const type of ["dragleave", "drop"]) {
  dom.ingestDropZone.addEventListener(type, (event) => {
    event.preventDefault();
    dom.ingestDropZone.classList.remove("is-dragging");
  });
}

dom.ingestDropZone.addEventListener("drop", (event) => {
  const files = event.dataTransfer?.files;
  if (!files?.length) return;
  dom.ingestFile.files = files;
  dom.ingestFile.dispatchEvent(new Event("change"));
});

dom.ingestForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  dom.ingestError.hidden = true;
  if (store.libraryJobId) {
    dom.ingestError.textContent = "Wait for the current library job to finish.";
    dom.ingestError.hidden = false;
    return;
  }
  const file = dom.ingestFile.files?.[0];
  if (!file) {
    dom.ingestError.textContent = "Choose a PDF, Markdown, or text file.";
    dom.ingestError.hidden = false;
    return;
  }
  const extension = file.name.toLowerCase().match(/\.(pdf|md|markdown|txt)$/)?.[1];
  if (!extension) {
    dom.ingestError.textContent = "Supported files are PDF, Markdown, and plain text.";
    dom.ingestError.hidden = false;
    return;
  }
  const pages = dom.ingestPages.value.trim();
  if (pages && !/^\d+(?:-\d+)?$/.test(pages)) {
    dom.ingestError.textContent = "Use a page or range such as 18-32.";
    dom.ingestError.hidden = false;
    return;
  }
  if (pages) {
    const [start, end = start] = pages.split("-").map(Number);
    if (start < 1 || end < start) {
      dom.ingestError.textContent = "The page range must start at 1 or later and end after it starts.";
      dom.ingestError.hidden = false;
      return;
    }
  }
  if (pages && extension !== "pdf") {
    dom.ingestError.textContent = "Page ranges are only available for PDF files.";
    dom.ingestError.hidden = false;
    return;
  }

  dom.ingestSubmit.disabled = true;
  dom.ingestSubmit.textContent = "Uploading...";
  store.libraryJobActivity = [];
  renderLibraryJob(
    { status: "running", phase: "Uploading", message: `Sending ${file.name} to the local workbench.` },
    { kind: "ingest", title: `Ingesting ${file.name}` },
  );
  dom.libraryJob.scrollIntoView({ behavior: "smooth", block: "center" });
  try {
    const payload = await api.ingest(file, {
      bookType: dom.ingestType.value,
      name: dom.ingestName.value.trim(),
      pages,
    });
    watchLibraryJob(payload, { kind: "ingest", title: `Ingesting ${file.name}` });
  } catch (error) {
    const message = errorMessage(error);
    dom.ingestError.textContent = message;
    dom.ingestError.hidden = false;
    renderLibraryJob(
      { status: "failed", phase: "Upload failed", message, error: message },
      { kind: "ingest", title: `Ingesting ${file.name}` },
    );
  } finally {
    dom.ingestSubmit.disabled = false;
    dom.ingestSubmit.textContent = "Start ingestion";
  }
});

dom.campaignLibraryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!store.slug || !store.campaign) return;
  dom.campaignLibraryError.hidden = true;
  const payload = {
    sources: checkedValues(dom.campaignSourceOptions),
    system_source: dom.campaignSystemSource.value || null,
    modules: checkedValues(dom.campaignModuleOptions),
    active_module: dom.campaignActiveModule.value || null,
  };
  dom.campaignLibrarySubmit.disabled = true;
  dom.campaignLibrarySubmit.textContent = "Saving...";
  try {
    const response = await api.updateLibrary(store.slug, payload);
    const campaign = response?.campaign || response?.meta || response;
    if (campaign && typeof campaign === "object") {
      store.campaign = { ...store.campaign, ...campaign };
      if (response?.settings) store.settings = response.settings;
      if (response?.media) store.media = response.media;
      updateCampaignHeader();
      if (response?.state) applyState(response.state);
    }
    renderCampaignLibrary();
    await loadBootstrap();
    toast("Campaign books updated.");
  } catch (error) {
    dom.campaignLibraryError.textContent = errorMessage(error);
    dom.campaignLibraryError.hidden = false;
  } finally {
    dom.campaignLibrarySubmit.disabled = false;
    dom.campaignLibrarySubmit.textContent = "Save campaign books";
  }
});

for (const input of [dom.settingsImages, dom.settingsMusic, dom.settingsMusicVolume]) {
  input.addEventListener("input", syncMediaSettingsControls);
  input.addEventListener("change", syncMediaSettingsControls);
}

dom.settingsModel.addEventListener("change", syncModelSettingsControls);

dom.settingsCredentialsButton.addEventListener("click", () => {
  for (const service of missingCredentialRequests()) {
    store.dismissedCredentials.delete(credentialDismissalKey(service));
  }
  promptForMissingCredentials({ force: true });
});

dom.credentialForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const service = store.credentialRequest;
  const apiKey = dom.credentialKey.value.trim();
  if (!service || !store.slug) return;
  if (!apiKey) {
    dom.credentialError.textContent = "Enter an API key to continue.";
    dom.credentialError.hidden = false;
    return;
  }
  dom.credentialError.hidden = true;
  dom.credentialSubmit.disabled = true;
  dom.credentialSubmit.textContent = "Saving...";
  try {
    const response = await api.saveCredential(store.slug, { service, apiKey });
    store.dismissedCredentials.delete(credentialDismissalKey(service));
    store.credentialRequest = null;
    dom.credentialDialog.close();
    applyPayload(response, { renderTranscript: false });
    toast("API key saved locally. The service is ready to use.");
  } catch (error) {
    dom.credentialError.textContent = errorMessage(error);
    dom.credentialError.hidden = false;
  } finally {
    dom.credentialSubmit.disabled = false;
    dom.credentialSubmit.textContent = "Save API key";
  }
});

dom.credentialDialog.addEventListener("close", () => {
  const service = store.credentialRequest;
  if (service) store.dismissedCredentials.add(credentialDismissalKey(service));
  store.credentialRequest = null;
  dom.credentialForm.reset();
});

dom.mediaPlayButton.addEventListener("click", () => {
  const player = activeMediaPlayer();
  if (!player?.src) {
    restoreMediaDock();
    return;
  }
  if (player.paused) {
    playBrowserAudio(
      player,
      store.audio.active,
      dom.mediaDockTitle.textContent,
      store.audio.active === "music" ? "Looping" : "",
    );
  } else {
    player.pause();
    setMediaDock(dom.mediaDockKind.textContent, dom.mediaDockTitle.textContent, "Paused");
  }
});

dom.foregroundAudioPlayer.addEventListener("ended", finishForegroundClip);

dom.foregroundAudioPlayer.addEventListener("error", () => {
  const failedClip = store.audio.currentClip;
  if (!failedClip) return;
  dom.mediaDockStatus.textContent = "This audio file could not be played. Continuing to the next clip.";
  dom.mediaDock.classList.add("is-paused");
  window.setTimeout(() => {
    if (store.audio.currentClip === failedClip) finishForegroundClip();
  }, 900);
});

dom.musicPlayer.addEventListener("play", () => {
  if (store.audio.active === "music") {
    setMediaDock("Background music", store.audio.musicTitle || "Campaign ambience", "Looping");
  }
});

dom.musicPlayer.addEventListener("pause", () => {
  if (store.audio.active === "music" && dom.musicPlayer.src) {
    setMediaDock("Background music", store.audio.musicTitle || "Campaign ambience", "Paused");
  }
});

dom.mediaDockVolume.addEventListener("input", () => {
  const volume = clampVolume(dom.mediaDockVolume.value);
  dom.musicPlayer.volume = volume;
  store.media.music_volume = volume;
  dom.settingsMusicVolume.value = String(volume);
  syncMediaSettingsControls();
});

dom.mediaDockVolume.addEventListener("change", () => {
  if (!store.slug) return;
  const volume = clampVolume(dom.mediaDockVolume.value);
  if (store.audio.volumeTimer) window.clearTimeout(store.audio.volumeTimer);
  store.audio.volumeTimer = window.setTimeout(async () => {
    try {
      await api.updateSettings(store.slug, { music_volume: volume });
    } catch (error) {
      toast(errorMessage(error), "error");
    }
  }, 180);
});

document.querySelectorAll("[data-message-kind]").forEach((button) => {
  button.addEventListener("click", () => setMessageKind(button.dataset.messageKind));
});

document.querySelectorAll("[data-inspector-tab]").forEach((button) => {
  button.addEventListener("click", () => setInspectorTab(button.dataset.inspectorTab));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const tabs = ["party", "scene", "encounter", "clocks", "usage"];
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
    model: dom.settingsModel.value,
    effort: dom.settingsEffort.value,
    thinking: dom.settingsThinking.checked,
    verbosity: dom.settingsVerbosity.value,
    tts_enabled: dom.settingsTts.checked,
    sound_effects_enabled: dom.settingsSfx.checked,
    images_enabled: dom.settingsImages.checked,
    images_auto: dom.settingsImages.checked && dom.settingsImagesAuto.checked,
    music_enabled: dom.settingsMusic.checked,
    music_auto: dom.settingsMusic.checked && dom.settingsMusicAuto.checked,
    music_volume: clampVolume(dom.settingsMusicVolume.value),
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
  if (document.hidden) {
    stopEventPolling();
    stopUsageRefresh();
  } else {
    startEventPolling();
    syncUsageRefresh();
  }
});

window.addEventListener("popstate", routeFromLocation);
window.addEventListener("beforeunload", () => {
  stopEventPolling();
  stopUsageRefresh();
  stopLibraryJobPolling();
});

setInspectorTab(store.inspectorTab);
setMessageKind("normal");
routeFromLocation();
