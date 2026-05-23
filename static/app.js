const form = document.getElementById("chatForm");
const messageInput = document.getElementById("message");
const chatEl = document.getElementById("chat");
const sendBtn = document.getElementById("sendBtn");
const clearBtn = document.getElementById("clearBtn");
const userBadge = document.getElementById("userBadge");
const agentBadge = document.getElementById("agentBadge");
const mcpBadge = document.getElementById("mcpBadge");
const mcpServerBadgesEl = document.getElementById("mcpServerBadges");
const chatListEl = document.getElementById("chatList");
const newChatBtn = document.getElementById("newChatBtn");
const renameChatBtn = document.getElementById("renameChatBtn");
const deleteChatBtn = document.getElementById("deleteChatBtn");
let currentChatId = "";
const MESSAGE_MIN_HEIGHT = 42;
const MESSAGE_MAX_HEIGHT = 180;

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderAssistantText(text) {
  // Escape first, then apply a minimal markdown transform.
  let safe = escapeHtml(text || "");
  const codeBlocks = [];

  safe = safe.replace(/```([\s\S]*?)```/g, (_, code) => {
    const token = `@@CODEBLOCK_${codeBlocks.length}@@`;
    codeBlocks.push(`<pre><code>${code.trimEnd()}</code></pre>`);
    return token;
  });

  safe = safe
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/^###\s+(.+)$/gm, "<strong>$1</strong>")
    .replace(/^##\s+(.+)$/gm, "<strong>$1</strong>")
    .replace(/^#\s+(.+)$/gm, "<strong>$1</strong>")
    .replace(/^\-\s+(.+)$/gm, "• $1");

  safe = safe.replace(/\n/g, "<br>");

  for (let idx = 0; idx < codeBlocks.length; idx += 1) {
    safe = safe.replace(`@@CODEBLOCK_${idx}@@`, codeBlocks[idx]);
  }

  return safe;
}

function setMessageText(row, role, text) {
  const contentEl = row.querySelector(".msg-content");
  if (!contentEl) return;
  if (role === "assistant") {
    contentEl.innerHTML = renderAssistantText(text);
    return;
  }
  contentEl.textContent = text;
}

function autoResizeMessageInput() {
  messageInput.style.height = "auto";
  const nextHeight = Math.max(
    MESSAGE_MIN_HEIGHT,
    Math.min(messageInput.scrollHeight, MESSAGE_MAX_HEIGHT),
  );
  messageInput.style.height = `${nextHeight}px`;
  messageInput.style.overflowY =
    messageInput.scrollHeight > MESSAGE_MAX_HEIGHT ? "auto" : "hidden";
}

function appendMessage(role, text) {
  const row = document.createElement("div");
  row.className = `msg ${role}`;
  const label = document.createElement("span");
  label.className = "msg-label";
  label.textContent = `${role === "user" ? "You" : "Agent"}:`;
  const content = document.createElement("span");
  content.className = "msg-content";
  row.appendChild(label);
  row.appendChild(content);
  setMessageText(row, role, text);
  chatEl.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
  return row;
}

function setBusy(isBusy) {
  messageInput.disabled = isBusy;
  sendBtn.disabled = isBusy;
  clearBtn.disabled = isBusy;
  // Keep chat actions available while a reply is streaming.
  newChatBtn.disabled = false;
  renameChatBtn.disabled = !currentChatId;
  deleteChatBtn.disabled = !currentChatId;
}

function renderConversation(conversation) {
  chatEl.innerHTML = "";
  for (const turn of conversation) {
    appendMessage(turn.role, turn.text);
  }
}

function truncateTitle(title) {
  if (!title) return "Untitled";
  return title.length > 36 ? `${title.slice(0, 33)}...` : title;
}

async function fetchChats() {
  const response = await fetch("/api/chats");
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "failed to fetch chats");
  return data.chats || [];
}

async function loadChat(chatId) {
  const response = await fetch(`/api/chats/${encodeURIComponent(chatId)}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "failed to load chat");
  currentChatId = chatId;
  renderConversation(data.conversation || []);
}

function renderChatList(chats) {
  chatListEl.innerHTML = "";
  for (const chat of chats) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `chat-item ${chat.id === currentChatId ? "active" : ""}`;
    btn.textContent = `${truncateTitle(chat.title)} (${chat.message_count})`;
    btn.dataset.chatId = chat.id;
    chatListEl.appendChild(btn);
  }
}

async function refreshChats() {
  const chats = await fetchChats();
  const hasCurrent = chats.some((chat) => chat.id === currentChatId);
  if (!hasCurrent) {
    currentChatId = chats.length > 0 ? chats[0].id : "";
  }
  renderChatList(chats);
  deleteChatBtn.disabled = !currentChatId;
  renameChatBtn.disabled = !currentChatId;
  return chats;
}

async function createChat(promptForName = true) {
  let title = "New chat";
  if (promptForName) {
    const input = window.prompt("Chat name:", "New chat");
    if (input === null) return null;
    title = input.trim() || "New chat";
  }

  const response = await fetch("/api/chats", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ title }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "failed to create chat");
  currentChatId = data.chat.id;
  await refreshChats();
  await loadChat(currentChatId);
  return data.chat.id;
}

async function deleteCurrentChat() {
  if (!currentChatId) {
    throw new Error("no chat selected");
  }

  const response = await fetch(`/api/chats/${encodeURIComponent(currentChatId)}`, {
    method: "DELETE",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "failed to delete chat");
  currentChatId = "";
  const chats = await refreshChats();
  if (chats.length === 0) {
    await createChat(false);
    return;
  }
  await loadChat(currentChatId);
}

async function renameCurrentChat() {
  const selectedText =
    chatListEl.querySelector(".chat-item.active")?.textContent || "Chat";
  const suggested = selectedText.replace(/\s\(\d+\)$/, "");
  const input = window.prompt("New chat name:", suggested);
  if (input === null) return;
  const title = input.trim();
  if (!title) throw new Error("chat name cannot be empty");

  const response = await fetch(`/api/chats/${encodeURIComponent(currentChatId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ title }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "failed to rename chat");
  await refreshChats();
}

async function refreshUser() {
  try {
    const response = await fetch("/api/me");
    const user = await response.json();
    if (!response.ok) throw new Error("user request failed");
    userBadge.textContent = `user: ${user.name}`;
    userBadge.classList.remove("off");
    userBadge.classList.add("ok");
  } catch {
    userBadge.textContent = "user: unknown";
    userBadge.classList.remove("ok");
    userBadge.classList.add("off");
  }
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status");
    const status = await response.json();
    if (!response.ok) throw new Error("status request failed");

    agentBadge.textContent = `agent: ${status.agent_mode}`;
    agentBadge.classList.remove("ok", "off");
    agentBadge.classList.add("ok");

    const connectedCount =
      typeof status.mcp_connected_count === "number" ? status.mcp_connected_count : 0;
    const totalServers =
      typeof status.mcp_total_servers === "number" ? status.mcp_total_servers : 0;

    if (status.mcp_connected) {
      if (totalServers > 1) {
        mcpBadge.textContent = `mcp: ${connectedCount}/${totalServers} connected`;
      } else {
        mcpBadge.textContent = "mcp: connected";
      }
      mcpBadge.classList.remove("off");
      mcpBadge.classList.add("ok");
    } else if (status.mcp_configured) {
      if (totalServers > 1) {
        mcpBadge.textContent = `mcp: ${connectedCount}/${totalServers} connected`;
      } else {
        mcpBadge.textContent = "mcp: configured, not reachable";
      }
      mcpBadge.classList.remove("ok");
      mcpBadge.classList.add("off");
    } else {
      mcpBadge.textContent = "mcp: not configured";
      mcpBadge.classList.remove("ok");
      mcpBadge.classList.add("off");
    }

    if (mcpServerBadgesEl) {
      mcpServerBadgesEl.innerHTML = "";
    }
    const mcpServers = Array.isArray(status.mcp_servers) ? status.mcp_servers : [];
    if (mcpServers.length > 0) {
      for (const server of mcpServers) {
        const chip = document.createElement("span");
        chip.className = "badge";
        const name = typeof server.name === "string" ? server.name : "unknown";
        const transport = typeof server.transport === "string" ? server.transport : "unknown";
        if (server.connected) {
          chip.textContent = `${name} (${transport}): connected`;
          chip.classList.add("ok");
        } else {
          chip.textContent = `${name} (${transport}): not reachable`;
          chip.classList.add("off");
        }
        if (mcpServerBadgesEl) {
          mcpServerBadgesEl.appendChild(chip);
        }
      }
    }
  } catch {
    agentBadge.textContent = "agent: unknown";
    mcpBadge.textContent = "mcp: unknown";
    agentBadge.classList.remove("ok");
    mcpBadge.classList.remove("ok");
    agentBadge.classList.add("off");
    mcpBadge.classList.add("off");
    if (mcpServerBadgesEl) {
      mcpServerBadgesEl.innerHTML = "";
    }
  }
}

async function clearConversation() {
  const response = await fetch(`/api/chats/${encodeURIComponent(currentChatId)}/clear`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error("clear failed");
  }
  chatEl.innerHTML = "";
}

async function streamChatMessage(message, onToken, onStatus) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message, chat_id: currentChatId }),
  });
  if (!response.ok || !response.body) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || "stream request failed");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() || "";

    for (const event of events) {
      const line = event
        .split("\n")
        .find((entry) => entry.startsWith("data: "));
      if (!line) continue;

      const payload = JSON.parse(line.slice(6));
      if (payload.type === "token") {
        onToken(payload.text || "");
      } else if (payload.type === "status") {
        if (onStatus) onStatus(payload.text || "");
      } else if (payload.type === "error") {
        throw new Error(payload.error || "stream error");
      }
    }
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = messageInput.value.trim();
  if (!message) return;

  appendMessage("user", message);
  messageInput.value = "";
  autoResizeMessageInput();
  messageInput.focus();
  setBusy(true);

  const assistantRow = appendMessage("assistant", "");
  let assistantText = "";

  try {
    await streamChatMessage(
      message,
      (token) => {
        assistantText += token;
        setMessageText(assistantRow, "assistant", assistantText);
        chatEl.scrollTop = chatEl.scrollHeight;
      },
      (status) => {
        if (!assistantText && status) {
          setMessageText(assistantRow, "assistant", `(${status})`);
          chatEl.scrollTop = chatEl.scrollHeight;
        }
      },
    );
    if (!assistantText.trim()) {
      setMessageText(assistantRow, "assistant", "(empty response)");
    }
  } catch (error) {
    setMessageText(assistantRow, "assistant", `Error: ${error.message}`);
  } finally {
    setBusy(false);
    refreshStatus();
    refreshChats();
  }
});

messageInput.addEventListener("input", autoResizeMessageInput);
messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

clearBtn.addEventListener("click", async () => {
  try {
    setBusy(true);
    await clearConversation();
  } catch (error) {
    appendMessage("assistant", `Error: ${error.message}`);
  } finally {
    setBusy(false);
    refreshChats();
  }
});

chatListEl.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const chatId = target.dataset.chatId;
  if (!chatId) return;

  try {
    setBusy(true);
    currentChatId = chatId;
    await loadChat(currentChatId);
    await refreshChats();
  } catch (error) {
    appendMessage("assistant", `Error: ${error.message}`);
  } finally {
    setBusy(false);
  }
});

newChatBtn.addEventListener("click", async () => {
  try {
    setBusy(true);
    await createChat();
  } catch (error) {
    appendMessage("assistant", `Error: ${error.message}`);
  } finally {
    setBusy(false);
  }
});

renameChatBtn.addEventListener("click", async () => {
  try {
    setBusy(true);
    await renameCurrentChat();
  } catch (error) {
    appendMessage("assistant", `Error: ${error.message}`);
  } finally {
    setBusy(false);
  }
});

deleteChatBtn.addEventListener("click", async () => {
  try {
    setBusy(true);
    await deleteCurrentChat();
  } catch (error) {
    appendMessage("assistant", `Error: ${error.message}`);
  } finally {
    setBusy(false);
  }
});

async function initialize() {
  try {
    await refreshUser();
    await refreshStatus();
    const chats = await refreshChats();
    if (chats.length === 0) {
      await createChat(false);
      return;
    }
    await loadChat(currentChatId);
  } catch (error) {
    appendMessage("assistant", `Error: ${error.message}`);
  }
  autoResizeMessageInput();
}

initialize();
