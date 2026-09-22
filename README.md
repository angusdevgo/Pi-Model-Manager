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
| ① 厂商分组顺序 | **Pi 硬编码**：`/model` 选择器与 `pi --list-models` 都按 `a.provider.localeCompare(b.provider)` 字母序分组 | ❌ 不可控制（`models.json` 的厂商键顺序对 Pi 无效） |
| ② 厂商内部的模型顺序 | 自定义厂商 = `models.json` 的 `models` **数组顺序**；内置厂商 = Pi 原生目录顺序 | ✅ 完全可控（拖拽 → 保存 → 重启 Pi 即生效） |
| ③ 默认 / 当前模型置顶 | Pi 把「当前模型」放第 1、「默认模型」放第 2 | ✅ 工具侧同款置顶显示 |

- **⭐ 默认置顶（模型列表右上角开关）**：把 Pi 默认模型在列表中置顶显示，并打上琥珀色高亮与 `⭐` 手柄，**仅改变显示层，绝不改动保存顺序**（该行在置顶期间禁止拖放，避免下标错位）；点击右侧 `☆ 设默认` 切换默认模型后置顶会自动跟随。
- **⇅ Pi 字母序（服务商标题栏开关）**：一键把左侧厂商列切换成 Pi 的字母序视图（此时厂商拖拽会被锁定并提示），让两边观感完全一致；关闭后恢复您的自定义拖拽顺序。
- **🔍 顺序自检（顶部操作栏按钮）**：一键生成三节式核对报告 —— 厂商分组顺序差异、每个厂商内部的模型顺序是否已落盘、默认模型与禁用模型清单，并给出 `✅ / ⚠️` 结论，方便直接截图留档。
- **开关持久化**：以上两个显示开关保存在 `~/.pi/agent/model-manager-settings.json`，不污染 `models.json` / `settings.json`，重启工具后保持选择。

> 💡 一句话总结：**厂商内部的模型顺序，工具说了算；厂商之间的先后顺序，Pi 固定为字母序。** 若您确实需要 Pi 也按工具顺序排列厂商，只能修改 Pi 安装目录里的排序逻辑（补丁式做法，Pi 升级会被覆盖），本工具不擅自改动第三方包文件。

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
├── README.md           # 中英双语权威使用文档
└── .gitignore          # 运行态文件过滤配置
```

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
| ① Provider group order | **Hardcoded in Pi**: both the `/model` selector and `pi --list-models` group providers via `a.provider.localeCompare(b.provider)` (alphabetical) | ❌ Not controllable (`models.json` key order is ignored by Pi) |
| ② Model order inside a provider | Custom providers = the `models` **array order** in `models.json`; built-in providers = Pi's native catalog order | ✅ Fully controllable (drag → save → restart Pi) |
| ③ Default / current model hoisting | Pi puts the *current* model first and the *default* model second | ✅ Mirrored by the tool |

- **⭐ Pin Default (toggle in the model toolbar)**: hoists Pi's default model to the top of the list with an amber highlight and a `⭐` handle. It is **display-only and never mutates the saved order**; the pinned row is locked against drops while pinned (avoids index skew) and follows the model when you click `☆ Set default`.
- **⇅ Pi Alphabetical (toggle on the provider panel header)**: switches the provider list to Pi's alphabetical view (provider dragging is locked and explained in a tooltip). Turn it off to get your own drag order back.
- **🔍 Order Self-Check (button in the header actions)**: produces a three-section report — provider-grouping difference, whether each provider's internal model order is already persisted, plus the default model and disabled-model inventory — with `✅ / ⚠️` verdicts, ready to be screenshotted for your records.
- **Persisted preferences**: both display toggles live in `~/.pi/agent/model-manager-settings.json`, keeping `models.json` / `settings.json` untouched, and survive restarts of the tool.

> 💡 TL;DR: **the model order inside a provider is yours to control; the order between providers is fixed to alphabetical in Pi.** If you truly need Pi itself to follow the tool's provider order, that requires patching the sorting logic inside Pi's installation (overwritten on every Pi upgrade) — this tool never rewrites third-party package files on its own.

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
├── README.md           # Bilingual documentation
└── .gitignore          # Git ignore rules
```

---

## 🤝 Community & Support 

- **LINUX DO 社区**: https://linux.do
