/* Copyright (C) 2026 Intel Corporation — SPDX-License-Identifier: Apache-2.0 */

(() => {
  "use strict";

  const form = document.getElementById("chat-form");
  if (!form) return;

  const messageInput = document.getElementById("chat-message");
  const runIdInput = document.getElementById("chat-run-id");
  const transcript = document.getElementById("chat-transcript");
  const emptyState = document.getElementById("chat-empty");
  const submitButton = document.getElementById("chat-submit");
  const status = document.getElementById("chat-status");
  const thinkingIndicator = document.getElementById("chat-thinking");
  const errorBox = document.getElementById("chat-error");
  const errorText = document.getElementById("chat-error-text");
  const retryButton = document.getElementById("chat-retry");
  const clearButton = document.getElementById("chat-clear");
  const defaultRunOption = document.getElementById("chat-default-run-option");
  const promptList = document.getElementById("prompt-list");
  const STORAGE_KEY_PREFIX = "apm.chat.history.v2";
  const PROMPTS_BY_MODE = {
    analysis: [
      { label: "Summarize findings", prompt: "Summarize the most important maintenance findings." },
      { label: "Top risks", prompt: "What are the top maintenance risks identified in the analysis?" },
      { label: "Recommended actions", prompt: "What maintenance actions are recommended, and why?" },
    ],
    detections: [
      { label: "Prioritize detections", prompt: "Which detections need immediate attention, and why?" },
      { label: "Recent detections", prompt: "List the most recent detections and their confidence levels." },
      { label: "Detections by class", prompt: "Break down the detections by class/category." },
    ],
    combined: [
      { label: "Compare evidence", prompt: "Compare the evidence and recommended maintenance actions." },
      { label: "Findings vs. detections", prompt: "How do the detection results support the analysis findings?" },
      { label: "Full picture", prompt: "Give me a complete picture combining analysis and detection evidence." },
    ],
  };
  const MAX_STORED_MESSAGES = 100;
  let lastRequest = null;
  let pending = false;
  let chatHistory = [];

  function selectedMode() {
    return document.querySelector('input[name="chat-mode"]:checked').value;
  }

  function updateRunScopeLabel() {
    defaultRunOption.textContent = selectedMode() === "detections"
      ? "All stored detections"
      : "Latest completed run";
  }

  function renderPromptSuggestions(mode) {
    if (!promptList) return;
    const prompts = PROMPTS_BY_MODE[mode || selectedMode()] || [];
    while (promptList.firstChild) promptList.removeChild(promptList.firstChild);
    prompts.forEach(({ label, prompt }) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "prompt-chip";
      button.dataset.prompt = prompt;
      button.textContent = label;
      button.addEventListener("click", () => {
        messageInput.value = button.dataset.prompt;
        messageInput.focus();
      });
      promptList.appendChild(button);
    });
  }

  function historyStorageKey() {
    const runId = runIdInput.value.trim();
    return `${STORAGE_KEY_PREFIX}:${encodeURIComponent(runId || "default")}`;
  }

  function persistHistory() {
    try {
      sessionStorage.setItem(historyStorageKey(), JSON.stringify(chatHistory));
    } catch (error) {
      console.debug("Unable to save chat history for this browser session:", error);
    }
  }

  function removeStoredHistory() {
    try {
      sessionStorage.removeItem(historyStorageKey());
    } catch (error) {
      console.debug("Unable to remove chat history for this browser session:", error);
    }
  }

  function storedMessage(kind, text, options) {
    const message = { kind, text };
    if (typeof options.mode === "string") message.mode = options.mode;
    if (options.query !== undefined && options.query !== null) message.query = options.query;
    if (options.data !== undefined && options.data !== null) message.data = options.data;
    return message;
  }

  // Highlights standalone numbers within a line of text using safe DOM nodes (no raw-HTML injection).
  function appendHighlightedText(parent, line) {
    const numberPattern = /\b\d[\d,]*(?:\.\d+)?%?\b/g;
    let lastIndex = 0;
    let match;
    while ((match = numberPattern.exec(line))) {
      if (match.index > lastIndex) parent.appendChild(document.createTextNode(line.slice(lastIndex, match.index)));
      const strong = document.createElement("strong");
      strong.className = "chat-highlight";
      strong.textContent = match[0];
      parent.appendChild(strong);
      lastIndex = numberPattern.lastIndex;
    }
    if (lastIndex < line.length) parent.appendChild(document.createTextNode(line.slice(lastIndex)));
  }

  function splitTableRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  }

  function isTableSeparatorRow(line) {
    const cells = splitTableRow(line);
    return cells.length > 0 && cells.every((cell) => /^:?-+:?$/.test(cell));
  }

  // Renders the assistant answer as paragraphs with highlighted numbers, and
  // Markdown-style "| a | b |" tables as real <table> elements. Falls back to
  // plain paragraphs when no table syntax is present.
  function renderAnswerContent(container, text) {
    const lines = text.split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (line.trim().startsWith("|") && i + 1 < lines.length && isTableSeparatorRow(lines[i + 1])) {
        const table = document.createElement("table");
        table.className = "md-table";
        const thead = document.createElement("thead");
        const headRow = document.createElement("tr");
        splitTableRow(line).forEach((cell) => {
          const th = document.createElement("th");
          th.textContent = cell;
          headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);
        const tbody = document.createElement("tbody");
        i += 2;
        while (i < lines.length && lines[i].trim().startsWith("|")) {
          const tr = document.createElement("tr");
          splitTableRow(lines[i]).forEach((cell) => {
            const td = document.createElement("td");
            appendHighlightedText(td, cell);
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
          i += 1;
        }
        table.appendChild(tbody);
        container.appendChild(table);
        continue;
      }
      if (line.trim() !== "") {
        const p = document.createElement("p");
        appendHighlightedText(p, line);
        container.appendChild(p);
      }
      i += 1;
    }
  }

  function appendMessage(kind, text, options = {}, save = true) {
    emptyState.hidden = true;
    const article = document.createElement("article");
    article.className = `chat-message chat-message-${kind}`;

    const heading = document.createElement("div");
    heading.className = "chat-message-heading";
    heading.textContent = kind === "user" ? "You" : "Assistant";
    if (options.mode) heading.textContent += ` · ${options.mode}`;
    article.appendChild(heading);

    const content = document.createElement("div");
    content.className = "chat-message-content";
    if (kind === "assistant") {
      renderAnswerContent(content, text);
    } else {
      content.textContent = text;
    }
    article.appendChild(content);

    if (options.query) {
      const query = document.createElement("p");
      query.className = "chat-response-query";
      const queryText = typeof options.query === "string"
        ? options.query
        : JSON.stringify(options.query);
      query.textContent = `Query: ${queryText}`;
      article.appendChild(query);
    }

    if (options.data !== undefined && options.data !== null) {
      const details = document.createElement("details");
      details.className = "chat-data";
      const summary = document.createElement("summary");
      summary.textContent = "View supporting data";
      const pre = document.createElement("pre");
      pre.textContent = typeof options.data === "string" ? options.data : JSON.stringify(options.data, null, 2);
      details.append(summary, pre);
      article.appendChild(details);
    }

    transcript.appendChild(article);
    transcript.scrollTop = transcript.scrollHeight;

    if (save) {
      chatHistory.push(storedMessage(kind, text, options));
      if (chatHistory.length > MAX_STORED_MESSAGES) {
        chatHistory = chatHistory.slice(-MAX_STORED_MESSAGES);
      }
      persistHistory();
    }
  }

  function restoreHistory() {
    let stored;
    try {
      stored = JSON.parse(sessionStorage.getItem(historyStorageKey()) || "[]");
    } catch (error) {
      console.debug("Unable to restore chat history for this browser session:", error);
      removeStoredHistory();
      return;
    }

    if (!Array.isArray(stored)) {
      removeStoredHistory();
      return;
    }

    chatHistory = stored.filter((message) => (
      message
      && (message.kind === "user" || message.kind === "assistant")
      && typeof message.text === "string"
      && message.text.length <= 100000
      && (message.mode === undefined || (
        typeof message.mode === "string" && message.mode.length <= 50
      ))
    )).slice(-MAX_STORED_MESSAGES);

    chatHistory.forEach((message) => {
      appendMessage(message.kind, message.text, {
        mode: message.mode,
        query: message.query,
        data: message.data,
      }, false);
    });

    if (chatHistory.length !== stored.length) persistHistory();
  }

  function showSelectedRunHistory() {
    transcript.querySelectorAll(".chat-message").forEach((message) => message.remove());
    chatHistory = [];
    lastRequest = null;
    errorBox.hidden = true;
    emptyState.hidden = false;
    restoreHistory();
    status.textContent = chatHistory.length
      ? "Chat history loaded for the selected run."
      : "No chat history for the selected run.";
  }

  function clearHistory() {
    transcript.querySelectorAll(".chat-message").forEach((message) => message.remove());
    chatHistory = [];
    removeStoredHistory();
    emptyState.hidden = false;
    lastRequest = null;
    errorBox.hidden = true;
    status.textContent = "Chat history cleared.";
    messageInput.focus();
  }

  function setPending(value) {
    pending = value;
    submitButton.disabled = value;
    messageInput.disabled = value;
    runIdInput.disabled = value;
    clearButton.disabled = value;
    document.querySelectorAll('input[name="chat-mode"]').forEach((input) => {
      input.disabled = value;
    });
    submitButton.textContent = value ? "Analyzing…" : "Send";
    status.textContent = value ? "Analyzing your question." : "";
    thinkingIndicator.hidden = !value;
    if (value) {
      transcript.appendChild(thinkingIndicator);
      transcript.scrollTop = transcript.scrollHeight;
    }
    transcript.setAttribute("aria-busy", String(value));
  }

  function showError(message) {
    errorText.textContent = message;
    errorBox.hidden = false;
    status.textContent = message;
  }

  async function sendRequest(request, appendUser = true) {
    if (pending) return;
    errorBox.hidden = true;
    if (appendUser) appendMessage("user", request.message);
    setPending(true);

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify(request),
      });

      let result;
      try {
        result = await response.json();
      } catch (_error) {
        throw new Error("The server returned an unreadable response.");
      }

      if (!response.ok) {
        const detail = typeof result.detail === "string" ? result.detail : "The request could not be completed.";
        throw new Error(detail);
      }
      if (!result || typeof result.answer !== "string") {
        throw new Error("The server response did not include an answer.");
      }

      thinkingIndicator.hidden = true;
      appendMessage("assistant", result.answer, {
        mode: typeof result.mode === "string" ? result.mode : request.mode,
        query: result.query,
        data: result.data,
      });
      status.textContent = "Answer received.";
      lastRequest = null;
    } catch (error) {
      lastRequest = request;
      showError(error instanceof Error ? error.message : "Unable to reach the chat service.");
    } finally {
      setPending(false);
      messageInput.focus();
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const message = messageInput.value.trim();
    if (!message || pending) {
      messageInput.focus();
      return;
    }

    const request = { message, mode: selectedMode() };
    const runId = runIdInput.value.trim();
    if (runId) request.run_id = runId;
    messageInput.value = "";
    sendRequest(request);
  });

  messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  retryButton.addEventListener("click", () => {
    if (lastRequest) sendRequest(lastRequest, false);
  });
  clearButton.addEventListener("click", clearHistory);
  runIdInput.addEventListener("change", showSelectedRunHistory);

  document.querySelectorAll('input[name="chat-mode"]').forEach((input) => {
    input.addEventListener("change", () => {
      updateRunScopeLabel();
      renderPromptSuggestions();
    });
  });
  restoreHistory();
  updateRunScopeLabel();
  renderPromptSuggestions();
})();
