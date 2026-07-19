const JSON_HEADERS = {
  Accept: "application/json",
  "Content-Type": "application/json",
};

export class ApiError extends Error {
  constructor(message, status = 0, details = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

function campaignPath(slug, suffix = "") {
  return `/api/campaigns/${encodeURIComponent(slug)}${suffix}`;
}

async function errorFromResponse(response) {
  let payload = null;
  let message = `${response.status} ${response.statusText}`.trim();
  try {
    payload = await response.json();
    message = payload.detail || payload.message || payload.error || message;
  } catch {
    try {
      const text = await response.text();
      if (text.trim()) message = text.trim();
    } catch {
      // Keep the HTTP status as the useful fallback.
    }
  }
  return new ApiError(message || "The request failed.", response.status, payload);
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers: {
        ...JSON_HEADERS,
        ...(options.headers || {}),
      },
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("OpenAdventure could not reach the local server.", 0, error);
  }

  if (!response.ok) throw await errorFromResponse(response);
  if (response.status === 204) return null;
  return response.json();
}

async function streamRequest(path, body, onEvent, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      cache: "no-store",
      headers: {
        ...JSON_HEADERS,
        Accept: "application/x-ndjson, application/json",
      },
      body: JSON.stringify(body ?? {}),
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("The stream could not connect to the local server.", 0, error);
  }

  if (!response.ok) throw await errorFromResponse(response);
  if (!response.body) {
    throw new ApiError("The server returned an empty event stream.", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatchLine = (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    let event;
    try {
      event = JSON.parse(trimmed);
    } catch (error) {
      throw new ApiError("The server sent an unreadable event.", response.status, {
        line: trimmed,
        error,
      });
    }
    onEvent(event);
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";
    for (const line of lines) dispatchLine(line);
  }

  buffer += decoder.decode();
  if (buffer.trim()) dispatchLine(buffer);
}

export const api = {
  bootstrap() {
    return request("/api/bootstrap");
  },

  createCampaign(payload) {
    return request("/api/campaigns", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  campaign(slug) {
    return request(campaignPath(slug));
  },

  state(slug) {
    return request(campaignPath(slug, "/state"));
  },

  events(slug) {
    return request(campaignPath(slug, "/events"));
  },

  turn(slug, payload, onEvent, options = {}) {
    return streamRequest(campaignPath(slug, "/turn"), payload, onEvent, options);
  },

  roll(slug, expression) {
    return request(campaignPath(slug, "/actions/roll"), {
      method: "POST",
      body: JSON.stringify({ expression }),
    });
  },

  undo(slug, count = 1) {
    return request(campaignPath(slug, "/actions/undo"), {
      method: "POST",
      body: JSON.stringify({ count }),
    });
  },

  retry(slug, onEvent, options = {}) {
    return streamRequest(campaignPath(slug, "/actions/retry"), {}, onEvent, options);
  },

  compact(slug, onEvent, options = {}) {
    return streamRequest(campaignPath(slug, "/actions/compact"), {}, onEvent, options);
  },

  recap(slug) {
    return request(campaignPath(slug, "/actions/recap"), {
      method: "POST",
      body: "{}",
    });
  },

  cancel(slug) {
    return request(campaignPath(slug, "/actions/cancel"), {
      method: "POST",
      body: "{}",
    });
  },

  updateSettings(slug, settings) {
    return request(campaignPath(slug, "/settings"), {
      method: "PATCH",
      body: JSON.stringify(settings),
    });
  },
};
