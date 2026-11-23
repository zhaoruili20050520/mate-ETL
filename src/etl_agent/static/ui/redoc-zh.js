/**
 * ReDoc 界面中文化。
 *
 * ReDoc 组件自身的内置文案（菜单、请求示例、数据结构、属性表头等）是英文，
 * 后端只负责 OpenAPI 内容中文化。本脚本在 ReDoc 渲染完成后，对界面元素的
 * 文本节点做简体中文替换，并监听后续渲染（切换接口、展开属性）继续替换。
 *
 * 为避免误改示例数据，脚本会跳过 <pre>、<code> 等代码块内的内容。
 */
(function () {
  "use strict";

  const EXACT_TEXT = {
    // 顶部与导航
    Menu: "目录",
    Search: "搜索",
    "Go to": "跳转到",
    "Back to": "返回",
    "Powered by ReDoc": "由 ReDoc 提供技术支持",
    // 加载与操作
    Loading: "加载中",
    "Loading...": "加载中…",
    "Download OpenAPI specification": "下载接口规范文件",
    Download: "下载",
    Copy: "复制",
    Copied: "已复制",
    "Copied!": "已复制",
    Expand: "展开",
    Collapse: "收起",
    "Expand all": "全部展开",
    "Collapse all": "全部收起",
    Close: "关闭",
    Authorize: "填写访问令牌",
    Authorization: "授权",
    // 接口区块
    Intro: "接口说明",
    "Request samples": "请求示例",
    "Response samples": "响应示例",
    "Callback payload": "回调载荷",
    Request: "请求",
    Response: "响应",
    "Request body": "请求体",
    "Request Schema": "请求数据结构",
    "Response Schema": "响应数据结构",
    "Request Body schema:": "请求体结构：",
    "Response Schema:": "响应结构：",
    Payload: "请求内容",
    "Content type": "内容类型",
    Schema: "数据结构",
    "Example Value": "示例值",
    Parameters: "请求参数",
    Responses: "响应说明",
    // 属性表
    Properties: "属性",
    "Additional properties": "其他属性",
    "additional property": "其他属性",
    "property name": "字段名",
    "property name*": "字段名*",
    "Array of": "数组，元素为",
    "[ items": "[ 元素",
    "Show child attributes": "展开下级属性",
    "Hide child attributes": "收起下级属性",
    "All of": "同时满足以下结构",
    "Any of": "满足任意一种",
    "One of": "满足其中一种",
    Required: "必填",
    Optional: "选填",
    Deprecated: "已废弃",
    "Read-only": "只读",
    "Write-only": "只写",
    Nullable: "可为空",
    Enum: "可选值",
    Values: "取值",
    Default: "默认值",
    Example: "示例",
    Minimum: "最小值",
    Maximum: "最大值",
    "Min Length": "最短长度",
    "Max Length": "最长长度",
    Pattern: "匹配规则",
    Format: "格式",
    "Multiple of": "取值倍数",
    "Unique items": "元素不重复",
    "Min Items": "最少元素个数",
    "Max Items": "最多元素个数",
    Discriminator: "区分字段",
    // 响应与安全
    "Status code": "状态码",
    "Media type": "内容类型",
    Value: "取值",
    "Security Scheme Type": "认证方式",
    "HTTP Authorization Scheme": "HTTP 认证方案",
    "Authorization URL": "授权地址",
    "Token URL": "令牌地址",
    "Refresh URL": "刷新令牌地址",
    Scopes: "授权范围",
    "Client ID": "客户端编号",
    "Client Secret": "客户端密钥",
    // 概览
    Contact: "联系方式",
    License: "许可协议",
    "Terms of Service": "服务条款",
    Version: "版本",
    Servers: "服务地址",
    Server: "服务地址",
    // 数据类型
    string: "文本",
    strings: "文本",
    integer: "整数",
    integers: "整数",
    number: "数字",
    numbers: "数字",
    boolean: "布尔值",
    booleans: "布尔值",
    array: "数组",
    arrays: "数组",
    object: "对象",
    objects: "对象",
    null: "空值",
    any: "任意类型",
    file: "文件",
    files: "文件",
    "date-time": "日期时间",
    date: "日期",
    uuid: "唯一标识",
    // 内容类型
    "application/json": "JSON 数据",
    "application/xml": "XML 数据",
    "application/x-www-form-urlencoded": "表单数据",
    "multipart/form-data": "多部分表单数据",
    "text/plain": "纯文本",
    "text/html": "网页文本",
    "application/octet-stream": "二进制数据",
    // 参数位置、必填标记与认证方式
    path: "路径",
    query: "查询",
    header: "请求头",
    cookie: "Cookie",
    required: "必填",
    optional: "选填",
    default: "默认值",
    "API Key": "API Key 认证",
    OAuth2: "OAuth2 认证",
    "OpenID Connect": "OpenID Connect 认证",
    // 其他界面文案
    Info: "接口说明",
    Operation: "操作接口",
    Examples: "示例",
    "Default response": "默认响应",
    "Callback payload samples": "回调请求示例",
    "Callback responses": "回调响应",
    "Client Error": "客户端错误",
    "Server Error": "服务端错误",
    "No results found": "没有匹配结果",
    "No sample": "暂无示例",
    "Not implemented": "暂不支持",
    "Schema not provided": "未提供数据结构",
    "See more": "展开更多",
    "See less": "收起",
    "Show pattern": "显示匹配规则",
    "Hide pattern": "隐藏匹配规则",
    "Something went wrong...": "出错了，请刷新页面重试…",
    "Theme setting": "主题设置",
    Webhook: "Webhook 回调",
    "ReDoc Version": "ReDoc 版本",
    URL: "地址",
    "Loading ...": "加载中…",
    // 兜底说明
    "No description": "暂无说明",
    "No description.": "暂无说明",
    "Nothing to see here": "暂无内容",
    "API docs by Redocly": "接口文档由 Redocly 提供",
    "expand error": "展开错误信息",
    // 授权弹窗
    "Available authorizations": "可用的授权方式",
    "Authorizations:": "授权方式：",
    "Authorizations": "授权方式",
    "Value:": "令牌内容：",
    Logout: "退出授权",
    "Show Password": "显示密码",
    "Hide Password": "隐藏密码",
    "Show password": "显示密码",
    "Hide password": "隐藏密码",
    "HTTP Bearer": "HTTP Bearer 认证",
    "requires authentication": "需要登录后才能调用",
    "Required scopes": "所需授权范围",
    Open: "打开",
    // 区块与无障碍文案
    Callbacks: "回调",
    "Callback": "回调",
    Links: "相关链接",
    Headers: "请求头",
    "Response Headers": "响应头",
    "Path Parameters": "路径参数",
    "Query Parameters": "查询参数",
    "Open sidebar": "打开目录",
    "Close sidebar": "关闭目录",
    "Skip to content": "跳到正文",
    "Scroll to top": "回到顶部",
  };

  const TEXT_RULES = [
    [/^Search(\.{3}|…)?$/i, "搜索…"],
    [/^Show (\d+) propert(?:y|ies)$/, "展开 $1 个属性"],
    [/^Show (\d+) more\.\.\.$/, "再显示 $1 项…"],
    [/^(\d+) propert(?:y|ies)$/, "$1 个属性"],
    [/^Expand (\d+) propert(?:y|ies)$/, "展开 $1 个属性"],
    [/^Collapse (\d+) propert(?:y|ies)$/, "收起 $1 个属性"],
    [/^Show all properties$/, "展开全部属性"],
    [/^\[\s*(\d+)\s*\.\.\s*(\d+)\s*\]\s*characters?$/i, "[ $1 .. $2 ] 个字符"],
    [/^\[\s*(\d+)\s*\.\.\s*\]\s*characters?$/i, "[ $1 .. ] 个字符"],
    [/^(\d+)\s*characters?$/i, "$1 个字符"],
    [/^Default:\s*(.*)$/i, "默认值：$1"],
    [/^Example:\s*(.*)$/i, "示例：$1"],
    [/^Enum:\s*(.*)$/i, "可选值：$1"],
    [/^Pattern:\s*(.*)$/i, "匹配规则：$1"],
    [/^Minimum:\s*(.*)$/i, "最小值：$1"],
    [/^Maximum:\s*(.*)$/i, "最大值：$1"],
    [/^Format:\s*(.*)$/i, "格式：$1"],
    [/^<=?\s*(\d+)\s*characters?$/i, "不超过 $1 个字符"],
    [/^>=\s*(\d+)\s*characters?$/i, "不少于 $1 个字符"],
    [/^\[\s*(\d+)\s*\.\.\s*(\d+)\s*\]\s*items?$/i, "$1 至 $2 项"],
    [/^\[\s*\.\.\s*(\d+)\s*\]\s*items?$/i, "不超过 $1 项"],
    [/^\[\s*items?$/i, "[ 元素"],
    [/^\[\s*(\d+)\s*\.\.\s*(\d+)\s*\]$/i, "$1 至 $2"],
    [/^\[\s*\.\.\s*(\d+)\s*\]$/i, "不超过 $1"],
    [/^\[\s*(\d+)\s*\.\.\s*\]$/i, "不少于 $1"],
    [/^>=\s*(-?\d+)$/i, "不小于 $1"],
    [/^<=\s*(-?\d+)$/i, "不大于 $1"],
    [/^Array of (.+?)\s*\(objects\)\s*or\s*(.+?)\s*\(null\)$/i, "$1 列表（可为空）"],
    [/^Array of (.+?)\s*\(null\)\s*or\s*(.+?)\s*\(objects\)$/i, "$2 列表（可为空）"],
    [/^Array of (.+?)\s*\(objects\)$/i, "$1 列表"],
    [/^Array of (.+?)\s*\(null\)$/i, "$1 列表（可为空）"],
    [/^Array of (.+)$/i, "$1 列表"],
    [/^(.+?)\s*\(objects\)\s*or\s*(.+?)\s*\(null\)$/i, "$1 或空值"],
    [/^(.+?)\s*\(null\)\s*or\s*(.+?)\s*\(objects\)$/i, "$2 或空值"],
    [/^(.+?)\s*\(objects\)$/i, "$1"],
    [/^(.+?)\s*\(null\)$/i, "$1（可为空）"],
    [/^(null|objects)\s+or\s+(null|objects)$/i, "可为空"],
    [/^(\d+)\s*objects?$/i, "$1 个字段"],
    [/^objects?$/i, "个字段"],
    [/^Expand (.+)$/i, "展开 $1"],
    [/^Collapse (.+)$/i, "收起 $1"],
    [/\s+or\s+/i, " 或 "],
  ];

  // 类型名对照：用于把「创建人 (string) or 创建人（可为空）」这类组合类型中文名化
  const TYPE_WORDS = {
    string: "文本",
    strings: "文本",
    integer: "整数",
    integers: "整数",
    number: "数字",
    numbers: "数字",
    boolean: "布尔值",
    booleans: "布尔值",
    array: "数组",
    arrays: "数组",
    object: "对象",
    objects: "对象",
    null: "空值",
    any: "任意类型",
    file: "文件",
    uuid: "唯一标识",
    "date-time": "日期时间",
  };

  function translateTypeName(trimmed) {
    const union = /^(.+?)\s*\(([A-Za-z][\w-]*)\)\s+or\s+(.+)$/.exec(trimmed);
    if (union) {
      const type = TYPE_WORDS[union[2].toLowerCase()];
      if (!type) return null;
      return `${union[1].trim()}（${type}）或 ${union[3].trim()}`;
    }
    const single = /^(.+?)\s*\(([A-Za-z][\w-]*)\)$/.exec(trimmed);
    if (single) {
      const type = TYPE_WORDS[single[2].toLowerCase()];
      if (!type) return null;
      return `${single[1].trim()}（${type}）`;
    }
    return null;
  }

  const LOWER_TEXT = {};
  Object.keys(EXACT_TEXT).forEach((key) => {
    const lower = key.toLowerCase();
    if (!Object.prototype.hasOwnProperty.call(LOWER_TEXT, lower)) {
      LOWER_TEXT[lower] = EXACT_TEXT[key];
    }
  });

  const SKIP_TAGS = new Set(["PRE", "CODE", "SCRIPT", "STYLE", "TEXTAREA", "KBD"]);

  function insideCode(element) {
    for (let node = element; node; node = node.parentElement) {
      if (SKIP_TAGS.has(node.tagName)) return true;
      if (node.classList && (node.classList.contains("redoc-json") || node.classList.contains("token"))) {
        return true;
      }
      const role = node.getAttribute && node.getAttribute("data-role");
      if (role === "code" || role === "json") return true;
    }
    return false;
  }

  function translateTo(value) {
    const trimmed = String(value ?? "").trim();
    if (!trimmed) return null;
    if (Object.prototype.hasOwnProperty.call(EXACT_TEXT, trimmed)) {
      return EXACT_TEXT[trimmed];
    }
    const fallback = LOWER_TEXT[trimmed.toLowerCase()];
    if (fallback) return fallback;
    if (trimmed.endsWith(":") || trimmed.endsWith("：")) {
      const label = trimmed.slice(0, -1).trim();
      const mapped = EXACT_TEXT[label] || LOWER_TEXT[label.toLowerCase()];
      if (mapped) return `${mapped}：`;
    }
    const typed = translateTypeName(trimmed);
    if (typed) return typed;
    for (const [pattern, replacement] of TEXT_RULES) {
      if (pattern.test(trimmed)) return trimmed.replace(pattern, replacement);
    }
    return null;
  }

  function translate(value) {
    const text = String(value ?? "");
    const localized = translateTo(text);
    if (!localized) return text;
    const trimmed = text.trim();
    if (localized === trimmed) return text;
    return text.replace(trimmed, localized);
  }

  function translateNode(node) {
    const next = translate(node.nodeValue);
    if (next !== node.nodeValue) node.nodeValue = next;
  }

  function translateAttributes(root) {
    const selector = "[placeholder], [title], [aria-label], [alt]";
    root.querySelectorAll(selector).forEach((element) => {
      ["placeholder", "title", "aria-label", "alt"].forEach((name) => {
        const value = element.getAttribute(name);
        if (!value) return;
        const next = translate(value);
        if (next !== value) element.setAttribute(name, next);
      });
    });
  }

  function translateTree(root) {
    if (!root || root.nodeType !== 1) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      if (!node.parentElement || insideCode(node.parentElement)) return;
      translateNode(node);
    });
    translateAttributes(root);
  }

  let scheduled = false;
  function scheduleTranslate() {
    if (scheduled) return;
    scheduled = true;
    window.setTimeout(() => {
      scheduled = false;
      translateTree(document.body);
    }, 0);
  }

  function start() {
    translateTree(document.body);
    const observer = new MutationObserver(scheduleTranslate);
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    document.addEventListener("redoc-ready", scheduleTranslate);
    // ReDoc 采用懒渲染，滚动或切换接口时才挂载新节点，
    // 这里定时补扫若干次作为兜底，避免个别环境监听不到变更。
    let ticks = 0;
    const timer = window.setInterval(function () {
      scheduleTranslate();
      ticks += 1;
      if (ticks >= 40) window.clearInterval(timer);
    }, 500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
