# -*- coding: utf-8 -*-
"""infer_model_specs 回归测试。

用法:  python tests/test_infer_specs.py
覆盖:
  1. 家族规格表（无目录时的兜底推断，验证 1M 模型不再识别成 128K）
  2. 目录匹配（chat: 前缀剥离 / vendor 别名归一 / 日期快照后缀）
  3. 目录勘误（gpt-6/6.1、gpt-5.4 目录标 272K，实测 1.05M）
  4. raw_item 上游直报数值最高优先
  5. 关键词显式长度（1m / 128k 等，含 3.5-turbo-0125 边界）
"""
import os, sys, json, importlib.util, types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

# GUI / 重依赖 stub（测试只验证推断逻辑，不需要 pywebview / yaml）
for _mod in ("webview", "yaml"):
    try:
        __import__(_mod)
    except ImportError:
        _m = types.ModuleType(_mod)
        if _mod == "webview":
            _m.start = lambda *a, **k: None
        else:
            _m.safe_load = lambda *a, **k: {}
            _m.safe_dump = lambda *a, **k: ""
            _m.YAMLError = Exception
        sys.modules[_mod] = _m

spec = importlib.util.spec_from_file_location("dapp", os.path.join(REPO, "src", "desktop_app.py"))
dapp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dapp)

infer = dapp.infer_model_specs

PASS, FAIL = 0, 0
def check(mid, expect_cw, expect_mt=None, raw=None, catalog=None, note=""):
    global PASS, FAIL
    s = infer(mid, raw, catalog)
    ok = s["contextWindow"] == expect_cw and (expect_mt is None or s["maxTokens"] == expect_mt)
    if ok:
        PASS += 1
        print("  PASS  %-52s cw=%-9d mt=%-8d %s" % (mid, s["contextWindow"], s["maxTokens"], note))
    else:
        FAIL += 1
        print("  FAIL  %-52s cw=%-9d mt=%-8d (期望 cw=%s mt=%s) %s" % (
            mid, s["contextWindow"], s["maxTokens"], expect_cw, expect_mt, note))

# ---------- 1. 家族规格表（catalog=None 走兜底推断） ----------
print("[1] 家族规格表（无目录兜底）")
check("glm-5.3",            1000000, 131072, catalog={}, note="GLM 1M")
check("glm-5.2-highspeed",  1000000, 131072, catalog={}, note="GLM 1M")
check("glm-5.3-flash",      1000000, 131072, catalog={}, note="GLM 1M")
check("glm-5.1",             200000, 131072, catalog={}, note="GLM-5 200K")
check("glm-4.6",             200000,  96000, catalog={}, note="GLM-4.6 200K")
check("glm-4.5",             128000,  96000, catalog={}, note="GLM-4.5 128K")
check("qwen3.7-max",        1000000, 131072, catalog={}, note="Qwen max 1M")
check("qwen3.8-flash",      1000000,  65536, catalog={}, note="Qwen flash 1M")
check("qwen3.7-plus",       1000000,  65536, catalog={}, note="Qwen plus 1M")
check("qwen3.6-27b",         262144,  65536, catalog={}, note="Qwen 标准线 262K")
check("qwen3-coder",         262144,  65536, catalog={}, note="Qwen coder 262K")
check("qwen-2.5-72b-instruct", 32768, 16384, catalog={}, note="Qwen2.5 32K")
check("kimi-k3",            1000000, 131072, catalog={}, note="Kimi K3 1M")
check("kimi-k2.6",           262144, 262144, catalog={}, note="Kimi K2.6")
check("kimi-k2.5",           262144,  98304, catalog={}, note="Kimi K2.5")
check("gpt-6-astra",        1050000, 128000, catalog={}, note="GPT-6 实测 1.05M")
check("gpt-6-luna",         1050000, 128000, catalog={}, note="GPT-6 实测 1.05M")
check("gpt-6-sol",          1050000, 128000, catalog={}, note="GPT-6 实测 1.05M")
check("gpt-6.1-sol",        1050000, 128000, catalog={}, note="GPT-6.1 实测 1.05M")
check("gpt-5.4",            1050000, 128000, catalog={}, note="GPT-5.4 实测 1.05M")
check("gpt-5.4-pro",        1050000, 128000, catalog={}, note="GPT-5.4 pro 1.05M")
check("gpt-5.4-mini",        400000, 128000, catalog={}, note="GPT-5.4 mini 400K")
check("gpt-5.4-nano",        400000, 128000, catalog={}, note="GPT-5.4 nano 400K")
check("gpt-5.3-codex",       400000, 128000, catalog={}, note="GPT-5.3 codex 400K")
check("gpt-5.2",             400000, 128000, catalog={}, note="GPT-5.2 400K")
check("gpt-4.1",           1047576,  32768, catalog={}, note="GPT-4.1 1M")
check("grok-4.20",         2000000, 1800000, catalog={}, note="Grok 4.20 2M")
check("grok-4.3",          1000000,   30000, catalog={}, note="Grok 4.3 1M")
check("grok-4.5",           500000, 450000, catalog={}, note="Grok 4.5 500K")
check("grok-4.7",           500000, 450000, catalog={}, note="Grok 4.7 500K")
check("deepseek-v4-pro",   1000000, 384000, catalog={}, note="DS v4 1M")
check("deepseek-v3.2",      131072,  65536, catalog={}, note="DS v3.2 128K")
check("deepseek-r1",        128000,  65536, catalog={}, note="DS R1 128K")
check("minimax-m3.1-flash", 1000000, 524288, catalog={}, note="M3.1 1M")
check("minimax-m2.5",        204800, 131072, catalog={}, note="M2.5 204800")
check("hy4-preview",       1048576,  64000, catalog={}, note="HY4 1M")
check("mimo-v2.6-pro",     1048576, 131072, catalog={}, note="MiMo 1M")
check("gemini-3-flash",    1048576,  65536, catalog={}, note="Gemini 3 1Mi")
check("gemini-2.5-pro",    2000000,  65536, catalog={}, note="Gemini 2.5P 2M")
check("claude-sonnet-5",    200000,  64000, catalog={}, note="Claude 200K")

# 关键词显式长度
print("[2] 关键词显式长度")
check("some-model-1m",       1048576, catalog={}, note="1m 后缀")
check("some-model-1m-0902",  1048576, catalog={}, note="1m+日期后缀")
check("some-model-2m",       2000000, catalog={}, note="2m 后缀")
check("some-model-256k",      256000, catalog={}, note="256k 后缀")
check("gpt-3.5-turbo-0125",   128000, catalog={}, note="0125 不误判 1m/2m")
check("foo-31m-bar",          128000, catalog={}, note="31m 不误判 1m")

# raw_item 上游直报优先
print("[3] 上游直报数值优先")
check("qwen3.7-max", 555000, 12345, raw={"context_length": 555000, "max_tokens": 12345}, catalog={}, note="直报覆盖")

# ---------- 4. 目录匹配（模拟 pi-ai catalog 结构） ----------
print("[4] 目录匹配")
mini_catalog = {
    "openai": {"models": [
        {"id": "chat:gpt-6-sol", "contextWindow": 272000, "maxTokens": 128000},
        {"id": "chat:gpt-5.4", "contextWindow": 272000, "maxTokens": 128000},
    ]},
    "zai": {"models": [
        {"id": "chat:glm-5.3", "contextWindow": 1000000, "maxTokens": 131072, "input": ["text", "image"], "reasoning": False},
    ]},
    "moonshotai": {"models": [
        {"id": "chat:moonshotai/kimi-k3", "contextWindow": 1000000, "maxTokens": 131072},
    ]},
    "qwen": {"models": [
        {"id": "chat:qwen/qwen3.7-max", "contextWindow": 1000000, "maxTokens": 131072},
    ]},
}
# chat: 前缀剥离后直接命中
check("gpt-6-sol",      1050000, 128000, catalog=mini_catalog, note="目录命中+勘误(1.05M)")
check("glm-5.3",        1000000, 131072, catalog=mini_catalog, note="目录命中 GLM")
# vendor 别名归一：zai-org -> zai
check("zai-org/glm-5.3", 1000000, 131072, catalog=mini_catalog, note="vendor 别名 zai-org->zai")
check("openrouter:zai-org/glm-5.3", 1000000, 131072, catalog=mini_catalog, note="openrouter 前缀+vendor 别名")
# 目录 ID 带 vendor 路径，查询为裸 ID
check("kimi-k3",        1000000, 131072, catalog=mini_catalog, note="目录 vendor 路径 -> 裸 ID 命中")
check("moonshotai/kimi-k3", 1000000, 131072, catalog=mini_catalog, note="目录 vendor 路径直接命中")
# 日期快照后缀
check("qwen3.7-max-0922", 1000000, 131072, catalog=mini_catalog, note="日期快照后缀命中")

# raw 直报在目录存在时依然最高优先
check("glm-5.3", 999000, catalog=mini_catalog, raw={"context_length": 999000}, note="直报 > 目录")

# 目录匹配但命中不了（不存在）——回落家族表
check("unknown-model-x", 128000, catalog={"p": {"models": [{"id": "chat:foo", "contextWindow": 111000, "maxTokens": 222000}]}},
      note="未命中回落默认")

print()
print("=" * 60)
print("PASS=%d  FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
