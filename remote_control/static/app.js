(() => {
  "use strict";

  const fragment = new URLSearchParams(location.hash.slice(1));
  const fromUrl = fragment.get("token");
  if (fromUrl) sessionStorage.setItem("myagents.remote.token", fromUrl);
  history.replaceState(null, "", location.pathname + location.search);
  const token = sessionStorage.getItem("myagents.remote.token") || "";

  const connection = document.querySelector("#connection");
  const statusPrimary = document.querySelector("#status-primary");
  const statusMeta = document.querySelector("#status-meta");
  const workspace = document.querySelector("#workspace");
  const activityDrawer = document.querySelector("#activity-drawer");
  const timeline = document.querySelector("#timeline");
  const timelineInner = document.querySelector("#timeline-inner");
  const emptyState = document.querySelector("#empty-state");
  const jumpLatest = document.querySelector("#jump-latest");
  const tasks = document.querySelector("#tasks");
  const tasksEmpty = document.querySelector("#tasks-empty");
  const taskToggle = document.querySelector("#task-toggle");
  const taskCount = document.querySelector("#task-count");
  const runningCount = document.querySelector("#running-count");
  const queuedCount = document.querySelector("#queued-count");
  const activeTaskCount = document.querySelector("#active-task-count");
  const drawerClose = document.querySelector("#drawer-close");
  const drawerBackdrop = document.querySelector("#drawer-backdrop");
  const permissionSection = document.querySelector("#permission-section");
  const permissionCount = document.querySelector("#permission-count");
  const permissions = document.querySelector("#permissions");
  const composer = document.querySelector("#composer");
  const message = document.querySelector("#message");
  const sendButton = document.querySelector("#send-button");
  const sendState = document.querySelector("#send-state");
  const toast = document.querySelector("#toast");
  const narrowScreen = matchMedia("(max-width: 900px)");

  let afterSeq = 0;
  let refreshing = false;
  let sending = false;
  let toastTimer = null;
  let lastConnectionError = "";
  let taskRenderSignature = "";

  function setPanel(open, {remember = true} = {}) {
    workspace.classList.toggle("panel-closed", !open);
    taskToggle.setAttribute("aria-expanded", String(open));
    activityDrawer.inert = !open;
    activityDrawer.setAttribute("aria-hidden", String(!open));
    drawerBackdrop.tabIndex = open && narrowScreen.matches ? 0 : -1;
    if (remember) {
      sessionStorage.setItem("myagents.remote.panel", open ? "open" : "closed");
    }
  }

  const savedPanel = sessionStorage.getItem("myagents.remote.panel");
  setPanel(
    narrowScreen.matches ? false : (savedPanel ? savedPanel === "open" : true),
    {remember: false},
  );

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    if (options.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, {...options, headers, cache: "no-store"});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }

  function setConnection(state, primary, meta) {
    connection.dataset.state = state;
    statusPrimary.textContent = primary;
    statusMeta.textContent = meta;
  }

  function showToast(text, kind = "info") {
    if (toastTimer) clearTimeout(toastTimer);
    toast.textContent = String(text);
    toast.dataset.kind = kind;
    toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
  }

  function formatTime(value) {
    if (typeof value !== "string") return "";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(parsed);
  }

  function formatDuration(item) {
    const start = new Date(item.started_at || item.created_at || "").getTime();
    const finish = new Date(item.finished_at || Date.now()).getTime();
    if (!Number.isFinite(start) || !Number.isFinite(finish) || finish < start) return "";
    const seconds = Math.max(0, Math.floor((finish - start) / 1000));
    if (seconds < 60) return `${seconds}秒`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}分${String(seconds % 60).padStart(2, "0")}秒`;
  }

  function speakerKind(speaker) {
    if (speaker === "user") return "user";
    if (speaker === "system") return "system";
    if (speaker === "activity") return "activity";
    return "agent";
  }

  function avatarLabel(speaker) {
    if (speaker === "user") return "YOU";
    if (speaker === "host") return "H";
    const clean = String(speaker || "A").replace(/[^a-zA-Z0-9]/g, "");
    return (clean.slice(0, 2) || "A").toUpperCase();
  }

  function appendInline(parent, source) {
    const text = String(source);
    const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
      const tokenText = match[0];
      const node = document.createElement(tokenText.startsWith("`") ? "code" : "strong");
      node.textContent = tokenText.startsWith("`") ? tokenText.slice(1, -1) : tokenText.slice(2, -2);
      parent.append(node);
      cursor = match.index + tokenText.length;
    }
    if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
  }

  function renderProse(parent, source) {
    const lines = String(source).split("\n");
    let paragraph = [];
    let list = null;
    let listKind = "";

    function flushParagraph() {
      if (!paragraph.length) return;
      const node = document.createElement("p");
      appendInline(node, paragraph.join("\n"));
      parent.append(node);
      paragraph = [];
    }

    function flushList() {
      list = null;
      listKind = "";
    }

    for (const line of lines) {
      const heading = line.match(/^#{1,3}\s+(.+)$/);
      const bullet = line.match(/^[-*]\s+(.+)$/);
      const ordered = line.match(/^\d+\.\s+(.+)$/);
      if (!line.trim()) {
        flushParagraph();
        flushList();
      } else if (heading) {
        flushParagraph();
        flushList();
        const node = document.createElement("h3");
        appendInline(node, heading[1]);
        parent.append(node);
      } else if (bullet || ordered) {
        flushParagraph();
        const kind = ordered ? "ol" : "ul";
        if (!list || listKind !== kind) {
          flushList();
          list = document.createElement(kind);
          listKind = kind;
          parent.append(list);
        }
        const item = document.createElement("li");
        appendInline(item, (ordered || bullet)[1]);
        list.append(item);
      } else {
        flushList();
        paragraph.push(line);
      }
    }
    flushParagraph();
  }

  function renderMessageContent(parent, source) {
    const chunks = String(source || "").split("```");
    chunks.forEach((chunk, index) => {
      if (index % 2 === 0) {
        renderProse(parent, chunk);
        return;
      }
      const code = chunk.replace(/^[a-zA-Z0-9_+.-]+\n/, "").replace(/\n$/, "");
      const pre = document.createElement("pre");
      const codeNode = document.createElement("code");
      codeNode.textContent = code;
      pre.append(codeNode);
      parent.append(pre);
    });
  }

  function appendMessage(item) {
    const speakerName = String(item.speaker || "system");
    const kind = speakerKind(speakerName);
    const row = document.createElement("article");
    row.className = `message message--${kind}`;
    row.dataset.seq = String(item.seq || "");

    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = avatarLabel(speakerName);
    avatar.setAttribute("aria-hidden", "true");

    const main = document.createElement("div");
    main.className = "message-main";
    const meta = document.createElement("div");
    meta.className = "message-meta";
    const speaker = document.createElement("span");
    speaker.className = "speaker";
    speaker.textContent = speakerName === "user" ? "你" : speakerName;
    const time = document.createElement("time");
    time.className = "message-time";
    time.textContent = formatTime(item.created_at);
    if (typeof item.created_at === "string") time.dateTime = item.created_at;
    meta.append(speaker, time);

    const content = document.createElement("div");
    content.className = "message-content";
    renderMessageContent(content, item.text);
    main.append(meta, content);
    row.append(avatar, main);
    timelineInner.append(row);
  }

  function nearTimelineBottom() {
    return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 96;
  }

  function scrollToLatest() {
    timeline.scrollTo({top: timeline.scrollHeight, behavior: "smooth"});
    jumpLatest.hidden = true;
  }

  function makeButton(label, className, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", action);
    return button;
  }

  function renderTasks(items) {
    const active = (Array.isArray(items) ? items : [])
      .filter((item) => item && ["running", "queued"].includes(item.status))
      .sort((left, right) => (left.status === "running" ? -1 : 1) - (right.status === "running" ? -1 : 1));
    const running = active.filter((item) => item.status === "running").length;
    const queued = active.length - running;
    runningCount.textContent = String(running);
    queuedCount.textContent = String(queued);
    activeTaskCount.textContent = String(active.length);
    const signature = JSON.stringify(active.map((item) => [
      item.command_id,
      item.status,
      item.message,
      item.created_at,
      item.started_at,
    ]));
    if (signature === taskRenderSignature) {
      for (const card of tasks.querySelectorAll(".task")) {
        const item = active.find((entry) => String(entry.command_id) === card.dataset.commandId);
        const label = card.querySelector(".task-status-label");
        if (item && label) {
          label.textContent = item.status === "running" ? `运行中 · ${formatDuration(item)}` : "等待发送";
        }
      }
      return active.length;
    }
    taskRenderSignature = signature;
    tasks.replaceChildren();
    tasksEmpty.hidden = active.length > 0;

    for (const item of active) {
      const card = document.createElement("article");
      card.className = "task";
      card.dataset.status = item.status;
      card.dataset.commandId = String(item.command_id || "");
      const top = document.createElement("div");
      top.className = "task-top";
      const dot = document.createElement("span");
      dot.className = "task-state";
      dot.setAttribute("aria-hidden", "true");
      const state = document.createElement("span");
      state.className = "task-status-label";
      state.textContent = item.status === "running" ? `运行中 · ${formatDuration(item)}` : "等待发送";
      const id = document.createElement("span");
      id.className = "task-id";
      id.textContent = String(item.command_id || "").slice(0, 8);
      top.append(dot, state, id);

      const body = document.createElement("p");
      body.className = "task-message";
      body.textContent = String(item.message || "");
      const actions = document.createElement("div");
      actions.className = "task-actions";
      const cancel = makeButton("取消", "small-button danger", async (event) => {
        event.currentTarget.disabled = true;
        try {
          await api(`/api/commands/${encodeURIComponent(item.command_id)}/cancel`, {method: "POST"});
          showToast("取消请求已发送");
          await refresh();
        } catch (error) {
          showToast(`取消失败：${error.message}`, "error");
        } finally {
          event.currentTarget.disabled = false;
        }
      });
      actions.append(cancel);
      card.append(top, body, actions);

      if (item.status === "running") {
        const steerForm = document.createElement("form");
        steerForm.className = "steer-form";
        steerForm.hidden = true;
        const steerInput = document.createElement("input");
        steerInput.placeholder = "补充当前任务的方向";
        steerInput.setAttribute("aria-label", "插话内容");
        const steerSubmit = document.createElement("button");
        steerSubmit.type = "submit";
        steerSubmit.className = "small-button";
        steerSubmit.textContent = "发送";
        steerForm.append(steerInput, steerSubmit);
        const steerToggle = makeButton("插话", "small-button", () => {
          steerForm.hidden = !steerForm.hidden;
          if (!steerForm.hidden) steerInput.focus();
        });
        actions.prepend(steerToggle);
        steerForm.addEventListener("submit", async (event) => {
          event.preventDefault();
          const instruction = steerInput.value.trim();
          if (!instruction) return;
          steerSubmit.disabled = true;
          try {
            await api(`/api/commands/${encodeURIComponent(item.command_id)}/steer`, {
              method: "POST",
              body: JSON.stringify({instruction}),
            });
            showToast("插话已提交到当前任务");
            steerForm.hidden = true;
          } catch (error) {
            showToast(`插话失败：${error.message}`, "error");
          } finally {
            steerSubmit.disabled = false;
          }
        });
        card.append(steerForm);
      }
      tasks.append(card);
    }
    return active.length;
  }

  function renderPermissions(items) {
    const pending = Array.isArray(items) ? items.filter(Boolean) : [];
    permissionSection.hidden = pending.length === 0;
    permissionCount.textContent = String(pending.length);
    permissions.replaceChildren();
    for (const item of pending) {
      const card = document.createElement("article");
      card.className = "permission";
      const agent = document.createElement("div");
      agent.className = "permission-agent";
      agent.textContent = `@${String(item.agent || "agent")}`;
      const title = document.createElement("div");
      title.className = "permission-title";
      title.textContent = String(item.tool_call?.title || "未命名工具");
      const help = document.createElement("p");
      help.className = "permission-help";
      help.textContent = "远程端只能拒绝；批准请回到本机 attach TUI。";
      const deny = makeButton("拒绝本次请求", "small-button danger", async (event) => {
        event.currentTarget.disabled = true;
        try {
          await api(`/api/permissions/${encodeURIComponent(item.request_id)}/deny`, {method: "POST"});
          showToast("权限请求已拒绝");
          await refresh();
        } catch (error) {
          showToast(`拒绝失败：${error.message}`, "error");
        } finally {
          event.currentTarget.disabled = false;
        }
      });
      card.append(agent, title, help, deny);
      permissions.append(card);
    }
    return pending.length;
  }

  async function refresh() {
    if (!token) {
      setConnection("offline", "链接缺少认证 token", "请重新打开 myagents remote 输出的完整地址");
      message.disabled = true;
      sendButton.disabled = true;
      return;
    }
    if (refreshing) return;
    refreshing = true;
    const stickToBottom = nearTimelineBottom();
    try {
      const [room, page, pending, commands] = await Promise.all([
        api("/api/room"),
        api(`/api/timeline?after_seq=${afterSeq}&limit=200`),
        api("/api/permissions"),
        api("/api/commands?limit=200"),
      ]);
      setConnection(
        "online",
        room.session_name === "default" ? "默认会话" : String(room.session_name),
        `daemon ${room.pid || "?"} · ${String(room.workdir || "")}`,
      );
      lastConnectionError = "";
      const newItems = Array.isArray(page.items) ? page.items : [];
      if (newItems.length) emptyState.hidden = true;
      for (const item of newItems) {
        appendMessage(item);
        if (Number.isInteger(item.seq)) afterSeq = Math.max(afterSeq, item.seq);
      }
      if (newItems.length && stickToBottom) {
        timeline.scrollTop = timeline.scrollHeight;
      } else if (newItems.length) {
        jumpLatest.hidden = false;
      }
      const activeCount = renderTasks(commands.items);
      const pendingCount = renderPermissions(pending.items);
      taskCount.textContent = String(activeCount + pendingCount);
      taskToggle.setAttribute("aria-label", `任务活动，${activeCount + pendingCount} 项`);
    } catch (error) {
      const detail = String(error.message || error);
      setConnection("offline", "后台连接中断", detail);
      if (detail !== lastConnectionError) showToast(`连接失败：${detail}`, "error");
      lastConnectionError = detail;
    } finally {
      refreshing = false;
    }
  }

  function resizeComposer() {
    message.style.height = "0px";
    message.style.height = `${Math.min(message.scrollHeight, 180)}px`;
  }

  async function submitMessage() {
    const text = message.value.trim();
    if (!text || sending) return;
    sending = true;
    sendButton.disabled = true;
    sendButton.dataset.sending = "true";
    sendState.dataset.state = "sending";
    sendState.textContent = "正在发送";
    try {
      const command = await api("/api/commands", {
        method: "POST",
        body: JSON.stringify({message: text, request_id: crypto.randomUUID()}),
      });
      if (message.value.trim() === text) message.value = "";
      resizeComposer();
      sendState.dataset.state = "idle";
      sendState.textContent = `已进入队列 ${String(command.command_id || "").slice(0, 8)}`;
      await refresh();
    } catch (error) {
      sendState.dataset.state = "error";
      sendState.textContent = `发送失败：${error.message}`;
      showToast(`发送失败：${error.message}`, "error");
    } finally {
      sending = false;
      sendButton.disabled = false;
      sendButton.dataset.sending = "false";
      message.focus();
    }
  }

  taskToggle.addEventListener("click", () => {
    setPanel(workspace.classList.contains("panel-closed"));
  });
  drawerClose.addEventListener("click", () => setPanel(false));
  drawerBackdrop.addEventListener("click", () => setPanel(false));
  jumpLatest.addEventListener("click", scrollToLatest);
  timeline.addEventListener("scroll", () => {
    if (nearTimelineBottom()) jumpLatest.hidden = true;
  }, {passive: true});
  message.addEventListener("input", resizeComposer);
  message.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submitMessage();
    }
  });
  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    submitMessage();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && narrowScreen.matches && !workspace.classList.contains("panel-closed")) {
      setPanel(false);
      taskToggle.focus();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
  narrowScreen.addEventListener("change", (event) => {
    if (event.matches) {
      setPanel(false, {remember: false});
      return;
    }
    const desktopPreference = sessionStorage.getItem("myagents.remote.panel");
    setPanel(desktopPreference ? desktopPreference === "open" : true, {remember: false});
  });

  resizeComposer();
  refresh();
  setInterval(refresh, 1000);
})();
