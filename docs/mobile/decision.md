# Android 前端方案

**决定：新建 React + Vite 手机前端（方案 2）。**首批只覆盖设备配对/连接、agent 会话列表与对话/任务进度和取消、设置。后续再按优先级增加观察池、候选、概览、个股。

现有 16 个 Jinja 页面大多确实是静态外壳，动态信息通过 JSON API 读取；没有发现必须在模板中注入的业务数据。页面上下文主要是标题、导航选中项、声明文本，以及个股 ticker、候选 ID、模板 ID。`/app/stocks/{ticker}`、`/app/candidates/{id}`、`/app/templates/{id}` 可以由静态前端按路径参数解析，不构成复用阻碍。现有 `/app` CSP 限制为同源脚本/样式且不允许内联脚本或样式；Tauri 包可以沿用这个约束，HTTP 由 Rust 侧插件发出，不靠 WebView CORS。

但现有实现不是可组合的 UI 组件库：约 10,146 行 ES 模块直接绑定每页 DOM，agent 依赖多块桌面工作台，settings 是密集表单。若复用，需要维护静态模板/路由、隔离桌面会话逻辑、接入插件 HTTP，并替换 agent SSE 的直接 fetch；`methods.js` 和 watchlist 还有独立请求入口。这个改动已经超出“改造量小”，而首批页面需要手机竖屏、触控和键盘优先的布局。新前端能把传输集中到一个模块，并直接保留现有 API 语义。Web UI 源文件和运行路径保持独立。

视觉使用 `web/static/app.css` 的设计 token：系统深浅色、蓝色主信息、橙色警示、紫色 AI 色、中文字体栈和间距/圆角变量；移动端重新排版，不复制桌面三栏。路径参数由 React Router/客户端路由读取，模板中的免责声明由手机壳固定呈现。开发版允许仅本地网关联调所需的明文 HTTP；发布配置只允许 HTTPS。

**存储与传输约定：**设备凭证放入 Tauri Store 插件写入的应用私有数据目录；不放 localStorage、不输出日志。`plugin-http` 在 Rust 侧执行请求并提供响应流；该选择已经核实支持 Android 与响应 `ReadableStream`。配对页手动输入网关、服务 ID、`zgd_…` 凭证，验证后保存。`plugin-notification` 与 `plugin-opener` 按工程规范安装；发现服务目录仅作为后续增强。
