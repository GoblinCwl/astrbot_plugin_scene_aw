const bridge = window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);

function fmtDuration(seconds) {
  if (seconds == null) return "--";
  seconds = Math.max(0, Number(seconds) || 0);

  if (seconds < 60) return `${Math.round(seconds)}s`;

  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;

  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h}h ${rest}m` : `${h}h`;
}

function fmtAge(value) {
  if (!value) return "--";
  const t = new Date(value).getTime();
  if (!Number.isFinite(t)) return "--";

  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 60) return `${Math.floor(sec)}s 前`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m 前`;
  return `${Math.floor(sec / 3600)}h 前`;
}

function setState(el, value, kind) {
  el.textContent = value;
  el.classList.remove("good", "warn", "bad");
  if (kind) el.classList.add(kind);
}

function renderCategoryProfile(scene = {}) {
  const profile = scene.category_profile || {};
  const entries = Object.entries(profile)
    .sort((a, b) => Number(b[1]) - Number(a[1]));

  const windowSeconds =
    Number(scene.profile_window_seconds || 0);

  $("profileWindow").textContent =
    windowSeconds >= 60
      ? `最近 ${Math.round(windowSeconds / 60)} 分钟`
      : `最近 ${windowSeconds}s`;

  const box = $("categoryProfile");

  if (!entries.length) {
    box.className = "profile-list empty";
    box.textContent = "暂无活动数据";
    return;
  }

  box.className = "profile-list";

  box.innerHTML = entries.map(([name, value]) => {
    const percent = Math.round(Number(value || 0) * 100);

    return `
      <div class="profile-row">
        <span class="profile-name">${escapeHtml(name)}</span>
        <div class="profile-bar">
          <div class="profile-fill" style="width:${percent}%"></div>
        </div>
        <span class="profile-value">${percent}%</span>
      </div>
    `;
  }).join("");
}

function renderThreads(threads = []) {
  $("threadCount").textContent = threads.length;

  if (!threads.length) {
    $("threads").className = "empty";
    $("threads").textContent = "尚未建立事务场景";
    return;
  }

  $("threads").className = "";

  $("threads").innerHTML = threads.map(t => `
    <div class="thread">
      <div class="thread-head">
        <span class="thread-name">
          ${t.role === "primary" ? "● " : ""}
          ${escapeHtml(t.name || "未命名场景")}
        </span>

        <span class="badge">
          ${Math.round(
            Number(t.confidence || 0) * 100
          )}%
        </span>
      </div>

      <div class="thread-meta">
        ${
          t.role === "primary"
            ? "主场景"
            : "并行场景"
        }
        · ${fmtAge(t.last_active_at)}
      </div>
    </div>
  `).join("");
}

function renderPendingConfirm(c = {}) {
  const suggestions = c.suggestions || [];

  // 后端只下发仍未自动分类的建议；不要在前端写死阈值，
  // 否则自定义自动应用阈值后会错误隐藏待确认项。
  const items = suggestions
    .sort((a, b) =>
      Number(b.confidence || 0) -
      Number(a.confidence || 0)
    );

  $("confirmCount").textContent = items.length;

  const box = $("confirmList");

  if (!items.length) {
    box.className = "confirm-list empty";
    box.textContent = "暂无待确认项目";
    return;
  }

  box.className = "confirm-list";

  const categories = c.category_names || ["其他"];

  box.innerHTML = items.map(item => `
    <div class="confirm-row"
         data-app="${escapeHtml(item.app)}">

      <div class="confirm-main">
        <span class="confirm-app">
          ${escapeHtml(item.app)}
        </span>

        <span class="confirm-meta">
          AI 建议：
          ${escapeHtml(item.category || "其他")}
          · ${Math.round(
            Number(item.confidence || 0) * 100
          )}%
        </span>
      </div>

      <div class="confirm-actions">
        <span class="select-wrap">
          <select class="confirm-select">
            ${categories.map(category => `
              <option value="${category}"
                ${category === item.category ? "selected" : ""}>
                ${category}
              </option>
            `).join("")}
          </select>
        </span>

        <button
          class="confirm-button"
          data-action="confirm-category"
          title="保存分类"
          aria-label="保存分类">
          ✓
        </button>
      </div>
    </div>
  `).join("");
}

function renderPrevious(scene) {
  const box = $("previousScene");

  if (!scene) {
    box.className = "previous-inline empty";
    box.textContent = "暂无上次场景";
    return;
  }

  box.className = "previous-inline";
  box.textContent = `上次：${scene.name || "--"} · ${
    scene.ended_at ? fmtAge(scene.ended_at) : "--"
  }`;
}

function renderActivities(items = []) {
  $("activityCount").textContent = items.length;
  const box = $("recentActivities");

  if (!items.length) {
    box.className = "activity-list empty";
    box.textContent = "暂无活动记录";
    return;
  }

  box.className = "activity-list";
  box.innerHTML = items.map(item => `
    <div class="activity-row">
      <div class="activity-head">
        <span class="activity-app">${escapeHtml(item.app || "--")}</span>
        <span class="badge neutral">${escapeHtml(item.category || "其他")}</span>
      </div>
      <div class="activity-title" title="${escapeHtml(item.title || "--")}">
        ${escapeHtml(item.title || "--")}
      </div>
      <div class="activity-meta">
        <span>${item.timestamp ? new Date(item.timestamp).toLocaleTimeString() : "--"}</span>
        <span>
          ${Number(item.event_count || 0) > 1 ? `${item.event_count} 条记录 · ` : ""}
          ${fmtDuration(item.duration_seconds)}
        </span>
      </div>
    </div>
  `).join("");
}

let categoryData = {};
let categoryView = null;
const expandedCategories = new Set();

function renderCategories(c = {}) {
  categoryData = c;
  $("totalApps").textContent = c.total_apps ?? 0;
$("knownApps").textContent = c.known_apps ?? 0;
  $("unknownApps").textContent = (c.unknown_apps || []).length;
  $("categoryCount").textContent = c.category_count ?? 0;

  const detail = $("categoryDetail");
  const editingCategory = detail.contains(document.activeElement);

  if (categoryView && !editingCategory) {
    renderCategoryContent(categoryView);
  }
}

function renderCategoryDetail(view) {
  const box = $("categoryDetail");

  // 只有真正点击当前已展开卡片时才折叠。
  if (categoryView === view && box.style.display !== "none") {
    categoryView = null;
    box.style.display = "none";

    document.querySelectorAll(".category-stat").forEach(el => {
      el.classList.remove("active");
    });

    return;
  }

  categoryView = view;
  renderCategoryContent(view);
}

function renderCategoryContent(view) {
  const box = $("categoryDetail");

  box.style.display = "block";
  box.classList.remove("hidden");

  document.querySelectorAll(".category-stat").forEach(el => {
    el.classList.toggle("active", el.dataset.view === view);
  });

  if (["all", "known", "unknown"].includes(view)) {
    const items = view === "all"
      ? (categoryData.all_app_items || [])
      : view === "known"
        ? (categoryData.known_app_items || [])
        : (categoryData.unknown_apps || []).map(item => ({
            app: typeof item === "string" ? item : item.app,
            category: null,
          }));

    renderAppCategoryEditor(box, items);
    return;
  }

  if (view === "categories") {
    const items = categoryData.main_categories || [];
    const appsByCategory = new Map(
      items.map(item => [item.name, []])
    );

    for (const item of categoryData.known_app_items || []) {
      const category = String(item.category || "其他")
        .split(" > ", 1)[0];
      if (appsByCategory.has(category)) {
        appsByCategory.get(category).push(item);
      }
    }

    const categoryNames = categoryData.category_names || ["其他"];

    box.innerHTML = items.length
      ? items.map(item => {
          const apps = appsByCategory.get(item.name) || [];
          const expanded = expandedCategories.has(item.name);

          return `
          <div class="category-group${expanded ? " expanded" : ""}">
            <button class="category-group-head"
                    data-action="toggle-category-apps"
                    data-category-name="${escapeHtml(item.name)}"
                    aria-expanded="${expanded}">
              <span class="category-group-label">
                <span class="category-chevron">›</span>
                ${escapeHtml(item.name)}
              </span>
              <span>${item.app_count ?? 0} 个应用</span>
            </button>
            <div class="category-app-list">
              ${apps.length
                ? apps.map(appItem => `
                    <div class="confirm-row category-app-editor"
                         data-app="${escapeHtml(appItem.app || "")}">
                      <span class="confirm-app">
                        ${escapeHtml(appItem.app || "--")}
                      </span>
                      <div class="confirm-actions">
                        <span class="select-wrap">
                          <select class="confirm-select">
                            ${categoryNames.map(category => `
                              <option value="${category}"
                                ${category === appItem.category
                                  ? "selected"
                                  : ""}>
                                ${category}
                              </option>
                            `).join("")}
                          </select>
                        </span>
                        <button class="confirm-button"
                                data-action="confirm-category"
                                title="保存分类"
                                aria-label="保存分类">
                          ✓
                        </button>
                      </div>
                    </div>
                  `).join("")
                : '<div class="category-empty">暂无应用</div>'}
            </div>
          </div>
        `;
        }).join("")
      : '<div class="category-empty">暂无主分类</div>';
  }
}

function renderAppCategoryEditor(box, items) {
  const categories = categoryData.category_names || ["其他"];

  if (!items.length) {
    box.innerHTML = '<div class="category-empty">暂无应用</div>';
    return;
  }

  box.innerHTML = items.map(item => `
    <div class="confirm-row" data-app="${escapeHtml(item.app || "")}">
      <span class="confirm-app">${escapeHtml(item.app || "--")}</span>
      <div class="confirm-actions">
        <span class="select-wrap">
          <select class="confirm-select">
            ${categories.map(category => `
              <option value="${category}"
                ${category === item.category ? "selected" : ""}>
                ${category}
              </option>
            `).join("")}
          </select>
        </span>
        <button class="confirm-button"
                data-action="confirm-category"
                title="保存分类"
                aria-label="保存分类">
          ✓
        </button>
      </div>
    </div>
  `).join("");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderJsonNode(value, label) {
  const isArray = Array.isArray(value);
  const isObject = value !== null && typeof value === "object";

  if (!isObject) {
    return `
      <div class="json-row">
        <span class="json-key">${escapeHtml(label)}</span>
        <span class="json-value json-${value === null ? "null" : typeof value}">
          ${escapeHtml(JSON.stringify(value))}
        </span>
      </div>
    `;
  }

  const entries = Object.entries(value);
  const shape = isArray ? "[ ]" : "{ }";

  return `
    <details class="json-node">
      <summary>
        <span class="json-key">${escapeHtml(label)}</span>
        <span class="json-shape">${shape}</span>
      </summary>
      <div class="json-children">
        ${entries.map(([key, child]) =>
          renderJsonNode(child, isArray ? `[${key}]` : key)
        ).join("")}
      </div>
    </details>
  `;
}

function renderJsonTree(element, value) {
  const signature = value == null ? "" : JSON.stringify(value);
  if (element.dataset.signature === signature) return;
  element.dataset.signature = signature;

  if (value == null) {
    element.className = "json-tree empty";
    element.textContent = "暂无";
    return;
  }

  element.className = "json-tree";
  element.innerHTML = renderJsonNode(value, "根节点");
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("copy failed");
}

function render(data) {
  const health = data.health || {};
  const scene = data.scene || {};
  const focus = scene.current_focus || {};
  const llm = data.last_llm || {};
  const comm = scene.communication || {};
  const signals = scene.signals || {};

  $("updatedAt").textContent =
    `更新 ${new Date().toLocaleTimeString()}`;

  setState(
    $("awStatus"),
    health.aw_status === "ACTIVE"
      ? "活跃"
      : health.aw_status === "AFK" ? "离开" : "离线",
    health.aw_status === "ACTIVE"
      ? "good"
      : health.aw_status === "AFK" ? "warn" : "bad"
  );

  $("sceneStatus").textContent =
    scene.primary_scene || "待判断";

  setState(
    $("afkStatus"),
    health.aw_status === "OFFLINE"
      ? "--"
      : signals.afk ? "是" : "否",
    health.aw_status === "OFFLINE"
      ? "bad"
      : signals.afk ? "warn" : "good"
  );

  renderThreads(scene.threads || []);
  renderCategoryProfile(scene);

  $("focusApp").textContent = focus.app || "--";
  $("focusCategory").textContent = focus.category || "--";
  $("focusTitle").textContent = focus.title || "--";
  $("focusDuration").textContent = fmtDuration(focus.duration_seconds);

  $("commStatus").textContent =
    comm.active ? "活跃" : "无";

  $("commDuration").textContent =
    fmtDuration(comm.active_seconds || 0);

  $("commApps").textContent =
    (comm.apps || []).join(" · ") || "--";

  renderPrevious(data.previous_scene);

  $("llmAge").textContent =
    llm.at ? fmtAge(llm.at) : "从未";

  $("llmReason").textContent = llm.reason || "--";
  $("llmLatency").textContent =
    llm.latency_ms != null ? `${llm.latency_ms}ms` : "--";
  $("llmInputTokens").textContent =
    llm.input_tokens != null ? `${llm.input_tokens} tokens` : "--";
  $("llmOutputTokens").textContent =
    llm.output_tokens != null ? `${llm.output_tokens} tokens` : "--";

  renderJsonTree($("llmInput"), llm.input_summary);
  renderJsonTree($("llmOutput"), llm.output);

  renderCategories(data.categories || {});
  renderPendingConfirm(data.categories || {});

  $("lastScan").textContent =
    health.last_scan_at ? fmtAge(health.last_scan_at) : "--";

  $("scanInterval").textContent =
    health.scan_interval_seconds != null
      ? `${health.scan_interval_seconds}s`
      : "--";

  $("bucketCount").textContent =
    health.bucket_count ?? 0;

  $("uptime").textContent =
    fmtDuration(health.uptime_seconds);

  setState(
    $("healthBadge"),
    health.running ? "运行中" : "已停止",
    health.running ? "good" : "bad"
  );

  renderActivities(data.recent_activities || []);
}

async function refresh() {
  try {
    const data = await bridge.apiGet("dashboard/state");
    render(data);
  } catch (err) {
    setState($("awStatus"), "ERROR", "bad");
    $("healthBadge").textContent = err?.message || "API ERROR";
  }
}

await bridge.ready();

document.addEventListener("click", (event) => {
  const button = event.target.closest(".category-stat");
  if (!button) return;

  renderCategoryDetail(button.dataset.view);
});

document.addEventListener("click", (event) => {
  const button = event.target.closest(
    '[data-action="toggle-category-apps"]'
  );
  if (!button) return;

  const category = button.dataset.categoryName;
  if (!category) return;

  if (expandedCategories.has(category)) {
    expandedCategories.delete(category);
  } else {
    expandedCategories.add(category);
  }

  renderCategoryContent("categories");
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-json-action]");
  if (!button) return;

  const tree = $(button.dataset.jsonTarget);
  if (!tree) return;

  const action = button.dataset.jsonAction;

  if (action === "expand") {
    tree.querySelectorAll("details.json-node").forEach(node => {
      node.open = true;
    });
    return;
  }

  if (action === "collapse") {
    tree.querySelectorAll("details.json-node").forEach(node => {
      node.open = false;
    });
    return;
  }

  if (action === "copy" && tree.dataset.signature) {
    const original = button.textContent;
    try {
      await copyText(tree.dataset.signature);
      button.textContent = "✓";
    } catch (err) {
      button.textContent = "失败";
    }
    setTimeout(() => {
      button.textContent = original;
    }, 1200);
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest(
    '[data-action="confirm-category"]'
  );

  if (!button) return;

  const row = button.closest(".confirm-row");
  if (!row) return;

  const app = row.dataset.app;
  const select = row.querySelector(".confirm-select");
  const category = select?.value;

  if (!app || !category) return;

  button.disabled = true;
  button.textContent = "…";

  try {
    await bridge.apiPost(
      "dashboard/confirm-category",
      {
        app,
        category,
      }
    );

    await refresh();

  } catch (err) {
    button.disabled = false;
    button.textContent = "↻";
  }
});

$("categoryWebClassifyBtn").addEventListener("click", async () => {
  const button = $("categoryWebClassifyBtn");
  button.disabled = true;
  button.textContent = "联网中…";

  try {
    const result = await bridge.apiPost(
      "dashboard/classify-categories-web",
      {}
    );
    if (result?.categories) {
      renderCategories(result.categories);
      renderPendingConfirm(result.categories);
    }
    const failed = Number(result?.failed || 0);
    const applied = Number(result?.applied || 0);
    const pending = Number(result?.pending || 0);
    button.textContent = failed
      ? `${applied} 已分类 · ${failed} 失败`
      : `${applied} 已分类 · ${pending} 待确认`;
  } catch (err) {
    button.textContent = "重试";
    button.title = err?.message || "批量联网识别失败";
  }

  await refresh();
  setTimeout(() => {
    button.disabled = false;
    button.textContent = "批量联网";
  }, 1200);
});

$("categoryClassifyBtn").addEventListener("click", async () => {
  const button = $("categoryClassifyBtn");
  button.disabled = true;
  button.textContent = "识别中…";

  try {
    const result = await bridge.apiPost(
      "dashboard/classify-categories",
      {}
    );
    if (result?.categories) {
      renderCategories(result.categories);
      renderPendingConfirm(result.categories);
    }
    button.textContent = `${
      Number(result?.applied || 0)
    } 已分类 · ${Number(result?.pending || 0)} 待确认`;
    await refresh();
  } catch (err) {
    button.textContent = "重试识别";
  } finally {
    button.disabled = false;
  }
});

$("refreshBtn").addEventListener("click", refresh);

await refresh();

const timer = setInterval(refresh, 2000);

window.addEventListener("beforeunload", () => {
  clearInterval(timer);
});
