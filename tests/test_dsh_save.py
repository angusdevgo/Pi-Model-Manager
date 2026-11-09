# -*- coding: utf-8 -*-
"""DSH 配置写盘回归测试。

用法:  python tests/test_dsh_save.py

背景（真实故障）：
    DSH 的 llm-pi-ai 适配器在 apply() 一开始就调用 resolveProfiles()，而后者对
    ``baseURL`` / ``displayName`` 为空字符串直接 throw：

        Error: llm-pi-ai: provider "xiaomi" has an empty baseURL

    一条空串就把整个插件打死 —— 所有 pi-ai 路由一起消失，DSH 设置页只剩
    DeepSeek。旧版本工具把 DSH 里「本来没写 baseURL」的目录路由读成 ""，再原样
    写回，于是只要在 DSH 页签点一次保存，全部导入的厂商就不见了。

覆盖:
  1. 空 baseURL / 空 displayName / 空 api 一律不写盘
  2. 非法 header 名字与多行值被丢弃（Fetch 不接受 → 同样会打死插件）
  3. 空 input 列表被省略（交给 DSH 默认模态）
  4. llm-pi-ai 节点里 providers 以外的用户配置键被保留
  5. cordis.patch.yml 顶部注释块被保留（PyYAML 重写会丢注释）
"""
import os
import sys
import types
import tempfile
import shutil
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

for _mod in ("webview",):
    try:
        __import__(_mod)
    except ImportError:
        _m = types.ModuleType(_mod)
        _m.start = lambda *a, **k: None
        sys.modules[_mod] = _m

import yaml  # noqa: E402
import dsh_config  # noqa: E402

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %-44s %s" % (name, detail))
    else:
        FAIL += 1
        print("  FAIL  %-44s %s" % (name, detail))


HEADER = (
    "# Your patch layer for this dsh profile, applied after every bundle layer:\n"
    "# a top-level YAML array of loader patch entries.\n\n"
)

SEED = HEADER + """- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: xiaomi
    model: mimo-v2.6-flash
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    extraUserSetting:
      keep: true
    providers:
      xiaomi:
        api: openai-completions
        apiKeyEnv: XIAOMI_API_KEY
        models:
          - id: mimo-v2.6-flash
            name: MiMo-V2.6-Flash
"""


def main():
    print("=" * 62)
    print("DSH 配置写盘（空值致命字段）回归测试")
    print("=" * 62)

    tmpdir = Path(tempfile.mkdtemp(prefix="pmm-dsh-test-"))
    cordis = tmpdir / "cordis.patch.yml"
    creds = tmpdir / ".credentials.yaml"
    try:
        cordis.write_text(SEED, encoding="utf-8")
        creds.write_text("version: 1\nrefs:\n  XIAOMI_API_KEY: sk-test\n", encoding="utf-8")

        cfg = dsh_config.load_dsh_config(cordis, creds)
        prov = cfg["providers"]["xiaomi"]
        check("缺少 baseURL 时读成空串", prov.get("baseUrl") == "", repr(prov.get("baseUrl")))

        # DSH base 层内置路由：必须被读到（供工具展示）
        check("内置路由出现在读取结果",
              cfg["providers"].get("deepseek-official", {}).get("_dshBuiltin") is True
              and cfg["providers"].get("deepseek-account", {}).get("_dshBuiltin") is True,
              str(sorted(k for k, v in cfg["providers"].items() if v.get("_dshBuiltin"))))

        # 模拟编辑器回写：空的 baseUrl / displayName / api / 空 input / 脏 header
        prov["baseUrl"] = ""
        prov["name"] = ""
        prov["api"] = ""
        prov["headers"] = {"X-Good": "1", "bad name": "2", "X-Multi": "a\nb"}
        prov["models"] = [{"id": "m", "name": "m", "input": []}]

        ok = dsh_config.save_dsh_config(cfg, cordis, creds)
        check("保存成功", ok)

        text = cordis.read_text(encoding="utf-8")
        patches = yaml.safe_load(text)
        node = next(e for e in patches if isinstance(e, dict) and e.get("id") == "llm-pi-ai")
        route = node["config"]["providers"]["xiaomi"]

        check("不写空 baseURL", "baseURL" not in route, repr(route.get("baseURL")))
        check("不写空 displayName", "displayName" not in route, repr(route.get("displayName")))
        check("不写空 api", "api" not in route, repr(route.get("api")))
        check("丢弃非法 header", route.get("headers") == {"X-Good": "1"}, str(route.get("headers")))
        check("省略空 input", "input" not in route["models"][0], str(route["models"][0]))
        check("保留 llm-pi-ai 其它配置键",
              node["config"].get("extraUserSetting") == {"keep": True},
              str(node["config"].get("extraUserSetting")))
        check("保留顶部注释块", text.startswith("# Your patch layer"), repr(text[:24]))
        check("其它 patch 节点未丢失",
              any(e.get("id") == "agent-default-model" for e in patches if isinstance(e, dict)))
        check("不把内置路由写进 llm-pi-ai",
              "deepseek-official" not in node["config"]["providers"]
              and "deepseek-account" not in node["config"]["providers"],
              str(sorted(node["config"]["providers"])))

        # 内置路由的禁用/恢复（DSH 只支持 disabled 补丁，无法删除）
        ok, msg = dsh_config.set_dsh_builtin_disabled("llm-deepseek", True, cordis)
        after = yaml.safe_load(cordis.read_text(encoding="utf-8"))
        stub = next((e for e in after if isinstance(e, dict) and e.get("id") == "llm-deepseek"), None)
        check("禁用内置路由写入 disabled: true",
              ok and isinstance(stub, dict) and stub.get("disabled") is True, msg)
        check("禁用后读取状态同步",
              dsh_config.load_dsh_config(cordis, creds)["providers"]["deepseek-official"].get("_dshDisabled") is True)

        ok, _ = dsh_config.set_dsh_builtin_disabled("llm-deepseek", False, cordis)
        after = yaml.safe_load(cordis.read_text(encoding="utf-8"))
        stub = next((e for e in after if isinstance(e, dict) and e.get("id") == "llm-deepseek"), None)
        check("恢复内置路由移除 disabled",
              ok and (stub is None or "disabled" not in stub), str(stub))
        check("恢复后读取状态同步",
              dsh_config.load_dsh_config(cordis, creds)["providers"]["deepseek-official"].get("_dshDisabled") is False)
        check("未知 entry 被拒绝",
              dsh_config.set_dsh_builtin_disabled("nope", True, cordis)[0] is False)

        # 回读不应再出现空串（模拟再次打开工具）
        again = dsh_config.load_dsh_config(cordis, creds)
        check("回读可用", again["providers"]["xiaomi"].get("baseUrl") == "")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("=" * 62)
    print("PASS=%d  FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
