// Bump this together with the ?v= query in index.html: the browser caches
// /static/ui/app.js by URL, so a stale copy silently keeps running old logic.
const APP_BUILD = "2026-10-27.3";
window.__appBuild = APP_BUILD;

const API_BASE = "/api/v1";
const TOKEN_KEY = "etl_agent_token_v2";
const SESSION_KEY = "etl_agent_session_v2";

// Login state lives in localStorage so a session survives browser restarts:
// tokens are issued without an ``exp`` claim while JWT_EXPIRE_MINUTES=0.
// sessionStorage stays as a fallback for tabs opened before the switch.
function readStoredAuth(key) {
  return localStorage.getItem(key) || sessionStorage.getItem(key) || "";
}

function storeAuth(token, session) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(SESSION_KEY);
}

function clearStoredAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(SESSION_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(SESSION_KEY);
}

const state = {
  token: readStoredAuth(TOKEN_KEY) || "",
  session: null,
  health: null,
  tenants: [],
  projects: [],
  connections: [],
  pipelines: [],
  runs: [],
  editingProjectId: null,
  editingConnectionId: null,
  connectionProjectId: null,
  clarifyingPipelineId: null,
  loading: false,
  kb: {
    docs: [],
    chunkDocId: null,
    chunkSeq: 0,
    candidates: [],
    publishedFaqs: [],
    gaps: [],
    metrics: null,
    audit: [],
    access: null,
    departments: [],
    rolePermissions: [],
    permissionCodes: [],
    members: [],
    tenantMembers: [],
    editingDepartmentId: null,
    editingRole: null,
  },
};

const els = {};

document.addEventListener("DOMContentLoaded", () => {
  // Only purge the legacy v1 keys; v2 credentials are intentionally persisted
  // in localStorage so a login is no longer lost when the browser closes.
  localStorage.removeItem("etl_agent_token");
  localStorage.removeItem("etl_agent_session");
  bindElements();
  bindActions();
  hydrateSession();
  showBuildBadge();
  if (state.token) {
    showApp();
    loadAll().then(() => openRequestedPage());
  } else {
    showLogin();
  }
  window.lucide.createIcons();
});

// Visible build stamp. If this badge is missing (bottom-right corner), the
// browser is still executing a cached app.js and every "fix" will look ignored.
function showBuildBadge() {
  if (document.getElementById("uiBuildBadge")) return;
  const badge = document.createElement("div");
  badge.id = "uiBuildBadge";
  badge.textContent = `UI ${APP_BUILD}`;
  badge.title = "前端构建版本，用于确认浏览器是否加载了最新脚本";
  badge.style.cssText =
    "position:fixed;right:10px;bottom:8px;z-index:60;font-size:11px;" +
    "line-height:1;padding:4px 7px;border-radius:999px;" +
    "background:rgba(15,23,42,.06);color:#64748b;pointer-events:none;";
  document.body.appendChild(badge);
}

function openRequestedPage() {
  const requested = new URLSearchParams(window.location.search).get("page");
  if (!requested || !document.getElementById(`page-${requested}`)) return;
  // Never jump into a page whose nav entry was hidden by permissions.
  const navItem = document.querySelector(`.nav-item[data-page="${requested}"]`);
  if (navItem && navItem.hidden) return;
  goToPage(requested);
}

function bindElements() {
  Object.assign(els, {
    appShell: document.getElementById("appShell"),
    sidebar: document.getElementById("sidebar"),
    backdrop: document.getElementById("backdrop"),
    menuButton: document.getElementById("menuButton"),
    loginView: document.getElementById("loginView"),
    usernameInput: document.getElementById("usernameInput"),
    passwordInput: document.getElementById("passwordInput"),
    loginButton: document.getElementById("loginButton"),
    authStatus: document.getElementById("authStatus"),
    logoutButton: document.getElementById("logoutButton"),
    refreshButton: document.getElementById("refreshButton"),
    servicePill: document.getElementById("servicePill"),
    userName: document.getElementById("userName"),
    userRole: document.getElementById("userRole"),
    userAvatar: document.getElementById("userAvatar"),
    pageTitle: document.getElementById("pageTitle"),
    pageSubtitle: document.getElementById("pageSubtitle"),
    toastRegion: document.getElementById("toastRegion"),
    overviewRefresh: document.getElementById("overviewRefresh"),
    flowGrid: document.getElementById("flowGrid"),
    recentRuns: document.getElementById("recentRuns"),
    runSummary: document.getElementById("runSummary"),
    systemSummary: document.getElementById("systemSummary"),
    systemDetails: document.getElementById("systemDetails"),
    createProjectButton: document.getElementById("createProjectButton"),
    createProjectPanel: document.getElementById("createProjectPanel"),
    cancelCreateProject: document.getElementById("cancelCreateProject"),
    saveProjectButton: document.getElementById("saveProjectButton"),
    projectNameInput: document.getElementById("projectNameInput"),
    projectDescInput: document.getElementById("projectDescInput"),
  });
}

function bindActions() {
  document.querySelectorAll(".nav-item[data-page]").forEach((item) => {
    item.addEventListener("click", () => {
      goToPage(item.dataset.page);
      closeMobileMenu();
    });
  });
  document.querySelectorAll("[data-goto]").forEach((item) => {
    item.addEventListener("click", () => goToPage(item.dataset.goto));
  });

  els.loginButton.addEventListener("click", login);
  els.logoutButton.addEventListener("click", logout);
  els.refreshButton.addEventListener("click", refreshCurrent);
  els.overviewRefresh.addEventListener("click", refreshCurrent);
  els.menuButton.addEventListener("click", openMobileMenu);
  els.backdrop.addEventListener("click", closeMobileMenu);
  document
    .querySelectorAll('a[href="/manual"], a[href^="/manual?"], a[href="/docs"], a[href^="/docs?"]')
    .forEach((link) => {
    link.addEventListener("click", (event) => {
      if (!state.token) {
        event.preventDefault();
        toast("需要先登录", "请先输入账号密码登录，再进入操作手册", "error");
      }
    });
  });
  els.createProjectButton.addEventListener("click", () => {
    state.editingProjectId = null;
    document.getElementById("projectFormTitle").textContent = "新建项目";
    els.projectNameInput.value = "";
    els.projectDescInput.value = "";
    els.createProjectPanel.hidden = false;
    els.projectNameInput.focus();
  });
  els.cancelCreateProject.addEventListener("click", () => {
    state.editingProjectId = null;
    els.createProjectPanel.hidden = true;
  });
  els.saveProjectButton.addEventListener("click", createProject);

  // Knowledge base bindings
  const kbImportBtn = document.getElementById("kbImportDoc");
  if (kbImportBtn) kbImportBtn.addEventListener("click", () => {
    document.getElementById("kbImportPanel").hidden = false;
  });
  const kbLoadDemo = document.getElementById("kbLoadDemo");
  if (kbLoadDemo) kbLoadDemo.addEventListener("click", kbLoadDemoDocs);
  const kbCancelImport = document.getElementById("kbCancelImport");
  if (kbCancelImport) kbCancelImport.addEventListener("click", () => {
    document.getElementById("kbImportPanel").hidden = true;
  });
  const kbSubmitDoc = document.getElementById("kbSubmitDoc");
  if (kbSubmitDoc) kbSubmitDoc.addEventListener("click", kbImportDocument);
  const kbBatchImport = document.getElementById("kbBatchImport");
  if (kbBatchImport) kbBatchImport.addEventListener("click", () => {
    document.getElementById("kbBatchPanel").hidden = false;
  });
  const kbCancelBatch = document.getElementById("kbCancelBatch");
  if (kbCancelBatch) kbCancelBatch.addEventListener("click", () => {
    document.getElementById("kbBatchPanel").hidden = true;
  });
  const kbSubmitBatch = document.getElementById("kbSubmitBatch");
  if (kbSubmitBatch) kbSubmitBatch.addEventListener("click", kbImportBatch);
  const kbCloseChunks = document.getElementById("kbCloseChunks");
  if (kbCloseChunks) kbCloseChunks.addEventListener("click", closeKbChunksPanel);

  // Delegated listeners: the document table is re-rendered after every
  // save/refresh, so binding per row would silently drop handlers.
  const kbDocsBody = document.getElementById("kbDocsBody");
  if (kbDocsBody) kbDocsBody.addEventListener("click", (event) => {
    const chunkButton = event.target.closest(".kb-chunks-btn");
    if (chunkButton) {
      kbLoadDocumentChunks(chunkButton.dataset.docId);
      return;
    }
    const publishButton = event.target.closest(".kb-publish-btn");
    if (publishButton && !publishButton.disabled) {
      kbPublishDoc(publishButton.dataset.docId, publishButton);
      return;
    }
    const deleteButton = event.target.closest(".kb-delete-doc-btn");
    if (deleteButton) kbDeleteDoc(deleteButton.dataset.docId, deleteButton);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const chunksPanel = document.getElementById("kbChunksPanel");
    if (chunksPanel && !chunksPanel.hidden) closeKbChunksPanel();
  });
  const kbAskSubmit = document.getElementById("kbAskSubmit");
  if (kbAskSubmit) kbAskSubmit.addEventListener("click", kbAskQuestion);
  const kbAskInput = document.getElementById("kbAskInput");
  if (kbAskInput) kbAskInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") kbAskQuestion();
  });
  const kbGapStatusFilter = document.getElementById("kbGapStatusFilter");
  if (kbGapStatusFilter) kbGapStatusFilter.addEventListener("change", () => {
    renderKbGaps();
  });
  const kbMetricsRefresh = document.getElementById("kbMetricsRefresh");
  if (kbMetricsRefresh) kbMetricsRefresh.addEventListener("click", () => {
    loadKnowledgePage("kb-metrics");
  });
  const kbClusterFaqs = document.getElementById("kbClusterFaqs");
  if (kbClusterFaqs) kbClusterFaqs.addEventListener("click", kbClusterFaqCandidates);
  const kbDepartmentCreate = document.getElementById("kbDepartmentCreate");
  if (kbDepartmentCreate) kbDepartmentCreate.addEventListener("click", () => {
    state.kb.editingDepartmentId = null;
    document.getElementById("kbDepartmentForm").hidden = false;
    document.getElementById("kbDepartmentName").value = "";
    document.getElementById("kbDepartmentCode").value = "";
    document.getElementById("kbDepartmentParent").value = "";
    document.getElementById("kbDepartmentSort").value = "0";
  });
  const kbCancelDepartment = document.getElementById("kbCancelDepartment");
  if (kbCancelDepartment) kbCancelDepartment.addEventListener("click", () => {
    document.getElementById("kbDepartmentForm").hidden = true;
  });
  const departmentSaveButton = document.getElementById("kbSaveDepartment");
  if (departmentSaveButton) {
    departmentSaveButton.addEventListener("click", kbSaveDepartment);
  }
  const memberSaveButton = document.getElementById("kbSaveMember");
  if (memberSaveButton) {
    memberSaveButton.addEventListener("click", kbSaveMemberAssignment);
  }
  const kbCancelRoleEditor = document.getElementById("kbCancelRoleEditor");
  if (kbCancelRoleEditor) kbCancelRoleEditor.addEventListener("click", () => {
    document.getElementById("kbRoleEditorPanel").hidden = true;
  });
  const rolePermissionSaveButton = document.getElementById("kbSaveRolePermissions");
  if (rolePermissionSaveButton) {
    rolePermissionSaveButton.addEventListener("click", kbSaveRolePermissions);
  }

  const createConnectionButton = document.getElementById("createConnectionButton");
  if (createConnectionButton) {
    createConnectionButton.addEventListener("click", () => {
      state.editingConnectionId = null;
      document.getElementById("connectionFormTitle").textContent = "新增数据连接";
      document.getElementById("connProjectId").disabled = false;
      clearConnectionForm();
      populateConnectionProjects();
      document.getElementById("connectionFormPanel").hidden = false;
    });
  }
  const cancelCreateConnection = document.getElementById("cancelCreateConnection");
  if (cancelCreateConnection) {
    cancelCreateConnection.addEventListener("click", () => {
      state.editingConnectionId = null;
      document.getElementById("connectionFormPanel").hidden = true;
    });
  }
  const saveConnectionButton = document.getElementById("saveConnectionButton");
  if (saveConnectionButton) saveConnectionButton.addEventListener("click", createConnection);
  const connectionProjectFilter = document.getElementById("connectionProjectFilter");
  if (connectionProjectFilter) {
    connectionProjectFilter.addEventListener("change", () => {
      state.connectionProjectId = connectionProjectFilter.value;
      renderConnections();
    });
  }
  const cancelClarifyPipeline = document.getElementById("cancelClarifyPipeline");
  if (cancelClarifyPipeline) {
    cancelClarifyPipeline.addEventListener("click", () => {
      state.clarifyingPipelineId = null;
      document.getElementById("pipelineClarifyPanel").hidden = true;
    });
  }
  const saveClarifyPipeline = document.getElementById("saveClarifyPipeline");
  if (saveClarifyPipeline) saveClarifyPipeline.addEventListener("click", submitPipelineClarification);

  ["usernameInput", "passwordInput"].forEach((id) => {
    document.getElementById(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") login();
    });
  });
}

function hydrateSession() {
  try {
    const raw = readStoredAuth(SESSION_KEY);
    state.session = raw ? JSON.parse(raw) : null;
  } catch {
    state.session = null;
  }
  if (state.session) {
    els.userName.textContent = state.session.username || "管理员";
    els.userRole.textContent = state.session.role || "本地环境";
    els.userAvatar.textContent = (state.session.username || "A").slice(0, 1).toUpperCase();
    const canCreateProject = state.session.role === "tenant_admin";
    els.createProjectButton.hidden = !canCreateProject;
    const canWrite = ["engineer", "project_admin", "tenant_admin"].includes(
      state.session.role,
    );
    const createConnectionButton = document.getElementById("createConnectionButton");
    if (createConnectionButton) createConnectionButton.hidden = !canWrite;
  }
}

function showLogin() {
  els.appShell.hidden = true;
  els.loginView.classList.remove("hidden");
}

function showApp() {
  els.loginView.classList.add("hidden");
  els.appShell.hidden = false;
  hydrateSession();
}

function setServicePill(status) {
  const pill = els.servicePill;
  pill.classList.remove("ok", "warn");
  const dot = pill.querySelector(".dot");
  const text = pill.querySelector(".pill-text");
  if (!status) {
    text.textContent = "服务未知";
    return;
  }
  if (status === "ok") {
    pill.classList.add("ok");
    text.textContent = "全部服务正常";
  } else {
    pill.classList.add("warn");
    text.textContent = "部分服务异常";
  }
}

function goToPage(page) {
  const titles = {
    overview: ["总览", "ETL 管道从生成到发布的可视化入口"],
    projects: ["项目", "租户内的项目与资源边界"],
    connections: ["数据连接", "数据源连接与测试状态"],
    pipelines: ["管道", "冻结后的 ETL 管道版本"],
    runs: ["执行记录", "执行生命周期与发布状态"],
    "kb-docs": ["文档管理", "知识库文档导入、分块与权限控制"],
    "kb-ask": ["问答工作台", "向知识库提问并查看检索详情"],
    "kb-faqs": ["FAQ 管理", "候选 FAQ 审核与已发布 FAQ 查看"],
    "kb-gaps": ["缺口清单", "知识库无法回答的问题汇总"],
    "kb-metrics": ["运营看板", "问答量、FAQ 沉淀与缺口收敛趋势"],
    "kb-admin": ["组织与权限", "部门树、知识角色与操作按钮权限"],
    system: ["系统状态", "控制面与数据面依赖"],
  };
  const [title, subtitle] = titles[page] || titles.overview;
  els.pageTitle.textContent = title;
  els.pageSubtitle.textContent = subtitle;

  document.querySelectorAll(".nav-item[data-page]").forEach((item) => {
    item.classList.toggle("active", item.dataset.page === page);
  });
  document.querySelectorAll(".page").forEach((section) => {
    section.classList.toggle("active", section.id === `page-${page}`);
  });
  if (page.startsWith("kb-")) {
    loadKnowledgePage(page);
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openMobileMenu() {
  els.sidebar.classList.add("open");
  els.backdrop.hidden = false;
}

function closeMobileMenu() {
  els.sidebar.classList.remove("open");
  els.backdrop.hidden = true;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  if (state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }
  const response = await fetch(path, { ...options, headers });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload?.detail || payload?.error?.message || payload?.message || response.statusText;
    if (response.status === 401 && state.token && !path.endsWith("/auth/login")) {
      expireSession();
      throw new Error("登录状态已失效，请重新登录。");
    }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = response.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

function expireSession() {
  state.token = "";
  state.session = null;
  clearStoredAuth();
  showLogin();
  els.authStatus.textContent = "登录状态已失效，请重新输入账号和密码。";
  window.lucide.createIcons();
}

async function login() {
  const username = els.usernameInput.value.trim();
  const password = els.passwordInput.value;
  if (!username || !password) {
    els.authStatus.textContent = "请输入用户名和密码";
    return;
  }
  els.loginButton.disabled = true;
  els.authStatus.textContent = "";
  try {
    const result = await api(`${API_BASE}/auth/login`, {
      method: "POST",
      body: { username, password },
    });
    state.token = result.access_token;
    state.session = {
      username: result.username,
      role: result.role,
      tenant_id: result.tenant_id,
      project_id: result.project_id,
    };
    storeAuth(result.access_token, state.session);
    hydrateSession();
    showApp();
    await loadAll();
    openRequestedPage();
    window.lucide.createIcons();
  } catch (error) {
    els.authStatus.textContent = error.message || "登录失败";
  } finally {
    els.loginButton.disabled = false;
  }
}

function logout() {
  state.token = "";
  state.session = null;
  clearStoredAuth();
  showLogin();
  window.lucide.createIcons();
}

async function loadAll() {
  if (state.loading) return;
  state.loading = true;
  els.refreshButton.disabled = true;
  try {
    await Promise.allSettled([loadHealth(), loadWorkspace(), loadKnowledgeAccess()]);
    renderAll();
    applyKnowledgePermissions();
    setServicePill(state.health?.status);
  } catch (error) {
    toast("加载失败", error.message, "error");
  } finally {
    state.loading = false;
    els.refreshButton.disabled = false;
    window.lucide.createIcons();
  }
}

async function refreshCurrent() {
  await loadAll();
  const activePage = document.querySelector(".page.active");
  if (activePage?.id?.startsWith("page-kb-")) {
    loadKnowledgePage(activePage.id.replace("page-", ""));
  }
}

async function loadHealth() {
  try {
    state.health = await api("/health");
  } catch {
    state.health = null;
  }
}

async function loadWorkspace() {
  state.tenants = [];
  state.projects = [];
  state.connections = [];
  state.pipelines = [];
  state.runs = [];

  const tenantPage = await api(`${API_BASE}/tenants?page=1&page_size=100`);
  state.tenants = tenantPage.items || [];
  if (!state.tenants.length) return;

  const tenant = state.tenants[0];
  state.session = { ...(state.session || {}), tenant_id: tenant.id };
  localStorage.setItem(SESSION_KEY, JSON.stringify(state.session));

  const projectPage = await api(`${API_BASE}/projects?tenant_id=${tenant.id}&page=1&page_size=100`);
  state.projects = projectPage.items || [];

  const projectRequests = state.projects.map(async (project) => {
    const projectId = project.id;
    const [connections, pipelines, runs] = await Promise.allSettled([
      api(`${API_BASE}/connections?project_id=${projectId}&page=1&page_size=500`),
      api(`${API_BASE}/pipelines?project_id=${projectId}&page=1&page_size=500`),
      api(`${API_BASE}/execution-runs?project_id=${projectId}&page=1&page_size=200`),
    ]);
    return {
      connections: connections.status === "fulfilled" ? connections.value.items || [] : [],
      pipelines: pipelines.status === "fulfilled" ? pipelines.value.items || [] : [],
      runs: runs.status === "fulfilled" ? runs.value.items || [] : [],
    };
  });

  const results = await Promise.all(projectRequests);
  results.forEach((result) => {
    state.connections.push(...result.connections);
    state.pipelines.push(...result.pipelines);
    state.runs.push(...result.runs);
  });
  state.runs.sort((a, b) => String(b.committed_at).localeCompare(String(a.committed_at)));
  if (!state.connectionProjectId) {
    const latestConnection = [...state.connections].sort((a, b) =>
      String(b.created_at).localeCompare(String(a.created_at)),
    )[0];
    state.connectionProjectId =
      latestConnection?.project_id || state.projects[0]?.id || null;
  }
}

function renderAll() {
  renderStats();
  renderFlow();
  renderRecentRuns();
  renderSystemSummary();
  renderProjectCards();
  renderConnections();
  renderPipelines();
  renderRuns();
  renderSystemDetails();
}

function renderStats() {
  setText("statTenants", state.tenants.length);
  setText("statProjects", state.projects.length);
  setText("statConnections", state.connections.length);
  setText("statPipelines", state.pipelines.length);
  setText("statRuns", state.runs.length);

  document.querySelectorAll("[data-badge]").forEach((badge) => {
    const key = badge.dataset.badge;
    badge.textContent = state[key]?.length ?? 0;
  });
}

function renderFlow() {
  const flow = [
    ["message-square-text", "需求解析"],
    ["scan-search", "元数据"],
    ["brain-cog", "规划生成"],
    ["file-code-2", "HOCON"],
    ["badge-check", "质量门禁"],
    ["user-check", "审批"],
    ["play-square", "执行"],
    ["rocket", "原子发布"],
  ];
  els.flowGrid.innerHTML = flow
    .map(
      ([icon, label]) => `
        <div class="flow-step">
          <div class="step-icon"><i data-lucide="${icon}"></i></div>
          <strong>${label}</strong>
        </div>`,
    )
    .join("");
}

function renderRecentRuns() {
  const runs = state.runs.slice(0, 6);
  if (!runs.length) {
    els.recentRuns.innerHTML = emptyState("play-square", "暂无执行", "提交已冻结版本后，会在这里显示运行状态。");
    return;
  }
  els.runSummary.textContent = `共 ${state.runs.length} 条`;
  els.recentRuns.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>项目</th>
          <th>状态</th>
          <th>开始时间</th>
        </tr>
      </thead>
      <tbody>
        ${runs
          .map(
            (run) => `
          <tr>
            <td>
              <div class="cell-title">
                <div class="cell-icon"><i data-lucide="boxes"></i></div>
                <div><strong>${esc(projectName(run.project_id))}</strong></div>
              </div>
            </td>
            <td>${runBadge(run.status)}</td>
            <td class="mono">${esc(formatDate(run.started_at || run.committed_at))}</td>
          </tr>`,
          )
          .join("")}
      </tbody>
    </table>`;
}

function renderSystemSummary() {
  const checks = state.health?.checks || {};
  els.systemSummary.innerHTML = [
    ["database", "PostgreSQL", checks.database || "未知"],
    ["zap", "Redis", checks.redis || "未知"],
    ["ship", "SeaTunnel", checks.seatunnel || "未知"],
  ]
    .map(
      ([icon, name, value]) => `
        <div class="system-row">
          <div class="system-icon"><i data-lucide="${icon}"></i></div>
          <div><strong>${name}</strong><small>${serviceStateLabel(value)}</small></div>
          <div class="spacer"></div>
          ${serviceBadge(value)}
        </div>`,
    )
    .join("");
}

function renderProjectCards() {
  const wrap = document.getElementById("projectCards");
  if (!state.projects.length) {
    wrap.innerHTML = `
      <div class="panel empty-state">
        <div>
          <div class="empty-icon"><i data-lucide="folder-open"></i></div>
          <strong>还没有项目</strong>
          <p>新建项目后即可配置数据连接和管道。</p>
        </div>
      </div>`;
    return;
  }

  wrap.innerHTML = state.projects
    .map((project) => {
      const connections = state.connections.filter((c) => c.project_id === project.id);
      const pipelines = state.pipelines.filter((p) => p.project_id === project.id);
      const runs = state.runs.filter((r) => r.project_id === project.id);
      return `
        <article class="project-card">
          <div class="project-card-head">
            <div class="cell-icon"><i data-lucide="boxes"></i></div>
            <strong>${esc(project.name)}</strong>
            ${statusBadge(project.status)}
            ${
              ["project_admin", "tenant_admin"].includes(state.session?.role)
                ? `<button class="button ghost project-edit-btn" data-project-id="${esc(project.id)}">
                    <i data-lucide="pencil"></i><span>编辑</span>
                  </button>`
                : ""
            }
            <button class="button ghost project-connections-btn" data-project-id="${esc(project.id)}">
              <i data-lucide="database"></i><span>查看连接</span>
            </button>
          </div>
          <div class="project-meta">
            <span><i data-lucide="database"></i>${connections.length} 连接</span>
            <span><i data-lucide="git-branch"></i>${pipelines.length} 管道</span>
            <span><i data-lucide="play-square"></i>${runs.length} 执行</span>
          </div>
          <div class="metric-row">
            <div class="metric"><strong>${connections.length}</strong><span>数据连接</span></div>
            <div class="metric"><strong>${pipelines.length}</strong><span>管道</span></div>
            <div class="metric"><strong>${runs.length}</strong><span>执行</span></div>
          </div>
        </article>`;
    })
    .join("");
  document.querySelectorAll(".project-edit-btn").forEach((button) => {
    button.addEventListener("click", () => editProject(button.dataset.projectId));
  });
  document.querySelectorAll(".project-connections-btn").forEach((button) => {
    button.addEventListener("click", () => {
      state.connectionProjectId = button.dataset.projectId;
      goToPage("connections");
    });
  });
}

function editProject(projectId) {
  const project = state.projects.find((item) => item.id === projectId);
  if (!project) return;
  state.editingProjectId = projectId;
  document.getElementById("projectFormTitle").textContent = "编辑项目";
  els.projectNameInput.value = project.name || "";
  els.projectDescInput.value = project.description || "";
  els.createProjectPanel.hidden = false;
  els.projectNameInput.focus();
}

function renderConnections() {
  const body = document.getElementById("connectionsBody");
  const filter = document.getElementById("connectionProjectFilter");
  if (filter) {
    filter.innerHTML = state.projects
      .map(
        (project) =>
          `<option value="${esc(project.id)}">${esc(project.name)}</option>`,
      )
      .join("");
    if (
      state.connectionProjectId &&
      state.projects.some((project) => project.id === state.connectionProjectId)
    ) {
      filter.value = state.connectionProjectId;
    } else {
      state.connectionProjectId = state.projects[0]?.id || null;
      if (state.connectionProjectId) filter.value = state.connectionProjectId;
    }
  }

  const connections = state.connectionProjectId
    ? state.connections.filter(
        (connection) => connection.project_id === state.connectionProjectId,
      )
    : [];

  if (!connections.length) {
    body.innerHTML = `<tr><td colspan="6">${emptyStateRow("database", "暂无数据连接")}</td></tr>`;
    return;
  }
  body.innerHTML = connections
    .map(
      (conn) => `
        <tr>
          <td>
            <div class="cell-title">
              <div class="cell-icon"><i data-lucide="database"></i></div>
              <div>
                <strong>${esc(conn.name)}</strong>
                <small>${esc(conn.database_name)} · ${esc(conn.username)}</small>
              </div>
            </div>
          </td>
          <td>${typeBadge(conn.source_type)}</td>
          <td class="mono">${esc(conn.host)}:${esc(conn.port)}</td>
          <td>${esc(projectName(conn.project_id))}</td>
          <td>${connectionStateBadge(conn.last_test_status, conn.last_test_message)}</td>
          <td>
            ${
              ["engineer", "project_admin", "tenant_admin"].includes(state.session?.role)
                ? `<button class="button ghost conn-test-btn" data-conn-id="${esc(conn.id)}" data-project-id="${esc(conn.project_id)}">
                    <i data-lucide="plug-zap"></i><span>测试</span>
                  </button>`
                : ""
            }
            ${
              ["project_admin", "tenant_admin"].includes(state.session?.role)
                ? `<button class="button ghost conn-edit-btn" data-conn-id="${esc(conn.id)}" data-project-id="${esc(conn.project_id)}">
                    <i data-lucide="pencil"></i><span>编辑</span>
                  </button>
                  <button class="button ghost conn-delete-btn" data-conn-id="${esc(conn.id)}" data-project-id="${esc(conn.project_id)}">
                    <i data-lucide="trash-2"></i><span>删除</span>
                  </button>`
                : ""
            }
          </td>
        </tr>`,
    )
    .join("");
  document.querySelectorAll(".conn-delete-btn").forEach((btn) => {
    btn.addEventListener("click", () => deleteConnection(btn.dataset.connId, btn.dataset.projectId));
  });
  document.querySelectorAll(".conn-edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => editConnection(btn.dataset.connId, btn.dataset.projectId));
  });
  document.querySelectorAll(".conn-test-btn").forEach((btn) => {
    btn.addEventListener("click", () => testConnection(btn.dataset.connId, btn.dataset.projectId));
  });
}

async function testConnection(connectionId, projectId) {
  try {
    const result = await api(
      `${API_BASE}/connections/${connectionId}/test?project_id=${projectId}`,
      { method: "POST" },
    );
    toast(
      result.success ? "连接成功" : "连接失败",
      result.message,
      result.success ? "success" : "error",
    );
    await loadAll();
  } catch (e) {
    toast("测试失败", e.message, "error");
  }
}

function populateConnectionProjects() {
  const select = document.getElementById("connProjectId");
  if (!select) return;
  select.innerHTML = state.projects
    .map((project) => `<option value="${esc(project.id)}">${esc(project.name)}</option>`)
    .join("");
}

async function createConnection() {
  const editingConnectionId = state.editingConnectionId;
  const projectId = document.getElementById("connProjectId").value;
  const name = document.getElementById("connNameInput").value.trim();
  const sourceType = document.getElementById("connTypeInput").value;
  const host = document.getElementById("connHostInput").value.trim();
  const port = Number(document.getElementById("connPortInput").value || 0);
  const databaseName = document.getElementById("connDatabaseInput").value.trim();
  const username = document.getElementById("connUserInput").value.trim();
  const password = document.getElementById("connPasswordInput").value;
  const description = document.getElementById("connDescInput").value.trim() || null;
  if (
    !projectId ||
    !name ||
    !host ||
    !port ||
    !databaseName ||
    !username ||
    (!editingConnectionId && !password)
  ) {
    toast("无法保存", "请填写完整连接信息", "error");
    return;
  }
  const btn = document.getElementById("saveConnectionButton");
  btn.disabled = true;
  try {
    const body = {
      name,
      source_type: sourceType,
      host,
      port,
      database_name: databaseName,
      username,
      description,
    };
    if (password) body.password = password;
    if (editingConnectionId) {
      await api(
        `${API_BASE}/projects/${projectId}/connections/${editingConnectionId}`,
        { method: "PATCH", body },
      );
    } else {
      await api(`${API_BASE}/connections`, {
        method: "POST",
        body: { ...body, project_id: projectId, password },
      });
    }
    state.editingConnectionId = null;
    document.getElementById("connectionFormPanel").hidden = true;
    clearConnectionForm();
    document.getElementById("connProjectId").disabled = false;
    toast(editingConnectionId ? "修改成功" : "保存成功", `连接“${name}”已保存`);
    await loadAll();
  } catch (e) {
    toast("保存失败", e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

function editConnection(connectionId, projectId) {
  const conn = state.connections.find((item) => item.id === connectionId);
  if (!conn) return;
  state.editingConnectionId = connectionId;
  populateConnectionProjects();
  document.getElementById("connProjectId").value = projectId;
  document.getElementById("connProjectId").disabled = true;
  document.getElementById("connectionFormTitle").textContent = "编辑数据连接";
  document.getElementById("connNameInput").value = conn.name || "";
  document.getElementById("connTypeInput").value = conn.source_type || "mysql";
  document.getElementById("connHostInput").value = conn.host || "";
  document.getElementById("connPortInput").value = conn.port || 3306;
  document.getElementById("connDatabaseInput").value = conn.database_name || "";
  document.getElementById("connUserInput").value = conn.username || "";
  document.getElementById("connPasswordInput").value = "";
  document.getElementById("connDescInput").value = conn.description || "";
  document.getElementById("connectionFormPanel").hidden = false;
}

function clearConnectionForm() {
  [
    "connNameInput",
    "connHostInput",
    "connDatabaseInput",
    "connUserInput",
    "connPasswordInput",
    "connDescInput",
  ].forEach((id) => {
    document.getElementById(id).value = "";
  });
  document.getElementById("connPortInput").value = "3306";
  document.getElementById("connTypeInput").value = "mysql";
}

async function deleteConnection(connectionId, projectId) {
  if (!window.confirm("确定删除这个数据连接吗？相关 Vault 凭据也会一起删除。")) return;
  try {
    await api(`${API_BASE}/projects/${projectId}/connections/${connectionId}`, { method: "DELETE" });
    toast("删除成功", "数据连接已删除");
    await loadAll();
  } catch (e) {
    toast("删除失败", e.message, "error");
  }
}

function renderPipelines() {
  const body = document.getElementById("pipelinesBody");
  if (!state.pipelines.length) {
    body.innerHTML = `<tr><td colspan="5">${emptyStateRow("git-branch", "暂无管道")}</td></tr>`;
    return;
  }
  body.innerHTML = state.pipelines
    .map(
      (pipeline) => `
        <tr>
          <td>
            <div class="cell-title">
              <div class="cell-icon"><i data-lucide="git-branch"></i></div>
              <div><strong>${esc(pipeline.name)}</strong></div>
            </div>
          </td>
          <td>${statusBadge(pipeline.status)}</td>
          <td>${esc(projectName(pipeline.project_id))}</td>
          <td class="mono">${esc(formatDate(pipeline.updated_at))}</td>
          <td>
            ${
              pipeline.status === "needs_clarification"
                ? `<button class="button ghost pipeline-clarify-btn" data-id="${esc(pipeline.id)}">
                    <i data-lucide="message-circle-question"></i><span>澄清</span>
                  </button>`
                : ""
            }
            ${
              ["draft", "needs_clarification", "error"].includes(pipeline.status)
                ? `<button class="button ghost pipeline-delete-btn" data-id="${esc(pipeline.id)}">
                    <i data-lucide="trash-2"></i><span>删除</span>
                  </button>`
                : '<span class="badge">已保护</span>'
            }
          </td>
        </tr>`,
    )
    .join("");
  document.querySelectorAll(".pipeline-clarify-btn").forEach((button) => {
    button.addEventListener("click", () => openPipelineClarify(button.dataset.id));
  });
  document.querySelectorAll(".pipeline-delete-btn").forEach((button) => {
    button.addEventListener("click", () => deletePipeline(button.dataset.id));
  });
}

async function openPipelineClarify(pipelineId) {
  state.clarifyingPipelineId = pipelineId;
  try {
    const detail = await api(`${API_BASE}/pipelines/${pipelineId}`);
    const requirement = detail.source_requirement || {};
    const plan = detail.etl_plan || {};
    const source = plan.source || {};
    const target = plan.target || {};
    const sync = plan.sync_strategy || {};
    const projectConnections = state.connections.filter(
      (connection) => connection.project_id === detail.project_id,
    );
    const connectionOptions = projectConnections
      .map(
        (connection) =>
          `<option value="${esc(connection.id)}">${esc(connection.name)}</option>`,
      )
      .join("");
    document.getElementById("clarifySourceConnection").innerHTML = connectionOptions;
    document.getElementById("clarifyTargetConnection").innerHTML = connectionOptions;
    document.getElementById("clarifySourceConnection").value =
      detail.source_connection_id || "";
    document.getElementById("clarifyTargetConnection").value =
      detail.target_connection_id || "";
    document.getElementById("clarifySourceSchema").value =
      requirement.source_schema || source.schema_name || "";
    document.getElementById("clarifySourceTable").value =
      requirement.source_table || source.table_name || "";
    document.getElementById("clarifyTargetSchema").value =
      requirement.target_schema || target.schema_name || "";
    document.getElementById("clarifyTargetTable").value =
      requirement.target_table || target.table_name || "";
    document.getElementById("clarifySyncMode").value =
      requirement.sync_mode || sync.mode || "full";
    document.getElementById("clarifyPrimaryKeys").value =
      (requirement.primary_keys || sync.primary_keys || []).join(", ");
    document.getElementById("clarifyWatermark").value =
      requirement.watermark_column || sync.watermark_column || "";
    document.getElementById("pipelineClarifyPanel").hidden = false;
  } catch (e) {
    toast("无法打开澄清", e.message, "error");
  }
}

async function submitPipelineClarification() {
  const pipelineId = state.clarifyingPipelineId;
  if (!pipelineId) return;
  const values = {
    source_connection_id: document.getElementById("clarifySourceConnection").value,
    target_connection_id: document.getElementById("clarifyTargetConnection").value,
    source_schema: document.getElementById("clarifySourceSchema").value.trim(),
    source_table: document.getElementById("clarifySourceTable").value.trim(),
    target_schema: document.getElementById("clarifyTargetSchema").value.trim(),
    target_table: document.getElementById("clarifyTargetTable").value.trim(),
    sync_mode: document.getElementById("clarifySyncMode").value,
    primary_keys: document.getElementById("clarifyPrimaryKeys").value.trim(),
    watermark_column: document.getElementById("clarifyWatermark").value.trim(),
  };
  const answers = Object.fromEntries(
    Object.entries(values).filter(([, value]) => value !== ""),
  );
  const button = document.getElementById("saveClarifyPipeline");
  button.disabled = true;
  try {
    const result = await api(`${API_BASE}/pipelines/${pipelineId}/clarify`, {
      method: "POST",
      body: { answers },
    });
    document.getElementById("pipelineClarifyPanel").hidden = true;
    state.clarifyingPipelineId = null;
    toast(
      result.status === "needs_clarification" ? "还需要补充信息" : "已完成生成",
      result.status === "needs_clarification"
        ? "请查看最新提示，继续补充缺少的内容"
        : "管道已继续生成",
    );
    await loadAll();
  } catch (e) {
    toast("澄清失败", e.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function deletePipeline(pipelineId) {
  if (!window.confirm("确定删除这个未冻结的管道吗？")) return;
  try {
    await api(`${API_BASE}/pipelines/${pipelineId}`, { method: "DELETE" });
    toast("删除成功", "管道已删除");
    await loadAll();
  } catch (e) {
    toast("删除失败", e.message, "error");
  }
}

function renderRuns() {
  const body = document.getElementById("runsBody");
  if (!state.runs.length) {
    body.innerHTML = `<tr><td colspan="6">${emptyStateRow("play-square", "暂无执行记录")}</td></tr>`;
    return;
  }
  body.innerHTML = state.runs
    .slice(0, 100)
    .map(
      (run) => `
        <tr>
          <td class="mono" title="${esc(run.id)}">${esc(shortId(run.id))}</td>
          <td>${runBadge(run.status)}</td>
          <td>${esc(projectName(run.project_id))}</td>
          <td class="mono">${esc(formatDate(run.started_at || run.committed_at))}</td>
          <td class="mono">${esc(formatDate(run.finished_at))}</td>
          <td class="mono">${run.seatunnel_job_id ? run.seatunnel_job_id : "—"}</td>
        </tr>`,
    )
    .join("");
}

function renderSystemDetails() {
  const health = state.health;
  const checks = health?.checks || {};
  els.systemDetails.innerHTML = [
    ["database", "PostgreSQL", checks.database || "未知", "控制面元数据、审批与证据账本"],
    ["zap", "Redis", checks.redis || "未知", "消息与任务队列"],
    ["ship", "SeaTunnel", checks.seatunnel || "未知", "Zeta 引擎执行节点"],
    ["shield-check", "凭据保险库", "ok", "数据库凭据安全存取"],
    ["box", "事务投递", "ok", "事务性可靠投递"],
    ["scroll-text", "证据账本", "ok", "哈希链证据审计"],
  ]
    .map(
      ([icon, name, value, note]) => `
        <div class="system-row">
          <div class="system-icon"><i data-lucide="${icon}"></i></div>
          <div><strong>${name}</strong><small>${note}</small></div>
          <div class="spacer"></div>
          ${serviceBadge(value)}
        </div>`,
    )
    .join("");

  setText("dbState", serviceStateLabel(checks.database));
  setText("redisState", serviceStateLabel(checks.redis));
  setText("seatunnelState", serviceStateLabel(checks.seatunnel));
  setText("versionLabel", health ? `v${health.version}` : "未知");
  setText("envLabel", health?.env || "dev");
}

async function createProject() {
  const editingProjectId = state.editingProjectId;
  if (
    !["project_admin", "tenant_admin"].includes(state.session?.role) ||
    (!editingProjectId && state.session?.role !== "tenant_admin")
  ) {
    toast("无法操作项目", "当前账号没有新建或编辑项目的权限", "error");
    return;
  }
  const tenantId = state.tenants[0]?.id;
  if (!tenantId) {
    toast("无法创建", "当前账号没有可用租户", "error");
    return;
  }
  const name = els.projectNameInput.value.trim();
  const description = els.projectDescInput.value.trim();
  if (!name) {
    toast("无法创建", "请填写项目名称", "error");
    return;
  }
  els.saveProjectButton.disabled = true;
  try {
    if (editingProjectId) {
      await api(`${API_BASE}/projects/${editingProjectId}`, {
        method: "PATCH",
        body: { name, description: description || null },
      });
    } else {
      await api(`${API_BASE}/projects`, {
        method: "POST",
        body: { tenant_id: tenantId, name, description: description || null },
      });
    }
    state.editingProjectId = null;
    els.projectNameInput.value = "";
    els.projectDescInput.value = "";
    els.createProjectPanel.hidden = true;
    toast(editingProjectId ? "修改成功" : "创建成功", `项目“${name}”已保存`);
    await loadAll();
  } catch (error) {
    toast("创建失败", error.message, "error");
  } finally {
    els.saveProjectButton.disabled = false;
  }
}

function projectName(projectId) {
  return state.projects.find((p) => p.id === projectId)?.name || shortId(projectId);
}

function runBadge(status) {
  const map = {
    pending: ["warn", "等待提交"],
    running: ["info", "运行中"],
    quality_checking: ["violet", "质量检查"],
    publishing: ["violet", "发布中"],
    succeeded: ["good", "成功"],
    failed: ["bad", "失败"],
    rolled_back: ["warn", "已回滚"],
  };
  const [tone, label] = map[status] || ["", status || "未知"];
  return `<span class="badge ${tone}"><span class="dot"></span>${label}</span>`;
}

function connectionStateBadge(status, message) {
  const map = {
    ok: ["good", "连接正常"],
    failed: ["bad", "连接失败"],
    testing: ["info", "测试中"],
    untested: ["warn", "未测试"],
  };
  const [tone, label] = map[status] || ["warn", status || "未知"];
  return `<span class="badge ${tone}" title="${esc(message || "")}">${label}</span>`;
}

function typeBadge(type) {
  const label = type?.toUpperCase() || "DB";
  const tone = { postgresql: "info", mysql: "violet", doris: "good", clickhouse: "warn" }[type] || "";
  return `<span class="badge ${tone}">${label}</span>`;
}

function statusBadge(status) {
  const labels = {
    active: ["good", "活跃"],
    draft: ["warn", "草稿"],
    generating: ["info", "生成中"],
    validated: ["good", "已校验"],
    frozen: ["violet", "已冻结"],
    needs_clarification: ["warn", "待澄清"],
    error: ["bad", "异常"],
  };
  const [tone, label] = labels[status] || ["", status || "未知"];
  return `<span class="badge ${tone}">${label}</span>`;
}

function serviceStateLabel(value) {
  if (value === "ok") return "运行正常";
  if (value === "error") return "存在异常";
  return "状态未知";
}

function serviceBadge(value) {
  if (value === "ok") return '<span class="badge good"><span class="dot"></span>正常</span>';
  if (value === "error") return '<span class="badge bad"><span class="dot"></span>异常</span>';
  return '<span class="badge warn"><span class="dot"></span>未知</span>';
}

function emptyState(icon, title, message) {
  return `
    <div class="empty-state">
      <div>
        <div class="empty-icon"><i data-lucide="${icon}"></i></div>
        <strong>${title}</strong>
        <p>${message}</p>
      </div>
    </div>`;
}

function emptyStateRow(icon, title) {
  return `<div class="empty-state"><div><div class="empty-icon"><i data-lucide="${icon}"></i></div><strong>${title}</strong></div></div>`;
}

function shortId(value) {
  if (!value) return "—";
  const text = String(value);
  return text.length > 14 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setText(id, value) {
  document.getElementById(id).textContent = value;
}

function toast(title, message, type = "success") {
  const icons = { success: "check-circle-2", error: "circle-alert" };
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.innerHTML = `
    <i data-lucide="${icons[type]}"></i>
    <div>
      <strong>${esc(title)}</strong>
      <p>${esc(message)}</p>
    </div>`;
  els.toastRegion.appendChild(node);
  window.lucide.createIcons();
  setTimeout(() => node.remove(), 3600);
}

/* ===================== Knowledge Base ===================== */

function kbApi(path, options = {}) {
  return api(`${API_BASE}/knowledge${path}`, options);
}

function loadKnowledgePage(page) {
  if (page === "kb-docs") kbLoadDocs();
  else if (page === "kb-ask") { /* lazy init only */ }
  else if (page === "kb-faqs") kbLoadFaqs();
  else if (page === "kb-gaps") kbLoadGaps();
  else if (page === "kb-metrics") kbLoadMetrics();
  else if (page === "kb-admin") kbLoadAdminPage();
}

function kbCan(permission) {
  return Boolean(state.kb.access?.permissions?.includes(permission));
}

function applyKnowledgePermissions() {
  const access = state.kb.access;
  if (!access) return;
  const setVisible = (id, visible) => {
    const element = document.getElementById(id);
    if (element) element.hidden = !visible;
  };
  // Nav entries follow the same permission set, so a viewer no longer sees
  // FAQ / gap / admin entry points that would only return 403.
  const setNavVisible = (page, visible) => {
    const item = document.querySelector(`.nav-item[data-page="${page}"]`);
    if (!item) return;
    item.hidden = !visible;
    const section = document.getElementById(`page-${page}`);
    if (!visible && section?.classList.contains("active")) {
      goToPage("overview");
    }
  };
  setNavVisible("kb-docs", kbCan("kb:document:read"));
  setNavVisible("kb-ask", kbCan("kb:ask"));
  setNavVisible("kb-faqs", kbCan("kb:faq:publish") || kbCan("kb:faq:cluster"));
  setNavVisible("kb-gaps", kbCan("kb:gap:resolve"));
  setNavVisible("kb-metrics", kbCan("kb:metrics:read"));
  setNavVisible("kb-admin", kbCan("dept:manage") || kbCan("perm:manage"));
  setVisible("kbLoadDemo", kbCan("kb:document:create"));
  setVisible("kbImportDoc", kbCan("kb:document:create"));
  setVisible("kbBatchImport", kbCan("kb:document:batch"));
  setVisible("kbAskSubmit", kbCan("kb:ask"));
  setVisible("kbClusterFaqs", kbCan("kb:faq:cluster"));
}

async function loadKnowledgeAccess() {
  try {
    state.kb.access = await kbApi("/access");
  } catch {
    state.kb.access = null;
  }
}

async function kbLoadDocs() {
  const body = document.getElementById("kbDocsBody");
  const summary = document.getElementById("kbDocsSummary");
  body.innerHTML = `<tr><td colspan="5"><div class="loading"><i data-lucide="loader-circle"></i>加载中...</div></td></tr>`;
  try {
    const res = await kbApi("/documents?page=1&page_size=100");
    state.kb.docs = res.items || [];
    summary.textContent = `共 ${state.kb.docs.length} 篇`;
    renderKbDocs();
  } catch (e) {
    summary.textContent = "加载失败";
    body.innerHTML = `<tr><td colspan="5"><div class="empty-state"><div><div class="empty-icon"><i data-lucide="file-question"></i></div><strong>加载失败</strong><p>${esc(e.message)}</p></div></div></td></tr>`;
  }
  window.lucide.createIcons();
}

function renderKbDocs() {
  const body = document.getElementById("kbDocsBody");
  if (!state.kb.docs.length) {
    body.innerHTML = `<tr><td colspan="5"><div class="empty-state"><div><div class="empty-icon"><i data-lucide="book-open"></i></div><strong>暂无文档</strong><p>点击"导入文档"添加知识库内容</p></div></div></td></tr>`;
    window.lucide.createIcons();
    return;
  }
  body.innerHTML = state.kb.docs.map((doc) => `
    <tr>
      <td>
        <div class="cell-title">
          <div class="cell-icon"><i data-lucide="file-text"></i></div>
          <div><strong>${esc(doc.title)}</strong><small>${esc(doc.category || "通用")}</small></div>
        </div>
      </td>
      <td>${kbDocStatusBadge(doc.status)}</td>
      <td>${doc.chunk_count ?? "—"}</td>
      <td>${esc(doc.category || "—")}</td>
      <td>
        ${kbCan("kb:chunk:update") ? `<button class="button ghost kb-chunks-btn" data-doc-id="${esc(doc.id)}">
          <i data-lucide="layers"></i><span>分块编辑</span>
        </button>` : ""}
        ${kbCan("kb:document:publish") ? `<button class="button ghost kb-publish-btn" data-doc-id="${esc(doc.id)}" ${doc.status === "published" ? "disabled" : ""}>
          <i data-lucide="send"></i><span>${doc.status === "published" ? "已发布" : "发布"}</span>
        </button>` : ""}
        ${kbCan("kb:document:delete") ? `<button class="button ghost kb-delete-doc-btn" data-doc-id="${esc(doc.id)}">
          <i data-lucide="trash-2"></i><span>删除</span>
        </button>` : ""}
      </td>
    </tr>`).join("");
  // Row handlers are delegated on #kbDocsBody (see bindActions), so
  // re-rendering this table can never leave a button without a listener.
  window.lucide.createIcons();
}

function kbDocStatusBadge(status) {
  const map = {
    draft: ["warn", "草稿"],
    chunking: ["info", "分块中"],
    published: ["good", "已发布"],
    archived: ["", "已归档"],
  };
  const [tone, label] = map[status] || ["", status || "未知"];
  return `<span class="badge ${tone}"><span class="dot"></span>${label}</span>`;
}

async function kbImportDocument() {
  const title = document.getElementById("kbDocTitle").value.trim();
  const content = document.getElementById("kbDocContent").value.trim();
  const category = document.getElementById("kbDocChunkSize").value.trim();
  const permissions = kbPermissionsFromFields(
    "kbDocVisibility",
    "kbDocDepartment",
    "kbDocRoles",
    "kbDocUsers",
  );
  if (!title || !content) {
    toast("导入失败", "请填写标题和内容", "error");
    return;
  }
  const btn = document.getElementById("kbSubmitDoc");
  btn.disabled = true;
  try {
    const body = { title, content, permissions };
    if (category) body.category = category;
    await kbApi("/documents", { method: "POST", body });
    toast("导入成功", `文档"${title}"已导入并自动分块`);
    document.getElementById("kbDocTitle").value = "";
    document.getElementById("kbDocContent").value = "";
    document.getElementById("kbDocDepartment").value = "";
    document.getElementById("kbDocRoles").value = "";
    document.getElementById("kbDocUsers").value = "";
    document.getElementById("kbDocVisibility").value = "public";
    document.getElementById("kbImportPanel").hidden = true;
    await kbLoadDocs();
  } catch (e) {
    toast("导入失败", e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

function kbPermissionValues(value) {
  return value
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function kbPermissionsFromFields(
  visibilityId,
  departmentsId,
  rolesId,
  usersId,
  editor = null,
) {
  const root = editor || document;
  const visibility = editor
    ? root.querySelector(".kb-chunk-visibility")?.value || "public"
    : document.getElementById(visibilityId)?.value || "public";
  const departments = kbPermissionValues(
    editor
      ? root.querySelector(".kb-chunk-departments")?.value || ""
      : document.getElementById(departmentsId)?.value || "",
  );
  const roles = kbPermissionValues(
    editor
      ? root.querySelector(".kb-chunk-roles")?.value || ""
      : document.getElementById(rolesId)?.value || "",
  );
  const users = kbPermissionValues(
    editor
      ? root.querySelector(".kb-chunk-users")?.value || ""
      : document.getElementById(usersId)?.value || "",
  );
  return {
    visibility: departments.length || roles.length || users.length
      ? "restricted"
      : visibility,
    departments,
    roles,
    users,
  };
}

function kbPermissionText(permissions) {
  const rule = permissions || {};
  if ((rule.visibility || "public") === "public") return "全局公开";
  if ((rule.visibility || "") === "tenant") return "租户内可用";
  const parts = [];
  if (rule.departments?.length) parts.push(`部门：${rule.departments.join("、")}`);
  if (rule.roles?.length) parts.push(`角色：${rule.roles.join("、")}`);
  if (rule.users?.length) parts.push(`个人：${rule.users.length} 人`);
  return parts.join("；") || "受限";
}

async function kbImportBatch() {
  const button = document.getElementById("kbSubmitBatch");
  try {
    const documents = parseKbBatchDocuments(
      document.getElementById("kbBatchContent").value,
    );
    if (!documents.length) {
      throw new Error("没有识别到可导入的文档");
    }
    button.disabled = true;
    const result = await kbApi("/documents/batch", {
      method: "POST",
      body: { documents },
    });
    toast("批量导入成功", `已导入 ${result.created} 篇，共 ${result.chunk_count} 个分块`);
    document.getElementById("kbBatchContent").value = "";
    document.getElementById("kbBatchPanel").hidden = true;
    await kbLoadDocs();
  } catch (error) {
    toast("批量导入失败", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function parseKbBatchDocuments(raw) {
  const blocks = raw
    .split(/^\s*-{3,}\s*$/m)
    .map((block) => block.trim())
    .filter(Boolean);
  return blocks.map((block) => {
    const lines = block.split(/\r?\n/);
    const title = (lines.shift() || "").trim();
    const metadata = {
      category: "",
      visibility: "public",
      departments: [],
      roles: [],
      users: [],
    };
    const bodyLines = [];
    for (const line of lines) {
      const match = line.match(/^(分类|访问范围|部门|角色|用户)\s*[:：]\s*(.*)$/);
      if (!match || bodyLines.length) {
        bodyLines.push(line);
        continue;
      }
      const [, key, value] = match;
      if (key === "分类") metadata.category = value.trim();
      else if (key === "访问范围") {
        metadata.visibility = value.trim() === "全局公开"
          ? "public"
          : (value.trim() || "public");
      } else if (key === "部门") metadata.departments = kbPermissionValues(value);
      else if (key === "角色") metadata.roles = kbPermissionValues(value);
      else if (key === "用户") metadata.users = kbPermissionValues(value);
    }
    return {
      title,
      content: bodyLines.join("\n").trim(),
      category: metadata.category || undefined,
      permissions: {
        visibility: metadata.departments.length || metadata.roles.length || metadata.users.length
          ? "restricted"
          : metadata.visibility,
        departments: metadata.departments,
        roles: metadata.roles,
        users: metadata.users,
      },
    };
  }).filter((item) => item.title && item.content);
}

function closeKbChunksPanel() {
  const panel = document.getElementById("kbChunksPanel");
  const body = document.getElementById("kbChunksBody");
  const title = document.getElementById("kbChunksTitle");
  // Invalidate any in-flight request so a late response cannot reopen it.
  state.kb.chunkSeq += 1;
  state.kb.chunkDocId = null;
  if (panel) panel.hidden = true;
  if (body) body.innerHTML = "";
  if (title) title.textContent = "文档分块";
}

function flashKbChunksPanel(panel) {
  panel.classList.remove("kb-panel-flash");
  void panel.offsetWidth; // restart the animation
  panel.classList.add("kb-panel-flash");
  window.setTimeout(() => panel.classList.remove("kb-panel-flash"), 1200);
}

function kbChunkPanelInViewport(panel) {
  const rect = panel.getBoundingClientRect();
  return rect.top >= 0 && rect.top < window.innerHeight - 160;
}

async function kbLoadDocumentChunks(docId) {
  const panel = document.getElementById("kbChunksPanel");
  const body = document.getElementById("kbChunksBody");
  const title = document.getElementById("kbChunksTitle");
  const doc = state.kb.docs.find((item) => item.id === docId);

  // A later click always wins: an older response must not overwrite it.
  state.kb.chunkSeq += 1;
  const seq = state.kb.chunkSeq;
  state.kb.chunkDocId = docId;

  panel.hidden = false;
  // Show the target document immediately so switching is visible even
  // before the request finishes.
  title.textContent = doc ? `${doc.title} · 分块编辑` : "文档分块";
  body.innerHTML = `<div class="loading"><i data-lucide="loader-circle"></i>加载中...</div>`;
  flashKbChunksPanel(panel);
  if (!kbChunkPanelInViewport(panel)) {
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  try {
    const detail = await kbApi(`/documents/${docId}`);
    if (seq !== state.kb.chunkSeq) return;
    title.textContent = `${detail.title} · 分块编辑`;
    renderKbChunks(detail);
  } catch (error) {
    if (seq !== state.kb.chunkSeq) return;
    body.innerHTML = `<p class="kb-error-text">${esc(error.message)}</p>`;
  }
  window.lucide.createIcons();
}

const kbPendingKeys = new Set();

// One in-flight request per object. Without this, every extra click fires
// another PUT/DELETE; the API is an idempotent overwrite, so N clicks produce
// N "保存成功" toasts while the button never appears to be busy.
function kbBeginPending(key, button, busyText) {
  if (kbPendingKeys.has(key)) return null;
  kbPendingKeys.add(key);
  if (!button) return { done: () => kbPendingKeys.delete(key) };
  const originalHtml = button.innerHTML;
  const wasDisabled = button.disabled;
  button.disabled = true;
  button.textContent = busyText;
  return {
    done: () => {
      kbPendingKeys.delete(key);
      if (!button.isConnected) return;
      button.disabled = wasDisabled;
      button.innerHTML = originalHtml;
    },
  };
}

function kbChunkSnapshot(editor) {
  const field = (selector) => (editor.querySelector(selector)?.value || "").trim();
  return JSON.stringify({
    title: field(".kb-chunk-title"),
    content: field(".kb-chunk-content"),
    visibility: editor.querySelector(".kb-chunk-visibility")?.value || "",
    departments: field(".kb-chunk-departments"),
    roles: field(".kb-chunk-roles"),
    users: field(".kb-chunk-users"),
  });
}

function renderKbChunks(doc) {
  const body = document.getElementById("kbChunksBody");
  if (!doc.chunks?.length) {
    body.innerHTML = `<p class="kb-muted-text">该文档没有可编辑分块。</p>`;
    return;
  }
  body.innerHTML = doc.chunks.map((chunk) => {
    const rule = chunk.permissions || {};
    return `
      <div class="kb-chunk-editor" data-chunk-id="${esc(chunk.id)}">
        <div class="kb-chunk-editor-head">
          <strong>分块 ${Number(chunk.chunk_index) + 1}</strong>
          <span>${esc(kbPermissionText(rule))}</span>
        </div>
        <input class="kb-chunk-title" value="${esc(chunk.title || "")}" placeholder="分块标题" />
        <textarea class="kb-chunk-content" rows="5">${esc(chunk.content || "")}</textarea>
        <div class="kb-permission-row">
          <select class="kb-chunk-visibility">
            <option value="public" ${rule.visibility === "public" ? "selected" : ""}>全局公开</option>
            <option value="tenant" ${rule.visibility === "tenant" ? "selected" : ""}>租户内可用</option>
            <option value="restricted" ${rule.visibility === "restricted" ? "selected" : ""}>受限</option>
          </select>
          <input class="kb-chunk-departments" value="${esc((rule.departments || []).join(","))}" placeholder="部门，逗号分隔" />
          <input class="kb-chunk-roles" value="${esc((rule.roles || []).join(","))}" placeholder="角色，逗号分隔" />
          <input class="kb-chunk-users" value="${esc((rule.users || []).join(","))}" placeholder="用户 UUID，逗号分隔" />
        </div>
        <div class="page-actions">
          <button class="button secondary kb-save-chunk">保存</button>
          <button class="button ghost kb-delete-chunk">
            <i data-lucide="trash-2"></i><span>删除分块</span>
          </button>
        </div>
      </div>`;
  }).join("");
  // Remember the values as rendered, so "保存" can skip a no-op write.
  body.querySelectorAll(".kb-chunk-editor").forEach((editor) => {
    editor.dataset.snapshot = kbChunkSnapshot(editor);
  });
  body.querySelectorAll(".kb-save-chunk").forEach((button) => {
    button.addEventListener("click", () => {
      const editor = button.closest(".kb-chunk-editor");
      kbSaveChunk(editor.dataset.chunkId, editor, button);
    });
  });
  body.querySelectorAll(".kb-delete-chunk").forEach((button) => {
    button.addEventListener("click", () => {
      const editor = button.closest(".kb-chunk-editor");
      kbDeleteChunk(editor.dataset.chunkId, button);
    });
  });
}

async function kbSaveChunk(chunkId, editor, button) {
  const pending = kbBeginPending(`chunk:${chunkId}`, button, "保存中...");
  if (!pending) return;
  try {
    const payload = {
      title: editor.querySelector(".kb-chunk-title").value.trim(),
      content: editor.querySelector(".kb-chunk-content").value.trim(),
      permissions: kbPermissionsFromFields(
        null,
        null,
        null,
        null,
        editor,
      ),
    };
    if (!payload.title || !payload.content) {
      toast("保存失败", "分块标题和内容不能为空", "error");
      return;
    }
    if (kbChunkSnapshot(editor) === editor.dataset.snapshot) {
      // Nothing changed: skip the write. Throttle the notice so a triple click
      // does not stack three identical toasts.
      const now = Date.now();
      if (now - Number(editor.dataset.noopAt || 0) > 800) {
        editor.dataset.noopAt = String(now);
        toast("没有改动", "分块内容与权限未变化，无需保存");
      }
      return;
    }
    await kbApi(`/chunks/${chunkId}`, { method: "PUT", body: payload });
    toast("保存成功", `分块内容与权限已更新 · UI ${APP_BUILD}`);
    closeKbChunksPanel();
    await kbLoadDocs();
  } catch (error) {
    toast("保存失败", error.message, "error");
  } finally {
    pending.done();
  }
}

async function kbDeleteChunk(chunkId, button) {
  if (!window.confirm("确定删除这个知识分块吗？")) return;
  const pending = kbBeginPending(`chunk:${chunkId}`, button, "删除中...");
  if (!pending) return;
  try {
    await kbApi(`/chunks/${chunkId}`, { method: "DELETE" });
    toast("删除成功", "知识分块已删除");
    closeKbChunksPanel();
    await kbLoadDocs();
  } catch (error) {
    toast("删除失败", error.message, "error");
  } finally {
    pending.done();
  }
}

const KB_PERMISSION_LABELS = {
  "kb:document:create": "创建文档",
  "kb:document:read": "查看文档",
  "kb:document:delete": "删除文档",
  "kb:document:publish": "发布文档",
  "kb:document:archive": "归档文档",
  "kb:document:batch": "批量导入",
  "kb:chunk:update": "编辑分块",
  "kb:chunk:delete": "删除分块",
  "kb:ask": "知识问答",
  "kb:faq:cluster": "生成 FAQ 候选",
  "kb:faq:publish": "发布 FAQ",
  "kb:faq:reject": "拒绝 FAQ",
  "kb:faq:delete": "删除 FAQ",
  "kb:gap:resolve": "处理知识缺口",
  "kb:gap:delete": "删除知识缺口",
  "kb:audit:read": "查看审计",
  "kb:audit:delete": "删除审计",
  "kb:metrics:read": "查看运营看板",
  "kb:member:read": "查看知识成员",
  "kb:member:assign": "分配知识成员",
  "dept:manage": "管理部门树",
  "perm:manage": "管理角色权限",
};

const KB_ROLE_LABELS = {
  tenant_admin: "租户管理员",
  system_admin: "系统管理员",
  knowledge_admin: "知识管理员",
  viewer: "普通用户 / 提问者",
  project_admin: "项目管理员",
  engineer: "数据工程师",
  auditor: "审计人员",
};

function flattenKbDepartments(nodes, result = []) {
  (nodes || []).forEach((node) => {
    result.push(node);
    flattenKbDepartments(node.children || [], result);
  });
  return result;
}

async function kbLoadAdminPage() {
  const tenantId = state.session?.tenant_id;
  const departmentSummary = document.getElementById("kbDepartmentsSummary");
  departmentSummary.textContent = "加载中...";
  const requests = [
    kbApi("/departments/tree").catch(() => []),
    kbApi("/role-permissions").catch(() => []),
    api(`${API_BASE}/permissions/codes`).catch(() => ({ codes: [] })),
    kbApi("/members").catch(() => []),
  ];
  if (tenantId) {
    requests.push(
      loadAllTenantMembers(tenantId).catch(() => []),
    );
  } else {
    requests.push(Promise.resolve([]));
  }
  const [departments, rolePermissions, codeResult, members, tenantMembers] =
    await Promise.all(requests);
  state.kb.departments = departments || [];
  state.kb.rolePermissions = rolePermissions || [];
  state.kb.permissionCodes = codeResult?.codes || [];
  state.kb.members = members || [];
  state.kb.tenantMembers = (tenantMembers || []).filter(
    (member) => !/^(ctuser|isouser)-/i.test(member.username || ""),
  );
  renderKbDepartments();
  renderKbMembers();
  renderKbRolePermissions();
  window.lucide.createIcons();
}

async function loadAllTenantMembers(tenantId) {
  const members = [];
  let page = 1;
  const pageSize = 500;
  while (true) {
    const result = await api(
      `${API_BASE}/tenants/${tenantId}/members?page=${page}&page_size=${pageSize}`,
    );
    members.push(...(result.items || []));
    if (members.length >= (result.total || 0) || !(result.items || []).length) {
      break;
    }
    page += 1;
  }
  return members;
}

function renderKbDepartments() {
  const tree = document.getElementById("kbDepartmentTree");
  const summary = document.getElementById("kbDepartmentsSummary");
  const flat = flattenKbDepartments(state.kb.departments);
  summary.textContent = `共 ${flat.length} 个部门`;
  if (!flat.length) {
    tree.innerHTML = `<div class="empty-state"><div><strong>暂无部门</strong><p>先创建一级部门，再维护下级组织。</p></div></div>`;
    populateKbDepartmentSelects();
    return;
  }
  const byParent = new Map();
  flat.forEach((department) => {
    const key = department.parent_id || "root";
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(department);
  });
  const renderLevel = (parentId, depth) => (byParent.get(parentId || "root") || [])
    .map((department) => `
      <div class="kb-tree-node" style="--depth:${depth}">
        <div>
          <i data-lucide="building-2"></i>
          <strong>${esc(department.name)}</strong>
          <span>${esc(department.code)}</span>
        </div>
        <button class="button ghost kb-department-edit" data-id="${esc(department.id)}">编辑</button>
        <button class="button ghost kb-department-delete" data-id="${esc(department.id)}">删除</button>
      </div>
      ${renderLevel(department.id, depth + 1)}
    `).join("");
  tree.innerHTML = renderLevel(null, 0);
  document.querySelectorAll(".kb-department-edit").forEach((button) => {
    button.addEventListener("click", () => kbEditDepartment(button.dataset.id));
  });
  document.querySelectorAll(".kb-department-delete").forEach((button) => {
    button.addEventListener("click", () => kbDeleteDepartment(button.dataset.id));
  });
  populateKbDepartmentSelects();
}

function populateKbDepartmentSelects() {
  const flat = flattenKbDepartments(state.kb.departments);
  const parent = document.getElementById("kbDepartmentParent");
  const memberDepartment = document.getElementById("kbMemberDepartment");
  if (parent) {
    parent.innerHTML = `<option value="">无上级部门</option>${flat
      .map((department) => `<option value="${esc(department.id)}">${esc(department.name)}</option>`)
      .join("")}`;
  }
  if (memberDepartment) {
    memberDepartment.innerHTML = `<option value="">不指定部门</option>${flat
      .map((department) => `<option value="${esc(department.name)}">${esc(department.name)}</option>`)
      .join("")}`;
  }
}

function kbEditDepartment(departmentId) {
  const department = flattenKbDepartments(state.kb.departments)
    .find((item) => item.id === departmentId);
  if (!department) return;
  state.kb.editingDepartmentId = departmentId;
  document.getElementById("kbDepartmentName").value = department.name || "";
  document.getElementById("kbDepartmentCode").value = department.code || "";
  document.getElementById("kbDepartmentParent").value = department.parent_id || "";
  document.getElementById("kbDepartmentSort").value = department.sort_order || 0;
  document.getElementById("kbDepartmentForm").hidden = false;
}

async function kbSaveDepartment() {
  const name = document.getElementById("kbDepartmentName").value.trim();
  const code = document.getElementById("kbDepartmentCode").value.trim();
  const parentId = document.getElementById("kbDepartmentParent").value || null;
  const sortOrder = Number(document.getElementById("kbDepartmentSort").value || 0);
  if (!name || !code) {
    toast("保存失败", "部门名称和编码不能为空", "error");
    return;
  }
  const payload = { name, code, parent_id: parentId, sort_order: sortOrder };
  try {
    if (state.kb.editingDepartmentId) {
      await kbApi(`/departments/${state.kb.editingDepartmentId}`, {
        method: "PATCH",
        body: payload,
      });
    } else {
      await kbApi("/departments", { method: "POST", body: payload });
    }
    toast("保存成功", "部门树已更新");
    state.kb.editingDepartmentId = null;
    document.getElementById("kbDepartmentForm").hidden = true;
    await kbLoadAdminPage();
  } catch (error) {
    toast("保存失败", error.message, "error");
  }
}

async function kbDeleteDepartment(departmentId) {
  if (!window.confirm("确定删除这个部门吗？子部门会自动上移一级。")) return;
  try {
    await kbApi(`/departments/${departmentId}`, { method: "DELETE" });
    toast("删除成功", "部门已删除");
    await kbLoadAdminPage();
  } catch (error) {
    toast("删除失败", error.message, "error");
  }
}

function renderKbMembers() {
  const body = document.getElementById("kbMemberBody");
  const userSelect = document.getElementById("kbMemberUser");
  if (userSelect) {
    userSelect.innerHTML = `<option value="">选择用户</option>${state.kb.tenantMembers
      .map((member) => `<option value="${esc(member.user_id)}">${esc(member.username)} · ${esc(member.role)}</option>`)
      .join("")}`;
  }
  if (!state.kb.members.length) {
    body.innerHTML = `<tr><td colspan="3"><div class="empty-state"><div><strong>暂无知识成员</strong><p>选择用户后分配知识角色。</p></div></div></td></tr>`;
    return;
  }
  const userMap = new Map(state.kb.members.map((item) => [item.user_id, item.username]));
  state.kb.tenantMembers.forEach((item) => userMap.set(item.user_id, item.username));
  body.innerHTML = state.kb.members.map((member) => `
    <tr>
      <td>${esc(userMap.get(member.user_id) || member.user_id)}</td>
      <td>${esc(KB_ROLE_LABELS[member.role] || member.role)}</td>
      <td>${esc(member.department || "—")}</td>
    </tr>`).join("");
}

async function kbSaveMemberAssignment() {
  const userId = document.getElementById("kbMemberUser").value;
  const role = document.getElementById("kbMemberRole").value;
  const department = document.getElementById("kbMemberDepartment").value || null;
  if (!userId) {
    toast("保存失败", "请选择用户", "error");
    return;
  }
  try {
    await kbApi("/members", {
      method: "POST",
      body: { user_id: userId, role, department },
    });
    toast("保存成功", "知识角色已更新");
    await kbLoadAdminPage();
  } catch (error) {
    toast("保存失败", error.message, "error");
  }
}

function renderKbRolePermissions() {
  const body = document.getElementById("kbRolePermissionsBody");
  const roles = state.kb.rolePermissions || [];
  if (!roles.length) {
    body.innerHTML = `<tr><td colspan="3"><div class="empty-state"><div><strong>无权限数据</strong><p>当前账号没有查看角色权限的权限。</p></div></div></td></tr>`;
    return;
  }
  body.innerHTML = roles.map((role) => `
    <tr>
      <td><div class="cell-title"><strong>${esc(KB_ROLE_LABELS[role.role_name] || role.role_name)}</strong><small>${esc(role.role_name)}</small></div></td>
      <td>${role.permissions?.length || 0} 项</td>
      <td><button class="button ghost kb-role-configure" data-role="${esc(role.role_name)}">配置</button></td>
    </tr>`).join("");
  document.querySelectorAll(".kb-role-configure").forEach((button) => {
    button.addEventListener("click", () => kbConfigureRole(button.dataset.role));
  });
}

function kbConfigureRole(roleName) {
  state.kb.editingRole = roleName;
  const role = state.kb.rolePermissions.find((item) => item.role_name === roleName);
  const selected = new Set(role?.permissions || []);
  document.getElementById("kbRoleEditorTitle").textContent =
    `${KB_ROLE_LABELS[roleName] || roleName} · 操作权限`;
  document.getElementById("kbRoleEditorSummary").textContent =
    `已选择 ${selected.size} 项`;
  document.getElementById("kbPermissionGrid").innerHTML =
    state.kb.permissionCodes.map((code) => `
      <label class="kb-permission-item">
        <input type="checkbox" value="${esc(code)}" ${selected.has(code) ? "checked" : ""} />
        <span><strong>${esc(KB_PERMISSION_LABELS[code] || code)}</strong><small>${esc(code)}</small></span>
      </label>`).join("");
  document.getElementById("kbRoleEditorPanel").hidden = false;
}

async function kbSaveRolePermissions() {
  if (!state.kb.editingRole) return;
  const permissions = Array.from(
    document.querySelectorAll("#kbPermissionGrid input:checked"),
  ).map((input) => input.value);
  try {
    const result = await kbApi("/role-permissions", {
      method: "PUT",
      body: { role_name: state.kb.editingRole, permissions },
    });
    state.kb.rolePermissions = result;
    toast("保存成功", "角色操作权限已更新");
    document.getElementById("kbRoleEditorPanel").hidden = true;
    renderKbRolePermissions();
  } catch (error) {
    toast("保存失败", error.message, "error");
  }
}

async function kbLoadDemoDocs() {
  try {
    const result = await kbApi("/demo/seed", { method: "POST" });
    toast("示例已载入", `已发布 ${result.total ?? 0} 篇示例文档`);
    await kbLoadDocs();
  } catch (e) {
    toast("载入失败", e.message, "error");
  }
}

async function kbPublishDoc(docId, button) {
  const pending = kbBeginPending(`doc:${docId}`, button, "发布中...");
  if (!pending) return;
  try {
    await kbApi(`/documents/${docId}/publish`, { method: "POST" });
    toast("发布成功", "文档已发布，问答时可检索");
    await kbLoadDocs();
  } catch (e) {
    toast("发布失败", e.message, "error");
  } finally {
    pending.done();
  }
}

async function kbDeleteDoc(docId, button) {
  if (!window.confirm("确定删除这篇文档及其所有分块吗？")) return;
  const pending = kbBeginPending(`doc:${docId}`, button, "删除中...");
  if (!pending) return;
  try {
    await kbApi(`/documents/${docId}`, { method: "DELETE" });
    toast("删除成功", "文档及其分块已删除");
    await kbLoadDocs();
  } catch (e) {
    toast("删除失败", e.message, "error");
  } finally {
    pending.done();
  }
}

async function kbAskQuestion() {
  const input = document.getElementById("kbAskInput");
  const question = input.value.trim();
  if (!question) return;
  if (!kbCan("kb:ask")) {
    toast(
      "没有问答权限",
      "当前账号没有被授予“知识问答”操作权限，请联系管理员在“组织与权限”中开启。",
      "error",
    );
    return;
  }
  const chatLog = document.getElementById("kbChatLog");
  const detail = document.getElementById("kbAskDetail");
  // Render user message
  if (chatLog.querySelector(".empty-state")) chatLog.innerHTML = "";
  chatLog.innerHTML += `<div class="kb-chat-msg user"><div class="kb-chat-bubble user"><p>${esc(question)}</p></div></div>`;
  chatLog.innerHTML += `<div class="kb-chat-msg bot"><div class="kb-chat-bubble bot loading"><div class="loading"><i data-lucide="loader-circle"></i>检索中...</div></div></div>`;
  chatLog.scrollTop = chatLog.scrollHeight;
  input.value = "";
  window.lucide.createIcons();
  const btn = document.getElementById("kbAskSubmit");
  btn.disabled = true;
  try {
    const res = await kbApi("/ask", { method: "POST", body: { question } });
    // Replace loading bubble
    const loadingBubble = chatLog.querySelector(".kb-chat-bubble.loading");
    if (loadingBubble) {
      loadingBubble.classList.remove("loading");
      loadingBubble.innerHTML = `<p>${esc(res.answer || "(无回答)")}</p>${res.faq_hit ? `<span class="badge info">FAQ命中${res.faq_cache_hit ? " · Redis缓存" : ""}</span>` : ""}${res.missing_permission ? '<span class="badge bad">权限不足</span>' : ""}${res.gap_created ? '<span class="badge warn">已记录缺口</span>' : ""}`;
    }
    // Render detail panel
    const allowedChunks = res.allowed_chunks || [];
    const blockedChunks = res.blocked_chunks || [];
    detail.innerHTML = `
      <div class="kb-retrieval-info">
        <h4>允许访问的 Chunk (${allowedChunks.length})</h4>
        <div class="kb-chunk-list">${allowedChunks.map((c) => `<span class="badge good" title="${esc(c.title || "")}">${esc(c.id)}</span>`).join("") || "<p class='kb-muted-text'>无</p>"}</div>
        <h4>被权限拦截 (${blockedChunks.length})</h4>
        <div class="kb-chunk-list">${blockedChunks.map((c) => `<span class="badge bad" title="${esc(c.title || "")}">${esc(c.id)}</span>`).join("") || "<p class='kb-muted-text'>无</p>"}</div>
        <div class="kb-meta-grid">
          <div><strong>FAQ命中</strong><span>${res.faq_hit ? "是" : "否"}</span></div>
          <div><strong>缓存命中</strong><span>${res.faq_cache_hit ? "是" : "否"}</span></div>
          <div><strong>缺口记录</strong><span>${res.gap_created ? "是" : "否"}</span></div>
          <div><strong>延迟</strong><span>${res.latency_ms ?? "—"}ms</span></div>
          <div><strong>会话ID</strong><span class="mono">${esc(res.session_id || "—")}</span></div>
        </div>
      </div>`;
    chatLog.scrollTop = chatLog.scrollHeight;
  } catch (e) {
    const loadingBubble = chatLog.querySelector(".kb-chat-bubble.loading");
    const friendlyMessage = e.status === 403
      ? (e.message.includes("kb:ask")
        ? "当前账号没有“知识问答”权限，请联系管理员在“组织与权限”中开启。"
        : `权限不足：${e.message}`)
      : e.message;
    if (loadingBubble) {
      loadingBubble.classList.remove("loading");
      loadingBubble.innerHTML = `<p class="kb-error-text">${esc(friendlyMessage)}</p>`;
    }
  } finally {
    btn.disabled = false;
    window.lucide.createIcons();
  }
}

async function kbLoadFaqs() {
  const cBody = document.getElementById("kbCandidatesBody");
  const pBody = document.getElementById("kbPublishedBody");
  cBody.innerHTML = `<tr><td colspan="4"><div class="loading"><i data-lucide="loader-circle"></i>加载中...</div></td></tr>`;
  pBody.innerHTML = `<tr><td colspan="3"><div class="loading"><i data-lucide="loader-circle"></i>加载中...</div></td></tr>`;
  try {
    const [candRes, pubRes] = await Promise.allSettled([
      kbApi("/faqs/candidates"),
      kbApi("/faqs/published"),
    ]);
    state.kb.candidates = candRes.status === "fulfilled" ? (Array.isArray(candRes.value) ? candRes.value : []) : [];
    state.kb.publishedFaqs = pubRes.status === "fulfilled" ? (Array.isArray(pubRes.value) ? pubRes.value : []) : [];
    document.getElementById("kbCandidatesSummary").textContent = `候选 ${state.kb.candidates.length}`;
    document.getElementById("kbPublishedSummary").textContent = `已发布 ${state.kb.publishedFaqs.length}`;
    renderKbCandidates();
    renderKbPublished();
  } catch (e) {
    cBody.innerHTML = `<tr><td colspan="4">加载失败：${esc(e.message)}</td></tr>`;
  }
  window.lucide.createIcons();
}

async function kbClusterFaqCandidates() {
  try {
    const result = await kbApi("/cluster?days=30&min_frequency=2", { method: "POST" });
    toast(
      "生成完成",
      result.length ? `已生成 ${result.length} 个候选 FAQ` : "暂未达到聚类条件，请先积累相同提问",
    );
    await kbLoadFaqs();
  } catch (e) {
    toast("生成失败", e.message, "error");
  }
}

function renderKbCandidates() {
  const body = document.getElementById("kbCandidatesBody");
  if (!state.kb.candidates.length) {
    body.innerHTML = `<tr><td colspan="4"><div class="empty-state"><div><div class="empty-icon"><i data-lucide="help-circle"></i></div><strong>暂无候选</strong><p>问答积累后会自动聚类生成</p></div></div></td></tr>`;
    window.lucide.createIcons();
    return;
  }
  body.innerHTML = state.kb.candidates.map((c) => `
    <tr>
      <td><div class="cell-title"><strong>${esc(c.cluster_question || c.question || "—")}</strong></div></td>
      <td>${c.frequency ?? "—"}</td>
      <td>${kbConfidenceBadge(c.confidence)}</td>
      <td>
        ${kbCan("kb:faq:publish") ? `<button class="button ghost kb-faq-publish" data-id="${esc(c.id)}"><i data-lucide="check"></i><span>发布</span></button>` : ""}
        ${kbCan("kb:faq:reject") ? `<button class="button ghost kb-faq-reject" data-id="${esc(c.id)}"><i data-lucide="x"></i><span>拒绝</span></button>` : ""}
        ${kbCan("kb:faq:delete") ? `<button class="button ghost kb-faq-delete" data-id="${esc(c.id)}"><i data-lucide="trash-2"></i><span>删除</span></button>` : ""}
      </td>
    </tr>`).join("");
  document.querySelectorAll(".kb-faq-publish").forEach((b) => b.addEventListener("click", () => kbPublishFaq(b.dataset.id)));
  document.querySelectorAll(".kb-faq-reject").forEach((b) => b.addEventListener("click", () => kbRejectFaq(b.dataset.id)));
  document.querySelectorAll(".kb-faq-delete").forEach((b) => b.addEventListener("click", () => kbDeleteFaqCandidate(b.dataset.id)));
  window.lucide.createIcons();
}

function renderKbPublished() {
  const body = document.getElementById("kbPublishedBody");
  if (!state.kb.publishedFaqs.length) {
    body.innerHTML = `<tr><td colspan="3"><div class="empty-state"><div><div class="empty-icon"><i data-lucide="check-circle"></i></div><strong>暂无已发布</strong><p>从候选 FAQ 审核发布</p></div></div></td></tr>`;
    window.lucide.createIcons();
    return;
  }
  body.innerHTML = state.kb.publishedFaqs.map((f) => `
    <tr>
      <td><div class="cell-title"><strong>${esc(f.question)}</strong></div></td>
      <td><small>${esc((f.answer || "").slice(0, 80))}${(f.answer || "").length > 80 ? "..." : ""}</small></td>
      <td>${kbCan("kb:faq:delete") ? `<button class="button ghost kb-published-faq-delete" data-id="${esc(f.id)}"><i data-lucide="trash-2"></i><span>删除</span></button>` : "—"}</td>
    </tr>`).join("");
  document.querySelectorAll(".kb-published-faq-delete").forEach((b) => b.addEventListener("click", () => kbDeletePublishedFaq(b.dataset.id)));
  window.lucide.createIcons();
}

function kbConfidenceBadge(conf) {
  if (conf == null) return '<span class="badge">—</span>';
  const pct = Math.round(conf * 100);
  if (pct >= 80) return `<span class="badge good">${pct}%</span>`;
  if (pct >= 50) return `<span class="badge warn">${pct}%</span>`;
  return `<span class="badge bad">${pct}%</span>`;
}

async function kbPublishFaq(id) {
  try {
    await kbApi(`/faqs/${id}/publish`, { method: "POST" });
    toast("发布成功", "FAQ 已发布，问答时优先命中");
    await kbLoadFaqs();
  } catch (e) {
    toast("发布失败", e.message, "error");
  }
}

async function kbRejectFaq(id) {
  try {
    await kbApi(`/faqs/${id}/reject`, { method: "POST" });
    toast("已拒绝", "候选 FAQ 已标记为拒绝");
    await kbLoadFaqs();
  } catch (e) {
    toast("操作失败", e.message, "error");
  }
}

async function kbDeleteFaqCandidate(id) {
  if (!window.confirm("确定删除这个 FAQ 候选吗？")) return;
  try {
    await kbApi(`/faqs/candidates/${id}`, { method: "DELETE" });
    toast("删除成功", "FAQ 候选已删除");
    await kbLoadFaqs();
  } catch (e) {
    toast("删除失败", e.message, "error");
  }
}

async function kbDeletePublishedFaq(id) {
  if (!window.confirm("确定删除这个已发布 FAQ 吗？")) return;
  try {
    await kbApi(`/faqs/published/${id}`, { method: "DELETE" });
    toast("删除成功", "已发布 FAQ 已删除");
    await kbLoadFaqs();
  } catch (e) {
    toast("删除失败", e.message, "error");
  }
}

async function kbLoadGaps() {
  const body = document.getElementById("kbGapsBody");
  const summary = document.getElementById("kbGapsSummary");
  body.innerHTML = `<tr><td colspan="6"><div class="loading"><i data-lucide="loader-circle"></i>加载中...</div></td></tr>`;
  try {
    const status = document.getElementById("kbGapStatusFilter").value;
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    const res = await kbApi(`/gaps${params.toString() ? "?" + params : ""}`);
    state.kb.gaps = Array.isArray(res) ? res : [];
    summary.textContent = `共 ${state.kb.gaps.length} 条`;
    renderKbGaps();
  } catch (e) {
    summary.textContent = "加载失败";
    body.innerHTML = `<tr><td colspan="6">加载失败：${esc(e.message)}</td></tr>`;
  }
  window.lucide.createIcons();
}

function renderKbGaps() {
  const body = document.getElementById("kbGapsBody");
  if (!state.kb.gaps.length) {
    body.innerHTML = `<tr><td colspan="6"><div class="empty-state"><div><div class="empty-icon"><i data-lucide="search-x"></i></div><strong>暂无缺口</strong><p>知识库能回答所有问题或还没有人提问</p></div></div></td></tr>`;
    window.lucide.createIcons();
    return;
  }
  body.innerHTML = state.kb.gaps.map((g) => `
    <tr>
      <td><div class="cell-title"><strong>${esc(g.question)}</strong></div></td>
      <td>${esc(g.department || "—")}</td>
      <td>${g.frequency ?? "—"}</td>
      <td>${g.highest_similarity != null ? (Math.round(g.highest_similarity * 100) + "%") : "—"}</td>
      <td>${kbGapStatusBadge(g.status)}</td>
      <td>
        ${kbCan("kb:gap:resolve") && g.status !== "resolved" ? `<button class="button ghost kb-gap-resolve" data-id="${esc(g.id)}"><i data-lucide="check"></i><span>标记解决</span></button>` : "—"}
        ${kbCan("kb:gap:delete") ? `<button class="button ghost kb-gap-delete" data-id="${esc(g.id)}"><i data-lucide="trash-2"></i><span>删除</span></button>` : ""}
      </td>
    </tr>`).join("");
  document.querySelectorAll(".kb-gap-resolve").forEach((b) => b.addEventListener("click", () => kbResolveGap(b.dataset.id)));
  document.querySelectorAll(".kb-gap-delete").forEach((b) => b.addEventListener("click", () => kbDeleteGap(b.dataset.id)));
  window.lucide.createIcons();
}

function kbGapStatusBadge(status) {
  const map = {
    open: ["warn", "待处理"],
    in_progress: ["info", "处理中"],
    resolved: ["good", "已解决"],
  };
  const [tone, label] = map[status] || ["", status || "未知"];
  return `<span class="badge ${tone}"><span class="dot"></span>${label}</span>`;
}

async function kbResolveGap(id) {
  try {
    await kbApi(`/gaps/${id}/resolve`, { method: "POST" });
    toast("已标记", "缺口已标记为解决");
    await kbLoadGaps();
  } catch (e) {
    toast("操作失败", e.message, "error");
  }
}

async function kbDeleteGap(id) {
  if (!window.confirm("确定删除这条知识缺口记录吗？")) return;
  try {
    await kbApi(`/gaps/${id}`, { method: "DELETE" });
    toast("删除成功", "知识缺口记录已删除");
    await kbLoadGaps();
  } catch (e) {
    toast("删除失败", e.message, "error");
  }
}

async function kbLoadMetrics() {
  const detail = document.getElementById("kbMetricsDetail");
  const auditBody = document.getElementById("kbAuditBody");
  detail.innerHTML = `<div class="loading"><i data-lucide="loader-circle"></i>加载中...</div>`;
  auditBody.innerHTML = `<tr><td colspan="5"><div class="loading"><i data-lucide="loader-circle"></i>加载中...</div></td></tr>`;
  try {
    const [metricsRes, auditRes] = await Promise.allSettled([
      kbApi("/metrics"),
      kbApi("/audits?limit=20"),
    ]);
    state.kb.metrics = metricsRes.status === "fulfilled" ? metricsRes.value : null;
    state.kb.audit = auditRes.status === "fulfilled" ? (Array.isArray(auditRes.value) ? auditRes.value : []) : [];
    renderKbMetrics();
  } catch (e) {
    detail.innerHTML = `<p class="kb-error-text">加载失败：${esc(e.message)}</p>`;
  }
  window.lucide.createIcons();
}

function renderKbMetrics() {
  const m = state.kb.metrics;
  if (!m) {
    document.getElementById("kbMetricsDetail").innerHTML = `<p class="kb-muted-text">暂无运营数据</p>`;
    return;
  }
  setText("kbMetricTotalQs", m.access_count ?? m.total_questions ?? 0);
  setText("kbMetricUniqueUsers", m.unique_users ?? 0);
  setText("kbMetricUnits", m.knowledge_units ?? 0);
  setText("kbMetricFaqHit", m.faq_hit_count ?? 0);
  setText("kbMetricOpenGaps", m.open_gaps ?? 0);
  setText("kbMetricDocs", m.published_documents ?? 0);
  setText("kbMetricResolvedGaps", m.resolved_gaps ?? 0);
  const topQuestions = m.top_questions || [];
  const hotKnowledge = m.hot_knowledge || [];
  const daily = m.daily || [];
  const maxDailyQuestions = Math.max(1, ...daily.map((item) => item.questions || 0));
  const detail = document.getElementById("kbMetricsDetail");
  detail.innerHTML = `
    <div class="kb-metrics-grid">
      <div class="kb-metric-row"><strong>${m.access_count ?? m.total_questions ?? 0}</strong><span>访问次数</span></div>
      <div class="kb-metric-row"><strong>${m.unique_users ?? 0}</strong><span>独立人数</span></div>
      <div class="kb-metric-row"><strong>${m.knowledge_units ?? 0}</strong><span>知识单元数</span></div>
      <div class="kb-metric-row"><strong>${m.faq_hit_count ?? 0}</strong><span>FAQ命中</span></div>
      <div class="kb-metric-row"><strong>${m.faq_hit_rate != null ? (Math.round(m.faq_hit_rate * 100) + "%") : "—"}</strong><span>命中率</span></div>
      <div class="kb-metric-row"><strong>${m.open_gaps ?? 0}</strong><span>待处理缺口</span></div>
      <div class="kb-metric-row"><strong>${m.resolved_gaps ?? 0}</strong><span>已解决缺口</span></div>
      <div class="kb-metric-row"><strong>${m.proposed_faqs ?? 0}</strong><span>候选FAQ</span></div>
      <div class="kb-metric-row"><strong>${m.published_documents ?? 0}</strong><span>已发布文档</span></div>
      <div class="kb-metric-row"><strong>${m.total_tokens ?? 0}</strong><span>Token 消耗</span></div>
      <div class="kb-metric-row"><strong>${m.blocked_access_count ?? 0}</strong><span>权限拦截</span></div>
      <div class="kb-metric-row"><strong>${m.average_latency_ms != null ? Math.round(m.average_latency_ms) + "ms" : "—"}</strong><span>平均响应时间</span></div>
    </div>
    <div class="kb-metrics-lists">
      <div>
        <h4>常见问题榜</h4>
        ${topQuestions.length ? topQuestions.map((item) => `
          <div class="kb-rank-row"><span>${esc(item.question)}</span><strong>${item.count} 次</strong></div>
        `).join("") : `<p class="kb-muted-text">暂无提问数据</p>`}
      </div>
      <div>
        <h4>知识热度榜</h4>
        ${hotKnowledge.length ? hotKnowledge.map((item) => `
          <div class="kb-rank-row"><span>${esc(item.title)}</span><strong>${item.hit_count} 次</strong></div>
        `).join("") : `<p class="kb-muted-text">暂无召回数据</p>`}
      </div>
    </div>
    <div class="kb-trend">
      <h4>Token 与响应时间趋势</h4>
      ${daily.length ? `<div class="data-table-wrap"><table class="data-table">
        <thead><tr><th>日期</th><th>访问</th><th>独立人数</th><th>Token</th><th>平均响应</th></tr></thead>
        <tbody>${daily.map((item) => `
          <tr>
            <td>${esc(item.date)}</td>
            <td><div class="kb-trend-cell"><span style="width:${Math.max(4, Math.round((item.questions || 0) / maxDailyQuestions * 100))}%"></span><strong>${item.questions || 0}</strong></div></td>
            <td>${item.unique_users || 0}</td>
            <td>${item.tokens || 0}</td>
            <td>${Math.round(item.average_latency_ms || 0)}ms</td>
          </tr>`).join("")}</tbody>
      </table></div>` : `<p class="kb-muted-text">暂无趋势数据</p>`}
    </div>`;
  const auditBody = document.getElementById("kbAuditBody");
  if (!state.kb.audit.length) {
    auditBody.innerHTML = `<tr><td colspan="5"><div class="empty-state"><div><div class="empty-icon"><i data-lucide="scroll-text"></i></div><strong>暂无审计</strong></div></div></td></tr>`;
  } else {
    auditBody.innerHTML = state.kb.audit.map((a) => `
      <tr>
        <td><div class="cell-title"><strong>${esc((a.question || "").slice(0, 50))}${(a.question || "").length > 50 ? "..." : ""}</strong></div></td>
        <td>${a.faq_hit ? '<span class="badge good">是</span>' : '<span class="badge">否</span>'}</td>
        <td>${a.token_estimate ?? "—"}</td>
        <td>${a.latency_ms ?? "—"}ms</td>
        <td>${kbCan("kb:audit:delete") ? `<button class="button ghost kb-audit-delete" data-id="${esc(a.id)}"><i data-lucide="trash-2"></i><span>删除</span></button>` : "—"}</td>
      </tr>`).join("");
    document.querySelectorAll(".kb-audit-delete").forEach((b) => b.addEventListener("click", () => kbDeleteAudit(b.dataset.id)));
  }
  window.lucide.createIcons();
}

async function kbDeleteAudit(id) {
  if (!window.confirm("确定删除这条审计记录吗？删除后运营指标会重新统计。")) return;
  try {
    await kbApi(`/audits/${id}`, { method: "DELETE" });
    toast("删除成功", "审计记录已删除");
    await kbLoadMetrics();
  } catch (e) {
    toast("删除失败", e.message, "error");
  }
}
