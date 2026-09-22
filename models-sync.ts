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

function inferModelSpecs(mid: string, model: Record<string, unknown>): { contextWindow: number; maxTokens: number; input: string[]; reasoning: boolean } {
  const target = mid.toLowerCase();
  let isVision = false;
  if (/gemini|claude|gpt-4o|chatgpt-4o|\bo[13]\b|vl|vision|omni|4v|visual|mimo/.test(target)) {
    isVision = true;
  }
  if (Array.isArray(model.input) && model.input.includes("image")) {
    isVision = true;
  }

  let isReasoning = false;
  if (/r1|reasoner|thinking|thought|qwq|zero|deepseek-v4|mimo/.test(target)) {
    isReasoning = true;
  } else if (/claude-3-7|claude-3\.7|\bo[13](-mini|-preview)?\b|flash-thinking|gemini-2\.5|gemini-3/.test(target)) {
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
    maxTokens = /3-7|3\.7|3-5-sonnet|3\.5-sonnet|sonnet-4/.test(target) ? 64000 : 8192;
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
    } else if (/r1|reasoner/.test(target)) {
      contextWindow = 128000;
      maxTokens = 65536;
      isReasoning = true;
    } else {
      contextWindow = 128000;
      maxTokens = 8192;
    }
  } else if (/\bo[13]\b/.test(target)) {
    contextWindow = 200000;
    maxTokens = 100000;
    isVision = true;
    isReasoning = true;
  } else if (/gpt-4o|chatgpt-4o|gpt-4\.5/.test(target)) {
    contextWindow = 128000;
    maxTokens = 16384;
    isVision = true;
  } else if (target.includes("qwen")) {
    contextWindow = /plus|max|1m/.test(target) ? 1000000 : 128000;
    maxTokens = 8192;
    if (/vl|omni|audio/.test(target)) isVision = true;
  } else if (target.includes("minimax") || target.includes("abab")) {
    contextWindow = 245760;
    maxTokens = 8192;
  } else if (target.includes("hy4") || target.includes("hunyuan")) {
    contextWindow = 256000;
    maxTokens = 16384;
  }

  if (/1m|1000k|1024k/.test(target)) contextWindow = 1048576;
  else if (/2m|2000k/.test(target)) contextWindow = 2000000;
  else if (/256k/.test(target)) contextWindow = 256000;
  else if (/200k/.test(target)) contextWindow = 200000;
  else if (/128k/.test(target)) contextWindow = 128000;

  if (typeof model.contextWindow === "number" && model.contextWindow > 0) contextWindow = model.contextWindow;
  if (typeof model.maxTokens === "number" && model.maxTokens > 0) maxTokens = model.maxTokens;

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
