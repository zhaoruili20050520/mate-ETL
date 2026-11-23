const TOKEN_KEY = "etl_agent_token_v2";
const SESSION_KEY = "etl_agent_session_v2";

// Mirrors app.js: read the persistent localStorage copy first, then fall back
// to sessionStorage so tabs opened before the switch keep working.
function readStoredToken() {
  return localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY) || "";
}

function readStoredSession() {
  return localStorage.getItem(SESSION_KEY) || sessionStorage.getItem(SESSION_KEY) || "null";
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

const OPERATION_LABELS = {
  register_api_v1_auth_register_post: "注册新用户",
  login_api_v1_auth_login_post: "登录并获取令牌",
  me_api_v1_auth_me_get: "获取当前用户信息",
  create_tenant_api_v1_tenants_post: "创建租户",
  list_tenants_api_v1_tenants_get: "查询当前用户所属租户",
  get_tenant_api_v1_tenants__tenant_id__get: "查询租户详情",
  add_tenant_member_api_v1_tenants__tenant_id__members_post: "添加租户成员",
  list_tenant_members_api_v1_tenants__tenant_id__members_get: "查询租户成员",
  remove_tenant_member_api_v1_tenants__tenant_id__members__user_id__delete: "移除租户成员",
  create_project_api_v1_projects_post: "创建项目",
  list_projects_api_v1_projects_get: "查询项目列表",
  get_project_api_v1_projects__project_id__get: "查询项目详情",
  add_project_member_api_v1_projects__project_id__members_post: "添加项目成员",
  list_project_members_api_v1_projects__project_id__members_get: "查询项目成员",
  remove_project_member_api_v1_projects__project_id__members__user_id__delete: "移除项目成员",
  list_connections_api_v1_projects__project_id__connections_get: "查询项目数据连接",
  create_connection_api_v1_projects__project_id__connections_post: "创建数据连接",
  get_connection_api_v1_projects__project_id__connections__connection_id__get: "查询数据连接详情",
  delete_connection_api_v1_projects__project_id__connections__connection_id__delete: "删除数据连接",
  test_connection_api_v1_projects__project_id__connections__connection_id__test_post: "测试数据连接",
  list_profiles_api_v1_projects__project_id__profiles_get: "查询项目元数据画像",
  create_connection_api_v1_connections_post: "创建数据连接",
  list_connections_api_v1_connections_get: "查询数据连接列表",
  test_connection_api_v1_connections__connection_id__test_post: "测试数据连接",
  trigger_explore_api_v1_connections__connection_id__metadata_explore_post: "触发元数据探查",
  list_metadata_profiles_api_v1_connections__connection_id__metadata_profiles_get: "查询元数据画像",
  get_exploration_task_status_api_v1_connections__connection_id__metadata_tasks__task_id__get:
    "查询元数据探查任务",
  generate_pipeline_api_v1_pipelines_generate_post: "根据自然语言生成管道",
  clarify_pipeline_api_v1_pipelines__pipeline_id__clarify_post: "提交澄清答案并继续生成",
  freeze_version_api_v1_pipelines__pipeline_id__versions_post: "冻结管道版本",
  list_versions_api_v1_pipelines__pipeline_id__versions_get: "查询管道版本列表",
  list_pipelines_api_v1_pipelines_get: "查询管道列表",
  get_pipeline_api_v1_pipelines__pipeline_id__get: "查询管道详情",
  delete_pipeline_api_v1_pipelines__pipeline_id__delete: "删除管道",
  get_version_api_v1_pipelines__pipeline_id__versions__version_id__get: "查询管道版本详情",
  submit_approval_api_v1_preparations__preparation_id__approvals_post: "提交审批或驳回",
  list_approvals_api_v1_preparations__preparation_id__approvals_get: "查询审批记录",
  create_preparation_api_v1_preparations_post: "创建执行准备",
  get_preparation_api_v1_preparations__preparation_id__get: "查询执行准备",
  commit_preparation_api_v1_preparations__preparation_id__commit_post: "提交执行准备",
  list_execution_runs_api_v1_execution_runs_get: "查询执行记录",
  get_execution_run_api_v1_execution_runs__run_id__get: "查询执行记录详情",
  get_run_metrics_api_v1_execution_runs__run_id__metrics_get: "查看执行运行数据",
  get_run_events_api_v1_execution_runs__run_id__events_get: "查看执行事件记录",
  get_quality_report_api_v1_execution_runs__run_id__quality_report_get: "查看数据质量报告",
  get_publish_attempt_api_v1_execution_runs__run_id__publish_attempt_get: "查看发布记录",
  cancel_execution_run_api_v1_execution_runs__run_id__cancel_post: "取消执行任务",
  rollback_execution_run_api_v1_execution_runs__run_id__rollback_post: "回滚执行任务",
  seed_demo_api_v1_knowledge_demo_seed_post: "载入知识库示例",
  list_documents_api_v1_knowledge_documents_get: "查看文档列表",
  create_document_api_v1_knowledge_documents_post: "新增知识文档",
  get_document_api_v1_knowledge_documents__doc_id__get: "查看文档详情",
  delete_doc_api_v1_knowledge_documents__doc_id__delete: "删除知识文档",
  publish_doc_api_v1_knowledge_documents__doc_id__publish_post: "发布知识文档",
  archive_doc_api_v1_knowledge_documents__doc_id__archive_post: "归档知识文档",
  update_chunk_endpoint_api_v1_knowledge_chunks__chunk_id__put: "修改文档分块",
  delete_chunk_endpoint_api_v1_knowledge_chunks__chunk_id__delete: "删除文档分块",
  ask_question_api_v1_knowledge_ask_post: "向知识库提问",
  trigger_clustering_api_v1_knowledge_cluster_post: "生成常见问题候选",
  list_faq_candidates_api_v1_knowledge_faqs_candidates_get: "查看常见问题候选",
  delete_faq_candidate_endpoint_api_v1_knowledge_faqs_candidates__candidate_id__delete:
    "删除常见问题候选",
  publish_faq_candidate_api_v1_knowledge_faqs__candidate_id__publish_post: "发布常见问题",
  reject_faq_candidate_api_v1_knowledge_faqs__candidate_id__reject_post: "驳回常见问题候选",
  list_published_faqs_api_v1_knowledge_faqs_published_get: "查看已发布常见问题",
  delete_published_faq_endpoint_api_v1_knowledge_faqs_published__faq_id__delete:
    "删除已发布常见问题",
  list_knowledge_gaps_api_v1_knowledge_gaps_get: "查看知识缺口",
  delete_knowledge_gap_api_v1_knowledge_gaps__gap_id__delete: "删除知识缺口",
  resolve_knowledge_gap_api_v1_knowledge_gaps__gap_id__resolve_post: "标记缺口已解决",
  list_qa_audits_api_v1_knowledge_audits_get: "查看问答审计",
  delete_qa_audit_api_v1_knowledge_audits__audit_id__delete: "删除问答审计",
  get_metrics_api_v1_knowledge_metrics_get: "查看运营数据",
  list_members_api_v1_knowledge_members_get: "查看知识库成员",
  assign_member_api_v1_knowledge_members_post: "设置知识库成员",
  update_project_api_v1_projects__project_id__patch: "更新项目信息",
  update_connection_api_v1_projects__project_id__connections__connection_id__patch:
    "更新数据连接",
  get_knowledge_access_api_v1_knowledge_access_get: "查询知识库访问范围",
  create_documents_batch_api_v1_knowledge_documents_batch_post: "批量新增知识文档",
  get_department_tree_api_v1_knowledge_departments_tree_get: "查询部门树",
  create_department_endpoint_api_v1_knowledge_departments_post: "创建部门",
  update_department_endpoint_api_v1_knowledge_departments__department_id__patch: "更新部门",
  delete_department_endpoint_api_v1_knowledge_departments__department_id__delete: "删除部门",
  get_role_permissions_api_v1_knowledge_role_permissions_get: "查询角色权限",
  put_role_permissions_api_v1_knowledge_role_permissions_put: "设置角色权限",
  get_benchmark_report_api_v1_benchmark__get: "查看评测报告",
  get_benchmark_text_api_v1_benchmark_text_get: "查看评测报告文本",
  list_depts_api_v1_departments_get: "查询部门列表",
  create_dept_api_v1_departments_post: "创建部门",
  get_tree_api_v1_departments_tree_get: "查询部门树",
  update_dept_api_v1_departments__dept_id__put: "更新部门",
  delete_dept_api_v1_departments__dept_id__delete: "删除部门",
  list_codes_api_v1_permissions_codes_get: "查询权限码清单",
  get_role_perms_api_v1_permissions_roles__role_name__get: "查询角色权限",
  assign_perms_api_v1_permissions_roles__role_name__post: "设置角色权限",
  get_my_permissions_api_v1_permissions_me_get: "查询我的权限",
  health_check_health_get: "服务健康检查",
};

const TYPE_LABELS = {
  string: "文本",
  integer: "整数",
  number: "数字",
  boolean: "布尔值",
  array: "数组",
  object: "对象",
  null: "空值",
  anyOf: "多种类型",
  oneOf: "多种类型",
  allOf: "组合结构",
  file: "文件",
  "date-time": "日期时间",
  date: "日期",
  uuid: "唯一标识",
  "string uuid": "唯一标识",
};

const FIELD_LABELS = {
  username: "账号",
  password: "密码",
  name: "名称",
  title: "标题",
  category: "分类",
  content: "内容",
  description: "说明",
  tenant_id: "租户",
  project_id: "项目",
  connection_id: "连接",
  source_connection_id: "源连接",
  target_connection_id: "目标连接",
  pipeline_id: "管道",
  version_id: "版本",
  preparation_id: "执行准备",
  candidate_id: "常见问题候选",
  faq_id: "常见问题",
  doc_id: "文档",
  chunk_id: "分块",
  gap_id: "缺口",
  audit_id: "审计",
  run_id: "执行记录",
  task_id: "任务",
  user_id: "用户",
  role: "角色",
  department: "部门",
  source_type: "数据库类型",
  host: "主机地址",
  port: "端口",
  database_name: "数据库名",
  database: "数据库",
  table: "数据表",
  table_name: "数据表",
  schema: "模式",
  schema_name: "模式",
  question: "问题",
  answer: "回答",
  comment: "备注",
  natural_language: "需求描述",
  sync_mode: "同步方式",
  source_table: "源数据表",
  target_table: "目标数据表",
  primary_keys: "主键字段",
  watermark_column: "增量字段",
  reason: "原因",
  decision: "审批决定",
  approval_role: "审批角色",
  data_classification: "数据分级",
  execution_budget: "执行预算",
  visibility: "可见范围",
  departments: "允许部门",
  roles: "允许角色",
  users: "允许用户",
  page: "页码",
  page_size: "每页数量",
  status: "状态",
  session_id: "会话编号",
  access_count: "访问次数",
  access_token: "访问令牌",
  token_type: "令牌类型",
  expires_in: "有效时间（秒）",
  user_id: "用户",
  tenant_id: "租户",
  project_id: "项目",
  connection_id: "连接",
  pipeline_id: "管道",
  pipeline_version_id: "管道版本",
  preparation_id: "执行准备",
  document_id: "文档",
  chunk_index: "分块序号",
  chunks: "文档分块",
  chunk_count: "分块数",
  field: "字段",
  transform: "转换方式",
  nullable: "允许为空",
  source_field: "源字段",
  target_field: "目标字段",
  source_type: "源类型",
  target_type: "目标类型",
  cleaning_rules: "清洗规则",
  quality_contract: "质量规则",
  rules: "规则列表",
  rule: "规则",
  params: "参数",
  sync_strategy: "同步策略",
  mode: "同步方式",
  batch_size: "批次大小",
  parallelism: "并行数",
  retry_times: "重试次数",
  resource_scope: "资源范围",
  execution_budget: "执行预算",
  rollback_plan: "回滚方案",
  dead_letter_table: "错误数据表",
  dead_letter_schema: "错误数据模式",
  max_error_ratio: "最大错误率",
  status: "状态",
  version_number: "版本号",
  sha256: "校验值",
  source_requirement: "原始需求",
  validation_results: "校验结果",
  validation_issues: "校验问题",
  frozen_by: "冻结人",
  risk_level: "风险等级",
  risk_reasoning: "风险说明",
  input_fingerprint: "输入指纹",
  prepared_by: "创建人",
  committed_by: "提交人",
  created_by: "创建人",
  created_at: "创建时间",
  updated_at: "更新时间",
  committed_at: "提交时间",
  started_at: "开始时间",
  finished_at: "完成时间",
  event_type: "事件类型",
  severity: "严重程度",
  message: "消息",
  details: "详细信息",
  occurred_at: "发生时间",
  read_rows: "读取行数",
  written_rows: "写入行数",
  error_rows: "错误行数",
  bytes_read: "读取字节数",
  bytes_written: "写入字节数",
  duration_seconds: "执行时长（秒）",
  output_amplification: "输出放大倍数",
  error_rejection_rate: "错误拒绝率",
  sampled_at: "采样时间",
  success: "成功",
  latency_ms: "耗时（毫秒）",
  cluster_question: "聚合问题",
  cluster_key: "聚类标识",
  frequency: "出现次数",
  recommended_answer: "推荐答案",
  related_chunk_ids: "关联分块",
  confidence: "置信度",
  answer: "回答",
  answers: "回答内容",
  faq_hit: "命中常见问题",
  faq_cache_hit: "是否命中常见问题缓存",
  faq_hit_count: "常见问题命中次数",
  faq_hit_rate: "常见问题命中率",
  gap_created: "是否记录缺口",
  missing_permission: "是否缺少权限",
  allowed_chunks: "允许查看的分块",
  blocked_chunks: "被拦截的分块",
  allowed_chunk_ids: "允许分块",
  blocked_chunk_ids: "被拦截分块",
  recalled_chunk_ids: "召回分块",
  token_estimate: "预计令牌数",
  open_gaps: "未解决缺口",
  resolved_gaps: "已解决缺口",
  proposed_faqs: "常见问题候选数",
  published_documents: "已发布文档",
  blocked_access_count: "权限拦截次数",
  average_latency_ms: "平均耗时（毫秒）",
  total_questions: "总问答数",
  unique_users: "独立用户数",
  daily: "每日统计",
  total: "总数",
  items: "列表",
  page: "页码",
  page_size: "每页数量",
  error: "错误",
  error_message: "错误信息",
  error_rate: "错误率",
  shadow_rows: "影子表行数",
  shadow_table: "影子表",
  backup_table: "备份表",
  seatunnel_job_id: "SeaTunnel 任务编号",
  capability_id: "授权编号",
  quality_report: "质量报告",
  publish_attempt: "发布记录",
  rollback_result: "回滚结果",
  id: "编号",
  code: "代码",
  detail: "详情",
  input: "输入位置",
  ctx: "上下文",
  loc: "位置",
  msg: "提示",
  latest: "最新记录",
  approver_id: "审批人",
  autofix_attempts: "自动修复次数",
  cancelled: "是否已取消",
  clarification_questions: "需要确认的问题",
  columns_info: "字段信息",
  etl_plan: "数据同步方案",
  execution_run_id: "执行记录",
  field_mappings: "字段映射",
  highest_similarity: "最高相似度",
  hocon_config: "执行配置",
  hocon_text: "执行配置内容",
  is_multi_tenant: "是否多租户共享",
  is_production_source: "是否生产数据源",
  last_login_at: "最近登录时间",
  last_test_latency_ms: "最近测试耗时（毫秒）",
  last_test_message: "最近测试结果",
  last_test_status: "最近测试状态",
  last_tested_at: "最近测试时间",
  max_execution_seconds: "最长执行时间（秒）",
  max_sample_rows: "最多采样行数",
  max_tables: "最多探查表数",
  permissions: "权限设置",
  profiled_at: "探查时间",
  rule_results: "规则检查结果",
  sample_data: "样例数据",
  signed_at: "签字时间",
  source: "源数据配置",
  stats_info: "统计信息",
  strategy: "处理策略",
  suggested_category: "建议分类",
  suggestion: "处理建议",
  table_count: "数据表数量",
  target: "目标数据配置",
  total_rows: "总行数",
  type: "类型",
  value_distribution: "数据分布",
  created: "新增数量",
  codes: "代码列表",
  children: "下级部门",
  document_ids: "文档编号列表",
  documents: "文档列表",
  hot_knowledge: "热门知识",
  knowledge_units: "知识单元",
  parent_id: "上级部门编号",
  permission_code: "权限码",
  permission_codes: "权限码列表",
  role_name: "角色名称",
  top_questions: "高频问题",
  total_tokens: "令牌总数",
  users: "允许用户",
  sort_order: "排序序号",
};

const VALUE_LABELS = {
  postgresql: "PostgreSQL",
  mysql: "MySQL",
  doris: "Doris",
  clickhouse: "ClickHouse",
  oracle: "Oracle",
  full: "全量同步",
  incremental: "增量同步",
  active: "正常",
  inactive: "停用",
  locked: "锁定",
  public: "所有人可见",
  tenant: "本租户可见",
  restricted: "指定人员可见",
  tenant_admin: "租户管理员",
  project_admin: "项目管理员",
  engineer: "数据工程师",
  auditor: "审计人员",
  viewer: "普通用户",
  knowledge_admin: "知识管理员",
  system_admin: "系统管理员",
  pending: "等待中",
  running: "运行中",
  quality_checking: "质量检查中",
  publishing: "发布中",
  succeeded: "成功",
  failed: "失败",
  rolled_back: "已回滚",
  draft: "草稿",
  validated: "已校验",
  frozen: "已冻结",
  approved: "已审批",
  rejected: "已驳回",
  proposed: "待审核",
  open: "待处理",
  in_progress: "处理中",
  resolved: "已解决",
  P0: "极高风险",
  P1: "高风险",
  P2: "中风险",
  P3: "低风险",
  data_owner: "数据负责人",
  security: "安全管理员",
  data_checker: "数据复核人",
  security_checker: "安全复核人",
  internal: "内部使用",
  confidential: "机密",
  archived: "已归档",
  published: "已发布",
  submitted: "已提交",
  committed: "已提交",
  overwrite: "覆盖写入",
  append: "追加写入",
  upsert: "更新或插入",
  error: "错误",
  warning: "警告",
  info: "提示",
  critical: "严重",
  untested: "未测试",
  ok: "正常",
  degraded: "部分异常",
  unknown: "未知",
  approve: "同意",
  reject: "驳回",
  low: "低风险",
  medium: "中风险",
  high: "高风险",
  development: "开发环境",
  staging: "测试环境",
  production: "生产环境",
};

let openapi = null;
let searchTerm = "";
let cachedProjects = null;

document.addEventListener("DOMContentLoaded", () => {
  const searchInput = document.getElementById("searchInput");
  searchInput.addEventListener("input", (event) => {
    searchTerm = event.target.value.trim().toLowerCase();
    renderDocs();
  });

  const root = document.getElementById("docsRoot");
  root.addEventListener("click", handleDocsClick);
  document.getElementById("manualLoginButton").addEventListener("click", manualLogin);
  ["manualUsername", "manualPassword"].forEach((id) => {
    document.getElementById(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") manualLogin();
    });
  });
  window.addEventListener("storage", (event) => {
    if (event.key === TOKEN_KEY) checkAuthStatus();
  });
  window.addEventListener("focus", checkAuthStatus);

  checkAuthStatus();
  loadOpenapi();
});

function setAuthHeader(authenticated, username = "", role = "") {
  const pill = document.getElementById("authPill");
  pill.classList.remove("ok", "warn");
  pill.classList.add(authenticated ? "ok" : "warn");
  document.querySelector("#authPill .pill-text").textContent = authenticated
    ? "已登录"
    : "未登录";
  document.getElementById("docsUserName").textContent = username || "未登录";
  document.getElementById("docsUserRole").textContent = role || "接口参考";
  document.getElementById("docsUserAvatar").textContent = (username || "A")
    .slice(0, 1)
    .toUpperCase();
}

function showManualLogin(message = "") {
  document.getElementById("manualAuthOverlay").hidden = false;
  document.getElementById("manualLoginStatus").textContent = message;
  setAuthHeader(false);
}

async function checkAuthStatus() {
  const token = readStoredToken();
  if (!token) {
    document.getElementById("manualAuthOverlay").hidden = true;
    setAuthHeader(false);
    return;
  }
  try {
    const response = await fetch("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (response.status === 401) {
      clearStoredAuth();
      showManualLogin("登录已失效，请重新登录。");
      return;
    }
    if (response.status === 403) {
      showManualLogin("当前账号权限不足，无法访问操作手册，请联系管理员。");
      setAuthHeader(false);
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const me = await response.json();
    document.getElementById("manualAuthOverlay").hidden = true;
    setAuthHeader(true, me.username, me.role);
  } catch (error) {
    document.getElementById("manualAuthOverlay").hidden = true;
    setAuthHeader(false);
  }
}

async function manualLogin() {
  const username = document.getElementById("manualUsername").value.trim();
  const password = document.getElementById("manualPassword").value;
  const status = document.getElementById("manualLoginStatus");
  if (!username || !password) {
    status.textContent = "请输入账号和密码";
    return;
  }
  const button = document.getElementById("manualLoginButton");
  button.disabled = true;
  status.textContent = "正在登录...";
  try {
    const response = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "账号或密码错误");
    }
    storeAuth(payload.access_token, {
      username: payload.username,
      role: payload.role,
      tenant_id: payload.tenant_id,
      project_id: payload.project_id,
    });
    await checkAuthStatus();
    status.textContent = "";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function loadOpenapi() {
  const root = document.getElementById("docsRoot");
  try {
    const response = await fetch("/openapi.json?v=5");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    openapi = await response.json();
    renderHero();
    renderDocs();
  } catch (error) {
    root.innerHTML = `
      <div class="panel">
        <div class="empty-state">
          <div>
            <div class="empty-icon"><i data-lucide="circle-alert"></i></div>
            <strong>接口定义加载失败</strong>
            <p>${esc(error.message)}</p>
          </div>
        </div>
      </div>`;
    window.lucide.createIcons();
  }
}

function renderHero() {
  const endpoints = collectEndpoints();
  const stats = document.getElementById("docsStats");
  const methodCounts = endpoints.reduce((acc, item) => {
    acc[item.method] = (acc[item.method] || 0) + 1;
    return acc;
  }, {});
  stats.innerHTML = `
    <div class="docs-stat"><strong>${endpoints.length}</strong><span>常用功能</span></div>
    <div class="docs-stat"><strong>${new Set(endpoints.map((e) => e.tags[0])).size}</strong><span>功能分组</span></div>
    <div class="docs-stat"><strong>${methodCounts["get"] || 0}</strong><span>查看类</span></div>
    <div class="docs-stat"><strong>${methodCounts.post || 0}</strong><span>操作类</span></div>
    <div class="docs-stat"><strong>${Object.keys(openapi.components?.schemas || {}).length}</strong><span>表单类型</span></div>`;
}

function collectEndpoints() {
  const endpoints = [];
  if (!openapi?.paths) return endpoints;
  Object.entries(openapi.paths).forEach(([path, pathItem]) => {
    ["get", "post", "put", "patch", "delete"].forEach((method) => {
      const operation = pathItem?.[method];
      if (!operation) return;
      endpoints.push({
        path,
        method,
        operation,
        tags: operation.tags?.length ? operation.tags : ["通用"],
      });
    });
  });
  return endpoints;
}

function renderDocs() {
  const root = document.getElementById("docsRoot");
  if (!openapi) return;
  const endpoints = collectEndpoints();
  const filtered = endpoints.filter((item) => {
    if (!searchTerm) return true;
    const haystack = [
      item.path,
      item.method,
      item.operation.summary || "",
      item.operation.description || "",
      item.tags.join(" "),
      displayTag(item.tags[0]),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(searchTerm);
  });

  const grouped = new Map();
  filtered.forEach((item) => {
    const tag = item.tags[0];
    if (!grouped.has(tag)) grouped.set(tag, []);
    grouped.get(tag).push(item);
  });

  const order = [
    "认证与用户",
    "租户管理",
    "项目与连接",
    "数据连接与探查",
    "管道生成",
    "审批",
    "执行准备",
    "执行记录",
    "知识库",
    "效果评测",
    "部门管理",
    "权限管理",
    "通用",
    "健康检查",
  ];
  const tagNames = [...grouped.keys()].sort((a, b) => {
    const ai = order.indexOf(displayTag(a));
    const bi = order.indexOf(displayTag(b));
    return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi) || a.localeCompare(b);
  });

  if (!filtered.length) {
    root.innerHTML = `
      <div class="panel">
        <div class="empty-state">
          <div>
            <div class="empty-icon"><i data-lucide="search-x"></i></div>
            <strong>没有匹配的接口</strong>
            <p>换一个关键词再试试。</p>
          </div>
        </div>
      </div>`;
    window.lucide.createIcons();
    return;
  }

  root.innerHTML = tagNames
    .map((tag) => {
      const items = grouped.get(tag);
      return `
        <section class="tag-section">
          <div class="tag-head">
            <i data-lucide="layers"></i>
            <h2>${esc(displayTag(tag))}</h2>
            <span>${items.length}</span>
          </div>
          ${items.map(renderEndpoint).join("")}
        </section>`;
    })
    .join("");
  window.lucide.createIcons();
  void enhanceDynamicFields();
}

async function enhanceDynamicFields() {
  const token = readStoredToken();
  if (!token) return;
  try {
    if (!cachedProjects) {
      let session = null;
      try {
        session = JSON.parse(readStoredSession() || "null");
      } catch {
        session = null;
      }
      const tenantId = session?.tenant_id;
      const projectQuery = tenantId
        ? `?tenant_id=${tenantId}&page=1&page_size=500`
        : "?page=1&page_size=500";
      cachedProjects = await fetchJson(
        `/api/v1/projects${projectQuery}`,
        token,
      );
    }
    const projects = Array.isArray(cachedProjects.items) ? cachedProjects.items : [];
    document.querySelectorAll('[data-body-field="project_id"]').forEach((input) => {
      if (input.tagName === "SELECT") return;
      const select = document.createElement("select");
      select.className = "body-field-input";
      select.dataset.bodyField = "project_id";
      select.dataset.bodyType = "string";
      select.innerHTML = projects
        .map((project) => `<option value="${esc(project.id)}">${esc(project.name)}</option>`)
        .join("");
      input.replaceWith(select);
      select.addEventListener("change", () => populateConnectionChoices(select));
      populateConnectionChoices(select);
    });
  } catch {
    // The plain text inputs remain available if dropdown data cannot load.
  }
}

async function populateConnectionChoices(projectSelect) {
  const endpoint = projectSelect.closest(".endpoint");
  if (!endpoint) return;
  const sourceInput = endpoint.querySelector('[data-body-field="source_connection_id"]');
  const targetInput = endpoint.querySelector('[data-body-field="target_connection_id"]');
  const connectionInput = endpoint.querySelector('[data-body-field="connection_id"]');
  if (!sourceInput && !targetInput && !connectionInput) return;
  try {
    const response = await fetchJson(
      `/api/v1/connections?project_id=${projectSelect.value}&page=1&page_size=500`,
      readStoredToken(),
    );
    const connections = Array.isArray(response.items) ? response.items : [];
    [
      [sourceInput, "选择源连接"],
      [targetInput, "选择目标连接"],
      [connectionInput, "选择连接"],
    ].forEach(([input, placeholder]) => {
      if (!input || input.tagName === "SELECT") return;
      const select = document.createElement("select");
      select.className = "body-field-input";
      select.dataset.bodyField = input.dataset.bodyField;
      select.dataset.bodyType = "string";
      select.innerHTML =
        `<option value="">${placeholder}</option>` +
        connections
          .map(
            (connection) =>
              `<option value="${esc(connection.id)}">${esc(connection.name)}</option>`,
          )
          .join("");
      input.replaceWith(select);
    });
  } catch {
    // Keep text fields when connection choices are unavailable.
  }
}

async function fetchJson(path, token) {
  const response = await fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function renderEndpoint(item) {
  const { path, method, operation } = item;
  const params = (operation.parameters || []).filter((p) => p.in === "path" || p.in === "query");
  const bodySchema = operation.requestBody?.content?.["application/json"]?.schema;
  const summary = operation.summary || OPERATION_LABELS[operation.operationId] || "接口";
  const isClarify = String(operation.operationId || "").startsWith("clarify_pipeline");
  return `
    <article class="endpoint" data-endpoint="${esc(path)}" data-method="${method}">
      <button class="endpoint-summary" data-toggle>
        <span class="method ${method}">${methodLabel(method)}</span>
        <span class="endpoint-copy">
          <strong>${esc(summary)}</strong>
          <small>${esc(methodLabel(method))} · ${esc(tagLabel(item.tags[0]))}</small>
        </span>
        <i class="chevron" data-lucide="chevron-down"></i>
      </button>
      <div class="endpoint-detail">
        <p class="endpoint-desc">${esc(summary)}</p>
        ${
          params.length
            ? `
          <div class="detail-block">
            <h4>需要填写的信息</h4>
            <div class="param-list">
              ${params
                .map(
                  (param) => `
                    <div class="param-row">
                      <div>
                        <strong>${esc(param.description || fieldLabel(param.name))}</strong>
                      </div>
                      <span>${param.required ? "必填" : "选填"}</span>
                      <input
                        class="param-input"
                        data-param-name="${esc(param.name)}"
                        data-param-in="${param.in}"
                        placeholder="${esc(param.required ? "请填写" : "选填")}"
                      />
                    </div>`,
                )
                .join("")}
            </div>
          </div>`
            : ""
        }
        ${
          bodySchema
            ? `
          <div class="detail-block">
            <h4>${isClarify ? "回答缺少的信息" : "填写以下信息"}</h4>
            <div class="${isClarify ? "clarify-form" : "body-form"}">
              ${isClarify ? renderClarifyForm() : renderBodyForm(bodySchema)}
            </div>
          </div>`
            : ""
        }
        <div class="detail-block">
          <button class="button accent" data-try-run>
            <i data-lucide="play"></i>
            <span>立即执行</span>
          </button>
          <div class="try-result" data-result hidden></div>
        </div>
      </div>
    </article>`;
}

function renderClarifyForm() {
  const fields = [
    ["source_schema", "源数据库/模式", "例如：testdb"],
    ["source_table", "源数据表", "例如：test_users"],
    ["target_schema", "目标数据库/模式", "例如：etl_target"],
    ["target_table", "目标数据表", "例如：test_users"],
    ["primary_keys", "主键字段", "多个字段用逗号分隔"],
    ["watermark_column", "增量字段", "增量同步时填写，例如：updated_at"],
  ];
  return `
    <div class="body-form">
      ${fields
        .map(
          ([key, label, placeholder]) => `
          <div class="field">
            <label>${label} · 选填</label>
            <div class="input-wrap">
              <input class="body-field-input" data-clarify-key="${key}"
                data-body-type="string" type="text" placeholder="${placeholder}" />
            </div>
          </div>`,
        )
        .join("")}
      <div class="field">
        <label>同步方式 · 选填</label>
        <div class="input-wrap">
          <select class="body-field-input" data-clarify-key="sync_mode" data-body-type="string">
            <option value="">保持原设置</option>
            <option value="full">全量同步</option>
            <option value="incremental">增量同步</option>
          </select>
        </div>
      </div>
    </div>`;
}

function renderBodyForm(schema) {
  const resolved = resolveRef(schema || {});
  const props = resolved.properties || {};
  if (!Object.keys(props).length) return "";
  const required = new Set(resolved.required || []);
  return Object.entries(props)
    .map(([key, value]) => renderBodyField(key, resolveRef(value), required.has(key), key))
    .join("");
}

function renderBodyField(key, schema, required, fieldPath) {
  const type = schema.type || (schema.properties ? "object" : "string");
  const label = (schema.description || fieldLabel(key)).replace(/[。.：:]\s*$/, "");
  const textareaTypes = new Set(["object", "description", "content", "natural_language", "answer"]);
  const isPassword = String(key).toLowerCase().includes("password");
  const isTextarea = textareaTypes.has(key) || textareaTypes.has(type);

  let control;
  if (Array.isArray(schema.enum) && schema.enum.length) {
    control = `<select class="body-field-input" data-body-field="${esc(fieldPath)}" data-body-type="string">
      ${schema.enum
        .map((item) => `<option value="${esc(item)}">${esc(valueLabel(item))}</option>`)
        .join("")}
    </select>`;
  } else if (isTextarea) {
    control = `<textarea
      class="code-block body-field-input"
      data-body-field="${esc(fieldPath)}"
      data-body-type="${type}"
      placeholder="${type === "object" ? "JSON 格式，一般可留空" : "请填写"}"></textarea>`;
  } else if (type === "boolean") {
    control = `<select class="body-field-input" data-body-field="${esc(fieldPath)}" data-body-type="boolean">
      <option value="true">是</option>
      <option value="false">否</option>
    </select>`;
  } else if (type === "integer" || type === "number") {
    control = `<input class="body-field-input" data-body-field="${esc(fieldPath)}" data-body-type="number"
      type="number" placeholder="请输入数字" />`;
  } else if (type === "array") {
    control = `<input class="body-field-input" data-body-field="${esc(fieldPath)}" data-body-type="array"
      type="text" placeholder="多个值用逗号分隔" />`;
  } else {
    control = `<input class="body-field-input" data-body-field="${esc(fieldPath)}"
      data-body-type="string" type="${isPassword ? "password" : "text"}" placeholder="请填写" />`;
  }

  const wrapper = isTextarea
    ? control
    : `<div class="input-wrap">${control}</div>`;
  return `
    <div class="field">
      <label>${esc(label)}${required ? " · 必填" : " · 选填"}</label>
      ${wrapper}
    </div>`;
}

function fieldLabel(name) {
  return FIELD_LABELS[name] || String(name);
}

function valueLabel(value) {
  return VALUE_LABELS[value] || String(value);
}

function readBodyFields(container) {
  const root = {};
  let objectError = false;
  container.querySelectorAll("[data-body-field]").forEach((input) => {
    const path = input.dataset.bodyField.split(".");
    const value = input.value.trim();
    let parsed;
    if (input.dataset.bodyType === "number") {
      parsed = value === "" ? null : Number(value);
    } else if (input.dataset.bodyType === "boolean") {
      parsed = value === "true";
    } else if (input.dataset.bodyType === "array") {
      parsed = value
        ? value
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean)
        : [];
    } else if (input.dataset.bodyType === "object") {
      try {
        parsed = value ? JSON.parse(value) : {};
      } catch {
        objectError = true;
        parsed = {};
      }
    } else {
      parsed = value === "" ? null : value;
    }
    let target = root;
    path.forEach((part, index) => {
      if (index === path.length - 1) {
        target[part] = parsed;
      } else {
        target[part] = target[part] || {};
        target = target[part];
      }
    });
  });
  if (objectError) {
    throw new Error("包含 JSON 格式的字段填写不正确，请检查格式");
  }
  return root;
}

function methodLabel(method) {
  const labels = {
    get: "查询",
    post: "写入",
    put: "更新",
    patch: "更新",
    delete: "删除",
  };
  return labels[method] || method.toUpperCase();
}

function tagLabel(tag) {
  return displayTag(tag || "default");
}

async function handleDocsClick(event) {
  const toggle = event.target.closest("[data-toggle]");
  if (toggle) {
    const endpoint = toggle.closest(".endpoint");
    endpoint.classList.toggle("open");
    window.lucide.createIcons();
    return;
  }
  const tryButton = event.target.closest("[data-try-run]");
  if (!tryButton) return;
  const endpoint = tryButton.closest(".endpoint");
  const resultBox = endpoint.querySelector("[data-result]");
  const path = endpoint.dataset.endpoint;
  const method = endpoint.dataset.method;
  if (!readStoredToken()) {
    showManualLogin("请先登录，再执行操作。");
    return;
  }
  const inputs = [...endpoint.querySelectorAll(".param-input")];
  const params = {};
  inputs.forEach((input) => {
    params[input.dataset.paramName] = input.value.trim();
  });
  const clarifyForm = endpoint.querySelector(".clarify-form");
  let requestBody = null;
  if (clarifyForm) {
    const answers = {};
    clarifyForm.querySelectorAll("[data-clarify-key]").forEach((input) => {
      const value = input.value.trim();
      if (value) answers[input.dataset.clarifyKey] = value;
    });
    requestBody = { answers };
  } else {
    const bodyForm = endpoint.querySelector(".body-form");
    if (bodyForm) {
    try {
      requestBody = readBodyFields(bodyForm);
    } catch {
      resultBox.hidden = false;
      resultBox.className = "try-result error";
      resultBox.innerHTML = `<pre class="code-block">${esc(
        "有 JSON 格式的字段填写不正确，请检查后再试",
      )}</pre>`;
      return;
    }
    }
  }
  tryButton.disabled = true;
  try {
    const url = buildTryUrl(path, params);
    const headers = {};
    const token = readStoredToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    if (requestBody !== null) headers["Content-Type"] = "application/json";
    const response = await fetch(url, {
      method,
      headers,
      body: requestBody === null ? undefined : JSON.stringify(requestBody),
    });
    const text = await response.text();
    let pretty = text;
    try {
      pretty = JSON.stringify(translateResult(JSON.parse(text)), null, 2);
    } catch {
      // keep plain text responses
    }
    const resultTitle = response.ok ? "操作成功" : "操作失败";
    resultBox.hidden = false;
    resultBox.className = `try-result ${response.ok ? "success" : "error"}`;
    resultBox.innerHTML = `
      <pre class="code-block">${esc(`${resultTitle}（${response.status}）\n\n${pretty}`)}</pre>`;
  } catch (error) {
    resultBox.hidden = false;
    resultBox.className = "try-result error";
    resultBox.innerHTML = `<pre class="code-block">${esc(error.message)}</pre>`;
  } finally {
    tryButton.disabled = false;
  }
}

function translateResult(value, key = "") {
  if (Array.isArray(value)) {
    return value.map((item) => translateResult(item, key));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([childKey, childValue]) => [
        fieldLabel(childKey),
        translateResult(childValue, childKey),
      ]),
    );
  }
  if (typeof value === "string") {
    return VALUE_LABELS[value] || value;
  }
  return value;
}

function buildTryUrl(path, params) {
  let pathWithValues = path;
  const query = new URLSearchParams();
  Object.entries(params).forEach(([name, value]) => {
    const marker = `{${name}}`;
    if (pathWithValues.includes(marker)) {
      pathWithValues = pathWithValues.replace(marker, encodeURIComponent(value || ""));
    } else if (value) {
      query.set(name, value);
    }
  });
  const qs = query.toString();
  return `${pathWithValues}${qs ? `?${qs}` : ""}`;
}

function schemaText(schema) {
  const resolved = resolveRef(schema || {});
  const rawType =
    resolved.type || resolved.anyOf?.[0]?.type || resolved.$ref?.split("/").pop() || "any";
  const type = String(rawType).replace("_", " ");
  return TYPE_LABELS[type] || resolved.title || type;
}

function resolveRef(schema, seen = new Set()) {
  if (!schema || typeof schema !== "object") return schema;
  if (schema.$ref) {
    const name = schema.$ref.split("/").pop();
    if (seen.has(name)) return { $ref: schema.$ref };
    seen.add(name);
    const target = openapi?.components?.schemas?.[name];
    if (target) {
      const copy = structuredClone(target);
      copy.title = copy.title || name;
      return resolveRef(copy, seen);
    }
    return schema;
  }
  if (Array.isArray(schema)) return schema.map((item) => resolveRef(item, seen));
  return Object.fromEntries(
    Object.entries(schema).map(([key, value]) => [key, resolveRef(value, seen)]),
  );
}

function displayTag(tag) {
  const labels = {
    auth: "认证与用户",
    tenants: "租户管理",
    projects: "项目与连接",
    connections: "数据连接与探查",
    pipelines: "管道生成",
    approvals: "审批",
    preparations: "执行准备",
    "execution-runs": "执行记录",
    knowledge: "知识库",
    benchmark: "效果评测",
    departments: "部门管理",
    permissions: "权限管理",
    general: "通用",
    health: "健康检查",
    default: "通用",
  };
  return labels[tag] || tag || labels.default;
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
