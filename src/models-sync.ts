import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { existsSync, readFileSync, watch, type FSWatcher } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/**
 * 把 ~/.pi/agent/models.json 同步进当前 Pi 会话。
 *
 * 原因：
 * 1. /reload 不会重读 models.json
 * 2. 已打开的 Pi 也不会监听该文件
 * 3. 内置目录（如 deepseek）会把 models-store.json 里的远程模型叠到自定义列表上
 *
 * 做法：按 models.json 对每个服务商调用 registerProvider(id, { models })。
 * 带 models 的注册会整表替换该服务商模型，桌面工具里的列表就是当前会话的唯一来源。
 */
const AGENT_DIR = process.env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
const MODELS_PATH = join(AGENT_DIR, "models.json");

type ProviderConfig = {
  name?: string;
  baseUrl?: string;
  apiKey?: string;
  api?: string;
  headers?: Record<string, string>;
  compat?: Record<string, unknown>;
  authHeader?: boolean;
  models?: Array<Record<string, unknown>>;
  modelOverrides?: Record<string, unknown>;
};

function stripJsonComments(text: string): string {
  let result = "";
  let inString = false;
  let escaped = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    const next = text[i + 1];
    if (inString) {
      result += ch;
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') {
      inString = true;
      result += ch;
      continue;
    }
    if (ch === "/" && next === "/") {
      while (i < text.length && text[i] !== "\n") i++;
      continue;
    }
    if (ch === "/" && next === "*") {
      i += 2;
      while (i + 1 < text.length && !(text[i] === "*" && text[i + 1] === "/")) i++;
      i++;
      continue;
    }
    result += ch;
  }
  return result;
}

function readModelsFile(): { providers: Record<string, ProviderConfig> } {
  if (!existsSync(MODELS_PATH)) return { providers: {} };
  const raw = readFileSync(MODELS_PATH, "utf8");
  const parsed = JSON.parse(stripJsonComments(raw));
  const providers =
    parsed && typeof parsed === "object" && parsed.providers && typeof parsed.providers === "object"
      ? parsed.providers
      : {};
  return { providers };
}

const CATALOG_PREFIX_RE = /^(chat|image|classifier|embed|embedding|rerank|audio|video):/;

// 常见 vendor 别名归一（聚合站命名 -> pi-ai 目录命名）
const VENDOR_ALIAS: Record<string, string> = {
  "zai-org": "zai", zhipuai: "zai", "z-ai": "zai", zai: "zai",
  alibaba: "qwen", qwen: "qwen",
  "deepseek-ai": "deepseek", deepseek: "deepseek",
  "x-ai": "xai", spacexai: "xai", xai: "xai",
  moonshot: "moonshotai", moonshotai: "moonshotai",
  "minimax-ai": "minimax", minimaxai: "minimax", minimax: "minimax",
  "bytedance-seed": "bytedance", bytedance: "bytedance",
  tencent: "tencent",
};

function catalogMatchKeys(mid: string): string[] {
  let s = String(mid || "").toLowerCase().trim();
  let prev: string | null = null;
  while (prev !== s) {
    prev = s;
    s = s.replace(CATALOG_PREFIX_RE, "");
  }
  if (!s) return [];
  const keys = [s];
  if (s.includes("/")) {
    const idx = s.indexOf("/");
    const vendor = s.slice(0, idx);
    const rest = s.slice(idx + 1);
    if (rest) {
      keys.push(rest);
      const canon = VENDOR_ALIAS[vendor];
      if (canon) keys.push(`${canon}/${rest}`);
    }
  }
  return [...new Set(keys.filter(Boolean))];
}

function inferModelSpecs(mid: string, model: Record<string, unknown>): { contextWindow: number; maxTokens: number; input: string[]; reasoning: boolean } {
  // 用于匹配的规范化键：取剥前缀 / 去路由后的形式
  const keys = catalogMatchKeys(mid);
  const target = (keys[0] || mid).toLowerCase();

  // 上游直报数值最优先（models.json 中手工填写或 /v1/models 返回）
  const rawCw = model.context_length ?? model.context_window ?? model.max_context_tokens ?? model.contextWindow;
  const rawMt = model.max_tokens ?? model.max_output_tokens ?? model.maxTokens;
  const rawCtx = typeof rawCw === "number" && rawCw >= 4096 ? rawCw : null;
  const rawMax = typeof rawMt === "number" && rawMt >= 512 ? rawMt : null;

  let isVision = /gemini|claude|gpt-4o|chatgpt-4o|gpt-4\.1|\bo[134]\b|vl|vision|omni|4v|visual|mimo/.test(target);
  if (Array.isArray(model.input) && model.input.includes("image")) {
    isVision = true;
  }

  let isReasoning = /r1|reasoner|thinking|thought|qwq|zero|deepseek-v4|mimo|gpt-5|gpt-6/.test(target);
  if (/claude-3-7|claude-3\.7|\bo[134](-mini|-preview)?\b|flash-thinking|gemini-2\.5|gemini-3/.test(target)) {
    isReasoning = true;
  } else if (model.reasoning !== undefined) {
    isReasoning = Boolean(model.reasoning);
  }

  let contextWindow = 128000;
  let maxTokens = 16384;

  if (target.includes("gemini")) {
    contextWindow = target.includes("pro") && (target.includes("1.5") || target.includes("2.0") || target.includes("2.5")) ? 2000000 : 1048576;
    maxTokens = 65536;
    isVision = true;
  } else if (target.includes("claude")) {
    contextWindow = 200000;
    maxTokens = /3-7|3\.7|3-5-sonnet|3\.5-sonnet|sonnet-4|4-5|4\.5|4-6|4\.6|4-7|4\.7|4-8|4\.8|sonnet-5|opus-5|haiku-4|haiku-5|fable/.test(target) ? 64000 : 8192;
    isVision = true;
  } else if (target.includes("mimo")) {
    contextWindow = 1048576;
    maxTokens = 131072;
    isVision = true;
    isReasoning = true;
  } else if (target.includes("deepseek")) {
    if (target.includes("v4")) {
      contextWindow = 1000000;
      maxTokens = 384000;
      isVision = true;
      isReasoning = true;
    } else if (/v3[.\-]?2/.test(target)) {
      contextWindow = 131072;
      maxTokens = 65536;
    } else if (/v3[.\-]?1/.test(target)) {
      contextWindow = 163840;
      maxTokens = 128000;
    } else if (/r1|reasoner/.test(target)) {
      contextWindow = 128000;
      maxTokens = 65536;
      isReasoning = true;
    } else {
      contextWindow = 128000;
      maxTokens = 8192;
    }
  } else if (target.includes("gpt-oss")) {
    contextWindow = 131072;
    maxTokens = target.includes("120") ? 131072 : 8192;
  } else if (/gpt-?6/.test(target) || target.includes("gpt-5.4") || target.includes("gpt-5-4")) {
    // gpt-6/6.1 与 gpt-5.4 全系=1.05M（官方真实物理容量，输入上限 922K），5.4-mini/nano=400K
    if (target.includes("mini") || target.includes("nano")) {
      contextWindow = 400000;
    } else {
      contextWindow = 1050000;
    }
    maxTokens = 128000;
    isVision = true;
    isReasoning = true;
  } else if (target.includes("gpt-5")) {
    // gpt-5 / 5.1 / 5.2 / 5.3 全系=400K，codex-spark=128K
    if (target.includes("codex") && target.includes("spark")) {
      contextWindow = 128000;
      maxTokens = 32000;
    } else {
      contextWindow = 400000;
      maxTokens = 128000;
    }
    isVision = true;
    isReasoning = true;
  } else if (/\bo[134]\b/.test(target)) {
    contextWindow = 200000;
    maxTokens = 100000;
    isVision = true;
    isReasoning = true;
  } else if (/gpt-4[.\-]1/.test(target)) {
    contextWindow = 1047576;
    maxTokens = 32768;
    isVision = true;
  } else if (/gpt-4o|chatgpt-4o|gpt-4\.5/.test(target)) {
    contextWindow = 128000;
    maxTokens = 16384;
    isVision = true;
  } else if (target.includes("grok")) {
    if (target.includes("4.20") || target.includes("4-20") || target.includes("multi-agent")) {
      contextWindow = 2000000;
      maxTokens = 1800000;
    } else if (/grok-?4[.\-]?3\b/.test(target)) {
      contextWindow = 1000000;
      maxTokens = 30000;
    } else if (/grok-?4[.\-]?[12]\b/.test(target)) {
      contextWindow = 1000000;
      maxTokens = 131072;
    } else if (/grok-?4[.\-]?[4-9]\b/.test(target)) {
      contextWindow = 500000;
      maxTokens = 450000;
    } else if (target.includes("build")) {
      contextWindow = 256000;
      maxTokens = 230400;
    } else {
      contextWindow = 256000;
      maxTokens = 32768;
    }
    isReasoning = !target.includes("non-reasoning");
  } else if (target.includes("qwen")) {
    if (/plus|max|1m/.test(target)) {
      contextWindow = 1000000;
      maxTokens = target.includes("max") ? 131072 : 65536;
    } else if (target.includes("flash")) {
      contextWindow = 1000000;
      maxTokens = 65536;
    } else if (target.includes("coder")) {
      contextWindow = 262144;
      maxTokens = 65536;
    } else if (/qwen-?2[.\-]?5/.test(target)) {
      contextWindow = 32768;
      maxTokens = 16384;
    } else if (/qwen-?3-\d+b/.test(target)) {
      contextWindow = 40960;
      maxTokens = 16384;
    } else {
      contextWindow = 262144;
      maxTokens = 65536;
    }
    if (/vl|omni|audio/.test(target)) isVision = true;
  } else if (target.includes("glm")) {
    if (/glm-?5[.\-]?[2-9]/.test(target)) {
      contextWindow = 1000000;
      maxTokens = 131072;
    } else if (/glm-?5/.test(target)) {
      contextWindow = 200000;
      maxTokens = 131072;
    } else if (/glm-?4[.\-]?[67]v/.test(target)) {
      contextWindow = 128000;
      maxTokens = 32768;
      isVision = true;
    } else if (/glm-?4[.\-]?[67]/.test(target)) {
      contextWindow = 200000;
      maxTokens = 96000;
    } else if (/glm-?4[.\-]?5v/.test(target)) {
      contextWindow = 66000;
      maxTokens = 16000;
      isVision = true;
    } else if (/glm-?4[.\-]?5/.test(target)) {
      contextWindow = 128000;
      maxTokens = 96000;
    } else {
      contextWindow = 128000;
      maxTokens = 8192;
    }
    if (/4v|vl/.test(target) || /glm-?[45][.\-]?\d*v/.test(target)) isVision = true;
  } else if (target.includes("kimi") || target.includes("moonshot")) {
    if (/kimi-?k3/.test(target)) {
      contextWindow = 1000000;
      maxTokens = 131072;
    } else if (/kimi-?k2[.\-]?[67]/.test(target)) {
      contextWindow = 262144;
      maxTokens = /k2[.\-]?6/.test(target) ? 262144 : 32768;
    } else if (/kimi-?k2[.\-]?5/.test(target)) {
      contextWindow = 262144;
      maxTokens = 98304;
    } else {
      contextWindow = 131072;
      maxTokens = 131072;
    }
    if (/vl|vision/.test(target)) isVision = true;
  } else if (target.includes("minimax") || target.includes("abab")) {
    if (/m3[.\-]?1/.test(target)) {
      contextWindow = 1000000;
      maxTokens = 524288;
    } else if (/\bm3\b/.test(target)) {
      contextWindow = 512000;
      maxTokens = 512000;
    } else {
      contextWindow = 204800;
      maxTokens = 131072;
    }
  } else if (target.includes("hy4") || target.includes("hunyuan")) {
    if (target.includes("hy3")) {
      contextWindow = 262144;
      maxTokens = 128000;
    } else {
      contextWindow = 1048576;
      maxTokens = 64000;
    }
  } else if (/step-?5/.test(target)) {
    contextWindow = 1000000;
    maxTokens = 64000;
  } else if (/step-?3/.test(target)) {
    contextWindow = 256000;
    maxTokens = 64000;
  } else if (target.includes("nemotron")) {
    if (target.includes("3.5") || target.includes("ultra")) {
      contextWindow = 1000000;
      maxTokens = target.includes("super-vl") ? 1048576 : 65000;
    } else {
      contextWindow = 262144;
      maxTokens = 262144;
    }
  }

  // 关键词显式长度匹配（覆盖家族表，可纠正后缀标注）
  if (/(?<![0-9a-z.])1m(?![0-9a-z])/.test(target) || target.includes("1000k") || target.includes("1024k")) {
    contextWindow = 1048576;
  } else if (/(?<![0-9a-z.])2m(?![0-9a-z])/.test(target) || target.includes("2000k")) {
    contextWindow = 2000000;
  } else if (/256k/.test(target)) {
    contextWindow = 256000;
  } else if (/200k/.test(target)) {
    contextWindow = 200000;
  } else if (/128k/.test(target)) {
    contextWindow = 128000;
  }

  if (rawCtx !== null) contextWindow = rawCtx;
  if (rawMax !== null) maxTokens = rawMax;

  return {
    contextWindow,
    maxTokens,
    input: isVision ? ["text", "image"] : ["text"],
    reasoning: isReasoning,
  };
}

function normalizeModel(model: Record<string, unknown>, provider: ProviderConfig) {
  const id = String(model.id || "").trim();
  if (!id) return null;
  const name = typeof model.name === "string" && model.name.trim() ? model.name.trim() : id;
  const inferred = inferModelSpecs(id, model);
  const input = Array.isArray(model.input) && model.input.length ? model.input : inferred.input;
  return {
    ...model,
    id,
    name,
    api: (typeof model.api === "string" && model.api) || provider.api || "openai-completions",
    reasoning: model.reasoning !== undefined ? Boolean(model.reasoning) : inferred.reasoning,
    input,
    cost: model.cost ?? { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: model.contextWindow ?? inferred.contextWindow,
    maxTokens: model.maxTokens ?? inferred.maxTokens,
  };
}

function normalizeProvider(provider: ProviderConfig): ProviderConfig {
  const models = Array.isArray(provider.models)
    ? provider.models
        .filter((model) => model && typeof model === "object")
        .map((model) => normalizeModel(model, provider))
        .filter((model): model is NonNullable<typeof model> => model !== null)
    : [];
  const config: ProviderConfig = {
    ...provider,
    models,
  };
  if (!config.api) config.api = "openai-completions";
  return config;
}

export default function (pi: ExtensionAPI) {
  const managedIds = new Set<string>();
  let watcher: FSWatcher | undefined;
  let debounce: ReturnType<typeof setTimeout> | undefined;
  let applying = false;
  let lastStamp = "";

  function fileStamp(): string {
    try {
      const raw = readFileSync(MODELS_PATH, "utf8");
      return `${raw.length}:${raw.slice(0, 120)}:${raw.slice(-120)}`;
    } catch {
      return "";
    }
  }

  function registerFromFile(): { count: number; errors: string[] } {
    const { providers } = readModelsFile();
    const nextIds = new Set(Object.keys(providers));
    const errors: string[] = [];

    for (const id of [...managedIds]) {
      if (!nextIds.has(id)) {
        try {
          pi.unregisterProvider(id);
        } catch {
          // 内置服务商可能无法真正注销
        }
        managedIds.delete(id);
      }
    }

    for (const [id, provider] of Object.entries(providers)) {
      if (!provider || typeof provider !== "object") continue;
      try {
        pi.registerProvider(id, normalizeProvider(provider));
        managedIds.add(id);
      } catch (error) {
        errors.push(`${id}: ${error instanceof Error ? error.message : String(error)}`);
      }
    }

    lastStamp = fileStamp();
    return { count: nextIds.size, errors };
  }

  async function applyFromFile(ctx: ExtensionContext, notify: boolean) {
    if (applying) return;
    applying = true;
    try {
      const { count, errors } = registerFromFile();
      await ctx.modelRegistry.refresh({ allowNetwork: false });
      const runtimeError = ctx.modelRegistry.getError?.();
      if (runtimeError) {
        ctx.ui.notify(`models.json 已读取，但校验失败: ${runtimeError}`, "error");
        return;
      }
      if (errors.length > 0) {
        ctx.ui.notify(`部分服务商同步失败: ${errors[0]}`, "warning");
        return;
      }
      if (notify) {
        const available = ctx.modelRegistry.getAvailable().length;
        ctx.ui.notify(`已同步 models.json（${count} 个服务商 / ${available} 个可用模型）`, "info");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      ctx.ui.notify(`同步 models.json 失败: ${message}`, "error");
    } finally {
      applying = false;
    }
  }

  function startWatch(ctx: ExtensionContext) {
    if (watcher) {
      watcher.close();
      watcher = undefined;
    }
    if (!existsSync(AGENT_DIR)) return;
    lastStamp = fileStamp();
    watcher = watch(AGENT_DIR, (_eventType, filename) => {
      if (filename && String(filename) !== "models.json") return;
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => {
        const stamp = fileStamp();
        if (stamp === lastStamp) return;
        void applyFromFile(ctx, true);
      }, 400);
    });
  }

  // 工厂阶段先注册一次，保证 pi --list-models 和新会话启动时就按文件覆盖内置目录
  const boot = registerFromFile();
  console.log(`models-sync 扩展已加载，覆盖 ${boot.count} 个服务商`);

  pi.registerCommand("sync-models", {
    description: "Reload models.json into the current Pi session",
    handler: async (_args, ctx) => {
      await applyFromFile(ctx, true);
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    await applyFromFile(ctx, false);
    startWatch(ctx);
  });

  pi.on("session_shutdown", async () => {
    if (debounce) clearTimeout(debounce);
    debounce = undefined;
    watcher?.close();
    watcher = undefined;
  });
}
