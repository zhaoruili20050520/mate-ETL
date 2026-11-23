"""OpenAPI 文档中文化后处理。

控制台、操作手册与接口文档都面向中文用户，但 FastAPI 自动生成的 OpenAPI
文档默认使用英文（由函数名派生的摘要、英文分组名、由字段名派生的标题等）。
本模块对 ``/openapi.json`` 做统一中文化后处理，让 ``/manual``（操作手册）与
``/redoc``（接口文档）中展示的说明文字全部为简体中文。

设计原则：

* 只翻译“给人看”的说明文字（摘要、描述、分组、标题、响应说明、枚举说明）；
* 不翻译机器契约（URL 路径、字段名、枚举取值、格式），避免破坏接口调用；
* 词表集中在本模块，新增接口时补一行映射即可。
"""

from __future__ import annotations

import copy
from typing import Any

INFO_TITLE = "ETL-Agent 数据编排平台"
INFO_DESCRIPTION = (
    "ETL-Agent 是面向数据工程的智能数据编排与治理平台，提供租户与项目权限、"
    "数据连接、元数据探查、自然语言生成数据管道、审批与执行、知识库问答等能力。\n\n"
    "使用说明：\n\n"
    "1. 先调用「认证与用户 / 登录并获取访问令牌」获取访问令牌；\n"
    "2. 点击右上角「授权」按钮填入令牌，或在请求头自行添加 "
    "``Authorization: Bearer <访问令牌>``；\n"
    "3. 各接口按功能分组，参数与返回值的说明均为简体中文。"
)

OPERATION_NOTE = "\n\n说明：请求头需携带 ``Authorization: Bearer <访问令牌>``，响应格式为 JSON。"

TAG_TITLES: dict[str, str] = {
    "auth": "认证与用户",
    "tenants": "租户管理",
    "projects": "项目与连接",
    "connections": "数据连接与探查",
    "pipelines": "管道生成",
    "approvals": "审批",
    "preparations": "执行准备",
    "execution-runs": "执行记录",
    "knowledge": "知识库",
    "benchmark": "效果评测",
    "departments": "部门管理",
    "permissions": "权限管理",
    "general": "通用",
}

TAG_DESCRIPTIONS: dict[str, str] = {
    "auth": "注册、登录以及查询当前登录用户信息。",
    "tenants": "租户的创建、查询以及租户成员管理。",
    "projects": "项目的创建、成员管理，以及项目范围内的数据连接。",
    "connections": "数据连接的维护、连通性测试与元数据探查。",
    "pipelines": "根据自然语言需求生成数据管道、澄清补充信息并冻结版本。",
    "approvals": "对执行准备提交审批意见并查询审批记录。",
    "preparations": "创建执行准备，完成审批后提交执行。",
    "execution-runs": "查看执行记录、运行指标、质量报告、发布记录，并支持取消与回滚。",
    "knowledge": "知识文档、分块、知识库问答、常见问题、知识缺口与运营数据管理。",
    "benchmark": "查看生成效果与问答效果的评测报告。",
    "departments": "部门树的维护与查询。",
    "permissions": "权限码、角色权限与当前用户权限查询。",
    "general": "未归入具体功能的通用接口。",
}

RESPONSE_DESCRIPTIONS: dict[str, str] = {
    "200": "请求成功",
    "201": "创建成功",
    "202": "请求已受理，正在处理",
    "204": "操作成功，无返回内容",
    "400": "请求参数有误",
    "401": "未登录或访问令牌已失效",
    "403": "没有权限执行该操作",
    "404": "资源不存在",
    "409": "资源冲突或当前状态不允许该操作",
    "422": "参数校验失败",
    "500": "服务内部错误",
    "503": "依赖服务不可用，请稍后重试",
}

PARAMETER_LOCATIONS: dict[str, str] = {
    "path": "路径参数",
    "query": "查询参数",
    "header": "请求头参数",
    "cookie": "Cookie 参数",
}

HTTP_METHOD_TITLES: dict[str, str] = {
    "get": "查询",
    "post": "创建或提交",
    "put": "整体更新",
    "patch": "局部更新",
    "delete": "删除",
}

OPERATION_SUMMARIES: dict[str, str] = {
    # ---- 认证与用户 ----
    "register_api_v1_auth_register_post": "注册新用户",
    "login_api_v1_auth_login_post": "登录并获取访问令牌",
    "me_api_v1_auth_me_get": "查询当前登录用户",
    # ---- 租户管理 ----
    "create_tenant_api_v1_tenants_post": "创建租户",
    "list_tenants_api_v1_tenants_get": "查询我所属的租户列表",
    "get_tenant_api_v1_tenants__tenant_id__get": "查询租户详情",
    "add_tenant_member_api_v1_tenants__tenant_id__members_post": "添加租户成员",
    "list_tenant_members_api_v1_tenants__tenant_id__members_get": "查询租户成员列表",
    "remove_tenant_member_api_v1_tenants__tenant_id__members__user_id__delete": "移除租户成员",
    # ---- 项目与连接 ----
    "create_project_api_v1_projects_post": "创建项目",
    "list_projects_api_v1_projects_get": "查询项目列表",
    "get_project_api_v1_projects__project_id__get": "查询项目详情",
    "update_project_api_v1_projects__project_id__patch": "更新项目信息",
    "add_project_member_api_v1_projects__project_id__members_post": "添加项目成员",
    "list_project_members_api_v1_projects__project_id__members_get": "查询项目成员列表",
    "remove_project_member_api_v1_projects__project_id__members__user_id__delete": "移除项目成员",
    "list_connections_api_v1_projects__project_id__connections_get": "查询项目下的数据连接",
    "create_connection_api_v1_projects__project_id__connections_post": "在项目下创建数据连接",
    "get_connection_api_v1_projects__project_id__connections__connection_id__get": (
        "查询数据连接详情"
    ),
    "update_connection_api_v1_projects__project_id__connections__connection_id__patch": (
        "更新数据连接"
    ),
    "delete_connection_api_v1_projects__project_id__connections__connection_id__delete": (
        "删除数据连接"
    ),
    "test_connection_api_v1_projects__project_id__connections__connection_id__test_post": (
        "测试数据连接连通性"
    ),
    "list_profiles_api_v1_projects__project_id__profiles_get": "查询项目的元数据画像",
    # ---- 数据连接与探查 ----
    "create_connection_api_v1_connections_post": "创建数据连接",
    "list_connections_api_v1_connections_get": "查询数据连接列表",
    "test_connection_api_v1_connections__connection_id__test_post": "测试数据连接连通性",
    "trigger_explore_api_v1_connections__connection_id__metadata_explore_post": "触发元数据探查",
    "list_metadata_profiles_api_v1_connections__connection_id__metadata_profiles_get": (
        "查询数据连接的元数据画像"
    ),
    "get_exploration_task_status_api_v1_connections__connection_id__metadata_tasks__task_id__get": (
        "查询元数据探查任务状态"
    ),
    # ---- 管道生成 ----
    "generate_pipeline_api_v1_pipelines_generate_post": "根据自然语言生成数据管道",
    "clarify_pipeline_api_v1_pipelines__pipeline_id__clarify_post": "提交澄清答案并继续生成",
    "freeze_version_api_v1_pipelines__pipeline_id__versions_post": "冻结管道版本",
    "list_versions_api_v1_pipelines__pipeline_id__versions_get": "查询管道版本列表",
    "list_pipelines_api_v1_pipelines_get": "查询管道列表",
    "get_pipeline_api_v1_pipelines__pipeline_id__get": "查询管道详情",
    "delete_pipeline_api_v1_pipelines__pipeline_id__delete": "删除管道",
    "get_version_api_v1_pipelines__pipeline_id__versions__version_id__get": "查询管道版本详情",
    # ---- 审批 ----
    "submit_approval_api_v1_preparations__preparation_id__approvals_post": "提交审批意见",
    "list_approvals_api_v1_preparations__preparation_id__approvals_get": "查询审批记录",
    # ---- 执行准备 ----
    "create_preparation_api_v1_preparations_post": "创建执行准备",
    "get_preparation_api_v1_preparations__preparation_id__get": "查询执行准备详情",
    "commit_preparation_api_v1_preparations__preparation_id__commit_post": "提交执行准备",
    # ---- 执行记录 ----
    "list_execution_runs_api_v1_execution_runs_get": "查询执行记录列表",
    "get_execution_run_api_v1_execution_runs__run_id__get": "查询执行记录详情",
    "get_run_metrics_api_v1_execution_runs__run_id__metrics_get": "查询执行运行指标",
    "get_run_events_api_v1_execution_runs__run_id__events_get": "查询执行事件记录",
    "get_quality_report_api_v1_execution_runs__run_id__quality_report_get": "查询数据质量报告",
    "get_publish_attempt_api_v1_execution_runs__run_id__publish_attempt_get": "查询数据发布记录",
    "cancel_execution_run_api_v1_execution_runs__run_id__cancel_post": "取消执行任务",
    "rollback_execution_run_api_v1_execution_runs__run_id__rollback_post": "回滚执行任务",
    # ---- 知识库 ----
    "get_knowledge_access_api_v1_knowledge_access_get": "查询知识库访问范围",
    "seed_demo_api_v1_knowledge_demo_seed_post": "载入知识库示例数据",
    "create_document_api_v1_knowledge_documents_post": "新增知识文档",
    "list_documents_api_v1_knowledge_documents_get": "查询知识文档列表",
    "create_documents_batch_api_v1_knowledge_documents_batch_post": "批量新增知识文档",
    "get_document_api_v1_knowledge_documents__doc_id__get": "查询知识文档详情",
    "delete_doc_api_v1_knowledge_documents__doc_id__delete": "删除知识文档",
    "publish_doc_api_v1_knowledge_documents__doc_id__publish_post": "发布知识文档",
    "archive_doc_api_v1_knowledge_documents__doc_id__archive_post": "归档知识文档",
    "update_chunk_endpoint_api_v1_knowledge_chunks__chunk_id__put": "修改文档分块",
    "delete_chunk_endpoint_api_v1_knowledge_chunks__chunk_id__delete": "删除文档分块",
    "ask_question_api_v1_knowledge_ask_post": "向知识库提问",
    "trigger_clustering_api_v1_knowledge_cluster_post": "生成常见问题候选",
    "list_faq_candidates_api_v1_knowledge_faqs_candidates_get": "查询常见问题候选列表",
    "delete_faq_candidate_endpoint_api_v1_knowledge_faqs_candidates__candidate_id__delete": (
        "删除常见问题候选"
    ),
    "publish_faq_candidate_api_v1_knowledge_faqs__candidate_id__publish_post": "发布常见问题候选",
    "reject_faq_candidate_api_v1_knowledge_faqs__candidate_id__reject_post": "驳回常见问题候选",
    "list_published_faqs_api_v1_knowledge_faqs_published_get": "查询已发布常见问题",
    "delete_published_faq_endpoint_api_v1_knowledge_faqs_published__faq_id__delete": (
        "删除已发布常见问题"
    ),
    "list_knowledge_gaps_api_v1_knowledge_gaps_get": "查询知识缺口列表",
    "delete_knowledge_gap_api_v1_knowledge_gaps__gap_id__delete": "删除知识缺口",
    "resolve_knowledge_gap_api_v1_knowledge_gaps__gap_id__resolve_post": "标记知识缺口已解决",
    "list_qa_audits_api_v1_knowledge_audits_get": "查询问答审计记录",
    "delete_qa_audit_api_v1_knowledge_audits__audit_id__delete": "删除问答审计记录",
    "get_metrics_api_v1_knowledge_metrics_get": "查询知识库运营数据",
    "get_department_tree_api_v1_knowledge_departments_tree_get": "查询部门树",
    "create_department_endpoint_api_v1_knowledge_departments_post": "创建部门",
    "update_department_endpoint_api_v1_knowledge_departments__department_id__patch": "更新部门",
    "delete_department_endpoint_api_v1_knowledge_departments__department_id__delete": "删除部门",
    "get_role_permissions_api_v1_knowledge_role_permissions_get": "查询角色权限",
    "put_role_permissions_api_v1_knowledge_role_permissions_put": "设置角色权限",
    "list_members_api_v1_knowledge_members_get": "查询知识库成员",
    "assign_member_api_v1_knowledge_members_post": "设置知识库成员",
    # ---- 效果评测 ----
    "get_benchmark_report_api_v1_benchmark__get": "查询评测报告",
    "get_benchmark_text_api_v1_benchmark_text_get": "查询评测报告文本",
    # ---- 部门管理 ----
    "list_depts_api_v1_departments_get": "查询部门列表",
    "create_dept_api_v1_departments_post": "创建部门",
    "get_tree_api_v1_departments_tree_get": "查询部门树",
    "update_dept_api_v1_departments__dept_id__put": "更新部门",
    "delete_dept_api_v1_departments__dept_id__delete": "删除部门",
    # ---- 权限管理 ----
    "list_codes_api_v1_permissions_codes_get": "查询权限码清单",
    "get_role_perms_api_v1_permissions_roles__role_name__get": "查询角色权限",
    "assign_perms_api_v1_permissions_roles__role_name__post": "设置角色权限",
    "get_my_permissions_api_v1_permissions_me_get": "查询我的权限",
    # ---- 通用 ----
    "health_check_health_get": "服务健康检查",
}

SCHEMA_TITLES: dict[str, str] = {
    # ---- 认证与用户 ----
    "RegisterRequest": "注册请求",
    "RegisterResponse": "注册结果",
    "LoginRequest": "登录请求",
    "TokenResponse": "访问令牌信息",
    "CurrentUserResponse": "当前登录用户信息",
    "UserPermissionsResponse": "我的权限",
    # ---- 租户与项目 ----
    "TenantCreate": "创建租户请求",
    "TenantResponse": "租户信息",
    "TenantMemberResponse": "租户成员信息",
    "AddTenantMemberRequest": "添加租户成员请求",
    "ProjectCreate": "创建项目请求",
    "ProjectUpdateRequest": "更新项目请求",
    "ProjectResponse": "项目信息",
    "ProjectMemberResponse": "项目成员信息",
    "AddProjectMemberRequest": "添加项目成员请求",
    # ---- 数据连接与探查 ----
    "DataSourceType": "数据源类型",
    "DataSourceConnectionCreate": "创建数据源连接请求",
    "DataSourceConnectionResponse": "数据连接信息",
    "ConnectionCreateRequest": "创建数据连接请求",
    "ConnectionUpdateRequest": "更新数据连接请求",
    "ConnectionTestResponse": "数据连接测试结果",
    "ExploreRequest": "元数据探查请求",
    "ExploreResponse": "元数据探查结果",
    "ExplorationTaskResponse": "元数据探查任务",
    "MetadataProfileResponse": "元数据画像",
    # ---- 管道生成 ----
    "GeneratePipelineRequest": "生成数据管道请求",
    "GeneratePipelineResponse": "生成数据管道结果",
    "ClarifyRequest": "补充澄清信息请求",
    "ClarifyResponse": "管道生成结果",
    "FreezeVersionRequest": "冻结管道版本请求",
    "PipelineSummaryResponse": "管道摘要",
    "PipelineDetailResponse": "管道详情",
    "PipelineVersionResponse": "管道版本",
    "EtlPlan": "数据同步方案",
    "SourceConfig": "源端配置",
    "TargetConfig": "目标端配置",
    "FieldMapping": "字段映射",
    "CleaningRule": "清洗规则",
    "QualityContract": "数据质量规则",
    "SyncStrategy": "同步策略",
    "SyncMode": "同步方式",
    "DataClassification": "数据分级",
    # ---- 审批与执行准备 ----
    "ApprovalRole": "审批角色",
    "ApprovalDecision": "审批决定",
    "CreateApprovalRequest": "提交审批请求",
    "ApprovalResponse": "审批记录",
    "CreatePreparationRequest": "创建执行准备请求",
    "PreparationResponse": "执行准备详情",
    "CommitResponse": "提交执行准备结果",
    # ---- 执行记录 ----
    "ExecutionRunResponse": "执行记录",
    "RunDetailResponse": "执行记录详情",
    "ExecutionEventResponse": "执行事件记录",
    "MetricSnapshotResponse": "指标快照",
    "MetricsListResponse": "指标列表",
    "OperationMetricsResponse": "运营指标",
    "QualityReportResponse": "数据质量报告",
    "ValidationIssue": "校验问题",
    "IssueSeverity": "问题严重程度",
    "PublishAttemptResponse": "数据发布记录",
    "CancelRunRequest": "取消执行任务请求",
    "CancelRunResponse": "取消执行任务结果",
    "RollbackRunRequest": "回滚执行任务请求",
    "RollbackRunResponse": "回滚执行任务结果",
    # ---- 知识库 ----
    "KnowledgeAccessResponse": "知识库访问范围",
    "KnowledgeRole": "知识库角色",
    "DocumentCreateRequest": "新增知识文档请求",
    "DocumentBatchCreateRequest": "批量新增知识文档请求",
    "DocumentBatchResponse": "批量新增知识文档结果",
    "DocumentResponse": "知识文档详情",
    "DocumentListItem": "知识文档列表项",
    "ChunkResponse": "文档分块",
    "ChunkUpdateRequest": "修改文档分块请求",
    "AskRequest": "知识库提问请求",
    "AskResponse": "知识库提问结果",
    "CandidateResponse": "常见问题候选",
    "PublishCandidateRequest": "发布常见问题候选请求",
    "PublishedFaqResponse": "已发布常见问题",
    "GapResponse": "知识缺口",
    "ResolveGapRequest": "标记知识缺口已解决请求",
    "AuditResponse": "问答审计记录",
    "KnowledgeMemberRequest": "设置知识库成员请求",
    "KnowledgeMemberResponse": "知识库成员信息",
    # ---- 部门与权限 ----
    "DepartmentCreateRequest": "创建部门请求",
    "DepartmentUpdateRequest": "更新部门请求",
    "DepartmentTreeNode": "部门树节点",
    "Role": "角色",
    "PermissionRule": "权限规则",
    "PermissionCodeListResponse": "权限码清单",
    "RolePermissionRequest": "设置角色权限请求",
    "AssignPermissionsRequest": "分配角色权限请求",
    "etl_agent__schemas__department__DepartmentResponse": "部门信息（部门管理）",
    "etl_agent__schemas__knowledge__DepartmentResponse": "部门信息（知识库）",
    "etl_agent__schemas__knowledge__RolePermissionResponse": "角色权限（知识库）",
    "etl_agent__schemas__permission__RolePermissionResponse": "角色权限（权限管理）",
    # ---- 分页与校验 ----
    "PageResponse_DataSourceConnectionResponse_": "分页结果（数据连接信息）",
    "PageResponse_DocumentListItem_": "分页结果（知识文档列表项）",
    "PageResponse_ExecutionEventResponse_": "分页结果（执行事件记录）",
    "PageResponse_ExecutionRunResponse_": "分页结果（执行记录）",
    "PageResponse_MetadataProfileResponse_": "分页结果（元数据画像）",
    "PageResponse_PipelineSummaryResponse_": "分页结果（管道摘要）",
    "PageResponse_PipelineVersionResponse_": "分页结果（管道版本）",
    "PageResponse_ProjectMemberResponse_": "分页结果（项目成员信息）",
    "PageResponse_ProjectResponse_": "分页结果（项目信息）",
    "PageResponse_TenantMemberResponse_": "分页结果（租户成员信息）",
    "PageResponse_TenantResponse_": "分页结果（租户信息）",
    "HTTPValidationError": "参数校验失败",
    "ValidationError": "参数校验错误项",
}

FIELD_TITLES: dict[str, str] = {
    "access_count": "访问次数",
    "access_token": "访问令牌",
    "allowed_chunk_ids": "允许查看的分块编号",
    "allowed_chunks": "允许查看的分块",
    "answer": "回答",
    "answers": "回答内容",
    "approval_role": "审批角色",
    "approver_id": "审批人",
    "audit_id": "审计记录编号",
    "autofix_attempts": "自动修复次数",
    "average_latency_ms": "平均耗时（毫秒）",
    "backup_table": "备份表",
    "batch_size": "批次大小",
    "blocked_access_count": "权限拦截次数",
    "blocked_chunk_ids": "被拦截的分块编号",
    "blocked_chunks": "被拦截的分块",
    "bytes_read": "读取字节数",
    "bytes_written": "写入字节数",
    "cancelled": "是否已取消",
    "capability_id": "授权编号",
    "category": "分类",
    "children": "下级部门",
    "chunk_count": "分块数",
    "chunk_index": "分块序号",
    "chunks": "文档分块",
    "clarification_questions": "需要确认的问题",
    "cleaning_rules": "清洗规则",
    "cluster_key": "聚类标识",
    "cluster_question": "聚合问题",
    "code": "代码",
    "codes": "代码列表",
    "columns_info": "字段信息",
    "comment": "备注",
    "committed_at": "提交时间",
    "committed_by": "提交人",
    "confidence": "置信度",
    "connection_id": "数据连接",
    "content": "内容",
    "created": "新增数量",
    "created_at": "创建时间",
    "created_by": "创建人",
    "ctx": "上下文",
    "daily": "每日统计",
    "data_classification": "数据分级",
    "database_name": "数据库名",
    "dead_letter_schema": "错误数据模式",
    "dead_letter_table": "错误数据表",
    "decision": "审批决定",
    "department": "部门",
    "departments": "允许部门",
    "description": "说明",
    "detail": "详情",
    "details": "详细信息",
    "document_id": "文档编号",
    "document_ids": "文档编号列表",
    "documents": "文档列表",
    "duration_seconds": "执行时长（秒）",
    "error": "错误",
    "error_message": "错误信息",
    "error_rate": "错误率",
    "error_rejection_rate": "错误拒绝率",
    "error_rows": "错误行数",
    "etl_plan": "数据同步方案",
    "event_type": "事件类型",
    "execution_budget": "执行预算",
    "execution_run_id": "执行记录",
    "expires_in": "有效时间（秒），0 表示永不过期",
    "faq_cache_hit": "是否命中常见问题缓存",
    "faq_hit": "是否命中常见问题",
    "faq_hit_count": "常见问题命中次数",
    "faq_hit_rate": "常见问题命中率",
    "field": "字段",
    "field_mappings": "字段映射",
    "finished_at": "完成时间",
    "frequency": "出现次数",
    "frozen_by": "冻结人",
    "gap_created": "是否记录知识缺口",
    "highest_similarity": "最高相似度",
    "hocon_config": "执行配置",
    "hocon_text": "执行配置内容",
    "host": "主机地址",
    "hot_knowledge": "热门知识",
    "id": "编号",
    "input": "输入位置",
    "input_fingerprint": "输入指纹",
    "is_multi_tenant": "是否多租户共享",
    "is_production_source": "是否生产数据源",
    "items": "列表",
    "knowledge_units": "知识单元",
    "last_login_at": "最近登录时间",
    "last_test_latency_ms": "最近测试耗时（毫秒）",
    "last_test_message": "最近测试结果",
    "last_test_status": "最近测试状态",
    "last_tested_at": "最近测试时间",
    "latency_ms": "耗时（毫秒）",
    "latest": "最新记录",
    "loc": "位置",
    "max_error_ratio": "最大错误率",
    "max_execution_seconds": "最长执行时间（秒）",
    "max_sample_rows": "最多采样行数",
    "max_tables": "最多探查表数",
    "message": "消息",
    "missing_permission": "是否缺少权限",
    "mode": "同步方式",
    "msg": "提示",
    "name": "名称",
    "natural_language": "需求描述",
    "nullable": "允许为空",
    "occurred_at": "发生时间",
    "open_gaps": "未解决的知识缺口",
    "output_amplification": "输出放大倍数",
    "page": "页码",
    "page_size": "每页数量",
    "parallelism": "并行数",
    "params": "参数",
    "parent_id": "上级部门编号",
    "password": "密码",
    "permission_code": "权限码",
    "permission_codes": "权限码列表",
    "permissions": "权限设置",
    "pipeline_id": "管道编号",
    "pipeline_version_id": "管道版本编号",
    "port": "端口",
    "preparation_id": "执行准备编号",
    "prepared_by": "创建人",
    "primary_keys": "主键字段",
    "profiled_at": "探查时间",
    "project_id": "项目编号",
    "proposed_faqs": "常见问题候选数",
    "publish_attempt": "发布记录",
    "published_documents": "已发布文档数",
    "quality_contract": "数据质量规则",
    "quality_report": "数据质量报告",
    "question": "问题",
    "read_rows": "读取行数",
    "reason": "原因",
    "recalled_chunk_ids": "召回的分块编号",
    "recommended_answer": "推荐答案",
    "related_chunk_ids": "关联分块编号",
    "resolved_gaps": "已解决的知识缺口",
    "resource_scope": "资源范围",
    "retry_times": "重试次数",
    "risk_level": "风险等级",
    "risk_reasoning": "风险说明",
    "role": "角色",
    "role_name": "角色名称",
    "roles": "允许角色",
    "rollback_plan": "回滚方案",
    "rollback_result": "回滚结果",
    "rule": "规则",
    "rule_results": "规则检查结果",
    "rules": "规则列表",
    "run_id": "执行记录编号",
    "sample_data": "样例数据",
    "sampled_at": "采样时间",
    "schema_name": "模式名",
    "seatunnel_job_id": "SeaTunnel 任务编号",
    "session_id": "会话编号",
    "severity": "严重程度",
    "sha256": "校验值",
    "shadow_rows": "影子表行数",
    "shadow_table": "影子表",
    "signed_at": "签字时间",
    "sort_order": "排序序号",
    "source": "源端配置",
    "source_connection_id": "源数据连接",
    "source_field": "源字段",
    "source_requirement": "原始需求",
    "source_table": "源数据表",
    "source_type": "源类型",
    "started_at": "开始时间",
    "stats_info": "统计信息",
    "status": "状态",
    "strategy": "处理策略",
    "success": "是否成功",
    "suggested_category": "建议分类",
    "suggestion": "处理建议",
    "sync_mode": "同步方式",
    "sync_strategy": "同步策略",
    "table_count": "数据表数量",
    "table_name": "数据表名",
    "target": "目标端配置",
    "target_connection_id": "目标数据连接",
    "target_field": "目标字段",
    "target_table": "目标数据表",
    "target_type": "目标类型",
    "task_id": "任务编号",
    "tenant_id": "租户编号",
    "title": "标题",
    "token_estimate": "预计令牌数",
    "token_type": "令牌类型",
    "top_questions": "高频问题",
    "total": "总数",
    "total_questions": "总问答数",
    "total_rows": "总行数",
    "total_tokens": "令牌总数",
    "transform": "转换方式",
    "type": "类型",
    "unique_users": "独立用户数",
    "updated_at": "更新时间",
    "user_id": "用户编号",
    "username": "账号",
    "users": "允许用户",
    "validation_issues": "校验问题",
    "validation_results": "校验结果",
    "value_distribution": "数据分布",
    "version_number": "版本号",
    "visibility": "可见范围",
    "watermark_column": "增量字段",
    "written_rows": "写入行数",
    # 路径参数与查询参数
    "candidate_id": "常见问题候选编号",
    "chunk_id": "分块编号",
    "days": "统计天数",
    "department_id": "部门编号",
    "dept_id": "部门编号",
    "doc_id": "文档编号",
    "faq_id": "常见问题编号",
    "gap_id": "知识缺口编号",
    "limit": "返回条数上限",
    "min_frequency": "最小出现次数",
    "offset": "跳过条数",
    "run": "运行序号",
    "version_id": "管道版本编号",
}

ENUM_TITLES: dict[str, str] = {
    # 角色
    "tenant_admin": "租户管理员",
    "project_admin": "项目管理员",
    "system_admin": "系统管理员",
    "knowledge_admin": "知识管理员",
    "engineer": "数据工程师",
    "auditor": "审计人员",
    "viewer": "普通用户",
    "approver": "审批人",
    "owner": "所有者",
    "admin": "管理员",
    "member": "成员",
    # 审批角色与决定
    "data_owner": "数据负责人",
    "security": "安全管理员",
    "data_checker": "数据复核人",
    "security_checker": "安全复核人",
    "approve": "同意",
    "reject": "驳回",
    # 通用状态
    "active": "正常",
    "inactive": "停用",
    "locked": "锁定",
    "draft": "草稿",
    "validated": "已校验",
    "frozen": "已冻结",
    "pending": "等待中",
    "running": "运行中",
    "quality_checking": "质量检查中",
    "publishing": "发布中",
    "succeeded": "成功",
    "failed": "失败",
    "rolled_back": "已回滚",
    "cancelled": "已取消",
    "approved": "已审批",
    "rejected": "已驳回",
    "proposed": "待审核",
    "open": "待处理",
    "in_progress": "处理中",
    "resolved": "已解决",
    "archived": "已归档",
    "published": "已发布",
    "submitted": "已提交",
    "committed": "已提交",
    # 数据源类型
    "postgresql": "PostgreSQL 数据库",
    "mysql": "MySQL 数据库",
    "doris": "Doris 数据库",
    "clickhouse": "ClickHouse 数据库",
    "oracle": "Oracle 数据库",
    # 同步方式与处理策略
    "full": "全量同步",
    "incremental": "增量同步",
    "overwrite": "覆盖写入",
    "append": "追加写入",
    "upsert": "更新或插入",
    # 数据分级与可见范围
    "public": "公开可见",
    "tenant": "本租户可见",
    "restricted": "指定人员可见",
    "internal": "内部使用",
    "confidential": "机密",
    # 严重程度
    "info": "提示",
    "warning": "警告",
    "error": "错误",
    "critical": "严重",
    "low": "低",
    "medium": "中",
    "high": "高",
    # 同步模式与清单策略
    "all": "全部同步",
    "include": "仅包含",
    "exclude": "仅排除",
    "merge": "合并",
    "replace": "替换",
}


SECURITY_SCHEME_NAME = "访问令牌"
SECURITY_SCHEME: dict[str, Any] = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "JWT",
    "description": "填入登录接口返回的访问令牌，粘贴令牌本身即可，无需添加 Bearer 前缀。",
}

# 无需登录即可调用的接口，不显示令牌锁标记。
PUBLIC_OPERATION_IDS = frozenset(
    {
        "register_api_v1_auth_register_post",
        "login_api_v1_auth_login_post",
        "health_check_health_get",
    }
)


def localize_openapi(spec: dict[str, Any]) -> dict[str, Any]:
    """返回中文化后的 OpenAPI 文档副本，不修改入参 ``spec``。"""
    localized = copy.deepcopy(spec)
    info = localized.setdefault("info", {})
    info["title"] = INFO_TITLE
    info["description"] = INFO_DESCRIPTION

    used_tags: list[str] = []
    for path_item in (localized.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.startswith("x-") or not isinstance(operation, dict):
                continue
            original_tag = _localize_operation(operation, method)
            if original_tag and original_tag not in used_tags:
                used_tags.append(original_tag)
        _localize_parameters(path_item.get("parameters"))

    localized["tags"] = [
        {
            "name": TAG_TITLES.get(tag, "通用"),
            "description": TAG_DESCRIPTIONS.get(tag, TAG_DESCRIPTIONS["general"]),
        }
        for tag in used_tags
    ]
    components = localized.setdefault("components", {})
    _localize_security(localized, components)
    _localize_components(components)
    _rename_schema_keys(components, localized)
    return localized


def _rename_schema_keys(components: Any, localized: dict[str, Any]) -> None:
    """把 ``components.schemas`` 的键名换成中文，并同步改写全部引用。

    ReDoc 会在字段类型后面用括号展示 ``$ref`` 指向的结构名，键名中文化之后
    文档里就不会再出现 ``(Role)`` 这类英文结构名。
    """
    if not isinstance(components, dict):
        return
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return

    mapping: dict[str, str] = {}
    used: dict[str, int] = {}
    for old_name in schemas:
        new_name = SCHEMA_TITLES.get(old_name) or old_name
        if new_name == old_name:
            continue
        count = used.get(new_name, 0) + 1
        used[new_name] = count
        mapping[old_name] = new_name if count == 1 else f"{new_name}{count}"
    if not mapping:
        return

    components["schemas"] = {mapping.get(name, name): schema for name, schema in schemas.items()}
    _rewrite_schema_refs(localized, mapping)


def _rewrite_schema_refs(node: Any, mapping: dict[str, str]) -> None:
    """递归改写 ``#/components/schemas/...`` 形式的引用名称。"""
    prefix = "#/components/schemas/"
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(prefix):
            target = ref[len(prefix) :]
            node["$ref"] = prefix + mapping.get(target, target)
        for value in node.values():
            _rewrite_schema_refs(value, mapping)
    elif isinstance(node, list):
        for item in node:
            _rewrite_schema_refs(item, mapping)


def _localize_security(localized: dict[str, Any], components: Any) -> None:
    """中文化认证方案名称与说明，并保持接口中的引用一致。"""
    if not isinstance(components, dict):
        return
    schemes = components.get("securitySchemes")
    if not isinstance(schemes, dict):
        schemes = {}
        components["securitySchemes"] = schemes
    if not schemes:
        schemes[SECURITY_SCHEME_NAME] = dict(SECURITY_SCHEME)
    else:
        renamed: dict[str, str] = {}
        for index, (name, scheme) in enumerate(list(schemes.items())):
            new_name = SECURITY_SCHEME_NAME if index == 0 else f"{SECURITY_SCHEME_NAME}{index + 1}"
            renamed[name] = new_name
            if isinstance(scheme, dict):
                scheme["description"] = SECURITY_SCHEME["description"]
                scheme.setdefault("bearerFormat", "JWT")
        for old_name, new_name in renamed.items():
            if old_name != new_name:
                schemes[new_name] = schemes.pop(old_name)
        _rename_security_references(localized, renamed)
    localized["security"] = [{next(iter(schemes)): []}]


def _rename_security_references(localized: dict[str, Any], renamed: dict[str, str]) -> None:
    """把接口定义中的认证方案引用同步改成中文名称。"""
    for path_item in (localized.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            security = operation.get("security")
            if not isinstance(security, list):
                continue
            operation["security"] = [
                {renamed.get(name, name): scopes for name, scopes in requirement.items()}
                if isinstance(requirement, dict)
                else requirement
                for requirement in security
            ]


def _localize_operation(operation: dict[str, Any], method: str) -> str:
    """中文化单个接口定义，返回其原始（英文）分组名。"""
    operation_id = str(operation.get("operationId") or "")
    tags = operation.get("tags") or []
    original_tag = str(tags[0]) if tags else "general"
    summary = OPERATION_SUMMARIES.get(operation_id) or _fallback_operation_summary(
        original_tag, method
    )
    operation["summary"] = summary
    operation["description"] = f"{summary}。{OPERATION_NOTE}"
    operation["tags"] = [TAG_TITLES.get(original_tag, TAG_TITLES["general"])]
    if operation_id in PUBLIC_OPERATION_IDS:
        operation["security"] = []
    _localize_parameters(operation.get("parameters"))

    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        request_body["description"] = "请求体（JSON 格式）"
        _localize_inline_contents(request_body.get("content"))
    for code, response in (operation.get("responses") or {}).items():
        if isinstance(response, dict):
            response["description"] = RESPONSE_DESCRIPTIONS.get(str(code), f"响应状态 {code}")
            _localize_inline_contents(response.get("content"))
    return original_tag


def _localize_inline_contents(content: Any) -> None:
    """中文化接口内联（未抽成公共结构）的请求体/响应体定义。"""
    if not isinstance(content, dict):
        return
    for media in content.values():
        if not isinstance(media, dict):
            continue
        schema = media.get("schema")
        if isinstance(schema, dict):
            _localize_inline_schema(schema, seen=set())


def _localize_inline_schema(node: Any, seen: set[int]) -> None:
    """递归中文化内联结构：标题、字段标题、字段说明与枚举说明。"""
    if not isinstance(node, dict) or id(node) in seen:
        return
    seen.add(id(node))
    title = node.get("title")
    if isinstance(title, str):
        zh_title = _inline_schema_title(title)
        if zh_title:
            node["title"] = zh_title
            title = zh_title
    if _is_english(node.get("description")):
        node["description"] = f"{title}。" if isinstance(title, str) else "接口数据结构。"
    _apply_enum_descriptions(node)

    properties = node.get("properties")
    if isinstance(properties, dict):
        for field_name, field_schema in properties.items():
            if not isinstance(field_schema, dict):
                continue
            label = FIELD_TITLES.get(field_name) or field_name
            field_schema["title"] = label
            field_schema["description"] = label
            _localize_inline_schema(field_schema, seen)

    for key in ("items", "additionalProperties", "allOf", "anyOf", "oneOf"):
        child = node.get(key)
        if isinstance(child, dict):
            _localize_inline_schema(child, seen)
        elif isinstance(child, list):
            for item in child:
                _localize_inline_schema(item, seen)


def _apply_enum_descriptions(node: dict[str, Any]) -> None:
    """为枚举补充中文说明（ReDoc 支持 ``x-enumDescriptions`` 扩展）。"""
    enum_values = node.get("enum")
    if isinstance(enum_values, list) and enum_values:
        node["x-enumDescriptions"] = [
            ENUM_TITLES.get(str(value), str(value)) for value in enum_values
        ]


def _inline_schema_title(title: str) -> str | None:
    """把 FastAPI 自动生成的内联结构标题（如 Response Xxx Get）转成中文。"""
    normalized = title.replace("_", " ").strip()
    if normalized == "Body" or normalized.startswith("Body "):
        return "请求体"
    if normalized.startswith("Response"):
        return "接口返回结果"
    return None


def _fallback_operation_summary(tag: str, method: str) -> str:
    """接口未登记中文摘要时的兜底文案。"""
    action = HTTP_METHOD_TITLES.get(method, "调用")
    return f"{action}{TAG_TITLES.get(tag, '接口')}"


def _localize_parameters(parameters: Any) -> None:
    if not isinstance(parameters, list):
        return
    for parameter in parameters:
        _localize_parameter(parameter)


def _localize_parameter(parameter: Any) -> None:
    if not isinstance(parameter, dict) or "$ref" in parameter:
        return
    name = str(parameter.get("name") or "")
    location = str(parameter.get("in") or "")
    label = FIELD_TITLES.get(name, name)
    prefix = PARAMETER_LOCATIONS.get(location, "参数")
    parameter["description"] = f"{prefix}：{label}"
    schema = parameter.get("schema")
    if isinstance(schema, dict):
        schema["title"] = label
        if "description" not in schema or _is_english(schema["description"]):
            schema["description"] = label


def _is_english(text: Any) -> bool:
    """判断说明文字是否仍为英文（不含中日韩字符且含英文字母）。"""
    if not isinstance(text, str):
        return False
    has_cjk = any("\u4e00" <= char <= "\u9fff" for char in text)
    return not has_cjk and any(char.isascii() and char.isalpha() for char in text)


def _localize_components(components: Any) -> None:
    if not isinstance(components, dict):
        return
    for name, schema in (components.get("schemas") or {}).items():
        _localize_schema(name, schema, seen=set())
    _localize_parameters(list((components.get("parameters") or {}).values()))


def _localize_schema(name: str, schema: Any, seen: set[int]) -> None:
    """中文化单个数据结构：标题、说明、字段标题、枚举说明。"""
    if not isinstance(schema, dict) or id(schema) in seen:
        return
    seen.add(id(schema))
    title = SCHEMA_TITLES.get(name) or name
    schema["title"] = title
    schema["description"] = f"{title}。"
    # ReDoc 展示结构名称时优先使用 x-displayName，保证这里也是中文。
    schema["x-displayName"] = title
    _localize_schema_body(schema, seen)


def _localize_schema_body(schema: dict[str, Any], seen: set[int]) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for field_name, field_schema in properties.items():
            if not isinstance(field_schema, dict):
                continue
            label = FIELD_TITLES.get(field_name)
            field_schema["title"] = label or field_name
            field_schema["description"] = label or f"字段：{field_name}"
            if id(field_schema) not in seen:
                seen.add(id(field_schema))
                _localize_schema_body(field_schema, seen)

    for key in ("items", "additionalProperties"):
        child = schema.get(key)
        if isinstance(child, dict) and id(child) not in seen:
            seen.add(id(child))
            _localize_schema_body(child, seen)

    for key in ("allOf", "anyOf", "oneOf"):
        for sub_schema in schema.get(key) or []:
            if isinstance(sub_schema, dict) and id(sub_schema) not in seen:
                seen.add(id(sub_schema))
                _localize_schema_body(sub_schema, seen)

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        schema["x-enumDescriptions"] = [
            ENUM_TITLES.get(str(value), str(value)) for value in enum_values
        ]


__all__ = [
    "ENUM_TITLES",
    "FIELD_TITLES",
    "INFO_DESCRIPTION",
    "INFO_TITLE",
    "OPERATION_SUMMARIES",
    "SCHEMA_TITLES",
    "TAG_TITLES",
    "localize_openapi",
]
