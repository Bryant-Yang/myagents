(() => {
  "use strict";

  const SETTLED_TASK_STATUSES = new Set([
    "completed", "failed", "cancelled", "interrupted",
  ]);
  const EVENT_KIND_LABELS = {
    queued: "进入队列",
    running: "开始运行",
    status: "阶段",
    tool: "工具",
    permission: "权限",
    plan: "协作计划",
    steering: "阶段补充",
    interjection_requested: "请求插话",
    interjection_accepted: "插话已接受",
    interjection_failed: "插话失败",
    interjection_uncertain: "插话结果不确定",
    completed: "本轮响应结束",
    failed: "调用失败",
    cancelled: "已取消",
  };

  function buildAgentCatalog(items) {
    const seen = new Set();
    const agents = [];
    for (const item of Array.isArray(items) ? items : []) {
      const name = typeof item?.name === "string" ? item.name.trim() : "";
      if (!name || seen.has(name)) continue;
      seen.add(name);
      const ready = item.ready === true;
      agents.push({
        name,
        ready,
        description: `${String(item.transport || "agent").toUpperCase()} · ${ready ? "可用" : "未就绪"}`,
      });
    }
    return agents.sort((left, right) => Number(right.ready) - Number(left.ready));
  }

  function findMentionContext(value, cursor, agents) {
    if (!Number.isInteger(cursor)) return null;
    const source = String(value || "");
    const left = source.slice(0, cursor);
    const match = left.match(/(^|[^A-Za-z0-9_])@([A-Za-z0-9_-]*)$/);
    if (!match) return null;
    const start = cursor - match[2].length - 1;
    const tail = source.slice(cursor).match(/^[A-Za-z0-9_-]*/)?.[0] || "";
    const end = cursor + tail.length;
    const outside = source.slice(0, start) + source.slice(end);
    const selected = new Set(
      Array.from(outside.matchAll(/@([A-Za-z0-9_-]+)/g), (item) => item[1]),
    );
    const prefix = match[2].toLowerCase();
    const candidates = agents.filter((item) => (
      !selected.has(item.name) && item.name.toLowerCase().startsWith(prefix)
    ));
    return candidates.length ? {start, end, items: candidates} : null;
  }

  function insertMention(value, range, name) {
    const source = String(value || "");
    const before = source.slice(0, range.start);
    const after = source.slice(range.end);
    const insertion = `@${name} `;
    return {
      value: before + insertion + after,
      caret: before.length + insertion.length,
    };
  }

  function classifyHistoryPage(page) {
    const events = Array.isArray(page?.items) ? page.items : [];
    const first = events[0] || null;
    const last = events[events.length - 1] || null;
    const status = ["completed", "failed", "cancelled"].includes(last?.kind)
      ? last.kind
      : Number(page?.total_count) > 0 ? "interrupted" : "unknown";
    return {
      status,
      started_at: first?.created_at || null,
      finished_at: SETTLED_TASK_STATUSES.has(status) && status !== "interrupted"
        ? last?.created_at || null
        : null,
      error: status === "failed" ? String(last?.text || "调用失败") : null,
    };
  }

  function selectVisibleTasks(items, timelineHints) {
    const source = Array.isArray(items) ? items.filter(Boolean) : [];
    const active = source
      .filter((item) => item && ["running", "queued"].includes(item.status))
      .sort((left, right) => (left.status === "running" ? -1 : 1) - (right.status === "running" ? -1 : 1));
    const currentIds = new Set(source.map((item) => String(item.command_id || "")));
    const persisted = Array.from(timelineHints || [])
      .filter((item) => (
        !currentIds.has(String(item.command_id))
        && SETTLED_TASK_STATUSES.has(item.status)
      ))
      .reverse();
    const terminal = source.filter(
      (item) => SETTLED_TASK_STATUSES.has(item.status));
    const recent = [...terminal, ...persisted]
      .sort((left, right) => String(right.finished_at || right.activity_at || right.created_at || "")
        .localeCompare(String(left.finished_at || left.activity_at || left.created_at || "")))
      .slice(0, 4);
    return {active, visible: [...active, ...recent]};
  }

  function visibleDetailEvents(page) {
    return (Array.isArray(page?.items) ? page.items : [])
      .filter((event) => event && Object.hasOwn(EVENT_KIND_LABELS, event.kind))
      .slice(-80);
  }

  if (typeof module === "object" && module.exports) {
    module.exports = {
      buildAgentCatalog,
      classifyHistoryPage,
      findMentionContext,
      insertMention,
      selectVisibleTasks,
      visibleDetailEvents,
    };
  }
  if (typeof document === "undefined") return;

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
  const mentionMenu = document.querySelector("#mention-menu");
  const mentionOptions = document.querySelector("#mention-options");
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
  let roomAgents = [];
  let mentionItems = [];
  let mentionIndex = 0;
  let mentionRange = null;
  let expandedTaskId = "";
  const timelineCommandHints = new Map();
  const taskDetailsCache = new Map();
  const taskDetailsLoading = new Set();

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
    rememberTimelineCommand(item);
  }

  function rememberTimelineCommand(item) {
    const commandId = typeof item.command_id === "string" ? item.command_id : "";
    if (!commandId) return;
    const existing = timelineCommandHints.get(commandId) || {};
    const messageText = item.speaker === "user"
      ? String(item.text || "")
      : existing.message || "历史任务";
    timelineCommandHints.delete(commandId);
    timelineCommandHints.set(commandId, {
      command_id: commandId,
      message: messageText,
      status: existing.status || "unknown",
      created_at: existing.created_at || item.created_at,
      activity_at: item.created_at || existing.activity_at,
      started_at: existing.started_at || null,
      finished_at: existing.finished_at || null,
      error: existing.error || null,
    });
    while (timelineCommandHints.size > 32) {
      timelineCommandHints.delete(timelineCommandHints.keys().next().value);
    }
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

  function setAgentCatalog(items) {
    roomAgents = buildAgentCatalog(items);
  }

  function mentionAtCursor() {
    return findMentionContext(
      message.value, message.selectionStart, roomAgents);
  }

  function closeMentionMenu() {
    mentionMenu.hidden = true;
    mentionItems = [];
    mentionRange = null;
    message.removeAttribute("aria-activedescendant");
    message.setAttribute("aria-expanded", "false");
  }

  function selectMention(index) {
    if (!mentionItems.length) return;
    mentionIndex = (index + mentionItems.length) % mentionItems.length;
    for (const [optionIndex, option] of Array.from(mentionOptions.children).entries()) {
      const selected = optionIndex === mentionIndex;
      option.setAttribute("aria-selected", String(selected));
      option.classList.toggle("is-selected", selected);
      if (selected) {
        message.setAttribute("aria-activedescendant", option.id);
        option.scrollIntoView({block: "nearest"});
      }
    }
  }

  function applyMention(index = mentionIndex) {
    const item = mentionItems[index];
    if (!item || !mentionRange) return;
    const updated = insertMention(message.value, mentionRange, item.name);
    message.value = updated.value;
    message.setSelectionRange(updated.caret, updated.caret);
    closeMentionMenu();
    resizeComposer();
    message.focus();
  }

  function updateMentionMenu() {
    const context = mentionAtCursor();
    if (!context) {
      closeMentionMenu();
      return;
    }
    mentionItems = context.items;
    mentionRange = {start: context.start, end: context.end};
    mentionIndex = Math.min(mentionIndex, mentionItems.length - 1);
    mentionOptions.replaceChildren();
    mentionItems.forEach((item, index) => {
      const option = document.createElement("button");
      option.type = "button";
      option.id = `mention-option-${index}`;
      option.className = "mention-option";
      option.dataset.ready = String(item.ready);
      option.setAttribute("role", "option");
      option.addEventListener("mousedown", (event) => event.preventDefault());
      option.addEventListener("click", () => applyMention(index));
      const label = document.createElement("span");
      label.className = "mention-name";
      label.textContent = `@${item.name}`;
      const description = document.createElement("span");
      description.className = "mention-description";
      description.textContent = item.description;
      option.append(label, description);
      mentionOptions.append(option);
    });
    mentionMenu.hidden = false;
    message.setAttribute("aria-expanded", "true");
    selectMention(mentionIndex);
  }

  function taskStatusText(item) {
    const duration = formatDuration(item);
    const withDuration = (label) => duration ? `${label} · ${duration}` : label;
    if (item.status === "running") return withDuration("运行中");
    if (item.status === "queued") return "等待发送";
    if (item.status === "completed") return withDuration("本轮响应结束");
    if (item.status === "failed") return withDuration("调用失败");
    if (item.status === "cancelled") return withDuration("已取消");
    if (item.status === "interrupted") return "上次运行中断";
    return String(item.status || "未知状态");
  }

  function visibleTaskItems(items) {
    return selectVisibleTasks(items, timelineCommandHints.values());
  }

  function appendDetailChip(parent, text) {
    const chip = document.createElement("span");
    chip.className = "detail-chip";
    chip.textContent = text;
    parent.append(chip);
  }

  function renderTaskDetails(panel, item, page) {
    panel.replaceChildren();
    panel.dataset.state = "ready";
    const summary = document.createElement("div");
    summary.className = "detail-summary";
    for (const agent of Array.isArray(page.agents) ? page.agents : []) {
      appendDetailChip(summary, `@${String(agent)}`);
    }
    appendDetailChip(summary, `${Number(page.total_count) || 0} 条过程事件`);
    if (Number(page.partial_char_count) > 0) {
      appendDetailChip(summary, `输出 ${Number(page.partial_char_count).toLocaleString("zh-CN")} 字`);
    }
    panel.append(summary);

    const counts = page.kind_counts && typeof page.kind_counts === "object"
      ? page.kind_counts
      : {};
    if (!counts.tool && !counts.permission) {
      const empty = document.createElement("p");
      empty.className = "detail-empty";
      empty.textContent = "本轮未调用工具或请求权限。";
      panel.append(empty);
    }

    if (item.error) {
      const error = document.createElement("p");
      error.className = "detail-error";
      error.textContent = String(item.error);
      panel.append(error);
    }

    const eventList = document.createElement("ol");
    eventList.className = "detail-events";
    const events = visibleDetailEvents(page);
    for (const event of events) {
      const row = document.createElement("li");
      row.className = "detail-event";
      row.dataset.kind = String(event.kind || "status");
      const rail = document.createElement("span");
      rail.className = "detail-rail";
      rail.setAttribute("aria-hidden", "true");
      const content = document.createElement("div");
      content.className = "detail-event-content";
      const meta = document.createElement("div");
      meta.className = "detail-event-meta";
      const kind = document.createElement("span");
      kind.textContent = EVENT_KIND_LABELS[event.kind] || String(event.kind || "状态");
      const agent = document.createElement("span");
      agent.textContent = `@${String(event.agent || "system")}`;
      const time = document.createElement("time");
      time.textContent = formatTime(event.created_at);
      meta.append(kind, agent, time);
      const text = document.createElement("p");
      const fullText = String(event.text || "");
      text.textContent = fullText.length > 1200 ? `${fullText.slice(0, 1200)}…` : fullText;
      content.append(meta, text);
      row.append(rail, content);
      eventList.append(row);
    }
    if (events.length) panel.append(eventList);
    if (Number(page.omitted_count) > 0) {
      const omitted = document.createElement("p");
      omitted.className = "detail-omitted";
      omitted.textContent = `已省略 ${Number(page.omitted_count)} 条中间事件，保留首尾关键证据。`;
      panel.append(omitted);
    }
  }

  async function loadTaskDetails(item, panel, {force = false} = {}) {
    const commandId = String(item.command_id || "");
    if (!commandId || taskDetailsLoading.has(commandId)) return;
    const cached = taskDetailsCache.get(commandId);
    const cacheFresh = cached && cached.status === item.status && (
      SETTLED_TASK_STATUSES.has(item.status) || Date.now() - cached.fetchedAt < 3000
    );
    if (cacheFresh && !force) {
      const renderKey = `${commandId}:${cached.fetchedAt}:${item.status}:${item.error || ""}`;
      if (panel.dataset.renderKey !== renderKey) {
        renderTaskDetails(panel, item, cached.page);
        panel.dataset.renderKey = renderKey;
      }
      return;
    }
    taskDetailsLoading.add(commandId);
    panel.dataset.state = "loading";
    panel.textContent = "正在读取过程详情…";
    try {
      const page = await api(`/api/commands/${encodeURIComponent(commandId)}/events?limit=80`);
      cacheTaskDetails(commandId, item.status, page);
      if (expandedTaskId === commandId && panel.isConnected) {
        renderTaskDetails(panel, item, page);
        panel.dataset.renderKey = `${commandId}:${taskDetailsCache.get(commandId).fetchedAt}:${item.status}:${item.error || ""}`;
      }
    } catch (error) {
      if (expandedTaskId === commandId && panel.isConnected) {
        panel.dataset.state = "error";
        panel.textContent = `详情读取失败：${error.message}`;
      }
    } finally {
      taskDetailsLoading.delete(commandId);
    }
  }

  function refreshExpandedDetails(items) {
    if (!expandedTaskId) return;
    const item = items.find((entry) => String(entry.command_id) === expandedTaskId);
    const card = Array.from(tasks.querySelectorAll(".task"))
      .find((entry) => entry.dataset.commandId === expandedTaskId);
    const panel = card?.querySelector(".task-details");
    if (!item || !panel) {
      expandedTaskId = "";
      return;
    }
    panel.hidden = false;
    loadTaskDetails(item, panel);
  }

  function cacheTaskDetails(commandId, status, page) {
    let cacheStatus = status;
    const hint = timelineCommandHints.get(commandId);
    if (hint && !["running", "queued"].includes(status)) {
      const classification = classifyHistoryPage(page);
      hint.status = classification.status;
      hint.started_at = hint.started_at || classification.started_at;
      hint.finished_at = classification.finished_at;
      hint.error = classification.error;
      cacheStatus = classification.status;
    }
    taskDetailsCache.delete(commandId);
    taskDetailsCache.set(commandId, {page, status: cacheStatus, fetchedAt: Date.now()});
    while (taskDetailsCache.size > 32) {
      taskDetailsCache.delete(taskDetailsCache.keys().next().value);
    }
  }

  async function hydrateRecentTimelineTasks(commandItems) {
    const currentIds = new Set(
      (Array.isArray(commandItems) ? commandItems : [])
        .map((item) => String(item?.command_id || "")),
    );
    const candidates = Array.from(timelineCommandHints.values())
      .reverse()
      .filter((item) => (
        item.status === "unknown"
        && !currentIds.has(String(item.command_id))
        && !taskDetailsLoading.has(String(item.command_id))
        && !taskDetailsCache.has(String(item.command_id))
      ))
      .slice(0, 4);
    await Promise.all(candidates.map(async (item) => {
      const commandId = String(item.command_id);
      taskDetailsLoading.add(commandId);
      try {
        const page = await api(
          `/api/commands/${encodeURIComponent(commandId)}/events?limit=80`);
        cacheTaskDetails(commandId, item.status, page);
      } catch (_error) {
        taskDetailsCache.set(commandId, {
          page: null,
          status: "unknown",
          fetchedAt: Date.now(),
        });
      } finally {
        taskDetailsLoading.delete(commandId);
      }
    }));
  }

  function renderTasks(items) {
    const {active, visible} = visibleTaskItems(items);
    const running = active.filter((item) => item.status === "running").length;
    const queued = active.length - running;
    runningCount.textContent = String(running);
    queuedCount.textContent = String(queued);
    activeTaskCount.textContent = String(visible.length);
    const signature = JSON.stringify(visible.map((item) => [
      item.command_id,
      item.status,
      item.message,
      item.created_at,
      item.started_at,
      item.finished_at,
      item.error,
    ]));
    if (signature === taskRenderSignature) {
      for (const card of tasks.querySelectorAll(".task")) {
        const item = visible.find((entry) => String(entry.command_id) === card.dataset.commandId);
        const label = card.querySelector(".task-status-label");
        if (item && label) {
          label.textContent = taskStatusText(item);
        }
      }
      refreshExpandedDetails(visible);
      return active.length;
    }
    taskRenderSignature = signature;
    tasks.replaceChildren();
    tasksEmpty.hidden = visible.length > 0;

    for (const item of visible) {
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
      state.textContent = taskStatusText(item);
      const id = document.createElement("span");
      id.className = "task-id";
      id.textContent = String(item.command_id || "").slice(0, 8);
      top.append(dot, state, id);

      const body = document.createElement("p");
      body.className = "task-message";
      body.textContent = String(item.message || "");
      const actions = document.createElement("div");
      actions.className = "task-actions";
      if (["running", "queued"].includes(item.status)) {
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
      }
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
      const detailPanel = document.createElement("div");
      detailPanel.className = "task-details";
      detailPanel.id = `task-details-${String(item.command_id || "")}`;
      detailPanel.hidden = expandedTaskId !== String(item.command_id);
      const details = makeButton(
        detailPanel.hidden ? "详情" : "收起",
        "small-button details-button",
        (event) => {
          const opening = expandedTaskId !== String(item.command_id);
          expandedTaskId = opening ? String(item.command_id) : "";
          for (const other of tasks.querySelectorAll(".task-details")) other.hidden = true;
          for (const button of tasks.querySelectorAll(".details-button")) {
            button.textContent = "详情";
            button.setAttribute("aria-expanded", "false");
          }
          detailPanel.hidden = !opening;
          event.currentTarget.textContent = opening ? "收起" : "详情";
          event.currentTarget.setAttribute("aria-expanded", String(opening));
          if (opening) loadTaskDetails(item, detailPanel, {force: true});
        },
      );
      details.setAttribute("aria-expanded", String(!detailPanel.hidden));
      details.setAttribute("aria-controls", detailPanel.id);
      actions.append(details);
      card.append(detailPanel);
      tasks.append(card);
    }
    refreshExpandedDetails(visible);
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
      setAgentCatalog(room.agents);
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
      await hydrateRecentTimelineTasks(commands.items);
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
    closeMentionMenu();
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
  message.addEventListener("input", () => {
    resizeComposer();
    mentionIndex = 0;
    updateMentionMenu();
  });
  message.addEventListener("click", updateMentionMenu);
  message.addEventListener("keyup", (event) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      updateMentionMenu();
    }
  });
  message.addEventListener("keydown", (event) => {
    if (!mentionMenu.hidden) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        selectMention(mentionIndex + 1);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        selectMention(mentionIndex - 1);
        return;
      }
      if (((event.key === "Enter" && !event.shiftKey) || event.key === "Tab")
          && !event.isComposing) {
        event.preventDefault();
        applyMention();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        closeMentionMenu();
        return;
      }
    }
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
  document.addEventListener("pointerdown", (event) => {
    if (!composer.contains(event.target) && !mentionMenu.contains(event.target)) {
      closeMentionMenu();
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
