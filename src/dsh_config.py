# -*- coding: utf-8 -*-
"""
dsh_config.py - DeepSeek Harness (DSH) 模型配置适配器

负责 DSH 的模型配置 (cordis.patch.yml) 与安全凭据 (~/.dsh/.credentials.yaml) 的
读取、双向转换与原子保存。
与 Pi Model Manager 统一数据模型完全兼容。
"""

import os
import re
import yaml
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List


DEFAULT_DSH_DIR = Path.home() / ".dsh"
DEFAULT_DSH_PROFILE_DIR = DEFAULT_DSH_DIR / "profiles" / "web"
DEFAULT_DSH_CORDIS_PATH = DEFAULT_DSH_PROFILE_DIR / "cordis.patch.yml"
DEFAULT_DSH_CREDENTIALS_PATH = DEFAULT_DSH_DIR / ".credentials.yaml"
LLM_PI_AI_PLUGIN_ID = "llm-pi-ai"
LLM_PI_AI_PLUGIN_NAME = "@deepseek-ai/dsh-llm-pi-ai"
# 合法 HTTP field-name token（与 Fetch Headers 的要求一致）
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# DSH base 层自带、不经 pi-ai 的固定路由。它们不在 cordis.patch.yml 的
# llm-pi-ai.providers 里，所以工具只读展示 + 通过 “disabled: true” 补丁开关，
# 绝不能写回 providers（写了会被拒绝或产生重复路由）。
DSH_BUILTIN_ROUTES: List[Dict[str, Any]] = [
    {
        "route": "deepseek-official",
        "label": "DeepSeek",
        "entryId": "llm-deepseek",
        "plugin": "@deepseek-ai/dsh-llm-deepseek-api-key",
        "keyRef": "DEEPSEEK_API_KEY",
        "baseUrl": "https://api.deepseek.com",
        "api": "anthropic-messages",
        "note": "DSH 内置 DeepSeek 官方 API（apiKeyEnv 鉴权）",
    },
    {
        "route": "deepseek-account",
        "label": "DeepSeek 账号",
        "entryId": "llm-deepseek-account",
        "plugin": "@deepseek-ai/dsh-llm-deepseek-account",
        "keyRef": "",
        "baseUrl": "https://api.deepseek.com",
        "api": "anthropic-messages",
        "note": "DSH 内置 DeepSeek 账号登录路由（OAuth token）",
    },
]
DSH_BUILTIN_BY_ENTRY = {spec["entryId"]: spec for spec in DSH_BUILTIN_ROUTES}


def get_dsh_paths(profile_name: str = "web") -> Tuple[Path, Path]:
    """返回 (cordis.patch.yml 路径, .credentials.yaml 路径)"""
    dsh_home = Path(os.environ.get("DSH_HOME", str(DEFAULT_DSH_DIR)))
    cordis_path = dsh_home / "profiles" / profile_name / "cordis.patch.yml"
    creds_path = dsh_home / ".credentials.yaml"
    return cordis_path, creds_path


def is_dsh_installed() -> bool:
    """检测本机是否存在 DSH 环境"""
    cordis_path, _ = get_dsh_paths()
    return cordis_path.parent.exists() or DEFAULT_DSH_DIR.exists()


def _leading_comment_block(text: str) -> str:
    """取文件头的注释块（含其后的空行），保留 DSH 自带的说明。

    PyYAML 重写会丢掉注释，而 ``cordis.patch.yml`` 顶部的几行说明是 DSH
    写给用户看的，每次保存都抹掉属于意外损失。
    """
    lines: List[str] = []
    for line in (text or "").splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("#") or (stripped == "" and lines):
            lines.append(line)
            continue
        break
    block = "".join(lines)
    return block if "#" in block else ""


def sanitize_dsh_headers(headers: Any) -> Dict[str, str]:
    """过滤掉 Fetch 无法发送的请求头。

    DSH 的 ``assertValidHeaders()`` 对任一非法 header 直接 throw，而它在
    ``resolveProfiles()`` 里被同步调用，一条脏 header 就会让整个 llm-pi-ai
    适配器挂掉（所有 pi-ai 路由一起消失）。写盘前先丢掉这些值。
    """
    if not isinstance(headers, dict):
        return {}
    clean: Dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME_RE.match(name):
            continue
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            continue
        clean[name] = value
    return clean


def sanitize_ref_name(provider_id: str, suffix: str = "") -> str:
    """生成合规的凭据引用名称，如 CPAMP_API_KEY"""
    clean_id = re.sub(r"[^A-Za-z0-9_]", "_", str(provider_id or "").strip()).upper()
    clean_id = re.sub(r"_+", "_", clean_id).strip("_")
    if not clean_id:
        clean_id = "CUSTOM_PROVIDER"
    if suffix:
        clean_suffix = re.sub(r"[^A-Za-z0-9_]", "_", str(suffix).strip()).upper()
        clean_suffix = re.sub(r"_+", "_", clean_suffix).strip("_")
        return f"{clean_id}_{clean_suffix}_API_KEY"
    return f"{clean_id}_API_KEY"


def load_dsh_credentials(creds_path: Optional[Path] = None) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """读取 DSH 凭证文件，返回 (refs 字典, 原始完整文档)"""
    if creds_path is None:
        _, creds_path = get_dsh_paths()

    refs: Dict[str, str] = {}
    raw_doc: Dict[str, Any] = {"version": 1, "records": {}}

    if not creds_path.exists():
        return refs, raw_doc

    try:
        with open(creds_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            raw_doc = data
            if isinstance(data.get("refs"), dict):
                for k, v in data["refs"].items():
                    if k and v is not None:
                        refs[str(k)] = str(v)
    except Exception as e:
        print(f"[DSH] 读取凭证文件失败 {creds_path}: {e}")

    return refs, raw_doc


def save_dsh_credentials(refs_to_update: Dict[str, Optional[str]], creds_path: Optional[Path] = None) -> bool:
    """更新并保存 DSH 凭证文件，支持增量更新或删除（值为 None 时删除）"""
    if creds_path is None:
        _, creds_path = get_dsh_paths()

    _, raw_doc = load_dsh_credentials(creds_path)
    if "version" not in raw_doc:
        raw_doc["version"] = 1

    current_refs = raw_doc.get("refs")
    if not isinstance(current_refs, dict):
        current_refs = {}
        raw_doc["refs"] = current_refs

    for k, v in refs_to_update.items():
        if not k:
            continue
        if v is None or v == "":
            current_refs.pop(k, None)
        else:
            current_refs[k] = str(v)

    creds_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = creds_path.with_suffix(".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            yaml.dump(raw_doc, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        temp_path.replace(creds_path)
        return True
    except Exception as e:
        print(f"[DSH] 写入凭据文件失败: {e}")
        if temp_path.exists():
            temp_path.unlink()
        return False


def _dsh_builtin_providers(patches: Any, refs: Dict[str, str]) -> Dict[str, Any]:
    """把 DSH base 层自带路由描述成只读 provider 条目。

    这些路由（DeepSeek 官方 API / DeepSeek 账号）由内置插件注册，不在
    ``llm-pi-ai.providers`` 里，所以工具不能编辑也不能删，只能用
    ``- id: <entryId>`` + ``disabled: true`` 补丁在 DSH 启动时禁用它们。
    """
    entries = [e for e in patches if isinstance(e, dict)] if isinstance(patches, list) else []
    disabled_ids = {e.get("id") for e in entries if e.get("disabled") is True}
    out: Dict[str, Any] = {}
    for spec in DSH_BUILTIN_ROUTES:
        key_ref = spec.get("keyRef") or ""
        out[spec["route"]] = {
            "name": spec["label"],
            "baseUrl": spec.get("baseUrl") or "",
            "apiKey": refs.get(key_ref, "") if key_ref else "",
            "api": spec.get("api") or "",
            "models": [],
            "_isBuiltin": True,
            "_dshBuiltin": True,
            "_dshEntryId": spec["entryId"],
            "_dshPlugin": spec["plugin"],
            "_dshKeyRef": key_ref,
            "_dshNote": spec.get("note") or "",
            "_dshDisabled": spec["entryId"] in disabled_ids,
        }
    return out


def load_dsh_config(cordis_path: Optional[Path] = None, creds_path: Optional[Path] = None) -> Dict[str, Any]:
    """读取 DSH 的 cordis.patch.yml 并转换成 Pi Model Manager 统一的 providers 字典格式"""
    if cordis_path is None or creds_path is None:
        cp, crp = get_dsh_paths()
        cordis_path = cordis_path or cp
        creds_path = creds_path or crp

    refs, _ = load_dsh_credentials(creds_path)
    providers: Dict[str, Any] = {}
    patches: Any = []

    if cordis_path.exists():
        try:
            with open(cordis_path, "r", encoding="utf-8") as f:
                patches = yaml.safe_load(f)
        except Exception as e:
            print(f"[DSH] 读取 cordis.patch.yml 失败: {e}")
            patches = []

    if not isinstance(patches, list):
        patches = []

    llm_entry = None
    for entry in patches:
        if isinstance(entry, dict):
            if entry.get("id") == LLM_PI_AI_PLUGIN_ID or entry.get("name") == LLM_PI_AI_PLUGIN_NAME:
                llm_entry = entry
                break

    dsh_providers: Any = None
    if llm_entry and isinstance(llm_entry.get("config"), dict):
        dsh_providers = llm_entry["config"].get("providers")

    for pid, pconf in (dsh_providers or {}).items() if isinstance(dsh_providers, dict) else []:
        if not isinstance(pconf, dict):
            continue

        display_name = pconf.get("displayName") or pid
        base_url = pconf.get("baseURL") or ""
        api_type = pconf.get("api") or "openai-completions"
        api_key_ref = pconf.get("apiKeyEnv") or ""
        api_key = refs.get(api_key_ref, "") if api_key_ref else ""

        # 转换模型列表
        models_list: List[Dict[str, Any]] = []
        raw_models = pconf.get("models") or []
        if isinstance(raw_models, list):
            for m in raw_models:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                mid = str(m["id"]).strip()
                mname = str(m.get("name") or mid).strip()
                ctx = m.get("contextWindow")
                max_tok = m.get("maxTokens")
                inputs = m.get("input")
                if not isinstance(inputs, list):
                    inputs = ["text"]

                reasoning = False
                rf = m.get("reasoningEfforts")
                if rf and rf is not False:
                    reasoning = True

                model_item: Dict[str, Any] = {
                    "id": mid,
                    "name": mname,
                    "contextWindow": ctx or 128000,
                    "maxTokens": max_tok or 8192,
                    "input": inputs,
                    "reasoning": reasoning,
                }
                models_list.append(model_item)

        provider_obj: Dict[str, Any] = {
            "name": display_name,
            "baseUrl": base_url,
            "apiKey": api_key,
            "api": api_type,
            "models": models_list,
            "_dshApiKeyEnv": api_key_ref,
        }

        # 复制额外支持的参数（如果有）
        if isinstance(pconf.get("headers"), dict):
            provider_obj["headers"] = pconf["headers"]
        if isinstance(pconf.get("compat"), dict):
            provider_obj["compat"] = pconf["compat"]

        providers[pid] = provider_obj

    # DSH 内置路由：只读展示（用户不去 pi-ai 里定义同名路由时不覆盖）
    for route, obj in _dsh_builtin_providers(patches, refs).items():
        providers.setdefault(route, obj)

    return {"providers": providers}


def save_dsh_config(cfg: Dict[str, Any], cordis_path: Optional[Path] = None, creds_path: Optional[Path] = None) -> bool:
    """把 Pi Model Manager 统一的 providers 字典转存至 DSH 的 cordis.patch.yml 与 .credentials.yaml"""
    if cordis_path is None or creds_path is None:
        cp, crp = get_dsh_paths()
        cordis_path = cordis_path or cp
        creds_path = creds_path or crp

    input_providers = cfg.get("providers") if isinstance(cfg, dict) else {}
    if not isinstance(input_providers, dict):
        input_providers = {}

    # 1. 准备 credentials 更新集
    creds_updates: Dict[str, Optional[str]] = {}
    dsh_providers_dict: Dict[str, Any] = {}

    for pid, pdata in input_providers.items():
        if not isinstance(pdata, dict):
            continue
        # DSH base 层内置路由（DeepSeek 官方 / 账号）不归属 llm-pi-ai，
        # 写进 providers 会造出重复路由，只走 disabled 补丁开关。
        if pdata.get("_dshBuiltin"):
            continue
        pid_clean = str(pid).strip()
        if not pid_clean:
            continue
        # Pi 内置服务商同理不写盘
        if pdata.get("_isBuiltin"):
            continue

        # 不在工具侧偷偷补默认值：DSH 对空字符串零容忍，而它本身对 displayName /
        # api 都有目录默认值，所以“空”就应该老老实实不写。
        display_name = str(pdata.get("name") or "").strip()
        base_url = str(pdata.get("baseUrl") or "").strip()
        api_type = str(pdata.get("api") or "").strip()
        main_key = str(pdata.get("apiKey") or "").strip()
        api_keys_pool = pdata.get("apiKeys") or []

        # 密钥池映射: id -> key
        pool_key_map = {}
        if isinstance(api_keys_pool, list):
            for kitem in api_keys_pool:
                if isinstance(kitem, dict) and kitem.get("id") and kitem.get("key"):
                    pool_key_map[str(kitem["id"])] = {
                        "name": str(kitem.get("name") or kitem["id"]),
                        "key": str(kitem["key"]).strip()
                    }

        # 检查是否有多 Key 绑定
        # 如果模型绑定了 pool key，需要按 key 进行路由裂变分组
        key_groups: Dict[str, Dict[str, Any]] = {}
        # 默认组 (主 key)
        key_groups["_main"] = {
            "key": main_key,
            "suffix": "",
            "label_suffix": "",
            "models": []
        }

        raw_models = pdata.get("models") or []
        for m in raw_models:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            mid = str(m["id"]).strip()
            mname = str(m.get("name") or mid).strip()
            key_ref = str(m.get("apiKeyRef") or "").strip()

            target_group = "_main"
            if key_ref and key_ref in pool_key_map:
                target_group = f"pool_{key_ref}"
                if target_group not in key_groups:
                    pinfo = pool_key_map[key_ref]
                    key_groups[target_group] = {
                        "key": pinfo["key"],
                        "suffix": key_ref,
                        "label_suffix": f" ({pinfo['name']})",
                        "models": []
                    }

            # 模型定义转换（空值一律省略，让 DSH 退回目录/适配器默认值）
            ctx = m.get("contextWindow")
            max_tok = m.get("maxTokens")
            inputs = m.get("input")
            if not isinstance(inputs, list):
                inputs = ["text"]
            inputs = [str(x) for x in inputs if str(x).strip()]

            m_dsh: Dict[str, Any] = {
                "id": mid,
                "name": mname,
                "contextWindow": int(ctx) if ctx else 128000,
                "maxTokens": int(max_tok) if max_tok else 8192,
            }
            if inputs:
                m_dsh["input"] = inputs
            if m.get("reasoning"):
                m_dsh["reasoningEfforts"] = {"high": "high"}

            key_groups[target_group]["models"].append(m_dsh)

        # 依次为各 key_group 生成 DSH route
        for g_id, g_data in key_groups.items():
            g_models = g_data["models"]
            # 如果主组为空且有其他组，允许跳过主组
            if not g_models and g_id == "_main" and len(key_groups) > 1:
                continue

            # route id (DSH 要求小写且标识符友好)
            route_id = pid_clean.lower()
            if g_data["suffix"]:
                safe_suf = re.sub(r"[^a-z0-9_-]", "", g_data["suffix"].lower())
                route_id = f"{route_id}-{safe_suf}"

            ref_var = sanitize_ref_name(pid_clean, g_data["suffix"])
            g_key = g_data["key"]

            if g_key:
                creds_updates[ref_var] = g_key
            else:
                creds_updates[ref_var] = None

            # 注意：DSH 的 llm-pi-ai 对空字符串零容忍 ——
            # resolveProfiles() 对 baseURL/displayName 为空串直接 throw，
            # 而 apply() 一开始就调用 profiles()，一条坏路由会拖垮整个插件，
            # 所有 pi-ai 路由全部消失（设置页只剩 DeepSeek）。
            # 因此空值一律“省略该字段”，让 DSH 退回目录默认值。
            route_profile: Dict[str, Any] = {}
            display_label = (display_name + g_data["label_suffix"]).strip() if display_name else ""
            if display_label:
                route_profile["displayName"] = display_label
            if base_url:
                route_profile["baseURL"] = base_url
            if api_type:
                route_profile["api"] = api_type
            route_profile["models"] = g_models
            if g_key:
                route_profile["apiKeyEnv"] = ref_var

            safe_headers = sanitize_dsh_headers(pdata.get("headers"))
            if safe_headers:
                route_profile["headers"] = safe_headers
            if isinstance(pdata.get("compat"), dict):
                route_profile["compat"] = pdata["compat"]

            dsh_providers_dict[route_id] = route_profile

    # 2. 保存凭据
    if creds_updates:
        save_dsh_credentials(creds_updates, creds_path)

    # 3. 更新 cordis.patch.yml
    patches: List[Dict[str, Any]] = []
    header = ""
    if cordis_path.exists():
        try:
            raw_text = cordis_path.read_text(encoding="utf-8")
            header = _leading_comment_block(raw_text)
            loaded = yaml.safe_load(raw_text)
            if isinstance(loaded, list):
                patches = loaded
        except Exception as e:
            print(f"[DSH] 读取原 cordis.patch.yml 失败: {e}")

    # 寻找并更新 llm-pi-ai 节点
    found_idx = -1
    for idx, entry in enumerate(patches):
        if isinstance(entry, dict):
            if entry.get("id") == LLM_PI_AI_PLUGIN_ID or entry.get("name") == LLM_PI_AI_PLUGIN_NAME:
                found_idx = idx
                break

    # 保留原 llm-pi-ai 节点里除 providers 外的其它配置键，避免本工具保存时误删用户设置
    preserved_config: Dict[str, Any] = {}
    if found_idx >= 0 and isinstance(patches[found_idx], dict):
        old_conf = patches[found_idx].get("config")
        if isinstance(old_conf, dict):
            preserved_config = {k: v for k, v in old_conf.items() if k != "providers"}
    preserved_config["providers"] = dsh_providers_dict

    llm_node: Dict[str, Any] = {
        "id": LLM_PI_AI_PLUGIN_ID,
        "name": LLM_PI_AI_PLUGIN_NAME,
        "config": preserved_config,
    }

    if found_idx >= 0:
        patches[found_idx] = llm_node
    else:
        patches.append(llm_node)

    return _write_patch_file(patches, cordis_path, header)


def _write_patch_file(patches: List[Dict[str, Any]], cordis_path: Path, header: str = "") -> bool:
    """原子写回 cordis.patch.yml（可选保留顶部注释块）。"""
    cordis_path.parent.mkdir(parents=True, exist_ok=True)
    temp_cordis = cordis_path.with_suffix(".tmp")
    try:
        with open(temp_cordis, "w", encoding="utf-8") as f:
            if header:
                f.write(header)
            yaml.dump(patches, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        temp_cordis.replace(cordis_path)
        return True
    except Exception as e:
        print(f"[DSH] 写入 cordis.patch.yml 失败: {e}")
        if temp_cordis.exists():
            try:
                temp_cordis.unlink()
            except Exception:
                pass
        return False


def set_dsh_builtin_disabled(entry_id: str, disabled: bool,
                             cordis_path: Optional[Path] = None) -> Tuple[bool, str]:
    """启用/禁用 DSH base 层的内置路由（写 ``- id: <entryId>`` + ``disabled`` 补丁）。

    内置路由（deepseek-official / deepseek-account）由 base 层插件注册，不在
    pi-ai providers 里，无法“删除”；DSH 唯一支持的关闭手段就是给对应插件条目
    打 disabled 补丁，重启后生效。

    返回 (是否写入成功, 附带说明)。
    """
    entry_id = str(entry_id or "").strip()
    if not entry_id:
        return False, "empty entry id"
    if entry_id not in DSH_BUILTIN_BY_ENTRY:
        return False, "unknown builtin entry: %s" % entry_id

    if cordis_path is None:
        cordis_path, _ = get_dsh_paths()

    patches: List[Dict[str, Any]] = []
    header = ""
    if cordis_path.exists():
        try:
            raw_text = cordis_path.read_text(encoding="utf-8")
            header = _leading_comment_block(raw_text)
            loaded = yaml.safe_load(raw_text)
            if isinstance(loaded, list):
                patches = loaded
        except Exception as e:
            return False, "读取 cordis.patch.yml 失败: %s" % e

    found = False
    for entry in patches:
        if isinstance(entry, dict) and entry.get("id") == entry_id:
            if disabled:
                entry["disabled"] = True
            else:
                entry.pop("disabled", None)
            found = True
            break

    if not found:
        if disabled:
            patches.append({"id": entry_id, "disabled": True})
        else:
            return True, "already enabled"

    if not _write_patch_file(patches, cordis_path, header):
        return False, "写入失败"
    return True, ""


def get_dsh_default_model(cordis_path: Optional[Path] = None) -> Tuple[str, str]:
    """读取 DSH 的默认模型，返回 (provider, model)"""
    if cordis_path is None:
        cordis_path, _ = get_dsh_paths()
    if not cordis_path.exists():
        return "", ""
    try:
        with open(cordis_path, "r", encoding="utf-8") as f:
            patches = yaml.safe_load(f)
        if isinstance(patches, list):
            for entry in patches:
                if isinstance(entry, dict) and entry.get("id") == "agent-default-model":
                    conf = entry.get("config") or {}
                    return str(conf.get("provider") or ""), str(conf.get("model") or "")
    except Exception as e:
        print(f"[DSH] 读取默认模型失败: {e}")
    return "", ""


def set_dsh_default_model(provider: str, model: str, cordis_path: Optional[Path] = None) -> bool:
    """设置 DSH 的默认模型 (agent-default-model)"""
    if cordis_path is None:
        cordis_path, _ = get_dsh_paths()
    if not cordis_path.exists():
        return False
    try:
        with open(cordis_path, "r", encoding="utf-8") as f:
            patches = yaml.safe_load(f)
        if not isinstance(patches, list):
            patches = []

        found = False
        for entry in patches:
            if isinstance(entry, dict) and entry.get("id") == "agent-default-model":
                if "config" not in entry or not isinstance(entry["config"], dict):
                    entry["config"] = {}
                entry["config"]["provider"] = provider
                entry["config"]["model"] = model
                found = True
                break

        if not found:
            patches.append({
                "id": "agent-default-model",
                "name": "@deepseek-ai/dsh-agent-default-model",
                "config": {
                    "provider": provider,
                    "model": model,
                    "reasoningEffort": "high"
                }
            })

        temp_cordis = cordis_path.with_suffix(".tmp")
        with open(temp_cordis, "w", encoding="utf-8") as f:
            yaml.dump(patches, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        temp_cordis.replace(cordis_path)
        return True
    except Exception as e:
        print(f"[DSH] 设置默认模型失败: {e}")
        return False

