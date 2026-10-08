(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    styles: [],
    logs: [],
    ws: null,
    authRequired: false,
    levelRank: { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40 },
  };

  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, 2600);
  }

  function showLogin(show, username) {
    document.body.classList.toggle("locked", !!show);
    $("#loginGate").hidden = !show;
    $("#btnLogout").hidden = !state.authRequired || !!show;
    if (username) $("#loginUser").value = username;
  }

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    const res = await fetch(path, {
      ...options,
      headers,
      credentials: "same-origin", // 仅依赖 HttpOnly Cookie，JS 读不到会话令牌
    });
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (res.status === 401 && path !== "/api/login") {
      showLogin(true);
      throw new Error("未登录或会话已过期");
    }
    if (!res.ok) {
      const detail = (data && (data.detail || data.message)) || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  async function ensureAuth() {
    const st = await api("/api/auth/status");
    state.authRequired = !!st.auth_required;
    if (!st.auth_required) {
      showLogin(false);
      return true;
    }
    if (st.authenticated) {
      showLogin(false);
      if (st.username) $("#loginUser").value = st.username;
      return true;
    }
    showLogin(true, st.username || "admin");
    return false;
  }

  function switchTab(name) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${name}`));
    if (name === "videos") loadVideos();
    if (name === "replies") loadReplies();
    if (name === "config") loadConfig();
  }

  async function loadStatus() {
    const s = await api("/api/status");
    $("#statEnabled").textContent = s.monitored_enabled;
    $("#statTotal").textContent = s.monitored_total;
    $("#statReplies").textContent = s.reply_total;
    $("#statTasks").textContent = s.active_tasks;
    const badge = $("#runBadge");
    badge.textContent = s.running ? "运行中" : "未运行";
    badge.className = `badge ${s.running ? "badge-on" : "badge-off"}`;
    $("#overviewMeta").innerHTML = [
      ["UP 主 UID", s.uploader_uid || "未设置"],
      ["默认 AI 风格", s.default_ai_style || "-"],
      ["评论检查间隔", `${s.check_interval} 秒`],
      ["视频发现间隔", `${s.discovery_interval} 秒`],
      ["已发现投稿", s.discovered_total],
      ["日期过滤", s.date_filter || "不限制"],
    ].map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join("");
  }

  function escapeHtml(str) {
    return str
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function renderLogs() {
    const minLevel = $("#logLevelFilter").value;
    const minRank = minLevel === "ALL" ? 0 : (state.levelRank[minLevel] || 0);
    const view = $("#logView");
    const stick = $("#logAutoScroll").checked &&
      (view.scrollTop + view.clientHeight >= view.scrollHeight - 40);
    view.innerHTML = state.logs
      .filter((l) => (state.levelRank[l.level] || 0) >= minRank)
      .map((l) => `
        <div class="log-line ${escapeHtml(l.level)}">
          <span class="t">${escapeHtml(l.time || "")}</span>
          <span class="lv">${escapeHtml(l.level || "")}</span>
          <span class="m">${escapeHtml(l.message || "")}</span>
        </div>
      `).join("");
    if (stick) view.scrollTop = view.scrollHeight;
  }

  function pushLog(item) {
    state.logs.push(item);
    if (state.logs.length > 2000) state.logs = state.logs.slice(-1500);
    renderLogs();
  }

  function connectLogs() {
    if (state.ws) {
      try { state.ws.close(); } catch (_) {}
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    // Cookie 会随 WebSocket 握手自动带上，不把 token 放进 URL
    const ws = new WebSocket(`${proto}://${location.host}/api/logs/ws`);
    state.ws = ws;
    $("#logConn").textContent = "连接中…";
    ws.onopen = () => { $("#logConn").textContent = "实时已连接"; };
    ws.onclose = () => {
      $("#logConn").textContent = "已断开，3 秒后重连…";
      setTimeout(connectLogs, 3000);
    };
    ws.onerror = () => { $("#logConn").textContent = "连接异常"; };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") {
          state.logs = msg.logs || [];
          renderLogs();
        } else if (msg.type === "log" && msg.log) {
          pushLog(msg.log);
        }
      } catch (_) {}
    };
  }

  async function loadStyles() {
    const data = await api("/api/styles");
    state.styles = data.styles || [];
    const opts = state.styles.map((s) =>
      `<option value="${escapeHtml(s.id)}">${escapeHtml(s.name || s.id)}</option>`
    ).join("");
    $("#addAiStyle").innerHTML = opts || '<option value="natural">natural</option>';
    $("#cfgDefaultStyle").innerHTML = opts;
  }

  async function loadVideos() {
    const data = await api("/api/videos");
    const body = $("#videoBody");
    if (!data.videos.length) {
      body.innerHTML = `<tr><td colspan="6" class="muted">暂无监控视频，可在上方添加 BV 号，或配置 UP 主 UID 自动发现。</td></tr>`;
      return;
    }
    body.innerHTML = data.videos.map((v) => {
      const link = `https://www.bilibili.com/video/${encodeURIComponent(v.bvid)}`;
      return `
        <tr data-bvid="${escapeHtml(v.bvid)}">
          <td>
            <div class="video-title">${escapeHtml(v.title || "未命名")}</div>
            <a class="video-bvid" href="${link}" target="_blank" rel="noopener">${escapeHtml(v.bvid)}</a>
          </td>
          <td>${v.use_ai ? escapeHtml(v.ai_style || "-") : "模板"}</td>
          <td>${v.interval ?? "-"}s</td>
          <td>${v.reply_count ?? 0}</td>
          <td><span class="pill ${v.enabled ? "pill-on" : "pill-off"}">${v.enabled ? "启用" : "停用"}</span></td>
          <td class="row-actions">
            <button class="btn sm" data-act="toggle" type="button">${v.enabled ? "停用" : "启用"}</button>
            <button class="btn sm" data-act="edit" type="button">改间隔</button>
            <button class="btn sm danger" data-act="del" type="button">删除</button>
          </td>
        </tr>
      `;
    }).join("");
  }

  async function loadReplies() {
    const data = await api("/api/replies?limit=80");
    const box = $("#replyList");
    if (!data.replies.length) {
      box.innerHTML = `<div class="reply-item muted">暂无回复记录</div>`;
      return;
    }
    box.innerHTML = data.replies.map((r) => `
      <article class="reply-item">
        <div class="reply-meta">
          <span>${escapeHtml(r.replied_at || "")}</span>
          <a href="https://www.bilibili.com/video/${encodeURIComponent(r.bvid)}" target="_blank" rel="noopener">${escapeHtml(r.bvid)}</a>
          <span>@${escapeHtml(r.username || "匿名")}</span>
        </div>
        <p class="reply-msg">${escapeHtml(r.message || "")}</p>
        <p class="reply-ai">↳ ${escapeHtml(r.ai_reply || "(模板回复)")}</p>
      </article>
    `).join("");
  }

  function fillConfigForm(cfg) {
    const app = cfg.app || {};
    const form = $("#configForm");
    form.uploader_uid.value = app.uploader_uid || "";
    form.default_check_interval.value = app.default_check_interval ?? 60;
    form.video_discovery_interval.value = app.video_discovery_interval ?? 3600;
    form.default_ai_style.value = app.default_ai_style || "natural";
    form.log_level.value = app.log_level || "INFO";
    form.video_after.value = app.video_after || "";
    form.video_before.value = app.video_before || "";
    const sec = cfg.security || {};
    $("#dateFilterHint").textContent =
      `当前日期过滤：${cfg.date_filter || "不限制"}；控制台密码：${sec.web_password_configured ? "已设置" : "未设置（仅建议本机使用）"}`;

    // 密钥永不回填到输入框，只显示是否已配置
    const qConfigured = !!(cfg.qwen && cfg.qwen.api_key_configured);
    form.qwen_api_key.value = "";
    form.qwen_api_key.placeholder = qConfigured ? "已配置（留空不修改）" : "请填写 API Key";
    form.qwen_base_url.value = (cfg.qwen && cfg.qwen.base_url) || "";
    form.qwen_model.value = (cfg.qwen && cfg.qwen.model) || "";

    const flags = (cfg.bilibili && cfg.bilibili.credential_configured) || {};
    form.sessdata.value = "";
    form.bili_jct.value = "";
    form.buvid3.value = "";
    form.ac_time_value.value = "";
    form.sessdata.placeholder = flags.sessdata ? "已配置（留空不修改）" : "请填写 SESSDATA";
    form.bili_jct.placeholder = flags.bili_jct ? "已配置（留空不修改）" : "请填写 bili_jct";
    form.buvid3.placeholder = flags.buvid3 ? "已配置（留空不修改）" : "请填写 buvid3";
    form.ac_time_value.placeholder = flags.ac_time_value ? "已配置（留空不修改）" : "可选";
    form.dedeuserid.value = (cfg.bilibili && cfg.bilibili.dedeuserid) || "";
  }

  async function loadConfig() {
    const cfg = await api("/api/config");
    fillConfigForm(cfg);
  }

  function bindEvents() {
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => switchTab(tab.dataset.tab));
    });
    $("#loginForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const err = $("#loginErr");
      err.hidden = true;
      try {
        await api("/api/login", {
          method: "POST",
          body: JSON.stringify({
            username: $("#loginUser").value.trim(),
            password: $("#loginPass").value,
          }),
        });
        $("#loginPass").value = "";
        showLogin(false);
        await afterLogin();
        toast("登录成功");
      } catch (e) {
        err.textContent = e.message;
        err.hidden = false;
      }
    });
    $("#btnLogout").addEventListener("click", async () => {
      try { await api("/api/logout", { method: "POST" }); } catch (_) {}
      if (state.ws) { try { state.ws.close(); } catch (_) {} }
      showLogin(true);
      toast("已退出");
    });
    $("#btnRefresh").addEventListener("click", async () => {
      try {
        await Promise.all([loadStatus(), loadStyles()]);
        toast("已刷新");
      } catch (e) { toast(e.message); }
    });
    $("#btnClearLogs").addEventListener("click", () => {
      state.logs = [];
      renderLogs();
    });
    $("#logLevelFilter").addEventListener("change", renderLogs);
    $("#btnRefreshReplies").addEventListener("click", () => loadReplies().catch((e) => toast(e.message)));

    $("#addVideoForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      try {
        await api("/api/videos", {
          method: "POST",
          body: JSON.stringify({
            bvid: String(fd.get("bvid") || "").trim(),
            title: String(fd.get("title") || "").trim() || null,
            use_ai: fd.get("use_ai") === "on",
            ai_style: fd.get("ai_style") || null,
            enabled: true,
          }),
        });
        ev.target.reset();
        $("#addAiStyle").selectedIndex = 0;
        toast("已添加监控");
        await Promise.all([loadVideos(), loadStatus()]);
      } catch (e) { toast(e.message); }
    });

    $("#videoBody").addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-act]");
      if (!btn) return;
      const tr = btn.closest("tr[data-bvid]");
      const bvid = tr && tr.dataset.bvid;
      if (!bvid) return;
      try {
        if (btn.dataset.act === "toggle") {
          await api(`/api/videos/${encodeURIComponent(bvid)}/toggle`, { method: "POST" });
          toast("状态已切换");
        } else if (btn.dataset.act === "edit") {
          const val = prompt("新的检查间隔（秒）", "60");
          if (val == null) return;
          const interval = Number(val);
          if (!Number.isFinite(interval) || interval < 10) {
            toast("间隔至少 10 秒");
            return;
          }
          await api(`/api/videos/${encodeURIComponent(bvid)}`, {
            method: "PUT",
            body: JSON.stringify({ interval }),
          });
          toast("间隔已更新");
        } else if (btn.dataset.act === "del") {
          if (!confirm(`确定删除 ${bvid}？`)) return;
          await api(`/api/videos/${encodeURIComponent(bvid)}`, { method: "DELETE" });
          toast("已删除");
        }
        await Promise.all([loadVideos(), loadStatus()]);
      } catch (e) { toast(e.message); }
    });

    $("#configForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const f = ev.target;
      const payload = {
        app: {
          uploader_uid: f.uploader_uid.value.trim(),
          default_check_interval: Number(f.default_check_interval.value) || 60,
          video_discovery_interval: Number(f.video_discovery_interval.value) || 3600,
          default_ai_style: f.default_ai_style.value,
          log_level: f.log_level.value,
          video_after: f.video_after.value.trim(),
          video_before: f.video_before.value.trim(),
        },
        qwen: {
          // 空字符串表示不修改已有 Key
          api_key: f.qwen_api_key.value.trim(),
          base_url: f.qwen_base_url.value.trim(),
          model: f.qwen_model.value.trim(),
        },
        bilibili: {
          credential: {
            sessdata: f.sessdata.value.trim(),
            bili_jct: f.bili_jct.value.trim(),
            buvid3: f.buvid3.value.trim(),
            dedeuserid: f.dedeuserid.value.trim(),
            ac_time_value: f.ac_time_value.value.trim(),
          },
        },
      };
      try {
        const res = await api("/api/config", { method: "PUT", body: JSON.stringify(payload) });
        fillConfigForm(res.config);
        $("#configMsg").textContent = "已保存";
        toast("配置已保存");
        await loadStatus();
      } catch (e) {
        $("#configMsg").textContent = e.message;
        toast(e.message);
      }
    });
  }

  async function afterLogin() {
    connectLogs();
    await loadStyles();
    await loadStatus();
  }

  async function boot() {
    bindEvents();
    try {
      const ok = await ensureAuth();
      if (ok) await afterLogin();
      setInterval(() => {
        if (!document.body.classList.contains("locked")) {
          loadStatus().catch(() => {});
        }
      }, 15000);
    } catch (e) {
      toast(`初始化失败: ${e.message}`);
      showLogin(true);
    }
  }

  boot();
})();
