# -*- coding: utf-8 -*-
"""端到端验证：真实 pi-ai 目录 + 勘误 + 存量刷新。"""
import sys, os, types, importlib.util

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
for m in ("webview", "yaml"):
    try:
        __import__(m)
    except ImportError:
        mm = types.ModuleType(m)
        if m == "webview":
            mm.start = lambda *a, **k: None
        else:
            mm.safe_load = lambda *a, **k: {}
            mm.safe_dump = lambda *a, **k: ""
            mm.YAMLError = Exception
        sys.modules[m] = mm

spec = importlib.util.spec_from_file_location("dapp", os.path.join(REPO, "src", "desktop_app.py"))
dapp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dapp)

catalog = dapp.load_builtin_catalog()
print("真实目录 provider 数: %d  模型数: %d" % (len(catalog), sum(len(p["models"]) for p in catalog.values())))
print()

tests = ["gpt-6-astra", "gpt-6.1-sol", "gpt-5.4", "gpt-5.4-mini", "glm-5.3", "glm-5.2",
         "qwen3.7-max", "qwen3.8-flash", "kimi-k3", "grok-4.5", "grok-4.20",
         "deepseek-v4-pro", "minimax-m3.1-flash", "hy4-preview", "mimo-v2.6-pro",
         "gemini-3-flash", "claude-sonnet-4-5", "openrouter:zai-org/glm-5.3"]
print("%-30s %10s %9s  %s" % ("model", "ctxWin", "maxTok", "来源"))
for mid in tests:
    s = dapp.infer_model_specs(mid, catalog=catalog)
    hit = dapp._catalog_model_lookup(catalog, mid)
    src = "catalog" if hit else "家族表"
    print("%-30s %10d %9d  %s" % (mid, s["contextWindow"], s["maxTokens"], src))

print()
print("存量模型刷新验证（旧的错误 128K 应被纠正）:")
legacy = [
    {"id": "glm-5.3", "contextWindow": 128000, "maxTokens": 8192},          # 历史默认痕迹 -> 官方 1M
    {"id": "qwen3.7-max", "contextWindow": 128000, "maxTokens": 8192},     # 同上
    {"id": "gpt-6-sol", "contextWindow": 272000, "maxTokens": 128000},     # 滞后目录值 -> 实测 1.05M
    {"id": "kimi-k3", "contextWindow": 128000, "maxTokens": 16384},
    {"id": "my-custom-model", "contextWindow": 99000, "maxTokens": 7000},  # 用户自定义 -> 不动
]
for m in legacy:
    before = (m.get("contextWindow"), m.get("maxTokens"))
    dapp.refresh_official_specs(m, catalog)
    after = (m.get("contextWindow"), m.get("maxTokens"))
    changed = "已刷新" if before != after else "保持不动"
    print("  %-22s %s -> %s   %s" % (m["id"], before, after, changed))
