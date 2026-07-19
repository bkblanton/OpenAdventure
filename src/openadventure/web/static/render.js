const SPECIAL_BLOCK = /^(#{1,4}\s+|```|~~~|>\s?|[-+*]\s+|\d+[.)]\s+|(?:---+|___+|\*\*\*+)\s*$)/;

export function node(tag, className = "", text = null) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== null && text !== undefined) element.textContent = String(text);
  return element;
}

export function safeMediaUrl(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value, window.location.href);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.origin !== window.location.origin) return null;
    return url.href;
  } catch {
    return null;
  }
}

function safeLinkUrl(value) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value, window.location.href);
    if (!["http:", "https:", "mailto:"].includes(url.protocol)) return null;
    return url.href;
  } catch {
    return null;
  }
}

function appendInline(parent, text) {
  const source = String(text ?? "");
  const inlineToken = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^\s)]+(?:\s+"[^"]*")?\))/g;
  let cursor = 0;
  let match;

  while ((match = inlineToken.exec(source)) !== null) {
    if (match.index > cursor) parent.append(document.createTextNode(source.slice(cursor, match.index)));
    const token = match[0];

    if (token.startsWith("`")) {
      parent.append(node("code", "", token.slice(1, -1)));
    } else if (token.startsWith("**") || token.startsWith("__")) {
      const strong = node("strong");
      appendInline(strong, token.slice(2, -2));
      parent.append(strong);
    } else if (token.startsWith("*") || token.startsWith("_")) {
      const emphasis = node("em");
      appendInline(emphasis, token.slice(1, -1));
      parent.append(emphasis);
    } else if (token.startsWith("[")) {
      const closing = token.indexOf("](");
      const label = token.slice(1, closing);
      const targetPart = token.slice(closing + 2, -1);
      const rawTarget = targetPart.match(/^([^\s]+)/)?.[1] || "";
      const target = safeLinkUrl(rawTarget);
      if (target) {
        const link = node("a", "", label);
        link.href = target;
        if (target.startsWith("http") && new URL(target).origin !== window.location.origin) {
          link.target = "_blank";
          link.rel = "noopener noreferrer";
        }
        parent.append(link);
      } else {
        parent.append(document.createTextNode(label));
      }
    } else {
      parent.append(document.createTextNode(token));
    }
    cursor = inlineToken.lastIndex;
  }

  if (cursor < source.length) parent.append(document.createTextNode(source.slice(cursor)));
}

function isTableDivider(line) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells = trimmed.split("|").map((cell) => cell.trim());
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function tableCells(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function startsBlock(lines, index) {
  const line = lines[index] || "";
  if (!line.trim()) return true;
  if (SPECIAL_BLOCK.test(line.trimStart())) return true;
  return index + 1 < lines.length && line.includes("|") && isTableDivider(lines[index + 1]);
}

function markdownFragment(markdown) {
  const fragment = document.createDocumentFragment();
  const lines = String(markdown ?? "")
    .replace(/\r\n?/g, "\n")
    .split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) {
      index += 1;
      continue;
    }

    const fence = trimmed.match(/^(```|~~~)(.*)$/);
    if (fence) {
      const marker = fence[1];
      const language = fence[2].trim();
      const codeLines = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith(marker)) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = node("pre");
      const code = node("code", "", codeLines.join("\n"));
      if (language) code.dataset.language = language;
      pre.append(code);
      fragment.append(pre);
      continue;
    }

    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const element = node(`h${heading[1].length}`);
      appendInline(element, heading[2]);
      fragment.append(element);
      index += 1;
      continue;
    }

    if (/^(---+|___+|\*\*\*+)\s*$/.test(trimmed)) {
      fragment.append(node("hr"));
      index += 1;
      continue;
    }

    if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const headers = tableCells(line);
      const table = node("table");
      const head = node("thead");
      const headRow = node("tr");
      for (const value of headers) {
        const cell = node("th");
        appendInline(cell, value);
        headRow.append(cell);
      }
      head.append(headRow);
      table.append(head);
      index += 2;
      const body = node("tbody");
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        const row = node("tr");
        const cells = tableCells(lines[index]);
        for (let cellIndex = 0; cellIndex < headers.length; cellIndex += 1) {
          const cell = node("td");
          appendInline(cell, cells[cellIndex] || "");
          row.append(cell);
        }
        body.append(row);
        index += 1;
      }
      table.append(body);
      const wrap = node("div", "table-wrap");
      wrap.append(table);
      fragment.append(wrap);
      continue;
    }

    if (/^>\s?/.test(trimmed)) {
      const quoteLines = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      const quote = node("blockquote");
      quote.append(markdownFragment(quoteLines.join("\n")));
      fragment.append(quote);
      continue;
    }

    const listMatch = trimmed.match(/^([-+*]|\d+[.)])\s+(.+)$/);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]);
      const list = node(ordered ? "ol" : "ul");
      while (index < lines.length) {
        const itemMatch = lines[index].trim().match(/^([-+*]|\d+[.)])\s+(.+)$/);
        if (!itemMatch || /^\d/.test(itemMatch[1]) !== ordered) break;
        const item = node("li");
        appendInline(item, itemMatch[2]);
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    }

    const paragraphLines = [line.trim()];
    index += 1;
    while (index < lines.length && !startsBlock(lines, index)) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    const paragraph = node("p");
    paragraphLines.forEach((paragraphLine, lineIndex) => {
      if (lineIndex > 0) paragraph.append(document.createTextNode(" "));
      appendInline(paragraph, paragraphLine);
    });
    fragment.append(paragraph);
  }

  return fragment;
}

export function renderMarkdown(container, markdown) {
  container.replaceChildren(markdownFragment(markdown));
}

export function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
}

function asArray(value) {
  if (Array.isArray(value)) return value;
  if (value && Array.isArray(value.items)) return value.items;
  return [];
}

function primitive(value) {
  return ["string", "number", "boolean"].includes(typeof value);
}

function displayValue(value) {
  if (Array.isArray(value)) return value.filter(primitive).join(", ");
  if (primitive(value)) return String(value);
  return "";
}

function emptyState(mark, title, copy) {
  const empty = node("div", "state-empty");
  empty.append(node("div", "state-empty-mark", mark));
  empty.append(node("h3", "", title));
  empty.append(node("p", "", copy));
  return empty;
}

function rawState(value) {
  if (typeof value === "string") return node("p", "raw-state", value);
  return node("p", "raw-state", "No current information.");
}

function resourcesFor(sheet) {
  const resources = sheet?.resources;
  if (!resources || typeof resources !== "object" || Array.isArray(resources)) return [];
  return Object.entries(resources).filter(([, resource]) => resource && typeof resource === "object");
}

function sheetCard(sheet, companion = false) {
  const card = node("article", "sheet-card");
  const heading = node("div", "sheet-heading");
  const titleWrap = node("div");
  titleWrap.append(node("h3", "", sheet.name || sheet.id || "Unnamed character"));
  const labels = [companion ? "companion" : sheet.kind, sheet.status]
    .filter(Boolean)
    .join(" · ");
  titleWrap.append(node("span", "sheet-kind", labels || "party member"));
  heading.append(titleWrap);
  card.append(heading);

  const fields = sheet.fields && typeof sheet.fields === "object" ? sheet.fields : {};
  const summaryFields = Object.entries(fields)
    .filter(([key, value]) => !["name", "secret", "hidden_notes", "gm_notes"].includes(key) && primitive(value))
    .slice(0, 5);
  if (summaryFields.length) {
    const summary = summaryFields.map(([key, value]) => `${key}: ${value}`).join(" · ");
    card.append(node("p", "sheet-summary", summary));
  }

  const resources = resourcesFor(sheet);
  if (resources.length) {
    const list = node("div", "resource-list");
    for (const [name, resource] of resources) {
      const current = Number(resource.current ?? 0);
      const maximum = Number(resource.max ?? 0);
      const minimum = Number(resource.min ?? 0);
      const span = maximum - minimum;
      const percentage = span > 0 ? Math.max(0, Math.min(100, ((current - minimum) / span) * 100)) : 0;
      const item = node("div", "resource");
      const head = node("div", "resource-head");
      head.append(node("span", "", name));
      head.append(node("span", "", `${current}/${maximum}`));
      const track = node("div", "resource-track");
      const fill = node("div", `resource-fill${percentage <= 25 ? " is-low" : ""}`);
      fill.style.width = `${percentage}%`;
      track.append(fill);
      item.append(head, track);
      list.append(item);
    }
    card.append(list);
  }

  const conditions = asArray(sheet.conditions).filter(primitive);
  if (conditions.length) {
    const list = node("ul", "condition-list");
    for (const condition of conditions) list.append(node("li", "condition-chip", condition));
    card.append(list);
  }

  const items = asArray(sheet.items).filter(primitive).slice(0, 6);
  if (items.length) {
    const list = node("ul", "field-chips");
    for (const item of items) list.append(node("li", "field-chip", item));
    card.append(list);
  }
  return card;
}

function renderParty(container, state) {
  const partyValue = state.party ?? state.characters;
  if (typeof partyValue === "string") {
    container.append(rawState(partyValue));
    return;
  }
  const party = asArray(partyValue);
  const companions = asArray(state.companions);
  if (!party.length && !companions.length) {
    container.append(
      emptyState("PC", "No party yet", "Ask the GM to create characters or import an existing sheet."),
    );
    return;
  }

  if (party.length) {
    const section = node("section", "state-section");
    section.append(node("h3", "state-section-title", "Adventurers"));
    for (const sheet of party) section.append(sheetCard(sheet));
    container.append(section);
  }
  if (companions.length) {
    const section = node("section", "state-section");
    section.append(node("h3", "state-section-title", "Traveling with you"));
    for (const sheet of companions) section.append(sheetCard(sheet, true));
    container.append(section);
  }
}

function renderScene(container, state) {
  const scene = state.scene;
  if (!scene) {
    container.append(emptyState("◎", "No scene set", "The current location will appear after the GM establishes it."));
    return;
  }
  if (typeof scene === "string") {
    container.append(rawState(scene));
    return;
  }

  const card = node("article", "scene-card");
  card.append(node("h3", "", scene.location || "Current scene"));
  if (scene.time) card.append(node("p", "scene-time", scene.time));
  if (scene.description) card.append(node("p", "scene-description", scene.description));

  const exits = asArray(scene.obvious_exits ?? scene.exits).filter(primitive);
  if (exits.length) {
    card.append(node("h4", "state-section-title", "Visible exits"));
    const list = node("ul", "scene-list");
    for (const exit of exits) list.append(node("li", "", exit));
    card.append(list);
  }

  const nearby = asArray(scene.unresolved_options ?? scene.nearby).filter(primitive);
  if (nearby.length) {
    card.append(node("h4", "state-section-title", "Nearby"));
    const list = node("ul", "scene-list");
    for (const option of nearby) list.append(node("li", "", option));
    card.append(list);
  }

  const flags = scene.flags && typeof scene.flags === "object" ? scene.flags : null;
  if (flags && Object.keys(flags).length) {
    const details = node("dl", "state-key-value");
    for (const [key, value] of Object.entries(flags)) {
      if (!primitive(value)) continue;
      const row = node("div");
      row.append(node("dt", "", key), node("dd", "", value));
      details.append(row);
    }
    card.append(details);
  }
  container.append(card);
}

function combatantHp(combatant) {
  if (combatant.hp && typeof combatant.hp === "object") {
    return `${combatant.hp.current ?? 0}/${combatant.hp.max ?? 0} HP`;
  }
  if (primitive(combatant.hp)) return `${combatant.hp} HP`;
  const sheetHp = combatant.sheet?.resources?.hp;
  if (sheetHp && typeof sheetHp === "object") {
    return `${sheetHp.current ?? 0}/${sheetHp.max ?? 0} HP`;
  }
  return "";
}

function renderEncounter(container, state) {
  const encounter = state.encounter;
  if (!encounter) {
    container.append(emptyState("⚔", "No active encounter", "Initiative and combatants appear here when a fight begins."));
    return;
  }
  if (typeof encounter === "string") {
    container.append(rawState(encounter));
    return;
  }
  if (encounter.status && encounter.status !== "active") {
    container.append(emptyState("⚔", "Encounter ended", encounter.name || "The field is quiet for now."));
    return;
  }

  const card = node("article", "encounter-card");
  const heading = node("div", "encounter-heading");
  heading.append(node("h3", "", encounter.name || "Active encounter"));
  heading.append(node("span", "encounter-round", `Round ${encounter.round ?? 1}`));
  card.append(heading);

  const combatants = asArray(encounter.combatants);
  if (combatants.length) {
    const list = node("div", "combatant-list");
    const turnIndex = Number(encounter.turn_index ?? -1);
    const currentTag = encounter.current?.tag || encounter.current_tag;
    combatants.forEach((combatant, index) => {
      const current = index === turnIndex || (currentTag && combatant.tag === currentTag);
      const down = combatant.active === false;
      const row = node("div", `combatant${current ? " is-current" : ""}${down ? " is-down" : ""}`);
      row.append(node("span", "initiative", combatant.initiative ?? "–"));
      const copy = node("div");
      copy.append(node("div", "combatant-name", combatant.tag || combatant.name || "Combatant"));
      const detail = [combatant.side, combatantHp(combatant), down ? "down" : ""]
        .filter(Boolean)
        .join(" · ");
      if (detail) copy.append(node("div", "combatant-detail", detail));
      row.append(copy);
      row.append(node("span", "combatant-side", current ? "Current" : ""));
      list.append(row);
    });
    card.append(list);
  } else if (encounter.summary) {
    card.append(node("p", "raw-state", encounter.summary));
  }
  container.append(card);
}

function clocksFrom(state) {
  if (typeof state.clocks === "string") return state.clocks;
  if (Array.isArray(state.clocks)) return state.clocks;
  if (state.clocks && Array.isArray(state.clocks.clocks)) return state.clocks.clocks;
  return [];
}

function renderClocks(container, state, mode) {
  const clockValue = clocksFrom(state);
  if (typeof clockValue === "string") {
    container.append(rawState(clockValue));
    return;
  }
  const clocks = clockValue.filter(
    (clock) => clock && clock.status !== "cancelled" && (mode === "assistant" || clock.visible !== false),
  );
  if (!clocks.length) {
    container.append(emptyState("◴", "No visible clocks", "Looming threats and deadlines appear here as they emerge."));
    return;
  }

  for (const clock of clocks) {
    const size = Math.max(1, Math.min(12, Number(clock.size ?? 4)));
    const filled = Math.max(0, Math.min(size, Number(clock.filled ?? 0)));
    const card = node("article", "clock-card");
    const heading = node("div", "clock-heading");
    heading.append(node("h3", "", clock.name || clock.id || "Progress clock"));
    heading.append(node("p", "", `${filled}/${size}`));
    card.append(heading);
    const segments = node("div", "clock-segments");
    segments.style.setProperty("--segments", String(size));
    for (let index = 0; index < size; index += 1) {
      segments.append(node("span", `clock-segment${index < filled ? " is-filled" : ""}`));
    }
    card.append(segments);
    if (clock.status === "filled") card.append(node("p", "clock-trigger", "Full"));
    if (mode === "assistant" && clock.trigger) {
      card.append(node("p", "clock-trigger", `Trigger: ${clock.trigger}`));
    }
    container.append(card);
  }
}

export function stateCounts(stateValue, mode = "gm") {
  const state = stateValue?.state && !stateValue.party ? stateValue.state : stateValue || {};
  const party = asArray(state.party ?? state.characters).length + asArray(state.companions).length;
  const clockValue = clocksFrom(state);
  const clocks = Array.isArray(clockValue)
    ? clockValue.filter(
        (clock) => clock?.status !== "cancelled" && (mode === "assistant" || clock?.visible !== false),
      ).length
    : 0;
  return { party, clocks };
}

function usageRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function usageNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

function usageField(usage, name, aliases = []) {
  for (const key of [name, ...aliases]) {
    if (key in usage) return usageNumber(usage[key]);
  }
  return 0;
}

function formatUsageNumber(value) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(usageNumber(value));
}

function formatUsageCost(value) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "Unavailable";
  const digits = amount < 0.01 ? 4 : amount < 1 ? 3 : 2;
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Math.max(0, amount));
}

function usageTokenTotal(usage) {
  return (
    usageField(usage, "input_tokens") +
    usageField(usage, "cache_read_input_tokens") +
    usageField(usage, "cache_creation_input_tokens") +
    usageField(usage, "output_tokens")
  );
}

function usageMetric(label, value, detail = "") {
  const item = node("div", "usage-metric");
  item.append(node("dt", "", label), node("dd", "", value));
  if (detail) item.append(node("small", "", detail));
  return item;
}

function usageBreakdownRow(label, value, note = "") {
  const row = node("div", "usage-breakdown-row");
  const copy = node("div");
  copy.append(node("dt", "", label));
  if (note) copy.append(node("small", "", note));
  row.append(copy, node("dd", "", value));
  return row;
}

function usageScopeCard(title, copy, rawUsage, cost) {
  const usage = usageRecord(rawUsage);
  const card = node("article", "usage-card");
  const heading = node("div", "usage-card-heading");
  const titleWrap = node("div");
  titleWrap.append(node("h3", "", title), node("p", "", copy));
  heading.append(titleWrap, node("span", "usage-cost", formatUsageCost(cost)));
  card.append(heading);

  const metrics = node("dl", "usage-metrics");
  metrics.append(
    usageMetric("Estimated token usage", formatUsageNumber(usageTokenTotal(usage))),
    usageMetric("Estimated cost", formatUsageCost(cost), "Model list-price estimate"),
  );
  card.append(metrics);

  const outputTokens = usageField(usage, "output_tokens");
  const thinkingTokens = Math.min(outputTokens, usageField(usage, "thinking_tokens"));
  const textTokens = Math.max(outputTokens - thinkingTokens, 0);
  const breakdown = node("dl", "usage-breakdown");
  breakdown.append(
    usageBreakdownRow("Input", formatUsageNumber(usageField(usage, "input_tokens"))),
    usageBreakdownRow("Cache read", formatUsageNumber(usageField(usage, "cache_read_input_tokens"))),
    usageBreakdownRow(
      "Cache write",
      formatUsageNumber(usageField(usage, "cache_creation_input_tokens")),
    ),
    usageBreakdownRow("Output", formatUsageNumber(outputTokens)),
    usageBreakdownRow("Text output", formatUsageNumber(textTokens), "Output excluding thinking"),
    usageBreakdownRow("Thinking", formatUsageNumber(thinkingTokens), "Included in output"),
  );

  const media = [
    ["Images", usageField(usage, "image_count", ["images_generated"]), "generated"],
    ["Narration", usageField(usage, "tts_characters", ["narration_characters"]), "characters"],
    ["Sound effects", usageField(usage, "sound_effect_seconds"), "seconds"],
    ["Music", usageField(usage, "music_seconds"), "seconds"],
  ];
  for (const [label, value, unit] of media) {
    if (value > 0) breakdown.append(usageBreakdownRow(label, `${formatUsageNumber(value)} ${unit}`));
  }
  card.append(breakdown);
  return card;
}

function renderUsage(container, state) {
  const report = usageRecord(state.usage);
  if (!Object.keys(report).length) {
    container.append(
      emptyState("$", "No usage yet", "Usage estimates will appear after the first model or media request."),
    );
    return;
  }
  const session = usageRecord(report.session);
  const totals = usageRecord(report.totals);
  const section = node("section", "usage-panel");
  const intro = node("div", "usage-intro");
  intro.append(node("h3", "state-section-title", "Usage so far"));
  intro.append(
    node(
      "p",
      "",
      "Usage reflects reported model counts where available; costs are rough list-price estimates. Thinking is billed as output and shown separately.",
    ),
  );
  section.append(intro);

  const scopes = node("div", "usage-scope-grid");
  scopes.append(
    usageScopeCard(
      "This session",
      "Activity since this campaign session opened.",
      session,
      report.session_cost_usd,
    ),
    usageScopeCard(
      "Campaign total",
      "All recorded activity for this campaign.",
      totals,
      report.cost_usd,
    ),
  );
  section.append(scopes);

  const byModel = usageRecord(report.by_model);
  const models = Object.entries(byModel).filter(([, usage]) => usage && typeof usage === "object");
  if (models.length) {
    const modelSection = node("section", "usage-models");
    modelSection.append(node("h3", "state-section-title", "By model"));
    const list = node("dl", "usage-model-list");
    for (const [model, modelUsage] of models) {
      const row = node("div", "usage-model-row");
      const modelCost = usageNumber(modelUsage.cost_usd);
      const modelThinking = usageField(modelUsage, "thinking_tokens");
      const copy = node("div");
      copy.append(node("dt", "", model));
      copy.append(
        node(
          "small",
          "",
          `${formatUsageNumber(usageTokenTotal(modelUsage))} tokens · ${formatUsageNumber(modelThinking)} thinking`,
        ),
      );
      row.append(copy, node("dd", "", formatUsageCost(modelCost)));
      list.append(row);
    }
    modelSection.append(list);
    section.append(modelSection);
  }

  container.append(section);
}

export function renderInspector(container, tab, stateValue, mode = "gm") {
  const state = stateValue?.state && !stateValue.party ? stateValue.state : stateValue || {};
  container.replaceChildren();
  switch (tab) {
    case "scene":
      renderScene(container, state);
      break;
    case "encounter":
      renderEncounter(container, state);
      break;
    case "clocks":
      renderClocks(container, state, mode);
      break;
    case "usage":
      renderUsage(container, state);
      break;
    case "party":
    default:
      renderParty(container, state);
      break;
  }
}
