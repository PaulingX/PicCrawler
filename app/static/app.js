const state = {
  rules: [],
  shelves: [],
  activeTab: null,
  page: 1,
  topics: [],
  newShelfRoots: [],
  selectedTopics: new Map(),
  onlineSearch: new Map(),
  localSearch: new Map(),
  onlineCategory: new Map(),
  topicImageCache: new Map(),
  topicCounts: new Map(),
  topicCountLoading: new Set(),
  viewer: {
    open: false,
    mode: null,
    offset: 0,
    hasMore: true,
    loading: false,
    payload: null,
  },
  folderPicker: {
    open: false,
    current: "",
    parent: "",
    resolver: null,
  },
};

const tabsEl = document.getElementById("tabs");
const actionsEl = document.getElementById("tab-actions");
const topicGridEl = document.getElementById("topic-grid");
const pageInfoEl = document.getElementById("page-info");
const jobListEl = document.getElementById("job-list");
const sidePanelEl = document.querySelector(".side-panel");
const downloadFabEl = document.getElementById("btn-download-fab");
const topbarEl = document.querySelector(".topbar");
const mobileToolsBtnEl = document.getElementById("btn-mobile-tools");

const viewerEl = document.getElementById("viewer");
const viewerBodyEl = document.getElementById("viewer-body");
const viewerTitleEl = document.getElementById("viewer-title");
const folderPickerEl = document.getElementById("folder-picker");
const folderPickerPathEl = document.getElementById("folder-picker-path");
const folderPickerListEl = document.getElementById("folder-picker-list");
const folderPickerUpEl = document.getElementById("folder-picker-up");
const folderPickerChooseEl = document.getElementById("folder-picker-choose");

async function fetchJSON(url, options = {}) {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

async function pickRuleDownloadDir(initialDir = "") {
  if (!folderPickerEl || !folderPickerListEl || !folderPickerPathEl || !folderPickerUpEl || !folderPickerChooseEl) {
    // Fallback for unexpected template mismatch.
    const data = await fetchJSON("/api/system/select-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initial_dir: initialDir || "" }),
    });
    return (data.path || "").trim();
  }

  if (state.folderPicker.open && typeof state.folderPicker.resolver === "function") {
    state.folderPicker.resolver("");
  }

  return new Promise((resolve) => {
    state.folderPicker.open = true;
    state.folderPicker.resolver = resolve;
    folderPickerEl.classList.remove("hidden");

    const bootPath = (initialDir || "").trim();
    loadFolderPicker(bootPath)
      .catch(async (error) => {
        showToast(error.message);
        await loadFolderPicker("");
      })
      .catch((error) => {
        showToast(error.message);
        closeFolderPicker("");
      });
  });
}

function closeFolderPicker(pickedPath = "") {
  if (!state.folderPicker.open) {
    return;
  }
  const resolver = state.folderPicker.resolver;
  state.folderPicker.open = false;
  state.folderPicker.current = "";
  state.folderPicker.parent = "";
  state.folderPicker.resolver = null;
  if (folderPickerEl) {
    folderPickerEl.classList.add("hidden");
  }
  if (typeof resolver === "function") {
    resolver((pickedPath || "").trim());
  }
}

async function loadFolderPicker(path = "") {
  const url = path
    ? `/api/system/directories?path=${encodeURIComponent(path)}`
    : "/api/system/directories";
  const data = await fetchJSON(url);

  state.folderPicker.current = (data.current || "").trim();
  state.folderPicker.parent = (data.parent || "").trim();
  renderFolderPickerItems(Array.isArray(data.items) ? data.items : []);
}

function renderFolderPickerItems(items) {
  if (!folderPickerPathEl || !folderPickerListEl || !folderPickerUpEl || !folderPickerChooseEl) {
    return;
  }

  folderPickerPathEl.textContent = state.folderPicker.current || "请选择磁盘";
  folderPickerUpEl.disabled = !state.folderPicker.parent;
  folderPickerChooseEl.disabled = !state.folderPicker.current;

  folderPickerListEl.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "folder-picker-empty";
    empty.textContent = "当前目录没有子文件夹";
    folderPickerListEl.appendChild(empty);
    return;
  }

  items.forEach((item) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "folder-picker-item";
    btn.innerHTML = `
      <span class="folder-picker-item-name">${escapeHtml(item.name || item.path || "")}</span>
      <span class="folder-picker-item-arrow">进入</span>
    `;
    btn.addEventListener("click", async () => {
      try {
        await loadFolderPicker(item.path || "");
      } catch (error) {
        showToast(error.message);
      }
    });
    folderPickerListEl.appendChild(btn);
  });
}

function renderShelfRootsDisplay() {
  const el = document.getElementById("shelf-roots-display");
  if (!el) return;
  el.value = state.newShelfRoots.join(" ; ");
}

function showToast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 2200);
}

function updateDownloadFab() {
  if (!downloadFabEl) {
    return;
  }
  if (!state.activeTab || state.activeTab.kind !== "online") {
    downloadFabEl.classList.add("is-hidden");
    return;
  }
  const selectedCount = activeSelectionSet().size;
  if (selectedCount <= 0) {
    downloadFabEl.classList.add("is-hidden");
    return;
  }
  downloadFabEl.textContent = `下载选中主题 (${selectedCount})`;
  downloadFabEl.classList.remove("is-hidden");
}

function activeTabKey(tab) {
  if (tab.kind === "online") {
    const category = getOnlineCategory(tab.id);
    return `${tab.kind}:${tab.id}:${category === null ? "" : category}`;
  }
  return `${tab.kind}:${tab.id}`;
}

function activeSelectionSet() {
  const key = activeTabKey(state.activeTab);
  if (!state.selectedTopics.has(key)) {
    state.selectedTopics.set(key, new Set());
  }
  return state.selectedTopics.get(key);
}

function getEnabledOnlineRules() {
  return state.rules.filter((rule) => Number(rule.enabled) === 1);
}

function getRuleById(ruleId) {
  return state.rules.find((item) => item.rule_id === ruleId) || null;
}

function getShelfById(shelfId) {
  return state.shelves.find((item) => String(item.shelf_id) === String(shelfId)) || null;
}

function getRuleCategories(ruleId) {
  const rule = getRuleById(ruleId);
  const categories = Array.isArray(rule?.categories) ? rule.categories : [];
  return Array.from(new Set(categories.map((item) => Number(item)).filter((item) => Number.isInteger(item))));
}

function getOnlineCategory(ruleId) {
  const categories = getRuleCategories(ruleId);
  if (!categories.length) {
    return null;
  }

  const saved = Number(state.onlineCategory.get(ruleId));
  if (categories.includes(saved)) {
    return saved;
  }
  return categories[0];
}

function getOnlineCategoryLabel(ruleId, categoryId) {
  const id = Number(categoryId);
  if (ruleId === "wnacg" && Number.isInteger(id)) {
    const labelMap = { 1: "汉化同人志", 9: "汉化单行本", 10: "汉化短篇" };
    return labelMap[id] || `cate-${id}`;
  }
  if (ruleId === "manxiangge" && Number.isInteger(id)) {
    const labelMap = { 2: "单行本", 4: "同人志" };
    return labelMap[id] || `cate-${id}`;
  }
  return `cate-${id}`;
}

function getOnlineQuery(ruleId) {
  return (state.onlineSearch.get(ruleId) || "").trim();
}

function getLocalQuery(shelfId) {
  return (state.localSearch.get(String(shelfId)) || "").trim();
}

function topicImageCacheKey(ruleId, topicId) {
  return `${ruleId}:${topicId}`;
}

function topicCountCacheKey(ruleId, topicId) {
  return `${ruleId}:${topicId}`;
}

function getTopicImageCache(ruleId, topicId) {
  return state.topicImageCache.get(topicImageCacheKey(ruleId, topicId)) || null;
}

function getTopicCount(ruleId, topicId) {
  const key = topicCountCacheKey(ruleId, topicId);
  if (!state.topicCounts.has(key)) {
    return null;
  }
  return state.topicCounts.get(key);
}

function setTopicCount(ruleId, topicId, count) {
  const value = Math.max(0, Number.parseInt(String(count || "0"), 10) || 0);
  const key = topicCountCacheKey(ruleId, topicId);
  state.topicCounts.set(key, value);

  topicGridEl.querySelectorAll(".topic-count[data-rule-id][data-topic-id]").forEach((el) => {
    if (el.dataset.ruleId === ruleId && el.dataset.topicId === topicId) {
      el.textContent = `${value} 张`;
    }
  });
}

async function ensureOnlineTopicCount(ruleId, topic) {
  const key = topicCountCacheKey(ruleId, topic.topic_id);
  if (state.topicCounts.has(key) || state.topicCountLoading.has(key)) {
    return;
  }

  state.topicCountLoading.add(key);
  try {
    const data = await fetchJSON(
      `/api/online/topic-count?rule=${encodeURIComponent(ruleId)}&topic_id=${encodeURIComponent(
        topic.topic_id
      )}&detail_url=${encodeURIComponent(topic.detail_url)}`
    );
    setTopicCount(ruleId, topic.topic_id, data.count);
  } catch (_) {
    // keep placeholder if count API fails
  } finally {
    state.topicCountLoading.delete(key);
  }
}

async function downloadSelectedTopics() {
  if (!state.activeTab || state.activeTab.kind !== "online") {
    updateDownloadFab();
    return;
  }

  const selected = Array.from(activeSelectionSet());
  if (selected.length === 0) {
    showToast("未选择主题");
    updateDownloadFab();
    return;
  }

  let okCount = 0;
  let skippedCount = 0;
  for (const topicId of selected) {
    const topic = state.topics.find((item) => item.topic_id === topicId);
    if (!topic) continue;

    try {
      const payload = {
        rule: state.activeTab.id,
        topic_id: topic.topic_id,
        title: topic.title,
        detail_url: topic.detail_url,
      };

      const cached = getTopicImageCache(state.activeTab.id, topic.topic_id);
      if (cached && cached.complete && Array.isArray(cached.urls) && cached.urls.length > 0) {
        payload.image_urls = cached.urls.slice();
      }

      const result = await fetchJSON("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (result?.skipped) {
        skippedCount += 1;
      } else {
        okCount += 1;
      }
    } catch (_) {
      showToast(`下载失败: ${topic.title}`);
    }
  }

  const suffix = skippedCount > 0 ? `，跳过 ${skippedCount} 个（目录已存在）` : "";
  showToast(`已加入下载队列 ${okCount} 个${suffix}`);
  activeSelectionSet().clear();
  renderTopics();
  updateDownloadFab();
  await pollJobs();
}

function decodeProxyImageUrl(viewerUrl) {
  if (!viewerUrl) return "";
  try {
    const parsed = new URL(viewerUrl, window.location.origin);
    if (!parsed.pathname.includes("/api/online/image-proxy")) {
      return viewerUrl;
    }
    return parsed.searchParams.get("url") || viewerUrl;
  } catch (_) {
    return viewerUrl;
  }
}

function buildImageProxyUrl(url, referer = "") {
  const encodedUrl = encodeURIComponent(url || "");
  if (!encodedUrl) {
    return "";
  }
  if (referer) {
    return `/api/online/image-proxy?url=${encodedUrl}&referer=${encodeURIComponent(referer)}`;
  }
  return `/api/online/image-proxy?url=${encodedUrl}`;
}

function normalizeProxyRawImageUrl(rawUrl) {
  const value = (rawUrl || "").trim();
  if (!value) {
    return "";
  }
  try {
    const parsed = new URL(value, window.location.origin);
    const host = (parsed.hostname || "").toLowerCase();
    if (host.endsWith(".wp.com")) {
      const path = (parsed.pathname || "").replace(/^\/+/, "");
      const slash = path.indexOf("/");
      if (slash > 0) {
        const originHost = path.slice(0, slash).trim().toLowerCase();
        const originPath = path.slice(slash);
        if (originHost.includes(".")) {
          const out = new URL(`https://${originHost}${originPath}`);
          out.search = parsed.search || "";
          return out.toString();
        }
      }
    }
    return parsed.toString();
  } catch (_) {
    return value;
  }
}

function ensureActiveTab() {
  const onlineRules = getEnabledOnlineRules();
  const onlineRuleIds = new Set(onlineRules.map((r) => r.rule_id));
  const shelfIds = new Set(state.shelves.map((s) => String(s.shelf_id)));

  let changed = false;
  if (state.activeTab && state.activeTab.kind === "online") {
    if (!onlineRuleIds.has(state.activeTab.id)) {
      state.activeTab = null;
      changed = true;
    }
  }
  if (state.activeTab && state.activeTab.kind === "local") {
    if (!shelfIds.has(state.activeTab.id)) {
      state.activeTab = null;
      changed = true;
    }
  }

  if (!state.activeTab) {
    if (onlineRules.length > 0) {
      state.activeTab = { kind: "online", id: onlineRules[0].rule_id };
      changed = true;
    } else if (state.shelves.length > 0) {
      state.activeTab = { kind: "local", id: String(state.shelves[0].shelf_id) };
      changed = true;
    }
  }

  return changed;
}

function fallbackProxyImage(imgEl) {
  if (!imgEl || imgEl.dataset.proxyFallbackTried === "1") {
    return false;
  }
  try {
    const parsed = new URL(imgEl.src, window.location.origin);
    if (!parsed.pathname.includes("/api/online/image-proxy")) {
      return false;
    }
    const raw = parsed.searchParams.get("url");
    if (!raw) {
      return false;
    }
    const referer = parsed.searchParams.get("referer") || "";
    const normalizedRaw = normalizeProxyRawImageUrl(raw);
    const retryProxyUrl = buildImageProxyUrl(normalizedRaw || raw, referer);
    if (!retryProxyUrl || retryProxyUrl === imgEl.src) {
      return false;
    }
    imgEl.dataset.proxyFallbackTried = "1";
    imgEl.src = retryProxyUrl;
    imgEl.referrerPolicy = "no-referrer";
    return true;
  } catch (_) {
    return false;
  }
}

function appendViewerImage(url, index, referer = "") {
  const itemEl = document.createElement("div");
  itemEl.className = "viewer-item";

  const indexEl = document.createElement("span");
  indexEl.className = "viewer-index";
  indexEl.textContent = String(index);

  const img = document.createElement("img");
  img.src = url;
  img.className = "viewer-img";
  img.loading = "lazy";
  img.referrerPolicy = "no-referrer";
  img.onerror = () => {
    const isProxy = img.src.includes("/api/online/image-proxy");
    if (!isProxy && referer && img.dataset.directProxyTried !== "1") {
      img.dataset.directProxyTried = "1";
      const proxyUrl = buildImageProxyUrl(img.src, referer);
      if (proxyUrl) {
        img.src = proxyUrl;
        return;
      }
    }
    if (fallbackProxyImage(img)) {
      return;
    }
    img.style.display = "none";
  };

  itemEl.appendChild(indexEl);
  itemEl.appendChild(img);
  viewerBodyEl.appendChild(itemEl);
}

async function bootstrap() {
  bindBaseEvents();
  await reloadAllMeta();
  await loadTopics();
  await pollJobs();
  setInterval(pollJobs, 5000);
}

async function reloadAllMeta() {
  const [ruleRes, shelfRes] = await Promise.all([fetchJSON("/api/rules"), fetchJSON("/api/shelves")]);

  state.rules = ruleRes.items || [];
  state.shelves = shelfRes.items || [];

  const validRuleIds = new Set(state.rules.map((r) => r.rule_id));
  const validShelfIds = new Set(state.shelves.map((s) => String(s.shelf_id)));
  Array.from(state.onlineSearch.keys()).forEach((ruleId) => {
    if (!validRuleIds.has(ruleId)) {
      state.onlineSearch.delete(ruleId);
    }
  });
  Array.from(state.localSearch.keys()).forEach((shelfId) => {
    if (!validShelfIds.has(String(shelfId))) {
      state.localSearch.delete(String(shelfId));
    }
  });
  Array.from(state.onlineCategory.keys()).forEach((ruleId) => {
    if (!validRuleIds.has(ruleId)) {
      state.onlineCategory.delete(ruleId);
    }
  });
  state.rules.forEach((rule) => {
    if (!state.onlineSearch.has(rule.rule_id)) {
      state.onlineSearch.set(rule.rule_id, "");
    }
    const categories = getRuleCategories(rule.rule_id);
    if (!categories.length) {
      state.onlineCategory.delete(rule.rule_id);
      return;
    }
    const selected = getOnlineCategory(rule.rule_id);
    state.onlineCategory.set(rule.rule_id, selected);
  });
  state.shelves.forEach((shelf) => {
    const key = String(shelf.shelf_id);
    if (!state.localSearch.has(key)) {
      state.localSearch.set(key, "");
    }
  });

  ensureActiveTab();
  renderTabs();
  renderActions();
}

function bindBaseEvents() {
  if (mobileToolsBtnEl && topbarEl) {
    mobileToolsBtnEl.addEventListener("click", () => {
      topbarEl.classList.toggle("mobile-tools-open");
      const expanded = topbarEl.classList.contains("mobile-tools-open");
      mobileToolsBtnEl.setAttribute("aria-expanded", expanded ? "true" : "false");
    });
  }

  const folderPickerCloseIds = ["folder-picker-close", "folder-picker-cancel", "folder-picker-mask"];
  folderPickerCloseIds.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", () => closeFolderPicker(""));
  });

  if (folderPickerUpEl) {
    folderPickerUpEl.addEventListener("click", async () => {
      if (!state.folderPicker.parent) return;
      try {
        await loadFolderPicker(state.folderPicker.parent);
      } catch (error) {
        showToast(error.message);
      }
    });
  }

  const folderRefreshEl = document.getElementById("folder-picker-refresh");
  if (folderRefreshEl) {
    folderRefreshEl.addEventListener("click", async () => {
      const path = state.folderPicker.current || "";
      try {
        await loadFolderPicker(path);
      } catch (error) {
        showToast(error.message);
      }
    });
  }

  if (folderPickerChooseEl) {
    folderPickerChooseEl.addEventListener("click", () => {
      if (!state.folderPicker.current) {
        showToast("请先进入要选择的目录");
        return;
      }
      closeFolderPicker(state.folderPicker.current);
    });
  }

  document.getElementById("btn-pick-shelf-root").addEventListener("click", async () => {
    try {
      const initialDir = state.newShelfRoots.length ? state.newShelfRoots[state.newShelfRoots.length - 1] : "";
      const picked = await pickRuleDownloadDir(initialDir);
      if (!picked) {
        return;
      }
      if (!state.newShelfRoots.includes(picked)) {
        state.newShelfRoots.push(picked);
      }
      renderShelfRootsDisplay();
    } catch (error) {
      showToast(error.message);
    }
  });

  document.getElementById("btn-clear-shelf-roots").addEventListener("click", () => {
    state.newShelfRoots = [];
    renderShelfRootsDisplay();
  });

  document.getElementById("btn-prev").addEventListener("click", async () => {
    if (state.page <= 1) return;
    state.page -= 1;
    await loadTopics();
  });

  document.getElementById("btn-next").addEventListener("click", async () => {
    state.page += 1;
    await loadTopics();
  });

  document.getElementById("btn-create-shelf").addEventListener("click", async () => {
    const name = document.getElementById("shelf-name").value.trim();
    const roots = state.newShelfRoots.slice(0, 10);
    if (!name || roots.length === 0) {
      showToast("请填写书架名称和路径");
      return;
    }

    try {
      await fetchJSON("/api/shelves", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, roots }),
      });
      showToast("已新增书架");
      document.getElementById("shelf-name").value = "";
      state.newShelfRoots = [];
      renderShelfRootsDisplay();
      state.page = 1;
      await reloadAllMeta();
      await loadTopics();
    } catch (error) {
      showToast(error.message);
    }
  });

  document.getElementById("viewer-close").addEventListener("click", closeViewer);
  document.getElementById("viewer-close-mask").addEventListener("click", closeViewer);
  if (downloadFabEl) {
    downloadFabEl.addEventListener("click", async () => {
      await downloadSelectedTopics();
    });
  }

  viewerBodyEl.addEventListener("scroll", async () => {
    if (!state.viewer.open || state.viewer.loading || !state.viewer.hasMore) return;
    const nearBottom = viewerBodyEl.scrollTop + viewerBodyEl.clientHeight >= viewerBodyEl.scrollHeight * 0.75;
    if (!nearBottom) return;
    await loadViewerImages();
  });

  renderShelfRootsDisplay();
  updateDownloadFab();
}

function renderTabs() {
  tabsEl.innerHTML = "";

  getEnabledOnlineRules().forEach((rule) => {
    const tab = document.createElement("button");
    tab.className = "tab";
    tab.textContent = `在线 · ${rule.name}`;

    const isActive = state.activeTab && state.activeTab.kind === "online" && state.activeTab.id === rule.rule_id;
    if (isActive) tab.classList.add("active");

    tab.addEventListener("click", async () => {
      state.activeTab = { kind: "online", id: rule.rule_id };
      state.page = 1;
      renderTabs();
      renderActions();
      await loadTopics();
    });

    tabsEl.appendChild(tab);
  });

  state.shelves.forEach((shelf) => {
    const tab = document.createElement("button");
    tab.className = "tab";
    tab.textContent = `本地 · ${shelf.name}`;

    const isActive = state.activeTab && state.activeTab.kind === "local" && state.activeTab.id === String(shelf.shelf_id);
    if (isActive) tab.classList.add("active");

    tab.addEventListener("click", async () => {
      state.activeTab = { kind: "local", id: String(shelf.shelf_id) };
      state.page = 1;
      renderTabs();
      renderActions();
      await loadTopics();
    });

    tabsEl.appendChild(tab);
  });
}

function renderRuleManagerSection() {
  const buttons = state.rules
    .map((rule) => {
      const enabled = Number(rule.enabled) === 1;
      const next = enabled ? 0 : 1;
      return `<button class="rule-toggle ${enabled ? "on" : "off"}" data-rule-id="${escapeHtml(rule.rule_id)}" data-next="${next}">${escapeHtml(rule.name)}：${enabled ? "开启" : "关闭"}</button>`;
    })
    .join("");

  return `<div class="rule-manager"><span>在线规则开关</span>${buttons}</div>`;
}

function bindRuleToggleEvents() {
  actionsEl.querySelectorAll(".rule-toggle").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const ruleId = btn.dataset.ruleId;
      const next = Number(btn.dataset.next || "0");
      try {
        await fetchJSON(`/api/rules/${encodeURIComponent(ruleId)}/enabled`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: next }),
        });
        showToast(next === 1 ? "规则已开启" : "规则已关闭");
        state.page = 1;
        await reloadAllMeta();
        await loadTopics();
      } catch (error) {
        showToast(error.message);
      }
    });
  });
}

function renderActions() {
  const managerHtml = renderRuleManagerSection();

  if (!state.activeTab) {
    actionsEl.innerHTML = managerHtml;
    bindRuleToggleEvents();
    updateDownloadFab();
    return;
  }

  if (state.activeTab.kind === "online") {
    const rule = getRuleById(state.activeTab.id);
    const categories = getRuleCategories(state.activeTab.id);
    const selectedCategory = getOnlineCategory(state.activeTab.id);
    const dir = rule ? rule.download_dir : "";
    const supportsSearch = Number(rule?.supports_search || 0) === 1;
    const currentQuery = supportsSearch ? getOnlineQuery(state.activeTab.id) : "";
    const categoryHtml = categories.length
      ? `<div class="category-switch"><span>分类</span>${categories
          .map((catId) => {
            const active = catId === selectedCategory;
            return `<button class="btn-online-category ${active ? "active" : ""}" data-category="${catId}">${escapeHtml(
              getOnlineCategoryLabel(state.activeTab.id, catId)
            )}</button>`;
          })
          .join("")}</div>`
      : "";

    actionsEl.innerHTML = `
      ${managerHtml}
      ${categoryHtml}
      ${
        supportsSearch
          ? `<div class="online-search-bar"><span>在线搜索</span><input id="online-search-input" type="text" value="${escapeHtml(currentQuery)}" placeholder="输入关键词" /><button id="btn-online-search">搜索</button><button id="btn-online-clear">清空</button></div>`
          : ""
      }
      <span>下载目录</span>
      <div class="dir-picker-row">
        <input id="rule-dir" type="text" value="${escapeHtml(dir)}" readonly />
        <button id="btn-pick-dir">选择目录</button>
      </div>
    `;

    bindRuleToggleEvents();

    actionsEl.querySelectorAll(".btn-online-category").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const category = Number(btn.dataset.category || "0");
        if (!Number.isInteger(category) || category <= 0) {
          return;
        }
        state.onlineCategory.set(state.activeTab.id, category);
        state.page = 1;
        renderActions();
        await loadTopics();
      });
    });

    if (supportsSearch) {
      const input = document.getElementById("online-search-input");
      const doSearch = async () => {
        state.onlineSearch.set(state.activeTab.id, input.value.trim());
        state.page = 1;
        await loadTopics();
      };

      document.getElementById("btn-online-search").addEventListener("click", doSearch);
      input.addEventListener("keydown", async (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        await doSearch();
      });

      document.getElementById("btn-online-clear").addEventListener("click", async () => {
        input.value = "";
        state.onlineSearch.set(state.activeTab.id, "");
        state.page = 1;
        await loadTopics();
      });
    }

    document.getElementById("btn-pick-dir").addEventListener("click", async () => {
      try {
        const currentDir = document.getElementById("rule-dir").value.trim();
        const picked = await pickRuleDownloadDir(currentDir);
        if (!picked) {
          return;
        }
        await fetchJSON(`/api/rules/${state.activeTab.id}/download_dir`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ download_dir: picked }),
        });
        showToast("目录已更新");
        await reloadAllMeta();
      } catch (error) {
        showToast(error.message);
      }
    });

    updateDownloadFab();
    return;
  }

  const shelfId = Number(state.activeTab.id);
  const shelf = getShelfById(state.activeTab.id);
  const allowDeleteShelf = shelf && shelf.source_type === "custom";
  const currentQuery = getLocalQuery(state.activeTab.id);
  actionsEl.innerHTML = `
    ${managerHtml}
    <div class="online-search-bar"><span>书架搜索</span><input id="local-search-input" type="text" value="${escapeHtml(
      currentQuery
    )}" placeholder="按主题名搜索" /><button id="btn-local-search">搜索</button><button id="btn-local-clear">清空</button></div>
    <span>本地书架扫描深度：最多 2 层目录</span>
    <button id="btn-refresh-shelf">刷新当前书架</button>
    ${
      allowDeleteShelf
        ? '<button id="btn-delete-shelf" class="btn-danger">删除当前书架</button>'
        : ""
    }
  `;

  bindRuleToggleEvents();
  updateDownloadFab();

  const localInput = document.getElementById("local-search-input");
  const doLocalSearch = async () => {
    state.localSearch.set(state.activeTab.id, localInput.value.trim());
    state.page = 1;
    await loadTopics();
  };
  document.getElementById("btn-local-search").addEventListener("click", doLocalSearch);
  localInput.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    await doLocalSearch();
  });
  document.getElementById("btn-local-clear").addEventListener("click", async () => {
    localInput.value = "";
    state.localSearch.set(state.activeTab.id, "");
    state.page = 1;
    await loadTopics();
  });

  document.getElementById("btn-refresh-shelf").addEventListener("click", async () => {
    try {
      const data = await fetchJSON(`/api/shelves/${shelfId}/refresh`, {
        method: "POST",
      });
      showToast(`刷新完成，主题 ${data.result.topics}，图片 ${data.result.images}`);
      state.page = 1;
      await loadTopics();
    } catch (error) {
      showToast(error.message);
    }
  });

  if (allowDeleteShelf) {
    document.getElementById("btn-delete-shelf").addEventListener("click", async () => {
      const confirmed = window.confirm("确认删除当前书架及其数据库中的主题/图片记录？");
      if (!confirmed) {
        return;
      }
      try {
        const data = await fetchJSON(`/api/shelves/${shelfId}`, { method: "DELETE" });
        const result = data.result || {};
        showToast(
          `已删除书架：${result.name || ""}（主题 ${result.deleted_topics || 0}，图片 ${result.deleted_images || 0}）`
        );
        state.page = 1;
        await reloadAllMeta();
        await loadTopics();
      } catch (error) {
        showToast(error.message);
      }
    });
  }
}

async function loadTopics() {
  if (!state.activeTab) {
    pageInfoEl.textContent = "";
    topicGridEl.innerHTML = '<div class="empty">当前没有启用在线规则，也没有本地书架</div>';
    updateDownloadFab();
    return;
  }

  let pageInfo = `第 ${state.page} 页`;

  try {
    if (state.activeTab.kind === "online") {
      const rule = getRuleById(state.activeTab.id);
      const supportsSearch = Number(rule?.supports_search || 0) === 1;
      const query = supportsSearch ? getOnlineQuery(state.activeTab.id) : "";
      const selectedCategory = getOnlineCategory(state.activeTab.id);
      const skipCategoryOnSearch = state.activeTab.id === "wnacg" && Boolean(query);

      let url = `/api/online/topics?rule=${encodeURIComponent(state.activeTab.id)}&page=${state.page}`;
      if (!skipCategoryOnSearch && selectedCategory !== null) {
        url += `&category=${encodeURIComponent(selectedCategory)}`;
        pageInfo += ` · 分类: ${getOnlineCategoryLabel(state.activeTab.id, selectedCategory)}`;
      }
      if (query) {
        url += `&q=${encodeURIComponent(query)}`;
        pageInfo += ` · 搜索: ${query}`;
      }

      const data = await fetchJSON(url);
      state.topics = data.items || [];
    } else {
      const shelfId = Number(state.activeTab.id);
      const query = getLocalQuery(state.activeTab.id);
      let url = `/api/shelves/${shelfId}/topics?page=${state.page}&page_size=20`;
      if (query) {
        url += `&q=${encodeURIComponent(query)}`;
        pageInfo += ` · 搜索: ${query}`;
      }
      const data = await fetchJSON(url);
      state.topics = data.items || [];
    }
  } catch (error) {
    state.topics = [];
    showToast(error.message);
  }

  pageInfoEl.textContent = pageInfo;
  renderTopics();
  updateDownloadFab();
}

function renderTopics() {
  topicGridEl.innerHTML = "";
  if (state.topics.length === 0) {
    topicGridEl.innerHTML = '<div class="empty">当前页没有主题</div>';
    updateDownloadFab();
    return;
  }

  const selectedSet = state.activeTab && state.activeTab.kind === "online" ? activeSelectionSet() : new Set();

  state.topics.forEach((topic) => {
    const card = document.createElement("div");
    card.className = "card card-topic";
    const cover = topic.cover_url || "";
    const title = topic.title || "未命名主题";
    const isOnline = state.activeTab.kind === "online";
    const hideCount = isOnline && state.activeTab.id === "4khd";
    const countText = isOnline
      ? (() => {
          if (hideCount) return "";
          const count = getTopicCount(state.activeTab.id, topic.topic_id);
          return count === null ? "统计中..." : `${count} 张`;
        })()
      : `${topic.total_images || 0} 张`;

    card.innerHTML = `
      <img class="card-cover" src="${escapeHtml(cover)}" alt="cover" loading="lazy" referrerpolicy="no-referrer" />
      <div class="card-body">
        <div class="card-title">${escapeHtml(title)}</div>
        <div class="card-meta">${isOnline ? "规则主题" : `路径: ${escapeHtml(topic.rel_path || ".")}`}</div>
        <div class="card-row">
          ${
            hideCount
              ? ""
              : `<span class="topic-count" data-rule-id="${isOnline ? escapeHtml(state.activeTab.id) : ""}" data-topic-id="${
                  isOnline ? escapeHtml(topic.topic_id) : ""
                }">${escapeHtml(countText)}</span>`
          }
          ${
            isOnline
              ? `<label class="selection-wrap"><input class="chk-topic" type="checkbox" ${selectedSet.has(topic.topic_id) ? "checked" : ""} /> 选中</label>`
              : ""
          }
        </div>
      </div>
    `;

    const coverEl = card.querySelector(".card-cover");
    if (coverEl) {
      coverEl.onerror = () => {
        if (fallbackProxyImage(coverEl)) {
          return;
        }
        coverEl.src = "";
        coverEl.alt = "no cover";
      };
    }

    card.addEventListener("click", (event) => {
      if (event.target instanceof Element && event.target.closest(".selection-wrap")) {
        return;
      }
      openViewer(topic);
    });

    if (isOnline) {
      card.querySelector(".chk-topic").addEventListener("change", (event) => {
        event.stopPropagation();
        if (event.target.checked) {
          selectedSet.add(topic.topic_id);
        } else {
          selectedSet.delete(topic.topic_id);
        }
        updateDownloadFab();
      });
      if (!hideCount) {
        ensureOnlineTopicCount(state.activeTab.id, topic).catch(() => {
          // no-op
        });
      }
    }

    topicGridEl.appendChild(card);
  });
  updateDownloadFab();
}

async function openViewer(topic) {
  state.viewer.open = true;
  state.viewer.offset = 0;
  state.viewer.hasMore = true;
  state.viewer.loading = false;

  if (state.activeTab.kind === "online") {
    state.viewer.mode = "online";
    state.viewer.payload = {
      rule: state.activeTab.id,
      topic_id: topic.topic_id,
      detail_url: topic.detail_url,
    };
    const cacheKey = topicImageCacheKey(state.activeTab.id, topic.topic_id);
    if (!state.topicImageCache.has(cacheKey)) {
      state.topicImageCache.set(cacheKey, {
        urls: [],
        complete: false,
      });
    }
  } else {
    state.viewer.mode = "local";
    state.viewer.payload = {
      topic_id: topic.topic_id,
    };
  }

  viewerTitleEl.textContent = topic.title || "主题";
  viewerBodyEl.innerHTML = "";
  viewerBodyEl.scrollTop = 0;
  viewerEl.classList.remove("hidden");
  document.body.style.overflow = "hidden";

  await loadViewerImages();
}

function closeViewer() {
  state.viewer.open = false;
  viewerEl.classList.add("hidden");
  document.body.style.overflow = "";
}

async function loadViewerImages() {
  if (!state.viewer.open || state.viewer.loading || !state.viewer.hasMore) return;
  state.viewer.loading = true;

  try {
    let data;
    let fetchedCount = 0;
    const startIndex = viewerBodyEl.querySelectorAll(".viewer-item").length;

    if (state.viewer.mode === "online") {
      const payload = state.viewer.payload;
      data = await fetchJSON(
        `/api/online/topic-images?rule=${encodeURIComponent(payload.rule)}&topic_id=${encodeURIComponent(
          payload.topic_id
        )}&detail_url=${encodeURIComponent(payload.detail_url)}&offset=${state.viewer.offset}&limit=20`
      );

      const urls = Array.isArray(data.items) ? data.items : [];
      fetchedCount = urls.length;
      urls.forEach((url, idx) => {
        appendViewerImage(url, startIndex + idx + 1, payload.detail_url || "");
      });

      const cacheKey = topicImageCacheKey(payload.rule, payload.topic_id);
      const cached = state.topicImageCache.get(cacheKey) || { urls: [], complete: false };
      urls.map((url) => decodeProxyImageUrl(url)).forEach((raw) => {
        if (!raw) return;
        if (!cached.urls.includes(raw)) {
          cached.urls.push(raw);
        }
      });
      cached.complete = !Boolean(data.has_more);
      state.topicImageCache.set(cacheKey, cached);

      if (Number.isFinite(Number(data.total)) && Number(data.total) >= 0) {
        setTopicCount(payload.rule, payload.topic_id, data.total);
      }
    } else {
      const payload = state.viewer.payload;
      data = await fetchJSON(`/api/shelves/topic/${payload.topic_id}/images?offset=${state.viewer.offset}&limit=20`);

      const items = Array.isArray(data.items) ? data.items : [];
      fetchedCount = items.length;
      items.forEach((item, idx) => {
        appendViewerImage(item.image_url, startIndex + idx + 1);
      });
    }

    const prevOffset = state.viewer.offset;
    const nextOffsetRaw = Number(data.next_offset);
    let nextOffset = Number.isFinite(nextOffsetRaw) ? Math.max(0, nextOffsetRaw) : prevOffset + fetchedCount;
    let hasMore = Boolean(data.has_more);

    if (hasMore && nextOffset <= prevOffset) {
      nextOffset = prevOffset + Math.max(1, fetchedCount);
    }

    const total = Number(data.total);
    if (Number.isFinite(total) && total >= 0 && nextOffset >= total) {
      hasMore = false;
    }

    state.viewer.offset = nextOffset;
    state.viewer.hasMore = hasMore;
  } catch (error) {
    showToast(error.message);
    state.viewer.hasMore = false;
  } finally {
    state.viewer.loading = false;

    // If current content is shorter than viewport, keep fetching next batch automatically.
    if (
      state.viewer.open &&
      state.viewer.hasMore &&
      viewerBodyEl.scrollHeight <= viewerBodyEl.clientHeight * 1.15
    ) {
      setTimeout(() => {
        loadViewerImages().catch(() => {
          // errors are already handled inside loadViewerImages
        });
      }, 0);
    }
  }
}

async function pollJobs() {
  try {
    const data = await fetchJSON("/api/download/jobs?limit=40");
    renderJobs(data.items || []);
  } catch (error) {
    if (sidePanelEl) sidePanelEl.classList.add("hidden");
    jobListEl.innerHTML = "";
  }
}

function renderJobs(items) {
  if (!items.length) {
    if (sidePanelEl) sidePanelEl.classList.add("hidden");
    jobListEl.innerHTML = "";
    return;
  }

  if (sidePanelEl) sidePanelEl.classList.remove("hidden");

  const total = items.length;
  const showing = items.slice(0, 2);
  jobListEl.innerHTML = "";
  const summary = document.createElement("div");
  summary.className = "queue-summary";
  summary.textContent = `下载中 ${total} 项`;
  jobListEl.appendChild(summary);

  showing.forEach((job) => {
    const div = document.createElement("div");
    div.className = "job-item";
    div.innerHTML = `
      <div>${escapeHtml(job.title)}</div>
      <div class="job-status">${escapeHtml(job.status)} · ${job.downloaded_images}/${job.total_images}</div>
    `;
    jobListEl.appendChild(div);
  });

  if (total > showing.length) {
    const more = document.createElement("div");
    more.className = "card-meta";
    more.textContent = `还有 ${total - showing.length} 项任务`;
    jobListEl.appendChild(more);
  }
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

bootstrap().catch((error) => {
  showToast(error.message || "初始化失败");
});










