(() => {
  "use strict";
  const fragment = new URLSearchParams(location.hash.slice(1));
  const fromUrl = fragment.get("token");
  if (fromUrl) sessionStorage.setItem("myagents.remote.token", fromUrl);
  history.replaceState(null, "", location.pathname + location.search);
  const token = sessionStorage.getItem("myagents.remote.token") || "";
  const status = document.querySelector("#status");
  const timeline = document.querySelector("#timeline");
  const tasks = document.querySelector("#tasks");
  const permissions = document.querySelector("#permissions");
  const composer = document.querySelector("#composer");
  const message = document.querySelector("#message");
  let afterSeq = 0;
  let refreshing = false;

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    if (options.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, {...options, headers, cache: "no-store"});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }

  function appendMessage(item) {
    const row = document.createElement("p");
    row.className = `message ${String(item.speaker || "system")}`;
    const speaker = document.createElement("span");
    speaker.className = "speaker";
    speaker.textContent = `[${String(item.speaker || "system")}]`;
    const text = document.createElement("span");
    text.textContent = String(item.text || "");
    row.append(speaker, text);
    timeline.append(row);
  }

  async function refresh() {
    if (!token) { status.textContent = "链接缺少 token；请重新从 myagents remote 输出打开"; return; }
    if (refreshing) return;
    refreshing = true;
    try {
      const [room, page, pending, commands] = await Promise.all([
        api("/api/room"),
        api(`/api/timeline?after_seq=${afterSeq}&limit=200`),
        api("/api/permissions"),
        api("/api/commands?limit=50"),
      ]);
      status.textContent = `${room.session_name} · daemon PID ${room.pid || "?"}`;
      for (const item of page.items || []) {
        appendMessage(item);
        if (Number.isInteger(item.seq)) afterSeq = Math.max(afterSeq, item.seq);
      }
      if ((page.items || []).length) timeline.scrollTop = timeline.scrollHeight;
      tasks.replaceChildren();
      for (const item of (commands.items || []).filter((entry) => ["running", "queued"].includes(entry.status))) {
        const row = document.createElement("div");
        row.className = "task";
        const label = document.createElement("span");
        label.textContent = `${item.status === "running" ? "运行" : "排队"} ${String(item.command_id).slice(0, 8)} · ${String(item.message || "").slice(0, 80)}`;
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.textContent = "取消";
        cancel.addEventListener("click", async () => {
          await api(`/api/commands/${encodeURIComponent(item.command_id)}/cancel`, {method: "POST"});
          await refresh();
        });
        row.append(label, cancel);
        tasks.append(row);
      }
      permissions.replaceChildren();
      for (const item of pending.items || []) {
        const card = document.createElement("div");
        card.className = "permission";
        const title = document.createElement("div");
        title.textContent = `@${item.agent} 等待本机权限：${item.tool_call?.title || "未命名工具"}`;
        const help = document.createElement("div");
        help.textContent = "远程端只能拒绝；批准请回到本机 attach TUI。";
        const deny = document.createElement("button");
        deny.className = "deny";
        deny.type = "button";
        deny.textContent = "拒绝本次请求";
        deny.addEventListener("click", async () => {
          await api(`/api/permissions/${encodeURIComponent(item.request_id)}/deny`, {method: "POST"});
          await refresh();
        });
        card.append(title, help, deny);
        permissions.append(card);
      }
    } catch (error) {
      status.textContent = `连接失败：${error.message}`;
    } finally {
      refreshing = false;
    }
  }

  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = message.value.trim();
    if (!text) return;
    message.disabled = true;
    try {
      await api("/api/commands", {method: "POST", body: JSON.stringify({message: text, request_id: crypto.randomUUID()})});
      message.value = "";
      await refresh();
    } catch (error) {
      status.textContent = `发送失败：${error.message}`;
    } finally {
      message.disabled = false;
      message.focus();
    }
  });

  refresh();
  setInterval(refresh, 1000);
})();
