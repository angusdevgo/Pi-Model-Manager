# -*- coding: utf-8 -*-
"""只读检查 ~/.pi/agent/models.json 中的存量低估参数。"""
import json, re

raw = open(r"C:/Users/Angus/.pi/agent/models.json", encoding="utf-8").read()
raw = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
cfg = json.loads(raw)

KEYS = ["glm-5", "qwen3", "kimi-k3", "gpt-6", "gpt-5.4", "deepseek-v4", "grok-4", "minimax-m3", "hy4", "mimo"]
total, stale = 0, []
for pid, p in (cfg.get("providers") or {}).items():
    for m in p.get("models") or []:
        if not isinstance(m, dict):
            continue
        total += 1
        cw = m.get("contextWindow")
        mid = str(m.get("id", "")).lower()
        if isinstance(cw, int) and cw <= 131072 and any(k in mid for k in KEYS):
            stale.append((pid, m.get("id"), cw, m.get("maxTokens")))

print("模型总数: %d   疑似被低估(<=128K)的主流新模型: %d" % (total, len(stale)))
for pid, mid, cw, mt in stale[:30]:
    print("  [%s] %-45s cw=%s mt=%s" % (pid, mid, cw, mt))
