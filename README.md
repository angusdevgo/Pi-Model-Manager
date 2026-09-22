#       <h1 align="center">⚡ Pi Model Manager</h1>

<p align="center">
  <a href="#-中文说明">中文说明</a> | <a href="#-english">English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Platform-Windows-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Platform">
  <img src="https://img.shields.io/badge/Pi--Agent-v0.84+-lightgrey?style=flat-square" alt="Pi Agent Version">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
</p>

---

<a id="-中文说明"></a>

## 🇨🇳 中文说明

### ⚡ 工具背景

在 [Pi-Coding-Agent](https://github.com/earendil-works/pi-coding-agent) 中引入自定义大模型服务商（如本地部署的 **Ollama**、**vLLM**、**LM Studio**，或者云端的 **DeepSeek**、**OpenRouter**、**MiniMax**、**SiliconFlow** 及各类 OpenAI / Claude / Gemini 兼容中转站）以往只能通过手动编辑 JSON 配置文件 (`models.json`)。这往往带来诸多痛点：

1. **格式脆弱易错**：手工编辑 JSON 时少写一个逗号或引号就会导致 Agent 启动失败。
2. **流程割裂且低效**：每次新增模型、换 Key 或调整端点，都需要查找字段、手写配置，并反复重启终端。
3. **连通性盲盒**：批量拉取数十个模型后，无法直观预知哪些可用、哪些由于网络限制或 Key 鉴权失败（如常见 401/403）无法调用。
4. **多密钥鉴权难题**：单厂商下的不同模型分组往往需要不同的 API Key（如 VIP Key、备用 Key），手动维护模型专属 Header 繁重且难以管理。
5. **静默启动与图标适配**：Windows 下运行 Python GUI 经常伴随黑框闪烁，快捷方式管理繁琐。

**Pi Model Manager** 是专为 Pi 设计的本地免安装轻量可视化工作台。它提供现代暗黑工业美学界面，全面打通服务商配置、**多 Key 密钥池自动调度**、**真实网络环境最小化测活**、**缓冲式模型拉取**、**模型快捷绑定/专属鉴权**、以及**无黑框静默启动**，让大模型配置与维护变得清晰、直观、可靠。

---

### ✨ 核心功能特性

#### 1. 🖥️ 现代化暗黑工业三栏工作台
- **纯本地极速运行**：基于 `pywebview` 构建，无需搭建本地 HTTP Web 服务，无网络依赖，启动即用。
- **三栏清晰视界**：左侧服务商列表（支持彩色协议徽章与实时搜索过滤）、中间厂商连接与密钥卡片、右侧模型管理与测活面板。
- **安全查看/编辑双模**：默认常态化数据查看模式，防止误改误触；点击「✏️ 编辑」唤起工业风编辑表单，支持 <kbd>Ctrl + S</kbd> 全局快捷保存。

#### 2. 🔑 灵活的双轨 API Key 与厂商密钥池 (Key Pool)
- **直接密钥 + 密钥池统一协同**：支持直接填写厂商主 API Key，更支持为当前厂商建立“密钥池”，按需录入多条具有唯一标识（ID）、备注别名（Name）和真实密钥（Secret）的 Key。
- **智能映射与双向回写**：主 Key 与密钥池条目自动互通，确保拉取、测活和模型绑定均有完整凭据保底。
- **多 Key 场景自由绑定**：模型支持绑定厂商主 Key、密钥池中的指定 Key，或指定模型专属独立 Key，满足复杂的额度分流与权限隔离需求。
- **密码明暗切换 (`👁️/🙈`)**：支持直观显隐真实密钥，告别复制核对时的隐患。

#### 3. ⚡ 全面真实网络测活 (Health Check)
- **单模型实时测活**：每个模型条目均配有 `⚡ 测活` 按钮，根据协议类型自动向远程发送最小化验证请求，即时反馈毫秒延迟与 HTTP 状态。
- **一键全量测活**：支持一键对当前服务商名下所有模型进行并发轮询测活，并输出通过/失败统计摘要。
- **拉取预览区同步测活**：在正式导入前，即可在拉取预览抽屉中一键预测试，排查不可用模型后再行导入。
- **多协议全链路适配**：深度适配 `openai-completions`、`openai-responses`、`anthropic-messages`、`google-generative-ai` 原生与中转协议，请求层已内建标准 User-Agent 及规范化 Authorization 头部注入。

#### 4. 🤖 缓冲式模型拉取与快捷管理
- **分段 Tab 视界**：在「📋 已配置模型」与「📥 拉取预览」之间无缝平滑切换，告别弹窗遮挡。
- **多 Key 弹性拉取**：在拥有多个 Key 的服务商下拉取远程模型时，弹出选择面板让您指定凭据进行拉取。
- **缓冲导入机制**：远程 `/models` 接口返回的模型先进入缓冲预览区，支持单选/全选批量导入，绝不强行覆盖既有配置。
- **即时别名修改**：模型 ID、显示别名支持在列表中一键点击就地重命名，失焦或回车自动写入。
- **一键设为默认**：点击模型旁的 `☆ 设默认` 即可一键更新全局 `settings.json` 的默认提供商与模型。

#### 5. 🪟 Windows 静默启动与快捷方式修复工装
- **无黑框后台静默启动**：内置 `launch.vbs` 和优化的 `PiModelManager.bat`，自动探测全局或环境变量中的 `pythonw.exe`，彻底告别 CMD 黑色控制台弹窗。
- **快捷方式一键自愈工装**：提供 `Fix-Shortcut.bat` 与 `fix_shortcut.ps1`，一键扫描桌面所有 Pi 相关快捷方式，自动将执行路径修正为静默启动脚本并配置独立 Python 图标。

#### 6. 🔄 配套热同步扩展 (models-sync.ts)
- 附带 TypeScript 编写的 Pi Agent 官方原生扩展，加载后常驻监听 `models.json` 变动，保存即自动热更新正在运行的 Pi 会话模型列表，**无需退出重启终端，也无需手动执行 `/reload`**。

#### 7. 🧭 顺序一致性（工具 ↔ Pi 透明化）
Pi 侧一共存在 **三种顺序**，本工具把每一种都讲清楚并提供对应开关，拒绝“拖了却没生效”的玄学：

| 顺序类型 | 谁说了算 | 工具是否可控制 |
| --- | --- | --- |
| ① 厂商分组顺序 | **Pi 硬编码**：`/model` 选择器、设置面板与 `pi --list-models` 都按 `a.provider.localeCompare(b.provider)` 字母序分组 | ⚠️ 默认不可控（`models.json` 的厂商键顺序对 Pi 无效）；可用下方 **🧩 顺序补丁** 让 Pi 跟随工具 |
| ② 厂商内部的模型顺序 | 自定义厂商 = `models.json` 的 `models` **数组顺序**；内置厂商 = Pi 原生目录顺序 | ✅ 完全可控（拖拽 → 保存 → 重启 Pi 即生效） |
| ③ 默认 / 当前模型置顶 | Pi 把「当前模型」放第 1、「默认模型」放第 2 | ✅ 工具侧同款置顶显示 |

- **⭐ 默认置顶（模型列表右上角开关）**：把 Pi 默认模型在列表中置顶显示，并打上琥珀色高亮与 `⭐` 手柄，**仅改变显示层，绝不改动保存顺序**（该行在置顶期间禁止拖放，避免下标错位）；点击右侧 `☆ 设默认` 切换默认模型后置顶会自动跟随。
- **⇅ Pi 字母序（服务商标题栏开关）**：一键把左侧厂商列切换成 Pi 的字母序视图（此时厂商拖拽会被锁定并提示），让两边观感完全一致；关闭后恢复您的自定义拖拽顺序。
- **🔍 顺序自检（顶部操作栏按钮）**：**左右两栏对照**，一眼看清差异 —— **左栏** = 工具窗口里的厂商顺序与模型名称，**右栏** = Pi 实际生效的分组结果，每一行右侧直接给 **✅ 一致 / ⚠️ 不一致**（错位行整行标黄）；顶部一行结论（如 `✅ 完全一致：工具窗口与 Pi 的厂商分组顺序、厂商内部模型顺序全部一致（6 厂商 / 16 模型）`），下方 6 行汇总（厂商分组顺序、厂商内部模型顺序、默认模型、禁用模型、Pi 排序补丁、工具自身体检）；左栏出现「未保存」徽标即表示该厂商的拖拽顺序还没写盘。左下角 **📋 复制全文** 可复制完整的六节文本报告（逐条含原因与修复指引）。弹窗限高 `88vh` + 独立滚动区，不会溢出屏幕。
- **🪟 长文弹窗显示完整性**：长文本弹窗统一为「容器限高 `88vh` + `overflow-y:auto` + `min-height:0`」，再也不会出现内容溢出屏幕、看不到顶部/底部（含底部按钮）的情况。
- **⚡ 启动性能**：补丁目标扫描加入**字面量预筛 + 窗口内精确校验**（不再在 4 MB 压缩包上整文件跑大正则），冷扫描 **4.6 s → 0.08 s**；启动时自动维护从 3 次接口调用合并为 1 次，启动关键链路合计 **≈0.3 s**。
- **🧩 顺序补丁（顶部操作栏按钮）**：让 **Pi 跟随工具的厂商顺序**，见下方第 8 节。
- **开关持久化**：以上两个显示开关保存在 `~/.pi/agent/model-manager-settings.json`，不污染 `models.json` / `settings.json`，重启工具后保持选择。

> 💡 一句话总结：**厂商内部的模型顺序，工具说了算；厂商之间的先后顺序，Pi 默认字母序 —— 想让它听工具的，打开「🧩 顺序补丁」。**

---

#### 8. 🧩 Pi 顺序补丁（让 Pi 的厂商分组顺序跟随工具）
默认情况下，Pi 的 `/model` 选择器按厂商 ID **字母序**分组，工具里的厂商拖拽顺序对它无效。点击顶部 **🧩 顺序补丁** 即可让 Pi 改为**严格按工具顺序**分组：

| 项目 | 说明 |
| --- | --- |
| 打补丁原理 | 把 Pi 中所有 `a.provider.localeCompare(b.provider)` 替换为读取顺序文件的排名比较，并注入一个约 1.6 KB 的运行时助手；**不改变任何业务逻辑**，仅在订单文件缺失/损坏时自动回退为 Pi 原生字母序 |
| 覆盖位置 | 自动扫描 Pi 的 `dist/` 目录按内容匹配（当前版本为 `bundle/chunks/chunk-*.js`、`cli/list-models.js`、`modes/interactive/components/model-selector.js`、`settings-selector.js`，共 6 处锚点），**不依赖固定文件名**，升级换 chunk 名也能重新命中 |
| 顺序来源 | `~/.pi/agent/pi-provider-order.json`，由工具在**每次保存后**自动刷新（内容 = 窗口里看到的厂商顺序；开启「⇅ Pi 字母序」时同步为字母序） |
| 生效时机 | 补丁后重启 Pi；顺序文件支持**热读取**（改了顺序只需重开 `/model` 选择器，无需重启 Pi） |
| 自动维护 | 开启后：① 工具每次 **💾 保存** 自动同步顺序文件并校验补丁；② 工具**启动时**检测到补丁缺失（如被 `npm i -g` 覆盖）自动重打。状态栏会提示结果 |
| 安全保障 | 修改前**字节级备份**原文件到 `~/.pi/agent/pi-order-patch-backup/`；写入采用临时文件 + 原子替换；每改一个文件都跑 `node --check` **语法门禁**，不过则**立即回滚**；「🩹 还原原版」可字节级完美还原（已验证 SHA256 一致） |
| 已知边界 | Pi 升级（`npm i -g @earendil-works/pi-coding-agent`）会覆盖 `dist`，补丁需重打 —— 已开启自动维护时会自动重打；若未来 Pi 改写了排序实现导致锚点失配，补丁会**安全失败并明确报错**，不会破坏安装 |
| 性能 | 扫描阶段用**字面量预筛 + 窗口内精确校验**（避开 4 MB 压缩包上的正则回溯）：冷扫描 **0.08 s**（优化前 4.6 s）；日常「保存 / 启动」只读 4 个目标文件 ≈ 0.1 s；仅在**真正需要重打补丁**时（如 Pi 刚升级）会有约 3~4 s 的正则替换开销，属一次性成本 |

> 🛡️ 本补丁只涉及本地单机显示顺序，不修改任何鉴权、订阅或网络请求逻辑；随时可一键还原。
>
> 🔬 **自检脚本**：`python tests/verify_order_patch.py` —— 在临时沙箱中跑完「打补丁 → 幂等复打 → 模拟升级重打 → 比较器行为 → 回退字母序 → 字节级还原 → 工具自身门禁 → 性能门禁 → 弹窗显示完整性 → 左右两栏对照（含错位/顺序文件过期/XSS 转义）」全流程（127 项断言），绝不触碰真实安装目录。
>
> 🧪 **工具自身门禁（防“界面空白”）**：工具内嵌的 JS 一旦有语法问题（例如源码里的 `\n` 被 Python 三引号提前解释成真换行，导致整个 `<script>` 解析失败、界面停在静态初始态），浏览器**不会给出可见报错**，非常难查。现在启动前会做两重体检：`scan_tool_js_escapes()`（转义体检）+ `node --check`（语法门禁），失败则**弹窗明确报错**并写入 stderr，不再静默失效；报告【6】随时可查。（历史上出现过一次该故障：界面显示空厂商列表 + 状态栏停在「● 就绪」——若您见到这个组合，请关闭并重开工具，然后看报告【6】。）

---

### 🚀 快速开始

#### 1. 环境依赖
本项目要求 Python 3.9+ 环境。首次使用请安装必要组件：
```bash
pip install pywebview
```

#### 2. 启动应用
* **推荐（静默启动）**：双击 `launch.vbs` 或运行 `Fix-Shortcut.bat` 生成桌面无黑框快捷方式后直接双击桌面图标启动。
* **命令行启动**：
  ```bash
  pythonw desktop_app.py
  # 或者带控制台调试输出：
  python desktop_app.py
  ```
* **自检提示**：若启动时弹出「Pi Model Manager 自检警告」，说明工具源码里内嵌的 JS 有语法问题（详见 §7 / §8 的自身门禁），弹窗会直接给出行号；控制台启动（`python desktop_app.py`）可在 stderr 同步看到。

---

### 🔄 免重启热同步扩展安装

若希望在桌面工具中点击 **💾 保存** 时，已打开的 Pi 终端立即生效新模型：

1. 将 `models-sync.ts` 复制到 Pi 的全局扩展目录：
   - **Windows 路径**：`%USERPROFILE%\.pi\agent\extensions\models-sync.ts`
2. 在已打开的 Pi 终端中输入一次 `/reload` 完成注册。
3. 此后桌面管理器任何保存动作（或 <kbd>Ctrl + S</kbd>）均会触发终端自动热重载，并弹出即时同步气泡提示。

---

### 📂 目录结构

```text
pi-model-manager/
├── desktop_app.py      # 桌面端主程序 (Pywebview GUI + 响应式前端)
├── models-sync.ts      # Pi Coding Agent 官方热重载扩展插件
├── launch.vbs          # Windows 静默无黑框后台启动脚本
├── PiModelManager.bat  # 智能探测路径启动批处理
├── Fix-Shortcut.bat    # 快捷方式一键修复引导入口
├── fix_shortcut.ps1    # PowerShell 快捷方式目标与图标重定向工装
├── tests/
│   └── verify_order_patch.py   # 🧩 顺序补丁全流程自检（沙箱运行，Pi 升级后可复验）
├── README.md           # 中英双语权威使用文档
└── .gitignore          # 运行态文件过滤配置
```

> 运行期文件（均在 `~/.pi/agent/`，不污染 Pi 配置）：`model-manager-settings.json`（工具开关）、
> `pi-provider-order.json`（补丁读取的厂商顺序）、`pi-order-patch-state.json`（补丁状态）、
> `pi-order-patch-backup/`（Pi 原文件字节级备份）。

---

<a id="-english"></a>

## 🌐 English

### ⚡ Background

Adding custom LLM providers (e.g., self-hosted **Ollama**, **vLLM**, **LM Studio**, or cloud providers like **DeepSeek**, **OpenRouter**, **MiniMax**, **SiliconFlow**, and various OpenAI/Claude/Gemini-compatible gateways) to [Pi-Coding-Agent](https://github.com/earendil-works/pi-coding-agent) previously required manual editing of `models.json`. This approach suffered from several limitations:

1. **Syntax Sensitivity**: A single misplaced comma or quote invalidates the JSON and halts the agent.
2. **Disconnected Workflow**: Adding models, updating endpoints, or switching keys required manual file editing followed by terminal restarts.
3. **Blind Availability**: After pulling dozens of models, there was no way to verify which endpoints were genuinely responsive or if credentials were valid (such as HTTP 401/403 errors).
4. **Credential Isolation**: Managing different API keys across different models under the same provider was cumbersome and error-prone.
5. **Console Windows on Windows**: Running Python GUI scripts on Windows frequently popped up intrusive command-prompt windows.

**Pi Model Manager** is a local, lightweight graphical workbench tailored for Pi. Featuring a high-contrast dark industrial interface, it integrates provider CRUD operations, a **multi-key pool manager**, **real-time connectivity testing**, **buffered model staging**, **granular key binding**, and **silent background execution**.

---

### ✨ Key Features

#### 1. 🖥️ Modern Dark Industrial 3-Column Workbench
- **100% Local & Fast**: Powered by `pywebview` without spinning up local HTTP servers or requiring a browser tab.
- **Three-Pane Layout**: Quick-filter provider sidebar on the left, connection settings in the center, and model management / health-check on the right.
- **View / Edit Modes**: Clean display mode by default to prevent accidental edits; unlocked edit mode with <kbd>Ctrl + S</kbd> global save.

#### 2. 🔑 Dual API Key Strategy & Key Pool
- **Direct Key & Key Pool Synergy**: Use a simple provider-level API key or maintain an extensive key pool with unique IDs, aliases, and secrets.
- **Bidirectional Fallback**: Guarantees valid authentication during remote model discovery, ping tests, and model runs.
- **Model-Level Key Assignment**: Bind models to the provider's default key, a designated key from the pool, or an isolated custom header.
- **Visibility Toggle (`👁️/🙈`)**: Easily view or mask secrets on demand.

#### 3. ⚡ Comprehensive Connectivity Testing (Health Check)
- **Per-Model Ping**: Dedicated `⚡ Test` button sends a minimal payload to test remote responsiveness, latency (ms), and HTTP status codes.
- **Batch Test**: Run full health checks across all models of a provider with success/failure statistics.
- **Pre-Import Verification**: Test endpoints inside the fetch preview before adding them to your permanent list.
- **Multi-Protocol Coverage**: Full support for `openai-completions`, `openai-responses`, `anthropic-messages`, and `google-generative-ai`, with normalized Bearer tokens and browser User-Agents.

#### 4. 🤖 Staged Model Discovery & Fast Management
- **Segmented Tabs**: Smoothly switch between configured models and remote fetch staging without visual interference.
- **Selective Import**: Remote models are loaded into a staging drawer where you can select, test, and alias models before importing.
- **Inline Renaming**: Double-click or click to edit model IDs and aliases on the fly.
- **Set as Default**: Set any model as the global default in `settings.json` with a single click.

#### 5. 🪟 Windows Silent Launch & Shortcut Repair Tooling
- **Console-Free Execution**: Bundled with `launch.vbs` and smart `PiModelManager.bat` to detect `pythonw.exe` and eliminate flashing command prompt windows.
- **Desktop Shortcut Fixer**: Run `Fix-Shortcut.bat` to scan and repoint all Pi desktop shortcuts to the silent launcher with a clean Python icon.

#### 6. 🔄 Live Hot-Reload Extension (models-sync.ts)
- Native TypeScript extension for Pi that monitors `models.json` changes in the background, updating active terminal sessions instantly without restarts or manual `/reload` calls.

#### 7. 🧭 Order Consistency (Tool ↔ Pi, Fully Transparent)
Pi exposes **three different orderings**; the tool documents each one and ships a switch for it, so a drag never silently fails again:

| Ordering | Source of truth | Controlled by tool? |
| --- | --- | --- |
| ① Provider group order | **Hardcoded in Pi**: the `/model` selector, the settings panel and `pi --list-models` all group providers via `a.provider.localeCompare(b.provider)` (alphabetical) | ⚠️ Not controllable by default (`models.json` key order is ignored by Pi); the **🧩 Order Patch** below makes Pi follow the tool instead |
| ② Model order inside a provider | Custom providers = the `models` **array order** in `models.json`; built-in providers = Pi's native catalog order | ✅ Fully controllable (drag → save → restart Pi) |
| ③ Default / current model hoisting | Pi puts the *current* model first and the *default* model second | ✅ Mirrored by the tool |

- **⭐ Pin Default (toggle in the model toolbar)**: hoists Pi's default model to the top of the list with an amber highlight and a `⭐` handle. It is **display-only and never mutates the saved order**; the pinned row is locked against drops while pinned (avoids index skew) and follows the model when you click `☆ Set default`.
- **⇅ Pi Alphabetical (toggle on the provider panel header)**: switches the provider list to Pi's alphabetical view (provider dragging is locked and explained in a tooltip). Turn it off to get your own drag order back.
- **🔍 Order Self-Check (button in the header actions)**: a **two-column side-by-side comparison** that makes divergences obvious — the **left column** is the provider order and model names in the tool window, the **right column** is what Pi actually applies, and every row carries an inline **✅ consistent / ⚠️ inconsistent** verdict (misaligned rows are highlighted). A one-line conclusion sits on top (e.g. `✅ 完全一致：……（6 厂商 / 6 模型）`), followed by six summary rows (provider grouping order, in-provider model order, default model, disabled models, Pi order patch, tool self-check); an unsaved drag shows a「未保存」chip in the left column. **📋 Copy all** in the bottom-left corner copies the full six-section text report (with the reason and the fix for each finding). The dialog is capped at `88vh` with its own scroll region, so it can never overflow the window.
- **🪟 Long-message dialogs never clip again**: every long-text dialog now uses «container capped at `88vh` + `overflow-y:auto` + `min-height:0`», so content can no longer overflow the screen and hide the top, the bottom or the footer buttons.
- **⚡ Startup performance**: the patch-target scan now uses a **literal pre-filter + small-window exact match** instead of running a backtracking regex across the whole 4 MB bundle — cold scan **4.6 s → 0.08 s** — and startup auto-maintenance collapsed from three API round-trips into one, bringing the whole startup chain to **≈0.3 s**.
- **🧩 Order Patch (button in the header actions)**: makes **Pi follow the tool's provider order** — see section 8 below.
- **Persisted preferences**: all display toggles live in `~/.pi/agent/model-manager-settings.json`, keeping `models.json` / `settings.json` untouched, and survive restarts of the tool.

> 💡 TL;DR: **the model order inside a provider is yours to control; the order between providers is alphabetical in Pi by default — turn on «🧩 Order Patch» to make it follow the tool.**

---

#### 8. 🧩 Pi Order Patch (make Pi's provider group order follow the tool)
By default Pi groups providers **alphabetically** by provider ID and ignores the tool's drag order. Click **🧩 Order Patch** in the header to make Pi group providers **strictly in the tool's order**:

| Item | Detail |
| --- | --- |
| How it works | Replaces every `a.provider.localeCompare(b.provider)` in Pi with a rank lookup backed by an order file, and injects a ~1.6 KB runtime helper. **No business logic is touched**, and a missing/corrupt order file falls back to Pi's native alphabetical ordering |
| Coverage | Content-based scan of Pi's `dist/` tree (today: `bundle/chunks/chunk-*.js`, `cli/list-models.js`, `modes/interactive/components/model-selector.js`, `settings-selector.js` — 6 anchors). **No hardcoded filenames**, so renamed chunks are still matched after upgrades |
| Order source | `~/.pi/agent/pi-provider-order.json`, refreshed by the tool **after every save** (it mirrors exactly what you see in the provider panel; enabling «⇅ Pi Alphabetical» mirrors alphabetical order) |
| When it applies | Restart Pi once after patching. The order file is read **hot**, so later reorderings only need re-opening the `/model` selector — no Pi restart |
| Auto-maintenance | Once enabled: ① every **💾 Save** in the tool re-syncs the order file and verifies the patch; ② tool startup detects a missing patch (e.g. wiped by `npm i -g`) and re-applies it. The status bar reports the outcome |
| Safety | **Byte-level backup** of every file to `~/.pi/agent/pi-order-patch-backup/`; writes are atomic (temp file + replace); every modified file must pass a `node --check` **syntax gate** or it is **rolled back immediately**; «🩹 Restore original» reverts byte-identically (SHA256-verified) |
| Known limits | Upgrading Pi (`npm i -g @earendil-works/pi-coding-agent`) overwrites `dist`, so the patch must be re-applied — auto-maintenance does this for you. If a future Pi rewrites its sorting code so the anchors no longer match, the patch **fails safely with a clear error** and never damages the installation |
| Performance | The scan uses a **literal pre-filter + small-window exact match** (avoiding regex backtracking over the 4 MB bundle): cold scan **0.08 s** (down from 4.6 s). Day-to-day «save / startup» only reads the four target files (≈0.1 s). The ~3–4 s regex substitution cost is paid **only when a patch genuinely has to be applied** (e.g. right after a Pi upgrade) — a one-off |

> 🛡️ The patch only affects local, single-machine display ordering. It modifies no authentication, subscription or network logic, and can be reverted at any time with one click.
>
> 🔬 **Self-test**: `python tests/verify_order_patch.py` — runs the whole «patch → idempotent re-patch → simulated upgrade re-patch → comparator behaviour → alphabetical fallback → byte-level restore → tool self-check → performance gate → dialog display integrity → two-column comparison (misalignment / stale order file / XSS escaping)» cycle (127 assertions) inside a throwaway sandbox; the real installation is never touched.
>
> 🧪 **Tool self-check gate (never go blank again)**: a syntax problem in the tool's embedded JS (for example a JS `\n` in the Python triple-quoted HTML string being turned into a real newline, which breaks the whole `<script>` block and freezes the UI in its static initial state) produces **no visible browser error**, making it very hard to diagnose. Startup now runs two audits — `scan_tool_js_escapes()` (escape audit) plus a `node --check` syntax gate — and **raises an explicit popup** (and writes to stderr) on failure instead of failing silently; report section 【6】 exposes the same verdicts at any time. (This failure mode did occur once: an empty provider list with the status bar still showing the initial «● 就绪» — if you see that combination, close and reopen the tool, then check report 【6】.)

---

### 🚀 Quick Start

#### 1. Requirements
Python 3.9+ is required. Install dependencies with:
```bash
pip install pywebview
```

#### 2. Launch
* **Recommended (Silent Launch)**: Double-click `launch.vbs` or run `Fix-Shortcut.bat` to create a clean desktop shortcut.
* **Via Command Line**:
  ```bash
  pythonw desktop_app.py
  # Or with console logging:
  python desktop_app.py
  ```
* **Self-check notice**: if a «Pi Model Manager 自检警告» popup appears at launch, the tool's embedded JS has a syntax problem (see the self-check gate in sections 7 / 8). The popup reports the offending line; launching with `python desktop_app.py` mirrors it on stderr.

---

### 🔄 Zero-Restart Live Sync Extension

To enable instant hot-reloading in running Pi sessions:

1. Copy `models-sync.ts` to Pi's global extension folder:
   - **Windows**: `%USERPROFILE%\.pi\agent\extensions\models-sync.ts`
2. Run `/reload` once in your active Pi terminal.
3. Every subsequent save in Pi Model Manager will immediately apply to active Pi sessions.

---

### 📂 File Structure

```text
pi-model-manager/
├── desktop_app.py      # Main Desktop Application (Pywebview + UI)
├── models-sync.ts      # Hot-reload extension for Pi Coding Agent
├── launch.vbs          # Silent background launcher (no CMD window)
├── PiModelManager.bat  # Smart environment detection launcher
├── Fix-Shortcut.bat    # Shortcut repair helper entrypoint
├── fix_shortcut.ps1    # PowerShell shortcut target & icon fixer
├── tests/
│   └── verify_order_patch.py   # 🧩 Order-patch end-to-end self-test (sandboxed, re-run after Pi upgrades)
├── README.md           # Bilingual documentation
└── .gitignore          # Git ignore rules
```

> Runtime files (all under `~/.pi/agent/`, never polluting Pi config): `model-manager-settings.json`
> (tool toggles), `pi-provider-order.json` (provider order read by the patch),
> `pi-order-patch-state.json` (patch state), `pi-order-patch-backup/` (byte-level backups of Pi originals).

---

## 🤝 Community & Support 

- **LINUX DO 社区**: https://linux.do
