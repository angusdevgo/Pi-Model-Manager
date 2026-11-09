import sys
import os

# 保护 pythonw.exe 免受 print() 导致的 NoneType 崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

import fnmatch
import html as _html
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import functools
from datetime import datetime
import urllib.error
import urllib.request
import webview
from pathlib import Path
import dsh_config

def is_dsh_path(path_val) -> bool:
    if not path_val:
        return False
    p = str(path_val).replace("\\", "/").lower()
    return "cordis.patch.yml" in p or "/.dsh/" in p

DEFAULT_AGENT_DIR = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
DEFAULT_MODELS_PATH = DEFAULT_AGENT_DIR / "models.json"
CURRENT_CONFIG_PATH = DEFAULT_MODELS_PATH
PI_SETTINGS_PATH = DEFAULT_AGENT_DIR / "settings.json"


# ==========================================================
# 错误日志 / 诊断（pythonw 无终端也能完整回溯报错）
# ==========================================================
LOG_PATH = DEFAULT_AGENT_DIR / "pi-model-manager.log"
LOG_MAX_BYTES = 1_500_000
LOG_DETAIL_LIMIT = 12000
_LOG_LOCK = threading.Lock()


def clip_text(value, limit=LOG_DETAIL_LIMIT):
    """错误详情不做粗暴截断：仅在超长时收口，并显式标注原始长度。"""
    text = "" if value is None else str(value)
    if not limit or len(text) <= limit:
        return text
    return text[:limit] + "\n…〔已截断显示，原始内容共 %d 字符；完整内容见日志文件〕" % len(text)


def write_log(level, source, message, detail=""):
    """追加一条日志到 ~/.pi/agent/pi-model-manager.log（自动轮转，异常静默）。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    block = "[%s] [%s] [%s] %s\n" % (ts, str(level).upper(), source, message)
    if detail:
        block += str(detail).rstrip() + "\n"
    try:
        with _LOG_LOCK:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                try:
                    backup = LOG_PATH.with_suffix(".log.1")
                    if backup.exists():
                        backup.unlink()
                    LOG_PATH.replace(backup)
                except Exception:
                    pass
            with LOG_PATH.open("a", encoding="utf-8") as fp:
                fp.write(block)
    except Exception:
        pass
    return block


def log_exception(source, exc=None, extra=""):
    """记录异常（含完整 traceback）并落盘。"""
    exc = exc if exc is not None else sys.exc_info()[1]
    tb = traceback.format_exc()
    if not tb or tb.strip() == "NoneType: None":
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else ""
    return write_log("ERROR", source, "%s: %s" % (type(exc).__name__, exc),
                     "\n".join(x for x in [extra, tb] if x))


def install_exception_hooks():
    """兜底捕获：主线程 / 子线程未处理异常全部落盘，避免 pythonw 下静默消失。"""
    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        write_log("ERROR", "uncaught", "%s: %s" % (exc_type.__name__, exc_value),
                  "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        write_log("ERROR", "thread:%s" % getattr(args.thread, "name", "?"),
                  "%s: %s" % (args.exc_type.__name__, args.exc_value),
                  "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = _hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook

# 始终在侧栏中展示的内置厂商（其余内置厂商仅在检测到凭据时展示）
CORE_BUILTIN_PROVIDERS = ["deepseek", "xiaomi", "openai", "anthropic", "google"]

# 内置厂商的兜底元信息（当无法读取 pi-ai 目录时使用）
BUILTIN_PROVIDERS_META = {
    "deepseek": {"name": "DeepSeek (Built-in)", "api": "openai-completions", "baseUrl": "https://api.deepseek.com", "env_key": "DEEPSEEK_API_KEY"},
    "xiaomi": {"name": "Xiaomi (Built-in)", "api": "openai-completions", "baseUrl": "https://api.xiaomimimo.com/v1", "env_key": "XIAOMI_API_KEY"},
    "openai": {"name": "OpenAI (Built-in)", "api": "openai-responses", "baseUrl": "https://api.openai.com/v1", "env_key": "OPENAI_API_KEY"},
    "anthropic": {"name": "Anthropic (Built-in)", "api": "anthropic-messages", "baseUrl": "https://api.anthropic.com", "env_key": "ANTHROPIC_API_KEY"},
    "google": {"name": "Google Gemini (Built-in)", "api": "google-generative-ai", "baseUrl": "https://generativelanguage.googleapis.com/v1beta", "env_key": "GEMINI_API_KEY"},
}

def strip_json_comments(text: str) -> str:
    result = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        next_ch = text[i + 1] if i + 1 < len(text) else ''
        if in_string:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue
        if ch == '/' and next_ch == '/':
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        if ch == '/' and next_ch == '*':
            i += 2
            while i + 1 < len(text) and not (text[i] == '*' and text[i + 1] == '/'):
                i += 1
            i += 2
            continue
        result.append(ch)
        i += 1
    return "".join(result)

def normalize_config_path(path_value=None):
    if not path_value:
        return CURRENT_CONFIG_PATH
    return Path(str(path_value)).expanduser()

def detect_config_schema(path, data):
    return "pi-providers"

def read_config_file(path):
    raw = path.read_text(encoding="utf-8-sig")
    clean = strip_json_comments(raw).strip()
    if not clean:
        return {"providers": {}}
    return json.loads(clean)

def load_pi_settings():
    if not PI_SETTINGS_PATH.exists():
        return {}
    try:
        raw = PI_SETTINGS_PATH.read_text(encoding="utf-8-sig")
        clean = strip_json_comments(raw).strip()
        return json.loads(clean) if clean else {}
    except Exception:
        return {}

def save_pi_settings(settings_dict):
    try:
        PI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PI_SETTINGS_PATH.write_text(json.dumps(settings_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


TOOL_SETTINGS_PATH = DEFAULT_AGENT_DIR / "model-manager-settings.json"

# 工具界面偏好默认值。与 Pi「顺序语义」相关的开关都持久化在这里。
DEFAULT_TOOL_PREFS = {
    # 模型列表顶部置顶 Pi 默认模型（仅显示层，不改动 models.json 保存顺序）
    "pinDefaultModel": True,
    # 厂商列按 Pi 的字母序显示（Pi 侧固定按厂商 ID 字母序分组，无法配置）
    "alignProviderOrder": False,
    # Pi 排序补丁自动维护（工具保存后自动打补丁 / Pi 升级后自动重打）
    "patchPiOrder": False,
}


def load_tool_settings():
    """读取工具自身偏好文件 model-manager-settings.json（容错 BOM / 注释 / 损坏）。"""
    if not TOOL_SETTINGS_PATH.exists():
        return {}
    try:
        raw = TOOL_SETTINGS_PATH.read_text(encoding="utf-8-sig")
        clean = strip_json_comments(raw).strip()
        data = json.loads(clean) if clean else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_tool_settings(settings):
    try:
        TOOL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOOL_SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def get_tool_prefs():
    prefs = dict(DEFAULT_TOOL_PREFS)
    stored = load_tool_settings()
    for key in DEFAULT_TOOL_PREFS:
        if isinstance(stored.get(key), bool):
            prefs[key] = stored[key]
    return prefs


def set_tool_pref(name, value):
    if name not in DEFAULT_TOOL_PREFS:
        return False
    stored = load_tool_settings()
    stored[name] = bool(value)
    return save_tool_settings(stored)


def pi_name_sort_key(text):
    """模拟 JS `String.prototype.localeCompare` 的字母序排序键（忽略大小写）。

    Pi 的 /model 选择器（model-selector.js -> sortModels）与 `pi --list-models`
    （cli/list-models.js）都用 `a.provider.localeCompare(b.provider)` 对厂商分组，
    即按厂商 ID 的字母序、忽略大小写。
    """
    return str(text).casefold()


# ==========================================================
# 「工具 ↔ Pi」顺序对照（左栏 = 工具窗口，右栏 = Pi 实际）
# ----------------------------------------------------------
# 左栏：工具窗口里看到的厂商顺序与模型名称（= models.json 保存顺序）
# 右栏：Pi 实际生效的结果 —— 厂商分组顺序（🧩 补丁生效时 = 顺序文件内容，
#       否则 = Pi 原生字母序）+ 各厂商内部的模型顺序（= models.json 数组顺序，
#       内置厂商为 Pi 原生目录顺序 + 自建模型追加）
# 每行右侧给出「✅ 一致 / ⚠️ 不一致」结果，长报告不再让人逐行读流水账。
# ==========================================================

def _order_snapshot(path_value=None):
    """收集顺序自检所需的全部派生数据（build_order_report / build_order_compare 共用）。"""
    path = normalize_config_path(path_value)
    try:
        disk_cfg = read_config_file(path) if path.exists() else {"providers": {}}
    except Exception:
        disk_cfg = {"providers": {}}
    if not isinstance(disk_cfg.get("providers"), dict):
        disk_cfg = {"providers": {}}
    try:
        display_cfg = load_config(path)
    except Exception:
        display_cfg = {"providers": {}}
    disk_providers = disk_cfg.get("providers") or {}
    display_providers = display_cfg.get("providers") or {}
    settings = load_pi_settings()
    prefs = get_tool_prefs()
    tool_order = list(display_providers.keys())
    try:
        pst = pi_order_patch_status()
    except Exception as e:
        pst = {"error": str(e)}
    return {
        "path": path,
        "diskProviders": disk_providers,
        "displayProviders": display_providers,
        "toolOrder": tool_order,
        "piOrder": sorted(tool_order, key=pi_name_sort_key),
        "patchOn": bool(pst.get("fullyPatched")) and not pst.get("versionChanged"),
        "pst": pst,
        "defaultProvider": str(settings.get("defaultProvider") or ""),
        "defaultModel": str(settings.get("defaultModel") or ""),
        "prefs": prefs,
    }


def _provider_model_ids(providers, pid):
    """取某厂商的模型 ID 列表（保持文件中的数组顺序）。"""
    if not pid:
        return []
    p = providers.get(pid) or {}
    return [str(m.get("id")) for m in (p.get("models") or [])
            if isinstance(m, dict) and m.get("id")]


def build_order_compare(path_value=None, alpha=False):
    """构造「工具 ↔ Pi」左右两栏对照数据（含逐行结果 / 汇总 / 结论）。"""
    sn = _order_snapshot(path_value)
    display_providers = sn["displayProviders"]
    disk_providers = sn["diskProviders"]
    custom_order = sn["toolOrder"]
    patch_on = sn["patchOn"]
    default_provider = sn["defaultProvider"]
    default_model = sn["defaultModel"]
    pst = sn["pst"]

    # 左栏 = 工具窗口实际显示顺序（打开「⇅ Pi 字母序」时窗口里看到的就是字母序）
    tool_order = sorted(custom_order, key=pi_name_sort_key) if alpha else list(custom_order)

    # 右栏 = Pi 实际生效顺序
    order_file = [str(x) for x in (pst.get("order") or [])]
    if patch_on and order_file:
        known = set(custom_order)
        pi_order = [p for p in order_file if p in known]
        pi_order += [p for p in sorted(known - set(pi_order), key=pi_name_sort_key)]
        pi_source = "order-file"
    else:
        # 未打补丁（或顺序文件为空 → 补丁助手回退）时，Pi 一律按字母序分组
        pi_order = sorted(custom_order, key=pi_name_sort_key)
        pi_source = "alpha-fallback" if patch_on else "alpha-native"
    stale_order_file = bool(patch_on and order_file and pi_order != tool_order)

    # 逐行对照（按位置比对）
    n = max(len(tool_order), len(pi_order))
    rows = []
    same_count = 0
    for i in range(n):
        left_id = tool_order[i] if i < len(tool_order) else ""
        right_id = pi_order[i] if i < len(pi_order) else ""
        same = bool(left_id) and left_id == right_id
        if same:
            same_count += 1
        rows.append({
            "i": i + 1,
            "leftId": left_id,
            "rightId": right_id,
            "leftModels": _provider_model_ids(display_providers, left_id),
            "rightModels": _provider_model_ids(disk_providers, right_id),
            "same": same,
            "leftStar": default_model if (left_id and left_id == default_provider) else "",
            "rightStar": default_model if (right_id and right_id == default_provider) else "",
        })

    # 厂商内部模型顺序：工具显示顺序 vs models.json 已保存顺序（= Pi 侧顺序）
    unsaved = []
    model_total = 0
    tool_model_total = 0
    for pid in custom_order:
        disp = _provider_model_ids(display_providers, pid)
        disk = _provider_model_ids(disk_providers, pid)
        tool_model_total += len(disp)
        model_total += len(disk)
        if disp != disk:
            unsaved.append(pid)
    model_ok = len(custom_order) - len(unsaved)

    disabled_rows = [(pid, str(m.get("id"))) for pid, p in display_providers.items()
                     for m in (p.get("models") or [])
                     if isinstance(m, dict) and m.get("disabled") is True and m.get("id")
                     and not m.get("_isBuiltinModel")]

    try:
        _bad_esc = scan_tool_js_escapes()
        _self_ok, _self_msg = verify_tool_html_js()
    except Exception as _e:
        _bad_esc, _self_ok, _self_msg = [], True, "自检异常（已跳过）: %s" % _e
    self_ok = (not _bad_esc) and _self_ok

    # 结论
    all_providers_same = (n > 0 and same_count == n and not stale_order_file)
    if not custom_order:
        verdict_cls, verdict = "info", "ℹ️ 未检测到厂商配置（models.json 为空）"
    elif all_providers_same and not unsaved:
        verdict_cls = "ok"
        verdict = ("✅ 完全一致：工具窗口与 Pi 的厂商分组顺序、厂商内部模型顺序全部一致"
                   + "（%d 厂商 / %d 模型）" % (len(custom_order), model_total))
    elif unsaved:
        verdict_cls, verdict = "warn", ("⚠️ 有 %d 个厂商的顺序尚未写盘：%s —— 点「💾 保存」后 Pi 即按新顺序"
                                        % (len(unsaved), "、".join(unsaved)))
    elif stale_order_file:
        verdict_cls, verdict = "warn", "⚠️ Pi 的顺序文件与工具当前顺序不同：点「💾 保存」或重开工具即可同步"
    else:
        verdict_cls = "warn"
        verdict = ("⚠️ 厂商分组顺序不一致：Pi 固定按字母序分组（未开启「🧩 顺序补丁」）；"
                   "厂商内部的模型顺序 %d/%d 一致 ✅" % (model_ok, len(custom_order)))

    tool_label = "工具窗口顺序" + ("（⇅ Pi 字母序）" if alpha else "")
    if pi_source == "order-file":
        pi_label = "Pi 实际分组（🧩 补丁生效）"
    elif pi_source == "alpha-fallback":
        pi_label = "Pi 实际分组（补丁已开·顺序文件空 → 字母序）"
    else:
        pi_label = "Pi 实际分组（原生字母序）"

    summary = [
        {"label": "厂商分组顺序",
         "value": ("%d/%d 一致 ✅" % (same_count, n)) if all_providers_same
                  else ("%d/%d 一致 ⚠️" % (same_count, n)),
         "ok": all_providers_same},
        {"label": "厂商内部模型顺序",
         "value": ("%d/%d 已落盘 ✅" % (model_ok, len(custom_order))) if not unsaved
                  else ("⚠️ 待保存：" + "、".join(unsaved)),
         "ok": not unsaved},
        {"label": "默认模型",
         "value": ("★ %s/%s" % (default_provider, default_model)) if default_model
                  else "⚠️ settings.json 未设置",
         "cls": "info" if default_model else "bad"},
        {"label": "禁用模型（Pi 侧不存在）",
         "value": "无" if not disabled_rows else ("%d 个：" % len(disabled_rows)
                                                + "、".join("%s/%s" % r for r in disabled_rows[:4])),
         "cls": "info"},
        {"label": "Pi 排序补丁",
         "value": ("%d/%d 个文件已打 🧩" % (pst.get("patchedCount", 0), pst.get("targetCount", 0)))
                  if patch_on else "未开启（Pi 固定字母序）",
         "cls": "ok" if patch_on else "info"},
        {"label": "工具自身体检",
         "value": "✅ 通过" if self_ok else "❌ 内嵌 JS 异常，界面可能停在静态初始态",
         "cls": "ok" if self_ok else "bad"},
    ]

    return {
        "success": True,
        "alpha": bool(alpha),
        "patchOn": patch_on,
        "piSource": pi_source,
        "staleOrderFile": stale_order_file,
        "unsavedIds": unsaved,
        "toolLabel": tool_label,
        "piLabel": pi_label,
        "toolCount": len(tool_order),
        "piCount": len(pi_order),
        "toolModelCount": tool_model_total,
        "modelCount": model_total,
        "sameCount": same_count,
        "rows": rows,
        "summary": summary,
        "verdict": {"cls": verdict_cls, "text": verdict},
        "disabledCount": len(disabled_rows),
        "selfCheckOk": self_ok,
        "selfCheckMsg": "" if self_ok else clip_text(_self_msg, 3000),
        "configPath": str(sn["path"]),
        "orderFilePath": str(pst.get("orderFilePath") or PI_PROVIDER_ORDER_PATH),
        "title": "顺序自检 · 工具 ↔ Pi" + ("（🧩 补丁生效）" if patch_on else ""),
        "hint": ("左栏 = 工具窗口实际显示；右栏 = Pi 实际生效结果。"
                 + "📋 复制全文 可复制完整的六节文本报告。"),
    }


def build_order_compare_html(cmp):
    """把对照数据渲染成弹窗 HTML（左右两栏 + 逐行一致/不一致）。"""
    def _e(s):
        return _html.escape(str(s if s is not None else ""), quote=True)

    def _models(models, star):
        if not models:
            return '<div class="oc-m oc-empty">（无模型）</div>'
        parts = []
        for mid in models:
            if star and mid == star:
                parts.append('<span class="oc-star">★ ' + _e(mid) + '</span>')
            else:
                parts.append(_e(mid))
        return '<div class="oc-m">' + ' · '.join(parts) + '</div>'

    v = cmp.get("verdict") or {}
    out = ['<div class="oc">']
    out.append('<div class="oc-verdict ' + _e(v.get("cls") or "info") + '">' + _e(v.get("text") or "") + '</div>')
    out.append('<div class="oc-tbl">')
    out.append('<div class="oc-hd">'
               '<div class="oc-c">' + _e(cmp.get("toolLabel")) + '<span class="oc-n">'
               + _e("%d 厂商 · %d 模型" % (cmp.get("toolCount", 0), cmp.get("toolModelCount", 0)))
               + '</span></div>'
               '<div class="oc-c">' + _e(cmp.get("piLabel")) + '<span class="oc-n">'
               + _e("%d 厂商 · %d 模型" % (cmp.get("piCount", 0), cmp.get("modelCount", 0)))
               + '</span></div>'
               '<div class="oc-c oc-res">结果</div></div>')
    unsaved = set(cmp.get("unsavedIds") or [])
    rows = cmp.get("rows") or []
    if not rows:
        out.append('<div class="oc-tr"><div class="oc-c oc-empty" style="grid-column:1 / -1;">'
                   '未检测到厂商配置（models.json 为空）</div></div>')
    for r in rows:
        left_id = r.get("leftId") or ""
        out.append('<div class="oc-tr' + ('' if r.get("same") else ' bad') + '">')
        out.append('<div class="oc-c"><div class="oc-top"><span class="oc-i">' + str(r.get("i", 0))
                   + '</span><b>' + _e(left_id or "—") + '</b>'
                   + ('<span class="oc-chip">未保存</span>' if left_id in unsaved else '')
                   + '</div>' + _models(r.get("leftModels"), r.get("leftStar")) + '</div>')
        out.append('<div class="oc-c"><div class="oc-top"><span class="oc-i">' + str(r.get("i", 0))
                   + '</span><b>' + _e(r.get("rightId") or "—") + '</b></div>'
                   + _models(r.get("rightModels"), r.get("rightStar")) + '</div>')
        out.append('<div class="oc-c oc-res">'
                   + ('<span class="oc-badge">✅</span>' if r.get("same") else '<span class="oc-badge">⚠️</span>')
                   + '</div>')
        out.append('</div>')
    out.append('</div>')

    out.append('<div class="oc-sum">')
    for item in cmp.get("summary") or []:
        cls = item.get("cls") or ("ok" if item.get("ok") else "bad")
        out.append('<div class="oc-sr"><span>' + _e(item.get("label"))
                   + '</span><b class="' + _e(cls) + '">' + _e(item.get("value")) + '</b></div>')
    out.append('</div>')

    out.append('<div class="oc-foot">配置：' + _e(cmp.get("configPath"))
               + '<br>顺序文件：' + _e(cmp.get("orderFilePath"))
               + (('<br>⚠️ ' + _e(cmp.get("selfCheckMsg"))) if cmp.get("selfCheckMsg") else '')
               + '</div>')
    out.append('</div>')
    return "".join(out)


def build_order_report(path_value=None):
    """生成「工具顺序 ↔ Pi 顺序」一致性自检报告（纯文本，供复制/存档）。

    Pi 侧一共存在三种顺序，其中只有「厂商内部的模型顺序」可以由工具控制：
      1. 厂商分组顺序 —— /model 选择器与 `pi --list-models` 都按厂商 ID 字母序分组
         （Pi 源码硬编码），models.json 的厂商键顺序对 Pi 无效。
         如需让 Pi 跟随工具顺序，可使用工具内的「🧩 Pi 顺序补丁」（文件见本报告【5】）。
      2. 厂商内部的模型顺序 —— 自定义厂商 = models.json 数组顺序（工具拖拽生效）；
         内置厂商 = Pi 原生目录顺序（目录内模型在工具侧锁定拖拽）。
      3. 默认/当前模型置顶 —— /model 选择器把「当前模型」放第 1、「默认模型」放第 2。
    """
    sn = _order_snapshot(path_value)
    path = sn["path"]
    disk_providers = sn["diskProviders"]
    display_providers = sn["displayProviders"]
    default_provider = sn["defaultProvider"]
    default_model = sn["defaultModel"]
    prefs = sn["prefs"]
    tool_order = sn["toolOrder"]
    pi_order = sn["piOrder"]
    pst = sn["pst"]
    patch_on = sn["patchOn"]
    pi_effective_order = tool_order if patch_on else pi_order

    lines = ["📄 配置文件: " + str(path), ""]
    lines.append("【1】厂商分组顺序 —— Pi 默认按字母序，可用「🧩 顺序补丁」改为跟随工具")
    lines.append("  · 工具当前顺序: " + " → ".join(tool_order[:14]) + (" …" if len(tool_order) > 14 else ""))
    lines.append("  · Pi 实际顺序  : " + " → ".join(pi_effective_order[:14]) + (" …" if len(pi_effective_order) > 14 else "")
                 + ("   （🧩 补丁生效中）" if patch_on else ""))
    if not tool_order:
        lines.append("  ⚠️  暂无厂商配置")
    elif patch_on:
        lines.append("  ✅ 已通过「🧩 顺序补丁」让 Pi 按工具顺序分组（重启 Pi 后生效；顺序文件热更新）")
        lines.append("     如需恢复 Pi 原生行为：点顶部「🧩 顺序补丁」→「🩹 还原原版」")
    elif tool_order == pi_order:
        lines.append("  ✅ 两边一致：工具里的厂商顺序恰好就是字母序")
    else:
        lines.append("  ⚠️  不一致：Pi 的 /model 选择器与 `pi --list-models` 都按")
        lines.append("      `a.provider.localeCompare(b.provider)` 分组（pi 0.87 硬编码，见")
        lines.append("      model-selector.js sortModels / cli/list-models.js），models.json")
        lines.append("      里的厂商键顺序对 Pi 无效 —— 工具拖拽厂商只改变工具自身视图。")
        lines.append("      → 想让工具也显示成 Pi 的字母序：点侧栏标题上的「⇅ Pi 字母序」开关。")
        lines.append("      → 想让 Pi 跟随工具的厂商顺序：点顶部「🧩 Pi 顺序补丁」（详见报告【5】）。")
    lines.append("")

    lines.append("【2】厂商内部的模型顺序 —— 工具说了算，保存后重启 Pi 即生效")
    ok_providers = 0
    mismatch_providers = []
    total_models = 0
    for pid in tool_order:
        p = display_providers.get(pid) or {}
        rows = [m for m in (p.get("models") or []) if isinstance(m, dict) and m.get("id")]
        disp_ids = [str(m.get("id")) for m in rows]
        disk_ids = [str(m.get("id")) for m in ((disk_providers.get(pid) or {}).get("models") or [])
                    if isinstance(m, dict) and m.get("id")]
        total_models += len(disp_ids)
        if disp_ids == disk_ids:
            ok_providers += 1
            mark, note = "✅", ""
        else:
            mismatch_providers.append(pid)
            mark, note = "⚠️", "  ← 工具显示顺序与 models.json 保存顺序不同，点击「💾 保存」写盘"
        if p.get("_isBuiltin"):
            catalog_ids = [str(m.get("id")) for m in rows if m.get("_isBuiltinModel")]
            extra_ids = [str(m.get("id")) for m in rows if not m.get("_isBuiltinModel")]
            lines.append(f"  {mark} [{pid}] 内置厂商 · {len(disp_ids)} 个模型{note}")
            lines.append(f"       · 目录内模型 {len(catalog_ids)} 个：顺序由 Pi 原生目录决定（工具已锁定拖拽 → 两边必然一致）")
            if extra_ids:
                lines.append("       · 自建模型 " + str(len(extra_ids)) + " 个：Pi 侧追加在目录之后，顺序 = " + " → ".join(extra_ids))
        else:
            lines.append(f"  {mark} [{pid}] 自定义厂商 · {len(disp_ids)} 个模型{note}")
            lines.append("       " + (" → ".join(disp_ids) if disp_ids else "（空）"))
    lines.append("")

    lines.append("【3】默认模型置顶（Pi 的 /model 选择器行为）")
    if default_model:
        lines.append(f"  ★ Pi 默认模型: {default_provider}/{default_model}")
        lines.append("  · Pi 选择器把「当前正在使用的模型」放第 1 位、「默认模型」放第 2 位，")
        lines.append("    其余模型保持上面的保存顺序。")
        lines.append("  · 工具侧：" + ("已开启「⭐ 默认置顶」→ 默认模型在该厂商列表顶部置顶显示（仅显示）"
                              if prefs.get("pinDefaultModel") else "「⭐ 默认置顶」已关闭 → 列表 = 保存顺序"))
    else:
        lines.append("  ⚠️  settings.json 未设置 defaultProvider / defaultModel（可在模型行的「☆ 设默认」按钮设置）")
    lines.append("")

    disabled_rows = [(pid, str(m.get("id"))) for pid, p in display_providers.items()
                     for m in (p.get("models") or [])
                     if isinstance(m, dict) and m.get("disabled") is True and m.get("id")
                     and not m.get("_isBuiltinModel")]
    lines.append("【4】禁用模型（保存后不写入 models.json → Pi 侧不存在）")
    lines.append("  " + ("、".join(f"{pid}/{mid}" for pid, mid in disabled_rows) if disabled_rows else "无"))
    lines.append("")

    lines.append("【5】Pi 排序补丁（让 Pi 的厂商分组顺序 = 工具的厂商顺序）")
    if pst.get("error"):
        lines.append("  ⚠️  " + str(pst.get("error")))
    else:
        auto = "已开启" if pst.get("enabled") else "未开启"
        lines.append(f"  · 自动维护: {auto}")
        lines.append(f"  · Pi 安装目录: {pst.get('packageDir')}（版本 {pst.get('piVersion') or '未知'}）")
        lines.append(f"  · 补丁状态: {pst.get('patchedCount', 0)}/{pst.get('targetCount', 0)} 个文件已打"
                     + ("  ✅" if pst.get("fullyPatched") else "  ⚠️"))
        for row in pst.get("targets") or []:
            lines.append("       " + ("✅" if row.get("patched") else "⬜") + " " + str(row.get("rel")))
        if pst.get("versionChanged"):
            lines.append(f"  ⚠️  Pi 已升级（{pst.get('stateVersion')} → {pst.get('piVersion')}），"
                         "需重新打补丁（保存任意修改或点「🧩 Pi 顺序补丁」即自动重打）")
        lines.append(f"  · 顺序文件: {pst.get('orderFilePath')}")
        lines.append("       当前内容 " + str(len(pst.get("order") or [])) + " 项: "
                     + (" → ".join((pst.get("order") or [])[:14]) + (" …" if len(pst.get("order") or []) > 14 else "")
                        if pst.get("order") else "（空，将回退为 Pi 原生字母序）"))
        lines.append(f"  · 备份目录: {pst.get('backupDir')}")
    lines.append("")

    lines.append("【6】工具自身体检（内嵌 JS 语法 / 转义隐患）")
    try:
        _bad = scan_tool_js_escapes()
        _ok, _msg = verify_tool_html_js()
    except Exception as _e:
        _bad, _ok, _msg = [], True, "自检异常（已跳过）: %s" % _e
    if _bad:
        lines.append("  ⚠️  源码中有 %d 处会被 Python 提前解释的转义（应写成双反斜杠）" % len(_bad))
        for _ln, _esc, _txt in _bad[:5]:
            lines.append("       第 %d 行 %s | %s" % (_ln, _esc, _txt))
    else:
        lines.append("  · 转义体检（HTML_CONTENT 源码区域）: ✅ 无隐患")
    if _ok:
        lines.append("  · 内嵌 JS 语法 node --check: ✅ 通过")
    else:
        lines.append("  · 内嵌 JS 语法 node --check: ❌ 失败 —— 界面会停在静态初始态（状态栏仍是初始文字）")
        lines.append("       " + clip_text(str(_msg).replace("\n", " "), 3000))
    lines.append("")

    lines.append("── 结论 ──")
    lines.append(f"  厂商 {len(tool_order)} 个 / 模型 {total_models} 个；厂商内部顺序一致 {ok_providers}/{len(tool_order)}")
    if mismatch_providers:
        lines.append("  ⚠️  以下厂商尚未把当前顺序写入 models.json：" + "、".join(mismatch_providers))
    else:
        lines.append("  ✅ 工具里的「厂商内部模型顺序」= models.json 保存顺序 = Pi 重启后的顺序")
    lines.append("  ℹ️  厂商之间的先后顺序：Pi 原生固定为字母序"
                 + ("；已用「🧩 顺序补丁」改为跟随工具 ✅" if patch_on
                    else "，如需让 Pi 跟随工具 → 使用「🧩 顺序补丁」（报告【5】）。"))
    return "\n".join(lines)


# ==========================================================
# Pi 排序补丁：让 Pi 的「厂商分组顺序」= 工具里的厂商顺序
# ----------------------------------------------------------
# Pi 的 /model 选择器、设置面板与 `pi --list-models` 都硬编码了
#     a.provider.localeCompare(b.provider)
# 来对厂商分组排序，因此 models.json 里的厂商键顺序对 Pi 完全无效。
#
# 本补丁把上述比较替换为「读顺序文件 → 按排名比较」，从而实现：
#   · 工具里拖拽/保存 → 顺序文件刷新 → Pi（重启或重开选择器）即按新顺序分组；
#   · 未列入顺序文件的厂商排在最后（它们之间仍按字母序），不会报错；
#   · 顺序文件缺失/损坏 → 自动回退为 Pi 原生字母序（零副作用）。
#
# 安全措施：锚点正则校验 → 原始文件备份 → 原子写入 → node --check 语法门禁
#           → 失败立即回滚 → 支持一键还原（revert_pi_order_patch）。
# ==========================================================

PI_PATCH_VERSION = 1
PI_PATCH_START = f"/* pi-model-manager:provider-order-patch v{PI_PATCH_VERSION} (start) */"
PI_PATCH_END = f"/* pi-model-manager:provider-order-patch v{PI_PATCH_VERSION} (end) */"
PI_PROVIDER_ORDER_PATH = DEFAULT_AGENT_DIR / "pi-provider-order.json"
PI_PATCH_BACKUP_DIR = DEFAULT_AGENT_DIR / "pi-order-patch-backup"
PI_PATCH_STATE_PATH = DEFAULT_AGENT_DIR / "pi-order-patch-state.json"

# 补丁锚点：minify 后形如 a.provider.localeCompare(b2.provider)
PI_PROVIDER_CMP_RE = re.compile(
    r"([A-Za-z_$][\w$]*)\.provider\.localeCompare\(([A-Za-z_$][\w$]*)\.provider\)")
# 还原锚点：__piomCmp$(a.provider,b2.provider)
PI_PATCHED_CMP_RE = re.compile(
    r"__piomCmp\$\(([A-Za-z_$][\w$]*)\.provider,([A-Za-z_$][\w$]*)\.provider\)")

# 注入到 Pi 源码中的运行时排序助手（纯 ESM 安全：不使用 import/require 语句，
# 依赖 Node 内置 process.getBuiltinModule，并对 require 作 TDZ 安全降级）
PI_PATCH_HELPER = """/* pi-model-manager:provider-order-patch v1 (start) */
const __piom$ = (() => {
  const pr = globalThis.process;
  const env = (pr && pr.env) || {};
  const agentDir = env.PI_CODING_AGENT_DIR || ((env.USERPROFILE || env.HOME || "") + "/.pi/agent");
  const orderFile = agentDir + "/pi-provider-order.json";
  const cache = { mtime: -1, list: [] };
  let fsMod;
  const resolveFs = () => {
    if (fsMod !== undefined) return fsMod;
    fsMod = null;
    try { if (pr && typeof pr.getBuiltinModule === "function") fsMod = pr.getBuiltinModule("node:fs") || null; } catch (e) {}
    if (!fsMod) { try { if (typeof require === "function") fsMod = require("node:fs") || null; } catch (e) {} }
    return fsMod;
  };
  return () => {
    const fs = resolveFs();
    if (!fs) return [];
    try {
      const st = fs.statSync(orderFile);
      if (st.mtimeMs !== cache.mtime) {
        const raw = JSON.parse(fs.readFileSync(orderFile, "utf8"));
        const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.order) ? raw.order : []);
        cache.list = list.map((x) => String(x));
        cache.mtime = st.mtimeMs;
      }
    } catch (e) { cache.mtime = -1; cache.list = []; }
    return cache.list;
  };
})();
const __piomRank$ = (pid) => {
  const list = __piom$();
  const id = String(pid);
  let i = list.indexOf(id);
  if (i < 0) {
    const low = id.toLowerCase();
    i = list.findIndex((x) => x.toLowerCase() === low);
  }
  return i < 0 ? list.length + 1000 : i;
};
const __piomCmp$ = (a, b) => {
  const ra = __piomRank$(a);
  const rb = __piomRank$(b);
  return ra !== rb ? ra - rb : String(a).localeCompare(String(b));
};
/* pi-model-manager:provider-order-patch v1 (end) */
"""

_PI_PATCH_TARGET_CACHE = {}


def invalidate_pi_patch_cache():
    _PI_PATCH_TARGET_CACHE.clear()


def get_pi_package_dir():
    """定位 Pi 的安装目录（补丁目标根目录）。"""
    candidates = []
    env_dir = os.environ.get("PI_PACKAGE_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent")
    candidates.extend([
        Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent",
        Path.home() / ".npm-global" / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent",
        Path("/usr/local/lib/node_modules/@earendil-works/pi-coding-agent"),
        Path("/usr/lib/node_modules/@earendil-works/pi-coding-agent"),
    ])
    for c in candidates:
        try:
            if (c / "dist").is_dir():
                return c
        except Exception:
            continue
    return None


def pi_installed_version(pkg_dir):
    if not pkg_dir:
        return ""
    try:
        data = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8-sig"))
        return str(data.get("version") or "")
    except Exception:
        return ""


def text_has_provider_cmp(text):
    r"""快速判定文本是否含「厂商排序锚点」。

    性能：直接在 4 MB 压缩包上跑 ``PI_PROVIDER_CMP_RE`` 会因 ``[\w$]*`` 逐字符回溯
    耗时 3s+（实测 275 个文件共 4.6s）。改为先用字面量定位（约 5ms），再在命中处
    附近的小窗口内做精确正则校验 —— 两者等效，快 30 倍以上。
    """
    needle = ".provider.localeCompare("
    idx = text.find(needle)
    while idx != -1:
        seg = text[max(0, idx - 400): idx + 400]
        if PI_PROVIDER_CMP_RE.search(seg):
            return True
        idx = text.find(needle, idx + 1)
    return False


def find_pi_patch_targets(pkg_dir, use_cache=True):
    """扫描 Pi dist 目录，找出所有含厂商排序锚点的 JS 文件（不依赖具体 chunk 名）。"""
    if not pkg_dir:
        return []
    key = str(pkg_dir) + "|" + pi_installed_version(pkg_dir)
    if use_cache and key in _PI_PATCH_TARGET_CACHE:
        return _PI_PATCH_TARGET_CACHE[key]
    found = []
    dist = pkg_dir / "dist"
    if dist.is_dir():
        for f in sorted(dist.rglob("*.js")):
            if f.name.endswith(".map"):
                continue
            try:
                text = read_pi_source(f)
            except Exception:
                continue
            if text_has_provider_cmp(text) or PI_PATCH_START in text:
                found.append(f)
    if use_cache:
        _PI_PATCH_TARGET_CACHE[key] = found
    return found


def pi_patch_backup_path(pkg_dir, target):
    try:
        rel = target.relative_to(pkg_dir)
        safe = "__".join(rel.parts)
    except Exception:
        safe = target.name
    return PI_PATCH_BACKUP_DIR / safe


def is_pi_file_patched(text):
    return PI_PATCH_START in text


def read_pi_source(path):
    """以「保留原始换行符」方式读取源码（newline=""），确保打/还原补丁字节级可逆。"""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def patch_pi_source(text):
    """把源文本中的厂商比较替换为按顺序文件排名比较，并注入助手。返回 (新文本, 替换数)。"""
    if PI_PATCH_START in text:
        return text, 0
    new_text, count = PI_PROVIDER_CMP_RE.subn(
        lambda m: f"__piomCmp$({m.group(1)}.provider,{m.group(2)}.provider)", text)
    if count == 0:
        return text, 0
    nl = "\r\n" if "\r\n" in text else "\n"
    helper = PI_PATCH_HELPER.replace("\r\n", "\n").replace("\n", nl)
    return helper + new_text, count


def unpatch_pi_source(text):
    """还原：移除注入的助手并恢复原生比较。返回 (新文本, 替换数)。"""
    if PI_PATCH_START not in text:
        return text, 0
    reverted = text
    start = reverted.find(PI_PATCH_START)
    end = reverted.find(PI_PATCH_END)
    if start != -1 and end != -1:
        end += len(PI_PATCH_END)
        while end < len(reverted) and reverted[end] in "\r\n":
            end += 1
        reverted = reverted[:start] + reverted[end:]
    else:
        # 标记不完整（仅单侧残留）→ 逐行剔除标记行，避免污染
        keep = [ln for ln in reverted.splitlines(True)
                if PI_PATCH_START not in ln and PI_PATCH_END not in ln]
        reverted = "".join(keep)
    reverted, count = PI_PATCHED_CMP_RE.subn(
        lambda m: f"{m.group(1)}.provider.localeCompare({m.group(2)}.provider)", reverted)
    return reverted, count


def _atomic_write_text(path, text):
    tmp = path.with_name(path.name + ".pimm.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(str(tmp), str(path))


def _backup_file(src, dst):
    """字节级备份（保留原始换行/元数据，不做任何编码转换）。"""
    tmp = dst.with_name(dst.name + ".pimm.tmp")
    shutil.copy2(str(src), str(tmp))
    os.replace(str(tmp), str(dst))


def verify_js_syntax(path):
    """用 node --check 做语法门禁（无 node 时跳过，不阻塞）。"""
    node = shutil.which("node") or "node"
    kwargs = {}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run([node, "--check", str(path)], capture_output=True,
                              text=True, timeout=120, **kwargs)
    except FileNotFoundError:
        return True, "未找到 node，跳过语法校验"
    except Exception as e:
        return True, f"语法校验异常（已跳过）: {e}"
    if proc.returncode == 0:
        return True, ""
    return False, clip_text((proc.stderr or proc.stdout or "").strip(), 8000)


def write_pi_provider_order(path_value=None, order_list=None):
    """写出 Pi 排序补丁读取的顺序文件（默认取工具窗口当前展示的厂商顺序）。"""
    if order_list is None:
        try:
            cfg = load_config(path_value)
            order_list = list((cfg.get("providers") or {}).keys())
            # 工具开启了「⇅ Pi 字母序」时，窗口里看到的是字母序 → 顺序文件同步为字母序，
            # 保证「Pi 分组顺序 == 工具窗口显示顺序」始终成立。
            if get_tool_prefs().get("alignProviderOrder"):
                order_list = sorted(order_list, key=pi_name_sort_key)
        except Exception:
            order_list = []
    payload = {
        "version": PI_PATCH_VERSION,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "source": "pi-model-manager：工具中的厂商显示顺序（补丁读取此顺序给 Pi 分组）",
        "order": [str(x) for x in (order_list or [])],
    }
    try:
        PI_PROVIDER_ORDER_PATH.parent.mkdir(parents=True, exist_ok=True)
        PI_PROVIDER_ORDER_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        return []
    return payload["order"]


def load_pi_patch_state():
    if not PI_PATCH_STATE_PATH.exists():
        return {}
    try:
        raw = PI_PATCH_STATE_PATH.read_text(encoding="utf-8-sig")
        clean = strip_json_comments(raw).strip()
        data = json.loads(clean) if clean else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pi_patch_state(state):
    try:
        PI_PATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PI_PATCH_STATE_PATH.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def apply_pi_order_patch(path_value=None, order_list=None):
    """给 Pi 安装目录打入「厂商按工具顺序分组」补丁（幂等、带备份与语法门禁）。"""
    result = {"success": False, "applied": [], "skipped": [], "failed": [],
              "error": "", "targets": 0, "replacements": 0, "order": []}
    pkg_dir = get_pi_package_dir()
    if pkg_dir is None:
        result["error"] = "未找到 Pi 安装目录（@earendil-works/pi-coding-agent）"
        return result
    order = write_pi_provider_order(path_value, order_list)
    result["order"] = order
    targets = find_pi_patch_targets(pkg_dir, use_cache=False)
    result["targets"] = len(targets)
    if not targets:
        result["error"] = "未在 Pi 安装目录中找到可打补丁的排序代码（可能 Pi 版本已改动）"
        return result
    try:
        PI_PATCH_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        result["error"] = f"无法创建备份目录: {e}"
        return result

    file_records = {}
    for target in targets:
        rel = str(target.relative_to(pkg_dir))
        try:
            original = read_pi_source(target)
        except Exception as e:
            result["failed"].append({"file": rel, "error": f"读取失败: {e}"})
            continue
        backup = pi_patch_backup_path(pkg_dir, target)
        try:
            if backup.exists():
                old_backup = read_pi_source(backup)
                # 备份内容与当前文件不一致 → Pi 已升级，刷新备份为新版原始文件
                if old_backup != original and PI_PATCH_START not in original:
                    _backup_file(target, backup)
            else:
                _backup_file(target, backup)
        except Exception as e:
            result["failed"].append({"file": rel, "error": f"备份失败: {e}"})
            continue

        if PI_PATCH_START in original:
            result["skipped"].append(rel)
            file_records[rel] = {"patched": True, "backup": str(backup)}
            continue

        patched, count = patch_pi_source(original)
        if count == 0:
            result["failed"].append({"file": rel, "error": "锚点未匹配，已跳过（Pi 可能改写了排序实现）"})
            continue
        try:
            _atomic_write_text(target, patched)
        except Exception as e:
            result["failed"].append({"file": rel, "error": f"写入失败: {e}"})
            continue
        ok, msg = verify_js_syntax(target)
        if not ok:
            try:
                _atomic_write_text(target, original)
            except Exception:
                pass
            result["failed"].append({"file": rel, "error": f"语法门禁未通过，已回滚: {msg}"})
            continue
        result["applied"].append(rel)
        result["replacements"] += count
        file_records[rel] = {"patched": True, "backup": str(backup), "replacements": count}

    invalidate_pi_patch_cache()
    result["success"] = bool(result["applied"] or result["skipped"]) and not result["failed"]
    state = {
        "version": PI_PATCH_VERSION,
        "piVersion": pi_installed_version(pkg_dir),
        "appliedAt": datetime.now().isoformat(timespec="seconds"),
        "packageDir": str(pkg_dir),
        "order": order,
        "files": file_records,
        "lastResult": {"applied": result["applied"], "failed": result["failed"]},
    }
    save_pi_patch_state(state)
    return result


def revert_pi_order_patch():
    """还原 Pi 原版排序逻辑（移除注入助手 + 恢复原生 localeCompare）。"""
    result = {"success": False, "reverted": [], "failed": [], "error": ""}
    pkg_dir = get_pi_package_dir()
    if pkg_dir is None:
        result["error"] = "未找到 Pi 安装目录"
        return result
    state = load_pi_patch_state()
    candidates = set(find_pi_patch_targets(pkg_dir, use_cache=False))
    for rel in (state.get("files") or {}):
        candidates.add(Path(rel) if Path(rel).is_absolute() else (pkg_dir / rel))
    if not candidates:
        result["error"] = "未找到任何补丁目标"
        return result
    for target in sorted(candidates, key=lambda p: str(p)):
        rel = str(target.relative_to(pkg_dir)) if str(target).startswith(str(pkg_dir)) else str(target)
        if not target.exists():
            continue
        try:
            current = read_pi_source(target)
        except Exception as e:
            result["failed"].append({"file": rel, "error": f"读取失败: {e}"})
            continue
        if not is_pi_file_patched(current):
            continue
        reverted, count = unpatch_pi_source(current)
        try:
            _atomic_write_text(target, reverted)
        except Exception as e:
            result["failed"].append({"file": rel, "error": f"写入失败: {e}"})
            continue
        ok, msg = verify_js_syntax(target)
        if not ok:
            try:
                _atomic_write_text(target, current)
            except Exception:
                pass
            result["failed"].append({"file": rel, "error": f"语法门禁未通过，已回滚: {msg}"})
            continue
        result["reverted"].append({"file": rel, "anchors": count})
    invalidate_pi_patch_cache()
    try:
        if PI_PATCH_STATE_PATH.exists():
            PI_PATCH_STATE_PATH.unlink()
    except Exception:
        pass
    result["success"] = not result["failed"]
    return result


def pi_order_patch_status():
    """补丁状态：安装位置 / Pi 版本 / 每个目标文件是否已打 / 是否需要重打。"""
    prefs = get_tool_prefs()
    status = {
        "enabled": bool(prefs.get("patchPiOrder")),
        "packageDir": "",
        "piVersion": "",
        "stateVersion": "",
        "versionChanged": False,
        "targets": [],
        "targetCount": 0,
        "patchedCount": 0,
        "fullyPatched": False,
        "needsRepatch": False,
        "order": [],
        "orderFilePath": str(PI_PROVIDER_ORDER_PATH),
        "backupDir": str(PI_PATCH_BACKUP_DIR),
        "statePath": str(PI_PATCH_STATE_PATH),
        "error": "",
    }
    pkg_dir = get_pi_package_dir()
    if pkg_dir is None:
        status["error"] = "未找到 Pi 安装目录"
        return status
    status["packageDir"] = str(pkg_dir)
    status["piVersion"] = pi_installed_version(pkg_dir)
    state = load_pi_patch_state()
    status["stateVersion"] = str(state.get("piVersion") or "")
    status["versionChanged"] = bool(status["stateVersion"]) and status["stateVersion"] != status["piVersion"]
    status["installAt"] = str(state.get("appliedAt") or "")
    try:
        if PI_PROVIDER_ORDER_PATH.exists():
            raw = json.loads(PI_PROVIDER_ORDER_PATH.read_text(encoding="utf-8-sig"))
            status["order"] = [str(x) for x in (raw.get("order") if isinstance(raw, dict) else raw) or []]
            status["orderUpdatedAt"] = str(raw.get("updatedAt") or "") if isinstance(raw, dict) else ""
    except Exception:
        status["order"] = []
    targets = find_pi_patch_targets(pkg_dir)
    status["targetCount"] = len(targets)
    for t in targets:
        try:
            patched = is_pi_file_patched(read_pi_source(t))
        except Exception:
            patched = False
        status["targets"].append({"file": str(t), "rel": str(t.relative_to(pkg_dir)), "patched": patched})
        if patched:
            status["patchedCount"] += 1
    status["fullyPatched"] = status["targetCount"] > 0 and status["patchedCount"] == status["targetCount"]
    status["needsRepatch"] = bool(status["enabled"]) and (
        not status["fullyPatched"] or status["versionChanged"])
    return status


def ensure_pi_order_patch(path_value=None):
    """自动维护入口：开启偏好时，保存/重启后自动补打补丁并刷新顺序文件。"""
    status = pi_order_patch_status()
    if not status["enabled"]:
        status["action"] = "disabled"
        return status
    if status["error"]:
        status["action"] = "error"
        return status
    if status["fullyPatched"] and not status["versionChanged"]:
        write_pi_provider_order(path_value)
        refreshed = pi_order_patch_status()
        refreshed["action"] = "order-refreshed"
        return refreshed
    result = apply_pi_order_patch(path_value)
    updated = pi_order_patch_status()
    updated["action"] = "patched" if result.get("success") else "patch-failed"
    updated["applyResult"] = result
    return updated


def model_reference(pid, model_id):
    return f"{pid}/{model_id}"


THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}


def pattern_matches_model(pattern, pid, model_id):
    """Pi 的 enabledModels 使用 glob 模式匹配 provider/model 或单独的 modelId。"""
    pat = str(pattern or "").strip()
    if not pat:
        return False
    # 允许 "provider/model:high" 这类带思考等级后缀的写法
    base_pat = pat
    last_colon = pat.rfind(":")
    if last_colon != -1:
        suffix = pat[last_colon + 1:].lower()
        if suffix in THINKING_LEVELS:
            base_pat = pat[:last_colon]
    for candidate in (base_pat, pat):
        ref = model_reference(pid, model_id)
        if candidate.lower() == ref.lower() or candidate.lower() == str(model_id).lower():
            return True
        if any(ch in candidate for ch in "*?["):
            try:
                if fnmatch.fnmatch(ref.lower(), candidate.lower()) or fnmatch.fnmatch(str(model_id).lower(), candidate.lower()):
                    return True
            except Exception:
                pass
    return False


def apply_enabled_state_to_config(cfg):
    """兼容占位（保留函数名，避免旧调用点报错）。

    模型“禁用”不再依赖 settings.json 的 enabledModels 白名单：
      · 内置模型 —— 目录由 Pi 原生提供，不可删除也不可隐藏；
      · 自定义模型 —— models.json 为唯一真相，禁用 = 不写入。
    因此本函数无事可做，原样返回。
    """
    return cfg


def clear_enabled_models_whitelist():
    """清除 settings.json 里由工具写入的 enabledModels 白名单。

    白名单是作用域过滤器：一旦存在，Pi 只显示匹配的模型，未列出的厂商会整体消失。
    禁用已改由 models.json 内容决定，白名单不再需要且有害。
    仅当现有白名单符合“工具生成”特征（全为 provider/* 形式的通配）时才清除，
    避免误删主人手工编写的精细白名单。
    """
    try:
        settings = load_pi_settings()
        current = settings.get("enabledModels")
        if not isinstance(current, list) or not current:
            return False
        looks_generated = all(
            isinstance(p, str) and p.endswith("/*") and p.count("/") == 1
            for p in current
        )
        if not looks_generated:
            return False
        settings.pop("enabledModels", None)
        return save_pi_settings(settings)
    except Exception:
        return False


def apply_model_overrides_to_config(cfg):
    """把 models.json 的 modelOverrides 应用到模型对象上（与 Pi 运行时行为一致）。"""
    for pid, provider in (cfg.get("providers") or {}).items():
        if not isinstance(provider, dict):
            continue
        overrides = provider.get("modelOverrides")
        if not isinstance(overrides, dict):
            continue
        for model in provider.get("models") or []:
            if not isinstance(model, dict) or not model.get("id"):
                continue
            ov = overrides.get(model["id"])
            if not isinstance(ov, dict):
                continue
            ov_headers = ov.get("headers")
            if isinstance(ov_headers, dict):
                merged_headers = dict(model.get("headers") or {})
                for hk, hv in ov_headers.items():
                    if hv is not None:
                        merged_headers[str(hk)] = hv
                if merged_headers:
                    model["headers"] = merged_headers
            if ov.get("name"):
                model["name"] = ov["name"]
    return cfg


def sync_pi_enabled_models(cfg):
    """已废弃，保留仅为兼容旧调用点。

    旧实现会把厂商写入 settings.json 的 enabledModels 白名单。白名单是**作用域过滤器**：
    一旦存在，Pi 只显示匹配的模型，未列出的厂商会整体消失，且顺序仍不可控。
    现在禁用语义已改为“不写入 models.json = Pi 侧不存在”，因此不再写白名单。

    这里只做一件事：清除工具历史上写下的白名单，让 Pi 回到“全部可用”状态。
    """
    return clear_enabled_models_whitelist()

def get_pi_ai_dist_dir():
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "node_modules" / "@earendil-works" / "pi-ai" / "dist",
        Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "node_modules" / "@earendil-works" / "pi-ai" / "dist",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def get_pi_ai_catalog_dir():
    dist = get_pi_ai_dist_dir()
    if dist is None:
        return None
    data_dir = dist / "providers" / "data"
    return data_dir if data_dir.is_dir() else None


_BUILTIN_CATALOG_CACHE = None


def load_builtin_catalog(force=False):
    """读取 pi-ai 内置模型目录。

    pi-ai 的 data/*.json 结构为 { "<api>": { "<modelId>": {完整模型定义} } }。
    返回 { providerId: { name, apis, baseUrl, env_keys, models: [完整模型定义] } }
    """
    global _BUILTIN_CATALOG_CACHE
    if _BUILTIN_CATALOG_CACHE is not None and not force:
        return _BUILTIN_CATALOG_CACHE

    catalog = {}
    data_dir = get_pi_ai_catalog_dir()
    if data_dir is not None:
        for f in sorted(data_dir.glob("*.json")):
            if f.name.startswith("."):
                continue
            try:
                raw = json.loads(f.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            for api_name, models in raw.items():
                if not isinstance(models, dict):
                    continue
                for mid, mdef in models.items():
                    if not isinstance(mdef, dict):
                        continue
                    pid = str(mdef.get("provider") or "").strip()
                    if not pid:
                        continue
                    entry = catalog.setdefault(pid, {"models": [], "apis": [], "baseUrl": "", "env_keys": []})
                    entry["models"].append(mdef)
                    if api_name and api_name not in entry["apis"]:
                        entry["apis"].append(api_name)
                    if not entry["baseUrl"] and mdef.get("baseUrl"):
                        entry["baseUrl"] = mdef["baseUrl"]

    # 从厂商实现文件中解析显示名与环境变量名
    dist = get_pi_ai_dist_dir()
    if dist is not None:
        prov_dir = dist / "providers"
        if prov_dir.is_dir():
            for f in sorted(prov_dir.glob("*.js")):
                if f.name.endswith(".models.js") or f.name == "all.js":
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                pid = None
                id_pos = -1
                for m in re.finditer(r'\bid:\s*"([^"]+)"', text):
                    if m.group(1) in catalog:
                        pid = m.group(1)
                        id_pos = m.end()
                        break
                if pid is None:
                    continue
                entry = catalog[pid]
                nm = re.search(r'\bname:\s*"([^"]+)"', text[id_pos:])
                if nm:
                    entry["name"] = nm.group(1)
                envs = []
                for em in re.finditer(r'envApiKeyAuth\(\s*"[^"]*"\s*,\s*\[([^\]]*)\]', text):
                    for s in re.findall(r'"([^"]+)"', em.group(1)):
                        if s not in envs:
                            envs.append(s)
                entry["env_keys"] = envs

    for pid, meta in BUILTIN_PROVIDERS_META.items():
        entry = catalog.setdefault(pid, {"models": [], "apis": [], "baseUrl": "", "env_keys": []})
        entry.setdefault("name", meta.get("name"))
        if not entry.get("baseUrl"):
            entry["baseUrl"] = meta.get("baseUrl", "")
        if not entry.get("apis") and meta.get("api"):
            entry["apis"] = [meta["api"]]
        if not entry.get("env_keys") and meta.get("env_key"):
            entry["env_keys"] = [meta["env_key"]]

    _BUILTIN_CATALOG_CACHE = catalog
    return catalog


def read_credential_sources():
    """读取 Pi 的 auth.json / models-store.json / 环境变量中的凭据信息。"""
    auth_data = {}
    auth_file = DEFAULT_AGENT_DIR / "auth.json"
    if auth_file.exists():
        try:
            loaded = json.loads(strip_json_comments(auth_file.read_text(encoding="utf-8-sig")))
            if isinstance(loaded, dict):
                auth_data = loaded
        except Exception:
            pass

    store_data = {}
    store_file = DEFAULT_AGENT_DIR / "models-store.json"
    if store_file.exists():
        try:
            loaded = json.loads(strip_json_comments(store_file.read_text(encoding="utf-8-sig")))
            if isinstance(loaded, dict):
                store_data = loaded
        except Exception:
            pass
    return auth_data, store_data


def resolve_provider_credential(pid, auth_data, catalog_entry):
    """返回 (api_key, auth_type)。优先 auth.json，其次环境变量。"""
    api_key = ""
    auth_type = None
    entry = auth_data.get(pid) if isinstance(auth_data, dict) else None
    if isinstance(entry, dict):
        api_key = entry.get("key") or entry.get("apiKey") or entry.get("access") or entry.get("token") or ""
        auth_type = entry.get("type", "api_key")
    elif isinstance(entry, str):
        api_key = entry
        auth_type = "api_key"
    if not api_key:
        for env_name in (catalog_entry or {}).get("env_keys", []) or []:
            val = os.environ.get(env_name, "")
            if val:
                api_key = val
                auth_type = "env"
                break
    return api_key, auth_type


def provider_is_authenticated(pid, auth_data, store_data, catalog_entry):
    """判断 Pi 侧该内置厂商是否具备可用凭据（无凭据的厂商在 Pi 中本就不出现）。

    凭据来源：
    1. auth.json 中存在非空条目 (api_key 或 oauth token)
    2. 环境变量 (如 DEEPSEEK_API_KEY)

    注意：models-store.json 仅为模型目录快照缓存，不代表已认证。
    """
    api_key, auth_type = resolve_provider_credential(pid, auth_data, catalog_entry)
    return bool(api_key or auth_type)


def scan_builtin_providers(existing_providers=None):
    """扫描 Pi 内置厂商及其完整模型目录。"""
    catalog = load_builtin_catalog()
    auth_data, store_data = read_credential_sources()
    existing_providers = existing_providers or {}

    builtins = {}
    for pid, entry in catalog.items():
        auth_entry = auth_data.get(pid) if isinstance(auth_data, dict) else None
        api_key, auth_type = resolve_provider_credential(pid, auth_data, entry)
        has_store = isinstance(store_data.get(pid), dict)
        is_core = pid in CORE_BUILTIN_PROVIDERS
        has_existing = pid in existing_providers
        authenticated = provider_is_authenticated(pid, auth_data, store_data, entry)

        # 仅扫描满足以下条件的内置厂商：
        # 1. 在 auth.json / models-store.json / 环境变量中有可用凭据 (authenticated)
        # 2. 或已经在用户的 models.json 中明确配置过 (has_existing)
        # 没有凭据且用户从未配置过的内置厂商不自动注入，避免界面充斥未配置项。
        if not (authenticated or has_existing):
            continue

        models = []
        seen_model_ids = {}
        for mdef in entry.get("models", []):
            if not isinstance(mdef, dict) or not mdef.get("id"):
                continue
            copy = json.loads(json.dumps(mdef))
            seen_model_ids[copy["id"]] = len(models)
            models.append(copy)

        # models-store.json 是 Pi 动态刷新后的真实厂商目录，优先级高于内置快照
        s_entry = store_data.get(pid)
        if isinstance(s_entry, dict):
            for sm in s_entry.get("models") or []:
                if not isinstance(sm, dict) or not sm.get("id"):
                    continue
                copy = json.loads(json.dumps(sm))
                copy.setdefault("provider", pid)
                idx = seen_model_ids.get(copy["id"])
                if idx is None:
                    seen_model_ids[copy["id"]] = len(models)
                    models.append(copy)
                else:
                    merged = json.loads(json.dumps(models[idx]))
                    merged.update(copy)
                    models[idx] = merged

        apis = entry.get("apis") or []
        display_name = entry.get("name") or pid
        if is_core and not display_name.endswith("(Built-in)"):
            display_name = f"{display_name} (Built-in)"

        builtins[pid] = {
            "name": display_name,
            "api": apis[0] if apis else (BUILTIN_PROVIDERS_META.get(pid, {}).get("api") or "openai-completions"),
            "baseUrl": entry.get("baseUrl") or BUILTIN_PROVIDERS_META.get(pid, {}).get("baseUrl", ""),
            "apiKey": api_key,
            "models": models,
            "_isBuiltin": True,
            "_authType": auth_type or "builtin",
            "_apiKeyScanned": bool(api_key),
        }
    return builtins

def clean_pi_config(cfg):
    if not isinstance(cfg, dict):
        return {"providers": {}}
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        cfg["providers"] = {}
        return cfg
    try:
        catalog = load_builtin_catalog()
    except Exception:
        catalog = {}
    for pid, provider in list(providers.items()):
        if not isinstance(provider, dict):
            continue
        provider.pop("_builtinOnly", None)
        # 废弃的历史字段：不再写盘
        provider.pop("_removed", None)
        provider.pop("_modelOrder", None)
        for key in ("name", "baseUrl", "apiKey", "api", "authHeader"):
            if provider.get(key) == "":
                provider.pop(key, None)

        # 清理与规范化 API Key 池 (apiKeys)
        api_keys_pool = provider.get("apiKeys")
        if isinstance(api_keys_pool, list):
            cleaned_pool = []
            seen_pool_ids = set()
            for kitem in api_keys_pool:
                if not isinstance(kitem, dict):
                    continue
                kid = str(kitem.get("id") or "").strip()
                ksecret = str(kitem.get("key") or "").strip()
                if not kid or not ksecret or kid in seen_pool_ids:
                    continue
                seen_pool_ids.add(kid)
                kname = str(kitem.get("name") or "").strip()
                cleaned_item = {"id": kid, "key": ksecret}
                if kname:
                    cleaned_item["name"] = kname
                cleaned_pool.append(cleaned_item)
            if cleaned_pool:
                provider["apiKeys"] = cleaned_pool
            else:
                provider.pop("apiKeys", None)
        else:
            provider.pop("apiKeys", None)

        # 构建可供快速查验的 pool 映射
        active_pool_map = {item["id"]: item["key"] for item in provider.get("apiKeys", [])}

        models = provider.get("models")
        if isinstance(models, list):
            cleaned = []
            seen = set()
            for model in models:
                if not isinstance(model, dict):
                    continue
                model_id = str(model.get("id", "")).strip()
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                model["id"] = model_id
                for key in ("name", "api", "baseUrl"):
                    if model.get(key) == "":
                        model.pop(key, None)
                if model.get("disabled") is True:
                    model["disabled"] = True
                else:
                    model.pop("disabled", None)

                # 处理模型级 Key / headers
                key_ref = str(model.get("apiKeyRef") or "").strip()
                headers = model.get("headers")
                if not isinstance(headers, dict):
                    headers = {}
                else:
                    headers = dict(headers)

                # 如果绑定了有效的池中 Key，同步更新 headers["Authorization"]
                if key_ref and key_ref in active_pool_map:
                    model["apiKeyRef"] = key_ref
                    headers["Authorization"] = f"Bearer {active_pool_map[key_ref]}"
                else:
                    model.pop("apiKeyRef", None)

                # 清理空 headers
                clean_headers = {}
                for hk, hv in headers.items():
                    if hk and hv:
                        clean_headers[str(hk)] = str(hv)
                if clean_headers:
                    model["headers"] = clean_headers
                else:
                    model.pop("headers", None)

                model.pop("_failStreak", None)
                model.pop("_lastError", None)

                # 官方参数对齐：缺失补齐；历史默认痕迹按官方推断值刷新
                refresh_official_specs(model, catalog)

                cleaned.append(model)
            provider["models"] = cleaned
    return cfg


def build_pi_disk_config(cfg):
    """生成真正写入 models.json 的内容。

    核心语义（重要）：
    - 内置厂商：模型目录由 Pi 原生提供，**既不删除也不隐藏**。写盘时补全完整目录
      元数据，保证 Pi 侧不会回落到 128K 上下文等默认值。
    - 自定义厂商：models.json 是唯一真相。被禁用的自定义模型**不写入**，
      即 Pi 侧不存在 —— 禁用 = 物理移除。
    - 写入的模型必须补全完整目录元数据（cost / contextWindow / maxTokens /
      reasoning / input / compat），否则 Pi 侧会回落到 128K 上下文等默认值。

    注意：Pi 的 ModelDefinitionSchema 没有 disabled 字段，因此内置模型的 disabled
    标记对 Pi 完全无效，工具侧也不提供该操作。
    """
    normalized = clean_pi_config(json.loads(json.dumps(cfg)))
    catalog = load_builtin_catalog()
    auth_data, _ = read_credential_sources()
    out_providers = {}

    for pid, provider in (normalized.get("providers") or {}).items():
        if not isinstance(provider, dict):
            continue
        entry = catalog.get(pid) or {}
        is_builtin = bool(provider.get("_isBuiltin")) or pid in catalog
        catalog_models = {}
        for mdef in entry.get("models") or []:
            if isinstance(mdef, dict) and mdef.get("id"):
                catalog_models[mdef["id"]] = mdef
        catalog_apis = entry.get("apis") or []
        provider_api = provider.get("api") or (catalog_apis[0] if catalog_apis else "")
        provider_base = provider.get("baseUrl") or entry.get("baseUrl") or ""

        out = {}

        # 显示名：内置厂商保留 Pi 原生名称，除非用户确实改过
        name = provider.get("name")
        if name:
            scanned_name = entry.get("name") or ""
            if not (is_builtin and name in (scanned_name, f"{scanned_name} (Built-in)")):
                out["name"] = name

        # Base URL / 协议：与内置目录一致时不重复写盘
        if provider.get("baseUrl"):
            if not (is_builtin and provider["baseUrl"] == entry.get("baseUrl")):
                out["baseUrl"] = provider["baseUrl"]
        if provider.get("api"):
            if not (is_builtin and provider["api"] in catalog_apis):
                out["api"] = provider["api"]

        # API Key：内置厂商如果与 auth.json / 环境变量一致则不写盘，避免密钥陈旧覆盖
        if provider.get("apiKey"):
            if is_builtin:
                scanned_key, _ = resolve_provider_credential(pid, auth_data, entry)
                if provider["apiKey"] != scanned_key:
                    out["apiKey"] = provider["apiKey"]
            else:
                out["apiKey"] = provider["apiKey"]

        for key in ("compat", "authHeader"):
            if provider.get(key) not in (None, "", {}):
                out[key] = provider[key]

        # 自定义 OpenAI 兼容服务商如果未配置 compat，自动补全安全兼容垫片（屏蔽 developer role、统一使用 max_tokens、关闭 store）
        if not is_builtin and provider.get("api") == "openai-completions" and not out.get("compat"):
            out["compat"] = {
                "supportsDeveloperRole": False,
                "maxTokensField": "max_tokens",
                "supportsStore": False,
            }

        if provider.get("apiKeys"):
            out["apiKeys"] = provider["apiKeys"]

        out_models = []
        overrides = {}
        # 按用户在工具中的排列顺序输出模型（数组顺序 = Pi 侧显示顺序）
        for model in provider.get("models") or []:
            if not isinstance(model, dict):
                continue
            mid = model.get("id")
            if not mid:
                continue
            headers = model.get("headers") if isinstance(model.get("headers"), dict) else {}
            headers = {str(k): str(v) for k, v in headers.items() if k and v}
            is_builtin_model = bool(model.get("_isBuiltinModel")) or mid in catalog_models

            # 禁用 = 不写入 → Pi 侧彻底不存在。
            # 仅对“自定义模型”成立；内置模型由 Pi 原生目录提供，必须完整保留
            # （既不删除也不隐藏），否则工具与 Pi 的模型集合会不一致。
            if model.get("disabled") is True and not is_builtin_model:
                continue

            if not is_builtin_model:
                # 用户自建模型：完整写出，必要时补全 api / baseUrl
                entry_out = {k: v for k, v in model.items()
                             if k not in ("_isBuiltinModel", "_builtinOrig", "apiKeyRef")
                             and k != "disabled"}
                if not entry_out.get("api") and provider_api and "api" not in out:
                    entry_out["api"] = provider_api
                if not entry_out.get("baseUrl") and provider_base and "baseUrl" not in out:
                    entry_out["baseUrl"] = provider_base

                # 确保自建模型写盘时具备完整的上下文与多模态定义（含官方参数刷新）
                refresh_official_specs(entry_out, catalog)

                out_models.append(entry_out)
                continue

            # 内置模型：以完整目录定义为基底，叠加用户改动
            base = json.loads(json.dumps(model.get("_builtinOrig") or catalog_models.get(mid) or {"id": mid}))
            base.pop("provider", None)
            base.pop("disabled", None)
            if model.get("name"):
                base["name"] = model["name"]
            for key in ("api", "baseUrl", "reasoning", "contextWindow", "maxTokens", "input", "cost", "compat", "thinkingLevelMap", "samplingParams"):
                if key in model and model.get(key) is not None:
                    base[key] = model[key]

            # 内置模型若在 catalog 中缺少必要字段，同样兜底补齐（含官方参数刷新）
            refresh_official_specs(base, catalog)

            out_models.append(base)

            original_headers = catalog_models.get(mid, {}).get("headers") or {}
            extra_headers = {k: v for k, v in headers.items() if original_headers.get(k) != v}
            if extra_headers:
                overrides[mid] = {**overrides.get(mid, {}), "headers": extra_headers}

        # 保留用户在 models.json 中已有的 modelOverrides
        managed_ids = {m.get("id") for m in (provider.get("models") or []) if isinstance(m, dict)}
        existing_overrides = provider.get("modelOverrides")
        if isinstance(existing_overrides, dict):
            for oid, oval in existing_overrides.items():
                if not isinstance(oval, dict):
                    continue
                oval = json.loads(json.dumps(oval))
                if oid in managed_ids:
                    # 该模型的 headers 已由界面接管（load_config 已合并进 model.headers），此处重新计算
                    oval.pop("headers", None)
                merged = {**oval, **overrides.get(oid, {})}
                if merged:
                    overrides[oid] = merged

        if out_models:
            out["models"] = out_models
        if overrides:
            out["modelOverrides"] = overrides

        # 顺序：数组顺序即 Pi 侧顺序；不再输出 _modelOrder。

        # 平安性检查：Pi 要求厂商条目至少包含一个“有效字段”，否则会在启动时报 composition error。
        # 参考 provider-composer.js: baseUrl / headers / compat / modelOverrides / models / apiKey / authHeader
        trigger_keys = ("baseUrl", "headers", "compat", "modelOverrides", "models", "apiKey", "authHeader", "oauth")
        if not any(k in out for k in trigger_keys):
            if out and provider_base:
                # 仅有 name 等修饰性字段：补上 baseUrl 以满足 Pi 的校验
                out["baseUrl"] = provider_base
            else:
                # 没有任何需要写盘的内容，完全跳过该厂商（避免无意义地钉住内置默认值）
                continue

        out_providers[pid] = out

    return {"providers": out_providers}


def load_config(path_value=None, merge_builtins=True):
    """读取工具内存配置。

    两种厂商语义：
    - 自定义厂商：models.json 是唯一真相。文件里没有的模型就不存在，不做目录回填；
      模型顺序 = models.json 数组顺序（与 Pi 侧一致）。
    - 内置厂商：合并 Pi 原生目录用于展示，顺序 = Pi 原生目录顺序；模型既不删除也不隐藏。
      未添加 Key / 未 OAuth 登录的内置厂商不进入列表。
    """
    path = normalize_config_path(path_value)
    cfg = {"providers": {}}
    if path.exists():
        try:
            data = read_config_file(path)
            if isinstance(data, dict) and isinstance(data.get("providers"), dict):
                cfg = dict(data)
        except Exception:
            pass

    # 历史遗留：旧版本用 _removed 标记“整厂隐藏”。该机制已废弃，此处一次性消化：
    # 直接从内存配置中剔除这些条目，避免它们被当作“已配置”而重新注入内置目录。
    for _pid in [k for k, v in (cfg.get("providers") or {}).items()
                 if isinstance(v, dict) and v.get("_removed")]:
        cfg["providers"].pop(_pid, None)

    if merge_builtins:
        try:
            builtins = scan_builtin_providers(cfg.get("providers") or {})
            for b_pid, b_prov in builtins.items():
                if b_pid not in cfg["providers"]:
                    cfg["providers"][b_pid] = b_prov
                else:
                    p = cfg["providers"][b_pid]
                    p["_isBuiltin"] = True
                    if not p.get("apiKey") and b_prov.get("apiKey"):
                        p["apiKey"] = b_prov["apiKey"]
                        p["_apiKeyScanned"] = True
                    if not p.get("baseUrl") and b_prov.get("baseUrl"):
                        p["baseUrl"] = b_prov["baseUrl"]
                    user_models = p.get("models", [])
                    user_model_ids = {m["id"]: m for m in user_models if isinstance(m, dict) and m.get("id")}
                    merged_models = []
                    # 先放内置模型（用户覆盖项合并进来），并保留完整目录定义供写盘比对
                    for bm in b_prov.get("models", []):
                        merged = json.loads(json.dumps(bm))
                        merged["_builtinOrig"] = json.loads(json.dumps(bm))
                        if bm["id"] in user_model_ids:
                            merged.update(user_model_ids[bm["id"]])
                            merged["_builtinOrig"] = json.loads(json.dumps(bm))
                        merged["_isBuiltinModel"] = True
                        merged_models.append(merged)
                    # 再放用户自建（非内置）模型
                    builtin_ids = {bm["id"] for bm in b_prov.get("models", [])}
                    for um in user_models:
                        if isinstance(um, dict) and um.get("id") not in builtin_ids:
                            merged_models.append(um)

                    # 顺序：内置模型的显示顺序**必须**等于 Pi 的原生目录顺序。
                    # Pi 的 applyModelsJson 对内置厂商只做“原地 upsert”，
                    # models.json 的数组顺序对内置模型无效 —— 这里也不能套用自定义
                    # 排序，否则工具与 Pi 的顺序会出现偏差。
                    p["models"] = merged_models
        except Exception:
            pass

    try:
        apply_enabled_state_to_config(cfg)
    except Exception:
        pass

    # 将 models.json 的 modelOverrides 投影到模型上（与 Pi 运行时一致），
    # 这样模型级独立 Key / 名称覆盖在界面上可见且可被取消。
    try:
        apply_model_overrides_to_config(cfg)
    except Exception:
        pass

    return cfg


def save_config(cfg, path_value=None):
    path = normalize_config_path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    disk_cfg = build_pi_disk_config(cfg)
    path.write_text(json.dumps(disk_cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return disk_cfg

def sanitize_filename(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return "export"
    safe = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", "."):
            safe.append(ch)
        else:
            safe.append("_")
    result = "".join(safe).strip("._")
    return result or "export"

def get_desktop_dir():
    desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
    return Path(desktop) if os.path.isdir(desktop) else Path.home()

def build_provider_export_text(provider_id, provider, source_path):
    provider = provider if isinstance(provider, dict) else {}
    display_name = provider.get("name") or provider_id
    base_url = provider.get("baseUrl") or ""
    api_key = provider.get("apiKey") or ""
    api_keys_pool = provider.get("apiKeys") or []
    api_type = provider.get("api") or "openai-completions"
    models = provider.get("models") or []
    
    pool_map = {k.get("id"): k for k in api_keys_pool if isinstance(k, dict) and k.get("id")}
    pool_lines = []
    for k in api_keys_pool:
        if not isinstance(k, dict):
            continue
        kid = k.get("id") or ""
        kname = k.get("name") or ""
        kkey = k.get("key") or ""
        kmasked = (kkey[:4] + "••••" + kkey[-3:]) if len(kkey) > 8 else "••••••••"
        desc = f" ({kname})" if kname else ""
        pool_lines.append(f"- [{kid}]{desc}: {kmasked}")
    pool_block = "\n".join(pool_lines) if pool_lines else "- (无密钥池，仅使用默认 Key)"

    model_lines = []
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "").strip()
        if not model_id:
            continue
        model_name = str(model.get("name") or "").strip()
        name_part = f" ({model_name})" if model_name and model_name != model_id else ""
        
        key_ref = model.get("apiKeyRef")
        headers = model.get("headers") or {}
        has_auth_header = bool(headers.get("Authorization") or headers.get("x-api-key"))
        
        auth_note = ""
        if key_ref and key_ref in pool_map:
            pool_item = pool_map[key_ref]
            ref_name = pool_item.get("name") or key_ref
            auth_note = f" [Key池: {ref_name}]"
        elif has_auth_header:
            auth_note = " [独立专属Key]"
        else:
            auth_note = " [继承默认Key]"

        if model.get("disabled") is True:
            auth_note += " [已禁用]"

        model_lines.append(f"- {model_id}{name_part}{auth_note}")
    model_block = "\n".join(model_lines) if model_lines else "- (无模型)"
    key_text = api_key if api_key else "（空）"
    source_text = str(source_path) if source_path else ""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "Pi Model Manager 导出\n"
        f"导出时间: {timestamp}\n"
        f"配置文件: {source_text}\n"
        "\n"
        "服务商信息\n"
        f"显示名称: {display_name}\n"
        f"服务商 ID: {provider_id}\n"
        f"URL: {base_url}\n"
        f"默认 Key: {key_text}\n"
        f"API类型: {api_type}\n"
        f"模型数量: {len(models)}\n"
        "\n"
        "密钥池 (Key Pool)\n"
        f"{pool_block}\n"
        "\n"
        "模型列表\n"
        f"{model_block}\n"
    )

def build_all_providers_export_text(config, source_path):
    providers = config.get("providers") if isinstance(config, dict) else {}
    providers = providers if isinstance(providers, dict) else {}
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_text = str(source_path) if source_path else ""
    parts = [
        "Pi Model Manager 导出",
        f"导出时间: {timestamp}",
        f"配置文件: {source_text}",
        f"服务商数量: {len(providers)}",
        "",
    ]
    for idx, (provider_id, provider) in enumerate(providers.items(), start=1):
        if not isinstance(provider, dict):
            continue
        parts.append(f"[{idx}] {provider.get('name') or provider_id}")
        parts.append(f"服务商 ID: {provider_id}")
        parts.append(f"URL: {provider.get('baseUrl') or ''}")
        parts.append(f"默认 Key: {provider.get('apiKey') or '（空）'}")
        parts.append(f"API类型: {provider.get('api') or 'openai-completions'}")
        
        api_keys_pool = provider.get("apiKeys") or []
        pool_map = {k.get("id"): k for k in api_keys_pool if isinstance(k, dict) and k.get("id")}
        if api_keys_pool:
            parts.append(f"密钥池: {len(api_keys_pool)} 组")
            for k in api_keys_pool:
                if isinstance(k, dict):
                    parts.append(f"  * [{k.get('id')}] {k.get('name') or ''}")
                    
        models = provider.get('models') or []
        parts.append(f"模型数量: {len(models)}")
        if models:
            parts.append("模型列表:")
            for model in models:
                if not isinstance(model, dict):
                    continue
                mid = str(model.get('id') or '').strip()
                if not mid:
                    continue
                mname = str(model.get('name') or '').strip()
                name_part = f" ({mname})" if mname and mname != mid else ""
                key_ref = model.get("apiKeyRef")
                headers = model.get("headers") or {}
                if key_ref and key_ref in pool_map:
                    k_item = pool_map[key_ref]
                    auth_note = f" [Key池: {k_item.get('name') or key_ref}]"
                elif bool(headers.get("Authorization") or headers.get("x-api-key")):
                    auth_note = " [独立专属Key]"
                else:
                    auth_note = ""
                parts.append(f"- {mid}{name_part}{auth_note}")
        else:
            parts.append("模型列表: - (无模型)")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"

def export_provider_txt(config, config_path, provider_id):
    if isinstance(config, str):
        config = json.loads(config)
    if not isinstance(config, dict):
        raise ValueError("配置数据无效")
    providers = config.get("providers") or {}
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        raise ValueError("未找到当前服务商")
    text = build_provider_export_text(provider_id, provider, config_path)
    base_dir = get_desktop_dir()
    filename = sanitize_filename(provider.get("name") or provider_id) + "-export.txt"
    out_path = base_dir / filename
    out_path.write_text(text, encoding="utf-8")
    return out_path, text

def export_all_providers_txt(config, config_path):
    if isinstance(config, str):
        config = json.loads(config)
    if not isinstance(config, dict):
        raise ValueError("配置数据无效")
    text = build_all_providers_export_text(config, config_path)
    base_dir = get_desktop_dir()
    filename = sanitize_filename(Path(config_path).stem if config_path else "providers") + "-all-export.txt"
    out_path = base_dir / filename
    out_path.write_text(text, encoding="utf-8")
    return out_path, text

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"

_CATALOG_PREFIX_RE = re.compile(r'^(chat|image|classifier|embed|embedding|rerank|audio|video):')

# 常见 vendor 别名归一（聚合站命名 -> pi-ai 目录命名）
_VENDOR_ALIAS = {
    "zai-org": "zai", "zhipuai": "zai", "z-ai": "zai", "zai": "zai",
    "alibaba": "qwen", "qwen": "qwen",
    "deepseek-ai": "deepseek", "deepseek": "deepseek",
    "x-ai": "xai", "spacexai": "xai", "xai": "xai",
    "moonshot": "moonshotai", "moonshotai": "moonshotai",
    "minimax-ai": "minimax", "minimaxai": "minimax", "minimax": "minimax",
    "bytedance-seed": "bytedance", "bytedance": "bytedance",
    "tencent": "tencent",
}


def _catalog_match_keys(mid):
    """把模型 ID 规范化为一组候选匹配键，用于和 pi-ai 目录（chat:vendor/name 格式）对齐。

    例：
      "chat:qwen/qwen3.7-max"      -> ["qwen/qwen3.7-max", "qwen3.7-max"]
      "openrouter:zai-org/glm-5.3" -> ["zai-org/glm-5.3", "glm-5.3", "zai/glm-5.3"]
      "deepseek-v3.2"              -> ["deepseek-v3.2"]
    """
    s = str(mid or "").lower().strip()
    prev = None
    while prev != s:
        prev = s
        s = _CATALOG_PREFIX_RE.sub("", s, count=1)
    if not s:
        return []
    keys = [s]
    if "/" in s:
        vendor, rest = s.split("/", 1)
        keys.append(rest)
        canon = _VENDOR_ALIAS.get(vendor)
        if canon and rest:
            keys.append(f"{canon}/{rest}")
    return [k for k in dict.fromkeys(keys) if k]


def _is_vision_id(target, raw_item=None):
    """根据 ID 关键词与上游原始数据判断是否多模态视觉模型。"""
    raw_item = raw_item if isinstance(raw_item, dict) else {}
    if any(k in target for k in ["gemini", "claude", "gpt-4o", "chatgpt-4o", "gpt-4.1",
                                  "o1", "o3", "o4", "vl", "vision", "omni", "4v", "visual", "mimo"]):
        return True
    inp = raw_item.get("input")
    if isinstance(inp, list) and "image" in inp:
        return True
    if any(raw_item.get(k) for k in ["multimodal", "is_multimodal"]):
        return True
    return False


def _is_reasoning_id(target, raw_item=None):
    """根据 ID 关键词与上游原始数据判断是否推理思考模型。"""
    raw_item = raw_item if isinstance(raw_item, dict) else {}
    if any(k in target for k in ["r1", "reasoner", "thinking", "thought", "qwq", "zero", "deepseek-v4", "mimo"]):
        return True
    if "claude-3-7" in target or "claude-3.7" in target:
        return True
    if re.search(r'\bo[134](-mini|-preview)?\b', target):
        return True
    if "flash-thinking" in target or "gemini-2.5" in target or "gemini-3" in target:
        return True
    if "gpt-5" in target or "gpt-6" in target:
        return True
    if raw_item.get("reasoning") is not None:
        return bool(raw_item["reasoning"])
    return False


# 目录索引缓存（catalog 是全局缓存的同一对象，用 id() 识别失效）
_CATALOG_INDEX_CACHE = {"ref": None, "entry_map": {}, "entry_list": []}


def _build_catalog_index(catalog):
    if _CATALOG_INDEX_CACHE["ref"] is catalog:
        return _CATALOG_INDEX_CACHE
    entry_keys = []  # [(key, model_def)]
    for _pid, pdata in catalog.items():
        for m in pdata.get("models", []):
            m_id = str(m.get("id") or "").lower().strip()
            if not m_id:
                continue
            for k in _catalog_match_keys(m_id):
                entry_keys.append((k, m))
    _CATALOG_INDEX_CACHE["ref"] = catalog
    _CATALOG_INDEX_CACHE["entry_map"] = dict(entry_keys)
    _CATALOG_INDEX_CACHE["entry_list"] = entry_keys
    return _CATALOG_INDEX_CACHE


# 日期后缀模式："claude-sonnet-4-5-20250929" -> 基座 "claude-sonnet-4-5"
_DATE_SUFFIX_RE = re.compile(r'[-_.]?[0-9]{4,8}([-_.][0-9a-z]+)*')


def _catalog_model_lookup(catalog, mid, raw_item=None):
    """在 pi-ai 内置目录中查找模型，返回 specs dict 或 None。

    匹配策略（按优先级）：
    1. raw_item 里自带的数值参数（上游 /v1/models 直报，最可信）
    2. 精确匹配（剥前缀 / vendor 别名归一后与目录条目全等）
    3. 后缀 / 日期快照匹配（自定义聚合站的长 ID）
    """
    if not catalog:
        return None
    raw_item = raw_item if isinstance(raw_item, dict) else {}

    def _specs_from(m):
        specs = {}
        if m.get("contextWindow"):
            specs["contextWindow"] = int(m["contextWindow"])
        if m.get("maxTokens"):
            specs["maxTokens"] = int(m["maxTokens"])
        if m.get("input"):
            specs["input"] = list(m["input"])
        if "reasoning" in m:
            specs["reasoning"] = bool(m["reasoning"])
        return specs if len(specs) >= 2 else None

    # 1. 上游直报的原始数值最优先
    raw_specs = {}
    cw = raw_item.get("context_length") or raw_item.get("context_window") or raw_item.get("max_context_tokens") or raw_item.get("contextWindow")
    mt = raw_item.get("max_tokens") or raw_item.get("max_output_tokens") or raw_item.get("maxTokens")
    if isinstance(cw, (int, float)) and cw >= 4096:
        raw_specs["contextWindow"] = int(cw)
    if isinstance(mt, (int, float)) and mt >= 512:
        raw_specs["maxTokens"] = int(mt)
    if isinstance(raw_item.get("input"), list) and "image" in raw_item["input"]:
        raw_specs["input"] = ["text", "image"]
    if raw_item.get("reasoning") is not None:
        raw_specs["reasoning"] = bool(raw_item["reasoning"])
    if "contextWindow" in raw_specs or "maxTokens" in raw_specs:
        return raw_specs

    target_keys = _catalog_match_keys(mid)
    if not target_keys:
        return None
    idx = _build_catalog_index(catalog)
    entry_map = idx["entry_map"]
    entry_keys = idx["entry_list"]

    # 2. 精确匹配
    for tk in target_keys:
        hit = entry_map.get(tk)
        if hit:
            specs = _specs_from(hit)
            if specs:
                return specs

    # 3. 后缀 / 日期快照匹配（要求键足够长，避免误命中）
    for tk in target_keys:
        if len(tk) < 6:
            continue
        for k, m in entry_keys:
            if len(k) < 6:
                continue
            if tk.endswith(k) or k.endswith(tk):
                specs = _specs_from(m)
                if specs:
                    return specs
            elif tk.startswith(k) and _DATE_SUFFIX_RE.fullmatch(tk[len(k):] or ""):
                specs = _specs_from(m)
                if specs:
                    return specs

    return None


def _catalog_override_for(t):
    """目录勘误：内置 pi-ai 目录滞后于官方真实物理参数的模型，以下列实测值为准。

    gpt-6/6.1 全系与 gpt-5.4 基座：官方真实物理容量 1.05M（输入上限 922K），
    内置目录仍标 272K，会导致 Agent 在 272K 提前截断/压缩。
    """
    t = str(t or "").lower()
    if ("gpt-6" in t or "gpt-5.4" in t or "gpt-5-4" in t) and "mini" not in t and "nano" not in t:
        return {"contextWindow": 1050000, "maxTokens": 128000}
    return None


# 历史默认填充痕迹 / 已知滞后的目录值——这些值不应视为用户自定义，允许被官方推断值刷新
_LEGACY_DEFAULT_CW = {128000, 245760, 272000}
_LEGACY_DEFAULT_MT = {8192, 16384}


def refresh_official_specs(model, catalog=None, force=False):
    """官方参数对齐（在位刷新 model dict）。

    原则：模型参数一律以官方为准。
    - 字段缺失 → 补齐官方推断值（原有行为）；
    - 字段值是历史默认填充痕迹（128000/8192 等旧默认）或已知的滞后目录值
      （如 gpt-6 系 272000）→ 按官方推断值刷新，纠正“1M 被识别成 128K”类的存量错误；
    - 其它值视为用户自定义，不覆盖；除非 force=True（用户显式要求重置）。

    注意：推断时不把 model 自身作为 raw_item 传入——否则旧值会被当成
    “上游直报数值”原样返回，刷新永远不生效。
    """
    mid = model.get("id")
    if not mid:
        return model
    # 仅保留能力标志（不携带数值），避免旧数值干扰推断
    raw_hint = {}
    if isinstance(model.get("input"), list):
        raw_hint["input"] = model["input"]
    if model.get("reasoning") is not None:
        raw_hint["reasoning"] = bool(model["reasoning"])
    try:
        specs = infer_model_specs(mid, raw_hint, catalog)
    except Exception:
        return model
    cw = model.get("contextWindow")
    mt = model.get("maxTokens")
    if force or not cw or (isinstance(cw, int) and cw in _LEGACY_DEFAULT_CW):
        if specs.get("contextWindow") and specs["contextWindow"] != cw:
            model["contextWindow"] = specs["contextWindow"]
    if force or not mt or (isinstance(mt, int) and mt in _LEGACY_DEFAULT_MT):
        if specs.get("maxTokens") and specs["maxTokens"] != mt:
            model["maxTokens"] = specs["maxTokens"]
    if not model.get("input") and specs.get("input"):
        model["input"] = specs["input"]
    if "reasoning" not in model and specs.get("reasoning"):
        model["reasoning"] = specs["reasoning"]
    return model


def infer_model_specs(mid, raw_item=None, catalog=None):
    """根据模型 ID、上游原始数据与内置目录智能推断 contextWindow, maxTokens, input, reasoning。

    优先级：
    1. raw_item 上游直报数值（context_length / max_tokens 等）
    2. pi-ai 内置目录匹配（chat:vendor/name 前缀已剥离对齐）
    3. 关键词显式长度（1m / 256k / ...）
    4. 家族规格表（按当前真实参数维护）
    """
    raw_item = raw_item if isinstance(raw_item, dict) else {}
    if catalog is None:
        try:
            catalog = load_builtin_catalog()
        except Exception:
            catalog = {}

    # 1+2. 上游数值 / 目录匹配继承
    if catalog:
        specs = _catalog_model_lookup(catalog, mid, raw_item)
        if specs:
            keys = _catalog_match_keys(mid)
            t = keys[0] if keys else str(mid or "").lower().strip()
            # 目录勘误：实测参数覆盖滞后的目录值
            override = _catalog_override_for(t)
            if override:
                specs = {**specs, **override}
            return {
                "contextWindow": int(specs.get("contextWindow") or 128000),
                "maxTokens": int(specs.get("maxTokens") or 16384),
                "input": list(specs.get("input") or (["text", "image"] if _is_vision_id(t, raw_item) else ["text"])),
                "reasoning": bool(specs.get("reasoning", _is_reasoning_id(t, raw_item))),
            }

    keys = _catalog_match_keys(mid)
    t = keys[0] if keys else str(mid or "").lower().strip()

    cw = raw_item.get("context_length") or raw_item.get("context_window") or raw_item.get("max_context_tokens") or raw_item.get("contextWindow")
    mt = raw_item.get("max_tokens") or raw_item.get("max_output_tokens") or raw_item.get("maxTokens")

    # 视觉多模态 / 推理思考判断
    is_vision = _is_vision_id(t, raw_item)
    is_reasoning = _is_reasoning_id(t, raw_item)

    # 上下文窗口与最大输出推断（家族规格表，参数对齐 pi-ai 目录 2026-02 数据）
    context_window = None
    max_tokens = None

    if "gemini" in t:
        # 2.5-pro=2M；flash / 3.x 全系=1M
        context_window = 2000000 if ("pro" in t and any(v in t for v in ["1.5", "2.0", "2.5"])) else 1048576
        max_tokens = 65536
        is_vision = True
    elif "claude" in t:
        context_window = 200000
        max_tokens = 64000 if any(k in t for k in ["3-7", "3.7", "3-5-sonnet", "3.5-sonnet", "sonnet-4",
                                                    "4-5", "4.5", "4-6", "4.6", "4-7", "4.7", "4-8", "4.8",
                                                    "sonnet-5", "opus-5", "haiku-4", "haiku-5", "fable"]) else 8192
        is_vision = True
    elif "mimo" in t:
        context_window = 1048576
        max_tokens = 131072
        is_vision = True
        is_reasoning = True
    elif "deepseek" in t:
        if "v4" in t:
            context_window = 1000000
            max_tokens = 384000
            is_reasoning = True
            is_vision = True
        elif "v3.2" in t or "v3-2" in t:
            context_window = 131072
            max_tokens = 65536
        elif "v3.1" in t or "v3-1" in t:
            context_window = 163840
            max_tokens = 128000
        elif any(k in t for k in ["r1", "reasoner"]):
            context_window = 128000
            max_tokens = 65536
            is_reasoning = True
        else:
            context_window = 128000
            max_tokens = 8192
    elif "gpt-oss" in t:
        context_window = 131072
        max_tokens = 131072 if "120" in t else 8192
    elif re.search(r'gpt-?6', t) or "gpt-5.4" in t or "gpt-5-4" in t:
        # gpt-6/6.1 与 gpt-5.4 全系=1.05M（官方真实物理容量，输入上限 922K），5.4-mini/nano=400K
        if "mini" in t or "nano" in t:
            context_window = 400000
        else:
            context_window = 1050000
        max_tokens = 128000
        is_reasoning = True
        is_vision = True
    elif "gpt-5" in t:
        # gpt-5 / 5.1 / 5.2 / 5.3 全系=400K，codex-spark=128K
        if "codex" in t and "spark" in t:
            context_window = 128000
            max_tokens = 32000
        else:
            context_window = 400000
            max_tokens = 128000
        is_reasoning = True
        is_vision = True
    elif re.search(r'\bo[134]\b', t):
        context_window = 200000
        max_tokens = 100000
        is_reasoning = True
        is_vision = True
    elif "gpt-4.1" in t or "gpt-4-1" in t:
        context_window = 1047576
        max_tokens = 32768
        is_vision = True
    elif "gpt-4o" in t or "chatgpt-4o" in t or "gpt-4.5" in t:
        context_window = 128000
        max_tokens = 16384
        is_vision = True
    elif "grok" in t:
        # grok-4.20=2M；4.1/4.2/4.3=1M；4.5~4.7=500K；grok-build=256K
        if "4.20" in t or "4-20" in t or "multi-agent" in t:
            context_window = 2000000
            max_tokens = 1800000
        elif re.search(r'grok-?4[.\-]?3\b', t):
            context_window = 1000000
            max_tokens = 30000
        elif re.search(r'grok-?4[.\-]?[12]\b', t):
            context_window = 1000000
            max_tokens = 131072
        elif re.search(r'grok-?4[.\-]?[4-9]\b', t):
            context_window = 500000
            max_tokens = 450000
        elif "build" in t:
            context_window = 256000
            max_tokens = 230400
        else:
            context_window = 256000
            max_tokens = 32768
        is_reasoning = "non-reasoning" not in t
    elif "qwen" in t:
        # qwen3.5+/3.6/3.7/3.8 标准线=262K，plus/max/flash 高端线=1M，coder=262K，qwen2.5=32K
        if any(k in t for k in ["plus", "max", "1m"]):
            context_window = 1000000
            max_tokens = 131072 if "max" in t else 65536
        elif "flash" in t:
            context_window = 1000000
            max_tokens = 65536
        elif "coder" in t:
            context_window = 262144
            max_tokens = 65536
        elif re.search(r'qwen-?2[.\-]?5', t):
            context_window = 32768
            max_tokens = 16384
        elif re.search(r'qwen-?3-\d+b', t):
            context_window = 40960
            max_tokens = 16384
        else:
            context_window = 262144
            max_tokens = 65536
        if any(k in t for k in ["vl", "omni", "audio"]):
            is_vision = True
    elif "glm" in t:
        # glm-5.2+/5.3=1M；glm-4.6/4.7/5/5.1=200K；glm-4.5=128K；4.5v/4.6v=视觉小窗
        if re.search(r'glm-?5[.\-]?[2-9]', t):
            context_window = 1000000
            max_tokens = 131072
        elif re.search(r'glm-?5', t):
            context_window = 200000
            max_tokens = 131072
        elif re.search(r'glm-?4[.\-]?[67]v', t):
            context_window = 128000
            max_tokens = 32768
            is_vision = True
        elif re.search(r'glm-?4[.\-]?[67]', t):
            context_window = 200000
            max_tokens = 96000
        elif re.search(r'glm-?4[.\-]?5v', t):
            context_window = 66000
            max_tokens = 16000
            is_vision = True
        elif re.search(r'glm-?4[.\-]?5', t):
            context_window = 128000
            max_tokens = 96000
        else:
            context_window = 128000
            max_tokens = 8192
        if any(k in t for k in ["4v", "vl"]) or re.search(r'glm-?[45][.\-]?\d*v', t):
            is_vision = True
    elif "kimi" in t or "moonshot" in t:
        # kimi-k3=1M；k2.6/k2.7=262K；k2.5=262K；k2=131K
        if re.search(r'kimi-?k3', t):
            context_window = 1000000
            max_tokens = 131072
        elif re.search(r'kimi-?k2[.\-]?[67]', t):
            context_window = 262144
            max_tokens = 262144 if re.search(r'k2[.\-]?6', t) else 32768
        elif re.search(r'kimi-?k2[.\-]?5', t):
            context_window = 262144
            max_tokens = 98304
        elif re.search(r'kimi-?k2\b', t):
            context_window = 131072
            max_tokens = 131072
        else:
            context_window = 131072
            max_tokens = 131072
        if any(k in t for k in ["vl", "vision"]):
            is_vision = True
    elif "minimax" in t or "abab" in t:
        # M3.1=1M；M3=512K；M2.x=204800
        if re.search(r'm3[.\-]?1', t):
            context_window = 1000000
            max_tokens = 524288
        elif re.search(r'\bm3\b', t):
            context_window = 512000
            max_tokens = 512000
        else:
            context_window = 204800
            max_tokens = 131072
    elif "hy4" in t or "hunyuan" in t:
        # hy4-preview=1M；hy3=262K
        if "hy3" in t:
            context_window = 262144
            max_tokens = 128000
        else:
            context_window = 1048576
            max_tokens = 64000
    elif re.search(r'step-?5', t):
        context_window = 1000000
        max_tokens = 64000
    elif re.search(r'step-?3', t):
        context_window = 256000
        max_tokens = 64000
    elif "nemotron" in t:
        if "3.5" in t or "ultra" in t:
            context_window = 1000000
            max_tokens = 1048576 if "super-vl" in t else 65000
        else:
            context_window = 262144
            max_tokens = 262144

    # 关键词显式长度匹配（覆盖家族表，可纠正如 128k 后缀模型）
    if re.search(r'(?<![0-9a-z.])1m(?![0-9a-z])', t) or "1000k" in t or "1024k" in t:
        context_window = 1048576
    elif re.search(r'(?<![0-9a-z.])2m(?![0-9a-z])', t) or "2000k" in t:
        context_window = 2000000
    elif re.search(r'(?<![0-9a-z.])256k(?![0-9a-z])', t):
        context_window = 256000
    elif re.search(r'(?<![0-9a-z.])200k(?![0-9a-z])', t):
        context_window = 200000
    elif re.search(r'(?<![0-9a-z.])128k(?![0-9a-z])', t):
        context_window = 128000
    elif re.search(r'(?<![0-9a-z.])64k(?![0-9a-z])', t):
        context_window = 64000
    elif re.search(r'(?<![0-9a-z.])32k(?![0-9a-z])', t):
        context_window = 32000

    # 优先采用 raw_item 提供的合法数值
    if isinstance(cw, (int, float)) and cw >= 4096:
        context_window = int(cw)
    if isinstance(mt, (int, float)) and mt >= 512:
        max_tokens = int(mt)

    final_cw = int(context_window or 128000)
    final_mt = int(max_tokens or 16384)
    final_input = ["text", "image"] if is_vision else ["text"]
    final_reasoning = bool(is_reasoning)

    return {
        "contextWindow": final_cw,
        "maxTokens": final_mt,
        "input": final_input,
        "reasoning": final_reasoning
    }

def fetch_models(base_url, api_key):
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        endpoint = f"{url}/models"
    elif url.endswith("/models"):
        endpoint = url
    else:
        endpoint = f"{url}/v1/models"
    
    headers = {
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    if api_key:
        api_key_clean = str(api_key).strip()
        if api_key_clean:
            headers["Authorization"] = api_key_clean if api_key_clean.lower().startswith("bearer ") else f"Bearer {api_key_clean}"
    
    req = urllib.request.Request(endpoint, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    
    items = []
    if "data" in body and isinstance(body["data"], list):
        items = body["data"]
    elif "models" in body and isinstance(body["models"], list):
        items = body["models"]
    elif isinstance(body, list):
        items = body
    
    catalog = load_builtin_catalog()
    models = []
    for it in items:
        if not isinstance(it, dict):
            mid = str(it)
            raw = {}
        else:
            mid = it.get("id") or it.get("name")
            raw = it
        if mid:
            mid_str = str(mid)
            name = raw.get("name") or mid_str
            specs = infer_model_specs(mid_str, raw, catalog)
            models.append({
                "id": mid_str,
                "name": str(name),
                "contextWindow": specs["contextWindow"],
                "maxTokens": specs["maxTokens"],
                "input": specs["input"],
                "reasoning": specs["reasoning"]
            })
    return sorted(models, key=lambda x: x["id"])

def test_model(base_url, api_key, api_type, model_id, custom_headers=None):
    base = (base_url or "").rstrip("/")
    if not base:
        return {"success": False, "error": "Base URL 为空"}
    start = time.time()
    try:
        custom_headers = custom_headers if isinstance(custom_headers, dict) else {}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        api_key_clean = str(api_key).strip() if api_key else ""
        auth_bearer = (api_key_clean if api_key_clean.lower().startswith("bearer ") else f"Bearer {api_key_clean}") if api_key_clean else ""

        # === 阶段 1：基础连通性探针 (Ping) ===
        if api_type == "anthropic-messages":
            endpoint = base + "/messages" if base.endswith("/v1") else base + "/v1/messages"
            headers["anthropic-version"] = "2023-06-01"
            if api_key_clean:
                headers["x-api-key"] = api_key_clean
            for k, v in custom_headers.items():
                if k and v:
                    headers[str(k)] = str(v)
            body = {"model": model_id, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
        elif api_type == "google-generative-ai":
            gbase = base
            if not gbase.endswith("/v1beta") and not gbase.endswith("/v1"):
                gbase = gbase + "/v1beta"
            endpoint = f"{gbase}/models/{model_id}:generateContent"
            if api_key_clean:
                headers["x-goog-api-key"] = api_key_clean
            for k, v in custom_headers.items():
                if k and v:
                    headers[str(k)] = str(v)
            body = {"contents": [{"parts": [{"text": "hi"}]}]}
        elif api_type == "openai-responses":
            endpoint = base + "/responses" if base.endswith("/v1") else base + "/v1/responses"
            if auth_bearer:
                headers["Authorization"] = auth_bearer
            for k, v in custom_headers.items():
                if k and v:
                    headers[str(k)] = str(v)
            body = {"model": model_id, "input": "hi", "max_output_tokens": 1}
        else:
            endpoint = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
            if auth_bearer:
                headers["Authorization"] = auth_bearer
            for k, v in custom_headers.items():
                if k and v:
                    headers[str(k)] = str(v)
            body = {"model": model_id, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        ping_latency_ms = int((time.time() - start) * 1000)

        # === 阶段 2：Agent 真实负载探针（流式 + 系统提示词 + 工具调用测试） ===
        agent_ready = True
        warning = None
        if api_type in ("openai-completions", "", None):
            agent_body = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": "You are a test assistant."},
                    {"role": "user", "content": "hi"}
                ],
                "max_tokens": 10,
                "stream": True,
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "test_ping",
                        "description": "health check",
                        "parameters": {
                            "type": "object",
                            "properties": {"q": {"type": "string"}}
                        }
                    }
                }]
            }
            try:
                agent_req = urllib.request.Request(endpoint, data=json.dumps(agent_body).encode("utf-8"), headers=headers, method="POST")
                with urllib.request.urlopen(agent_req, timeout=15) as a_resp:
                    a_resp.read(512)
            except urllib.error.HTTPError as ae:
                ae_detail = ""
                try:
                    ae_body = ae.read().decode("utf-8", errors="replace")
                    ae_detail = clip_text(ae_body, 8000)
                except Exception:
                    pass
                low = ae_detail.lower()
                if any(w in low for w in ("tool", "function", "unsupported parameter", "not supported")):
                    agent_ready = False
                    warning = "不支持工具调用(仅可纯对话)"
                else:
                    return {"success": False, "status": ae.code, "error": f"Agent探针报错: {ae_detail}", "latency_ms": ping_latency_ms, "model": model_id}
            except Exception as ae:
                agent_ready = False
                warning = "流式/工具探针异常: " + clip_text(ae, 600)
        elif api_type == "anthropic-messages":
            agent_body = {
                "model": model_id,
                "max_tokens": 10,
                "stream": True,
                "system": "You are a test assistant.",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{
                    "name": "test_ping",
                    "description": "health check",
                    "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}
                }]
            }
            try:
                agent_req = urllib.request.Request(endpoint, data=json.dumps(agent_body).encode("utf-8"), headers=headers, method="POST")
                with urllib.request.urlopen(agent_req, timeout=15) as a_resp:
                    a_resp.read(512)
            except urllib.error.HTTPError as ae:
                ae_detail = ""
                try:
                    ae_detail = clip_text(ae.read().decode("utf-8", errors="replace"), 8000)
                except Exception:
                    pass
                if any(w in ae_detail.lower() for w in ("tool", "function")):
                    agent_ready = False
                    warning = "不支持工具调用(仅可纯对话)"
                else:
                    return {"success": False, "status": ae.code, "error": f"Agent探针报错: {ae_detail}", "latency_ms": ping_latency_ms, "model": model_id}
            except Exception as ae:
                agent_ready = False
                warning = "流式/工具探针异常: " + clip_text(ae, 600)

        total_latency_ms = int((time.time() - start) * 1000)
        res = {
            "success": True,
            "latency_ms": total_latency_ms,
            "ping_latency_ms": ping_latency_ms,
            "agent_ready": agent_ready,
            "model": model_id,
        }
        if warning:
            res["warning"] = warning
        return res
    except urllib.error.HTTPError as e:
        latency_ms = int((time.time() - start) * 1000)
        detail = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(err_body)
                detail = str(parsed.get("error", parsed))
            except Exception:
                detail = err_body
            detail = clip_text(detail, 8000)
            write_log("WARN", "test_model", "HTTP %s %s" % (e.code, model_id), clip_text(err_body, 8000))
        except Exception:
            pass
        return {"success": False, "status": e.code, "error": f"HTTP {e.code}: {detail}",
                "endpoint": endpoint, "model": model_id, "latency_ms": latency_ms,
                "logPath": str(LOG_PATH)}
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        log_exception("test_model", e, "endpoint=%s model=%s" % (locals().get("endpoint", ""), model_id))
        return {"success": False, "error": clip_text(e, 4000),
                "traceback": clip_text(traceback.format_exc(), 8000),
                "endpoint": locals().get("endpoint", ""), "model": model_id,
                "latency_ms": latency_ms, "logPath": str(LOG_PATH)}

class ApiBridge:
    def __init__(self):
        self._window = None
        self._last_selected_provider = None

    def set_window(self, win):
        self._window = win

    # ---------- 日志 / 诊断接口（前端可读、可复制、可打开） ----------
    def log_error(self, source, message, detail=""):
        """前端 JS 报错回写日志文件，避免界面报错一闪而过。"""
        write_log("ERROR", "ui:%s" % source, str(message), clip_text(detail, 8000))
        return {"success": True, "path": str(LOG_PATH)}

    def get_error_log(self, max_lines=400):
        try:
            if not LOG_PATH.exists():
                return {"success": True, "text": "", "path": str(LOG_PATH), "totalLines": 0}
            with LOG_PATH.open("r", encoding="utf-8", errors="replace") as fp:
                lines = fp.readlines()
            try:
                tail_n = max(1, int(max_lines or 400))
            except Exception:
                tail_n = 400
            return {"success": True, "text": "".join(lines[-tail_n:]),
                    "path": str(LOG_PATH), "totalLines": len(lines)}
        except Exception as e:
            return {"success": False, "error": clip_text(e, 2000), "path": str(LOG_PATH)}

    def clear_error_log(self):
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOG_PATH.write_text("", encoding="utf-8")
            return {"success": True, "path": str(LOG_PATH)}
        except Exception as e:
            return {"success": False, "error": clip_text(e, 2000), "path": str(LOG_PATH)}

    def open_log_file(self):
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if not LOG_PATH.exists():
                LOG_PATH.write_text("", encoding="utf-8")
            os.startfile(str(LOG_PATH))
            return {"success": True, "path": str(LOG_PATH)}
        except Exception as e:
            return {"success": False, "error": clip_text(e, 2000), "path": str(LOG_PATH)}

    def discover_configs(self):
        path = DEFAULT_MODELS_PATH.resolve()
        config = load_config(path)
        targets = [{
            "id": "pi",
            "label": "⚡ Pi",
            "title": "Pi Coding Agent (models.json)",
            "path": str(path),
            "providerCount": len(config.get("providers", {})),
            "exists": path.exists(),
            "schema": "pi-providers",
            "editable": True,
        }]

        if dsh_config.is_dsh_installed():
            cordis_path, creds_path = dsh_config.get_dsh_paths()
            dsh_cfg = dsh_config.load_dsh_config(cordis_path, creds_path)
            targets.append({
                "id": "dsh",
                "label": "🪐 DSH",
                "title": "DeepSeek Harness (cordis.patch.yml)",
                "path": str(cordis_path),
                "providerCount": len(dsh_cfg.get("providers", {})),
                "exists": cordis_path.exists(),
                "schema": "dsh-providers",
                "editable": True,
            })

        return {
            "targets": targets,
            "defaultPath": str(DEFAULT_MODELS_PATH)
        }

    def get_config(self, path=None):
        if path and is_dsh_path(path):
            cordis_path, creds_path = dsh_config.get_dsh_paths()
            cfg = dsh_config.load_dsh_config(cordis_path, creds_path)
            return {
                "config": cfg,
                "path": str(cordis_path),
                "schema": "dsh-providers",
                "editable": True,
                "selectedProviderId": self._last_selected_provider
            }

        config_path = normalize_config_path(path)
        cfg = load_config(config_path, merge_builtins=True)
        return {
            "config": cfg,
            "path": str(config_path),
            "schema": "pi-providers",
            "editable": True,
            "selectedProviderId": self._last_selected_provider
        }

    def save_config(self, config_json_str, path=None):
        try:
            data = json.loads(config_json_str) if isinstance(config_json_str, str) else config_json_str
            if path and is_dsh_path(path):
                cordis_path, creds_path = dsh_config.get_dsh_paths()
                ok = dsh_config.save_dsh_config(data, cordis_path, creds_path)
                if not ok:
                    return {"success": False, "error": "写入 DSH 配置文件失败"}
                saved = dsh_config.load_dsh_config(cordis_path, creds_path)
                model_count = sum(len(p.get("models", [])) for p in saved.get("providers", {}).values() if isinstance(p, dict))
                return {
                    "success": True,
                    "target": "dsh",
                    "providerCount": len(saved.get("providers", {})),
                    "modelCount": model_count,
                    "disabledCount": 0,
                    "orderPatch": {"action": "dsh-saved"}
                }

            config_path = normalize_config_path(path)
            disabled_count = 0
            for p in (data.get("providers") or {}).values():
                if not isinstance(p, dict):
                    continue
                for m in p.get("models") or []:
                    if isinstance(m, dict) and m.get("disabled") is True:
                        disabled_count += 1
            save_config(data, config_path)
            saved = load_config(config_path, merge_builtins=False)
            model_count = sum(len(p.get("models", [])) for p in saved.get("providers", {}).values() if isinstance(p, dict))
            # 保存后：刷新 Pi 排序补丁读取的顺序文件；如已开启自动维护则自动重打补丁
            order_patch = None
            try:
                write_pi_provider_order(config_path)
                prefs = get_tool_prefs()
                if prefs.get("patchPiOrder"):
                    st = ensure_pi_order_patch(config_path)
                    order_patch = {
                        "action": st.get("action") or "",
                        "fullyPatched": bool(st.get("fullyPatched")),
                        "targetCount": st.get("targetCount", 0),
                        "patchedCount": st.get("patchedCount", 0),
                        "piVersion": st.get("piVersion") or "",
                        "error": st.get("error") or "",
                    }
                else:
                    order_patch = {"action": "disabled", "error": ""}
            except Exception as e:
                order_patch = {"action": "error", "error": str(e)}
            return {"success": True, "target": "pi", "providerCount": len(saved.get("providers", {})), "modelCount": model_count,
                    "disabledCount": disabled_count, "orderPatch": order_patch}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def import_from_counterpart(self, current_path):
        """支持一键从对端（Pi <-> DSH）导入配置到当前编辑区"""
        try:
            if current_path and is_dsh_path(current_path):
                # 当前是 DSH，从 Pi 导入
                pi_path = DEFAULT_MODELS_PATH.resolve()
                cfg = load_config(pi_path, merge_builtins=False)
                return {"success": True, "source": "Pi", "config": cfg}
            else:
                # 当前是 Pi，从 DSH 导入
                cordis_path, creds_path = dsh_config.get_dsh_paths()
                cfg = dsh_config.load_dsh_config(cordis_path, creds_path)
                # DSH base 层内置路由（DeepSeek 官方/账号）不归属 pi-ai，
                # 写进 Pi 的 models.json 毫无意义，导入时直接排除
                cfg["providers"] = {
                    pid: p for pid, p in (cfg.get("providers") or {}).items()
                    if not (isinstance(p, dict) and p.get("_dshBuiltin"))
                }
                return {"success": True, "source": "DSH", "config": cfg}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_dsh_builtin_disabled(self, entry_id, disabled):
        """禁用/恢复 DSH base 层内置路由（写 disabled 补丁，需重启 DSH）。"""
        try:
            ok, msg = dsh_config.set_dsh_builtin_disabled(entry_id, bool(disabled))
            if not ok:
                return {"success": False, "error": msg or "写入 cordis.patch.yml 失败"}
            return {"success": True, "entryId": entry_id, "disabled": bool(disabled), "restartRequired": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def fetch_models(self, base_url, api_key):
        try:
            models = fetch_models(base_url, api_key)
            return {"success": True, "models": models}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def infer_specs(self, model_id):
        try:
            specs = infer_model_specs(model_id, catalog=load_builtin_catalog())
            return {"success": True, "specs": specs}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def test_model(self, base_url, api_key, api_type, model_id, custom_headers=None):
        try:
            return test_model(base_url, api_key, api_type, model_id, custom_headers)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_default_model(self, path=None):
        try:
            if path and is_dsh_path(path):
                cordis_path, _ = dsh_config.get_dsh_paths()
                dp, dm = dsh_config.get_dsh_default_model(cordis_path)
                return {"success": True, "defaultProvider": dp, "defaultModel": dm}
            s = load_pi_settings()
            return {
                "success": True,
                "defaultProvider": s.get("defaultProvider") or "",
                "defaultModel": s.get("defaultModel") or ""
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_tool_prefs(self):
        """读取工具界面偏好（顺序相关开关）。"""
        try:
            return {"success": True, "prefs": get_tool_prefs()}
        except Exception as e:
            return {"success": False, "error": str(e), "prefs": dict(DEFAULT_TOOL_PREFS)}

    def set_tool_pref(self, name, value):
        """持久化单个工具界面偏好。"""
        try:
            set_tool_pref(name, value)
            return {"success": True, "prefs": get_tool_prefs()}
        except Exception as e:
            return {"success": False, "error": str(e), "prefs": dict(DEFAULT_TOOL_PREFS)}

    def get_order_report(self):
        """工具顺序 ↔ Pi 顺序一致性自检报告。"""
        try:
            text = build_order_report(None)
            summary = ""
            try:
                tail = text.split("── 结论 ──")[-1]
                parts = []
                for raw in tail.split("\n"):
                    s = raw.strip().lstrip("✅⚠️❌ℹ️ “ ” ").strip()
                    if s:
                        parts.append(s)
                summary = " · ".join(parts[:2])
            except Exception:
                summary = ""
            return {"success": True, "text": text, "summary": summary}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_order_compare(self, alpha=False):
        """顺序自检（左右两栏对照）：返回已渲染的 HTML + 完整文本报告（供 📋 复制全文）。"""
        try:
            cmp = build_order_compare(None, bool(alpha))
            cmp["html"] = build_order_compare_html(cmp)
            try:
                cmp["reportText"] = build_order_report(None)
            except Exception as e:
                cmp["reportText"] = "（完整报告生成失败: %s）" % e
            return cmp
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_order_patch_status(self):
        """Pi 排序补丁状态。"""
        try:
            return {"success": True, "status": pi_order_patch_status()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def apply_order_patch(self, enabled=True):
        """立即给 Pi 打「厂商按工具顺序分组」补丁，并（默认）开启自动维护。"""
        try:
            if enabled:
                set_tool_pref("patchPiOrder", True)
            result = apply_pi_order_patch(None)
            status = pi_order_patch_status()
            return {"success": bool(result.get("success")), "result": result, "status": status,
                    "error": "" if result.get("success") else (result.get("error") or "补丁未完全打入")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def revert_order_patch(self, enabled=True):
        """还原 Pi 原版排序逻辑，并（默认）关闭自动维护。"""
        try:
            if enabled:
                set_tool_pref("patchPiOrder", False)
            result = revert_pi_order_patch()
            status = pi_order_patch_status()
            return {"success": bool(result.get("success")), "result": result, "status": status,
                    "error": "" if result.get("success") else (result.get("error") or "还原失败")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def ensure_order_patch(self):
        """启动/保存后的自动维护：需要时重打补丁并刷新顺序文件（未开启则什么都不做）。"""
        try:
            return {"success": True, "status": ensure_pi_order_patch(None)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_default_model(self, provider_id, model_id, path=None):
        try:
            if path and is_dsh_path(path):
                cordis_path, _ = dsh_config.get_dsh_paths()
                ok = dsh_config.set_dsh_default_model(provider_id, model_id, cordis_path)
                if ok:
                    return {"success": True, "defaultProvider": provider_id, "defaultModel": model_id}
                return {"success": False, "error": "写入 cordis.patch.yml 失败"}
            s = load_pi_settings()
            s["defaultProvider"] = str(provider_id or "").strip()
            s["defaultModel"] = str(model_id or "").strip()
            if not s["defaultProvider"]:
                s.pop("defaultProvider", None)
            if not s["defaultModel"]:
                s.pop("defaultModel", None)
            ok = save_pi_settings(s)
            if ok:
                return {"success": True, "defaultProvider": s.get("defaultProvider", ""), "defaultModel": s.get("defaultModel", "")}
            return {"success": False, "error": "写入 settings.json 失败"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def rescan_builtins(self):
        try:
            builtins = scan_builtin_providers()
            return {"success": True, "builtins": builtins}
        except Exception as e:
            return {"success": False, "error": str(e)}


    def export_provider_txt(self, config_json_str, path, provider_id):
        try:
            config_path = normalize_config_path(path)
            data = json.loads(config_json_str) if isinstance(config_json_str, str) else config_json_str
            out_path, text = export_provider_txt(data, config_path, provider_id)
            return {"success": True, "path": str(out_path), "content": text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def export_all_providers_txt(self, config_json_str, path):
        try:
            config_path = normalize_config_path(path)
            data = json.loads(config_json_str) if isinstance(config_json_str, str) else config_json_str
            out_path, text = export_all_providers_txt(data, config_path)
            return {"success": True, "path": str(out_path), "content": text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def export_current_provider(self):
        try:
            config_path = normalize_config_path(None)
            data = load_config(config_path)
            provider_id = self._last_selected_provider
            if not provider_id:
                return {"success": False, "error": "请先选择一个服务商"}
            out_path, text = export_provider_txt(data, config_path, provider_id)
            return {"success": True, "path": str(out_path), "content": text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_selected_provider(self, provider_id):
        self._last_selected_provider = provider_id
        return {"success": True}

    def restart_pi(self):
        try:
            subprocess.Popen(["wt.exe", "powershell.exe", "-NoExit", "-Command", "pi"], shell=True)
        except Exception:
            subprocess.Popen(["cmd.exe", "/c", "start", "powershell.exe", "-NoExit", "-Command", "pi"], shell=True)
        return {"success": True}

    def minimize_window(self):
        if self._window:
            self._window.minimize()

    def maximize_window(self):
        if self._window:
            try:
                self._window.restore() if getattr(self, '_maximized', False) else self._window.maximize()
                self._maximized = not getattr(self, '_maximized', False)
            except Exception:
                pass

    def close_window(self):
        if self._window:
            self._window.destroy()

    def start_window_resize(self, direction="se"):
        if not self._window:
            return {"success": False}
        try:
            native = getattr(self._window, "native", None)
            hwnd = int(native.Handle.ToInt32()) if (native and hasattr(native, "Handle")) else int(ctypes.windll.user32.GetForegroundWindow())
            dir_map = {
                "w": 1, "left": 1,
                "e": 2, "right": 2,
                "n": 3, "top": 3,
                "nw": 4, "top-left": 4,
                "ne": 5, "top-right": 5,
                "s": 6, "bottom": 6,
                "sw": 7, "bottom-left": 7,
                "se": 8, "bottom-right": 8,
            }
            code = dir_map.get(str(direction).lower(), 8)
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(hwnd, 0x0112, 0xF000 | code, 0)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def resize_window(self, width, height):
        if self._window:
            try:
                w = max(960, int(width))
                h = max(580, int(height))
                self._window.resize(w, h)
                return {"success": True, "width": w, "height": h}
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": False}

    def save_window_size(self, width, height):
        try:
            w = max(960, int(width))
            h = max(580, int(height))
            stored = load_tool_settings()
            stored["windowWidth"] = w
            stored["windowHeight"] = h
            save_tool_settings(stored)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}


def _api_guard(func):
    """任何 js_api 方法抛异常：完整 traceback 落盘 + 返回结构化错误给前端。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            write_log("ERROR", "api:%s" % func.__name__, "%s: %s" % (type(exc).__name__, exc), tb)
            return {
                "success": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "traceback": clip_text(tb, 8000),
                "logPath": str(LOG_PATH),
            }

    # pywebview 用 inspect.getfullargspec 取参数名生成 JS 代理，
    # wrapper 的参数被 *args/**kwargs 吞掉后必须手动固定签名，否则前端会丢参。
    try:
        wrapper.__signature__ = inspect.signature(func)
    except (TypeError, ValueError):
        pass
    return wrapper


for _api_name, _api_member in list(vars(ApiBridge).items()):
    if not _api_name.startswith("_") and inspect.isfunction(_api_member):
        setattr(ApiBridge, _api_name, _api_guard(_api_member))
del _api_name, _api_member


HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>模型配置</title>
<style>
  :root {
    /* ==========================================================================
       Ann UI Skill — 拟态视网膜双端高质感设计系统
       纯深空暗夜基调 · 0 白屏闪烁 · Mica 磨砂玻璃质感 · 动态微辉光
       ========================================================================== */
    --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    --mono-family: "JetBrains Mono", "Cascadia Code", "Fira Code", Consolas, monospace;

    /* 永固深空暗夜底座 (Zero-Flash Dark Base) */
    --bg: #090d16;
    --card: rgba(18, 24, 38, 0.82);
    --card-border: rgba(255, 255, 255, 0.08);
    --card-border-hover: rgba(99, 102, 241, 0.35);
    --subcard: rgba(30, 41, 59, 0.55);
    --subcard-hover: rgba(51, 65, 85, 0.65);
    
    --text-primary: #f8fafc;
    --text-secondary: #cbd5e1;
    --text-muted: #64748b;
    --text-subtle: #475569;

    /* 双生重音渐变 (Accent Gradients) */
    --primary: #6366f1;
    --primary-glow: rgba(99, 102, 241, 0.28);
    --primary-gradient: linear-gradient(135deg, #6366f1 0%, #8b5cf6 50%, #d946ef 100%);
    --primary-soft: rgba(99, 102, 241, 0.14);

    --accent-green: #10b981;
    --accent-green-glow: rgba(16, 185, 129, 0.25);
    --accent-green-gradient: linear-gradient(135deg, #10b981 0%, #059669 100%);

    --accent-amber: #f59e0b;
    --accent-amber-glow: rgba(245, 158, 11, 0.25);

    --accent-rose: #ef4444;
    --accent-rose-glow: rgba(239, 68, 68, 0.25);

    --glass-shadow: 0 12px 36px 0 rgba(0, 0, 0, 0.45);
    --modal-shadow: 0 24px 64px rgba(0, 0, 0, 0.8), 0 0 0 1px rgba(255, 255, 255, 0.08);

    /* 兼容原代码业务逻辑使用的 --b-* 变量体系 */
    --b-bg: var(--bg);
    --b-bg-2: rgba(14, 20, 32, 0.88);
    --b-surface: rgba(18, 24, 38, 0.78);
    --b-surface-2: rgba(30, 41, 59, 0.65);
    --b-line: var(--card-border);
    --b-line-2: rgba(255, 255, 255, 0.14);
    --b-text: var(--text-primary);
    --b-text-2: var(--text-secondary);
    --b-text-3: var(--text-muted);
    --b-text-4: var(--text-subtle);
    --b-accent: var(--primary);
    --b-accent-2: #818cf8;
    --b-accent-soft: var(--primary-soft);
    --b-blue: #3b82f6;
    --b-green: var(--accent-green);
    --b-amber: var(--accent-amber);
    --b-purple: #a855f7;
    --b-sans: var(--font-family);
    --b-mono: var(--mono-family);
  }

  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    user-select: none;
    -webkit-tap-highlight-color: transparent;
  }

  html, body {
    background-color: var(--bg) !important;
    color: var(--text-primary);
    font-family: var(--font-family);
    font-size: 13px;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    position: relative;
    color-scheme: dark;
    line-height: 1.6;
  }

  /* 顶部环境紫蓝环境辉光 (UI Skill 特色) */
  body::before {
    content: "";
    position: fixed;
    top: -110px;
    left: 50%;
    transform: translateX(-50%);
    width: 120%;
    height: 240px;
    background: radial-gradient(circle, var(--primary-glow) 0%, transparent 70%);
    pointer-events: none;
    z-index: 0;
  }

  input, select, textarea, button { font-family: inherit; font-size: inherit; }

  /* ==================== Header 极客拟态顶栏 ==================== */
  header {
    height: 48px;
    background: var(--b-bg-2);
    backdrop-filter: blur(24px) saturate(180%);
    -webkit-backdrop-filter: blur(24px) saturate(180%);
    border-bottom: 1px solid var(--card-border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 14px;
    flex-shrink: 0;
    position: relative;
    z-index: 40;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }
  .header-left, .header-right { display: flex; align-items: center; gap: 10px; }
  .target-tabs {
    display: inline-flex;
    align-items: center;
    background: rgba(15, 23, 42, 0.7);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    padding: 3px;
    gap: 4px;
    margin-left: 14px;
    height: 32px;
    box-sizing: border-box;
    flex-shrink: 0;
  }
  .target-tab-btn {
    height: 26px;
    padding: 0 10px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: none;
    background: transparent;
    color: var(--text-muted);
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    display: inline-flex;
    align-items: center;
    gap: 6px;
    user-select: none;
    white-space: nowrap;
    line-height: 1;
  }
  .target-tab-btn:hover {
    color: var(--text-main);
    background: rgba(255, 255, 255, 0.08);
  }
  .target-tab-btn.active {
    background: var(--primary-gradient);
    color: #fff;
    box-shadow: 0 2px 10px rgba(99, 102, 241, 0.4);
  }
  .target-tab-btn .badge-count {
    background: rgba(0, 0, 0, 0.35);
    border-radius: 10px;
    padding: 1px 6px;
    font-size: 10px;
    font-weight: 700;
    line-height: 1.2;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    opacity: 0.95;
  }
  .logo-title {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 600;
    font-size: 14px;
    letter-spacing: 0.5px;
    cursor: default;
  }
  .brand-mark {
    width: 28px;
    height: 28px;
    border-radius: 8px;
    background: var(--primary-gradient);
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    font-weight: 800;
    box-shadow: 0 4px 14px var(--primary-glow);
    flex-shrink: 0;
  }
  .sub-en {
    font-size: 8.5px;
    font-weight: 600;
    color: #64748B;
    font-family: var(--mono-family);
    letter-spacing: 1.4px;
    text-transform: uppercase;
    margin-top: 1px;
  }
  .brand-text {
    display: flex;
    flex-direction: column;
    line-height: 1.15;
    justify-content: center;
  }
  .brand-title {
    font-size: 13.5px;
    font-weight: 700;
    letter-spacing: 0.6px;
    color: #F8FAFC;
  }

  /* ==================== 拟态视网膜按钮体系 ==================== */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    height: 28px;
    padding: 0 10px;
    border-radius: 6px;
    border: 1px solid transparent;
    cursor: pointer;
    font-size: 11.5px;
    font-weight: 500;
    line-height: 1;
    transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease;
    white-space: nowrap;
    position: relative;
    box-sizing: border-box;
    user-select: none;
  }
  .btn:active { opacity: 0.85; }

  .btn-primary {
    background: var(--primary-gradient);
    color: #FFF;
    border-color: rgba(255, 255, 255, 0.18);
    box-shadow: 0 2px 8px var(--primary-glow);
  }
  .btn-primary:hover {
    filter: brightness(1.1);
  }

  .btn-secondary {
    background: var(--subcard);
    color: var(--text-primary);
    border-color: var(--card-border);
    backdrop-filter: blur(12px);
  }
  .btn-secondary:hover {
    background: var(--subcard-hover);
    border-color: rgba(255, 255, 255, 0.18);
    color: #FFF;
  }

  .btn-ghost {
    background: transparent;
    color: var(--text-secondary);
    border-color: var(--card-border);
  }
  .btn-ghost:hover {
    background: rgba(255, 255, 255, 0.06);
    color: var(--text-primary);
    border-color: rgba(255, 255, 255, 0.18);
  }

  .btn-emerald {
    background: rgba(16, 185, 129, 0.12);
    color: #34d399;
    border: 1px solid rgba(16, 185, 129, 0.35);
  }
  .btn-emerald:hover {
    background: rgba(16, 185, 129, 0.22);
    border-color: rgba(16, 185, 129, 0.6);
    color: #6ee7b7;
  }

  .btn-indigo {
    background: rgba(99, 102, 241, 0.12);
    color: #a5b4fc;
    border: 1px solid rgba(99, 102, 241, 0.35);
  }
  .btn-indigo:hover {
    background: rgba(99, 102, 241, 0.22);
    border-color: rgba(99, 102, 241, 0.6);
    color: #c7d2fe;
  }

  .btn-rose {
    background: rgba(239, 68, 68, 0.15);
    color: #fca5a5;
    border-color: rgba(239, 68, 68, 0.35);
  }
  .btn-rose:hover {
    background: rgba(239, 68, 68, 0.26);
    border-color: #fca5a5;
    box-shadow: 0 2px 12px var(--accent-rose-glow);
  }

  .btn-purple {
    background: rgba(168, 85, 247, 0.15);
    color: #d8b4fe;
    border-color: rgba(168, 85, 247, 0.35);
  }
  .btn-purple:hover {
    background: rgba(168, 85, 247, 0.26);
    border-color: #d8b4fe;
    box-shadow: 0 2px 12px rgba(168, 85, 247, 0.25);
    transform: translateY(-1px);
  }

  .btn-amber {
    background: rgba(245, 158, 11, 0.15);
    color: #fcd34d;
    border-color: rgba(245, 158, 11, 0.35);
  }
  .btn-amber:hover {
    background: rgba(245, 158, 11, 0.26);
    border-color: #fcd34d;
    box-shadow: 0 2px 12px var(--accent-amber-glow);
  }

  /* ==================== 窗口控制组件 ==================== */
  .window-controls { display: flex; align-items: center; gap: 2px; margin-left: 6px; }
  .win-btn {
    width: 32px;
    height: 32px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    color: var(--text-muted);
    font-size: 13px;
    transition: all 0.14s ease;
  }
  .win-btn:hover { background: rgba(255, 255, 255, 0.08); color: var(--text-primary); }
  .win-btn.close:hover { background: var(--accent-rose); color: #FFF; }

  /* ==================== 双栏核心布局 ==================== */
  .layout {
    flex: 1;
    display: flex;
    overflow: hidden;
    position: relative;
    z-index: 10;
  }

  /* ==================== 侧边栏：极客服务商列表 ==================== */
  .sidebar {
    width: 260px;
    min-width: 220px;
    max-width: 380px;
    background: rgba(13, 18, 30, 0.85);
    backdrop-filter: blur(20px) saturate(180%);
    -webkit-backdrop-filter: blur(20px) saturate(180%);
    border-right: 1px solid var(--card-border);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
  }
  .sidebar-header {
    height: 44px;
    padding: 0 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--card-border);
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    background: transparent;
    flex-shrink: 0;
  }
  .sidebar-header .count-chip {
    font-size: 10px;
    font-family: var(--mono-family);
    padding: 1px 6px;
    border-radius: 10px;
    background: rgba(255, 255, 255, 0.06);
    color: var(--text-muted);
    border: 1px solid var(--card-border);
  }
  .sidebar-search {
    padding: 8px 10px;
    border-bottom: 1px solid var(--card-border);
    background: transparent;
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }
  .search-input {
    width: 100%;
    background: var(--subcard);
    border: 1px solid var(--card-border);
    border-radius: 6px;
    padding: 5px 9px;
    color: var(--text-primary);
    font-family: var(--font-family);
    font-size: 12px;
    outline: none;
    transition: all 0.16s ease;
  }
  .search-input:focus {
    border-color: var(--primary);
    background: rgba(30, 41, 59, 0.8);
    box-shadow: 0 0 0 2px var(--primary-glow);
  }
  .search-input::placeholder { color: var(--text-muted); }

  .provider-list {
    flex: 1;
    overflow-y: auto;
    padding: 8px 8px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  /* 厂商单行卡片 (.p-row) */
  .p-row {
    height: 38px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 10px;
    border-radius: 8px;
    cursor: pointer;
    border: 1px solid transparent;
    transition: all 0.16s cubic-bezier(0.16, 1, 0.3, 1);
    position: relative;
    user-select: none;
  }
  .p-row:hover {
    background: var(--subcard);
    border-color: var(--card-border);
  }
  .p-row.active {
    background: linear-gradient(90deg, rgba(99, 102, 241, 0.18) 0%, rgba(99, 102, 241, 0.04) 100%);
    border-color: rgba(99, 102, 241, 0.35);
    border-left: 3px solid var(--primary);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
  }
  .p-row.dragging { opacity: 0.35; }
  .p-row.drag-over { border-top: 2px solid var(--primary); }

  .p-row-handle {
    font-size: 11px;
    color: var(--text-muted);
    cursor: grab;
    flex-shrink: 0;
    line-height: 1;
    opacity: 0.6;
    transition: opacity 0.15s;
  }
  .p-row:hover .p-row-handle { opacity: 1; color: var(--text-secondary); }

  .p-row-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--text-muted);
    transition: box-shadow 0.2s;
  }
  .p-row-dot.proto-openai-dot { background: var(--b-blue); box-shadow: 0 0 6px rgba(59, 130, 246, 0.6); }
  .p-row-dot.proto-claude-dot { background: #d946ef; box-shadow: 0 0 6px rgba(217, 70, 239, 0.6); }
  .p-row-dot.proto-gemini-dot { background: var(--accent-green); box-shadow: 0 0 6px rgba(16, 185, 129, 0.6); }
  .p-row-dot.proto-resp-dot   { background: var(--primary); box-shadow: 0 0 6px var(--primary-glow); }

  .p-row-name {
    font-size: 12px;
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
    font-weight: 500;
  }
  .p-row.active .p-row-name { font-weight: 600; color: #FFF; }

  .p-row-badges {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }
  .p-proto {
    font-size: 9px;
    font-family: var(--mono-family);
    padding: 1px 5px;
    border-radius: 4px;
    background: rgba(59, 130, 246, 0.14);
    color: #93c5fd;
    border: 1px solid rgba(59, 130, 246, 0.3);
    line-height: 1.2;
  }
  .p-proto.proto-builtin {
    background: rgba(168, 85, 247, 0.16);
    color: #d8b4fe;
    border-color: rgba(168, 85, 247, 0.35);
  }
  .p-count {
    font-size: 10px;
    font-family: var(--mono-family);
    color: var(--text-muted);
    padding: 0 3px;
  }

  .p-row-actions {
    display: none;
    align-items: center;
    gap: 2px;
    flex-shrink: 0;
  }
  .p-row:hover .p-row-actions,
  .p-row.active .p-row-actions { display: inline-flex; }
  .p-row-btn {
    width: 22px;
    height: 22px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--text-muted);
    border-radius: 4px;
    cursor: pointer;
    font-size: 11px;
    padding: 0;
    transition: all 0.12s;
  }
  .p-row-btn:hover { background: rgba(255, 255, 255, 0.1); color: var(--text-primary); }
  .p-row-btn.del:hover { background: var(--accent-rose); color: #FFF; }

  /* 侧边拖拽手柄 */
  .col-resizer {
    width: 4px;
    background: transparent;
    cursor: col-resize;
    flex-shrink: 0;
    transition: background 0.15s;
    z-index: 15;
  }
  .col-resizer:hover, .col-resizer.resizing { background: var(--primary); }

  /* ==================== 主工作区：模型管理 ==================== */
  .column-models {
    flex: 1;
    min-width: 0;
    background: transparent;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .model-work-tabs {
    height: 42px;
    min-height: 42px;
    padding: 0 14px;
    background: rgba(14, 20, 32, 0.75);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border-bottom: 1px solid var(--card-border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-shrink: 0;
    box-sizing: border-box;
  }
  .model-tab-buttons { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  /* 操作栏自适应：空间不足时内部横向滚动，绝不把按钮截断在窗口外 */
  #modelsBarActions, #fetchBarActions {
    min-width: 0;
    overflow-x: auto;
    scrollbar-width: none;
  }
  #modelsBarActions::-webkit-scrollbar, #fetchBarActions::-webkit-scrollbar { display: none; }
  #modelsBarActions > *, #fetchBarActions > * { flex-shrink: 0; }
  .model-tab-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 28px;
    padding: 0 10px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    color: var(--text-muted);
    border: 1px solid transparent;
    transition: all 0.16s ease;
    white-space: nowrap;
    user-select: none;
    box-sizing: border-box;
    flex-shrink: 0;
  }
  .model-tab-btn:hover {
    color: var(--text-primary);
    background: rgba(255, 255, 255, 0.05);
  }
  .model-tab-btn.active {
    color: #FFF;
    background: var(--subcard);
    border-color: rgba(255, 255, 255, 0.12);
    font-weight: 600;
  }
  .model-tab-badge {
    font-size: 10px;
    font-family: var(--mono-family);
    padding: 1px 5px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.08);
    color: var(--text-muted);
    border: 1px solid var(--card-border);
    line-height: 1.2;
  }
  .model-tab-btn.active .model-tab-badge {
    background: var(--primary);
    color: #FFF;
    border-color: var(--primary);
  }

  .model-view-pane {
    flex: 1;
    display: none;
    flex-direction: column;
    overflow: hidden;
    padding: 10px 14px 12px 14px;
    box-sizing: border-box;
  }
  .model-view-pane.active { display: flex; }

  .model-add-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    flex-shrink: 0;
  }
  .input {
    background: var(--subcard);
    border: 1px solid var(--card-border);
    border-radius: 6px;
    padding: 5px 10px;
    color: var(--text-primary);
    font-size: 12px;
    outline: none;
    transition: all 0.16s ease;
  }
  .input:focus {
    border-color: var(--primary);
    background: rgba(30, 41, 59, 0.85);
    box-shadow: 0 0 0 2px var(--primary-glow);
  }
  .input::placeholder { color: var(--text-muted); }

  /* 优雅细暗夜滚动条 */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.12); border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.22); }

  /* ==================== 表头与容器 (Retina Card) ==================== */
  .model-table-header {
    height: 32px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 12px;
    background: rgba(18, 24, 38, 0.88);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid var(--card-border);
    border-bottom: none;
    border-radius: 8px 8px 0 0;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    flex-shrink: 0;
    box-sizing: border-box;
  }
  .model-table-header .th-handle { width: 20px; flex-shrink: 0; text-align: center; }
  .model-table-header .th-alias  { width: 200px; flex-shrink: 0; padding-left: 6px; box-sizing: border-box; }
  .model-table-header .th-id     { flex: 1; min-width: 140px; padding-left: 6px; box-sizing: border-box; }
  .model-table-header .th-status { width: 70px; flex-shrink: 0; text-align: center; }
  .model-table-header .th-actions{ width: 290px; flex-shrink: 0; text-align: right; }

  /* 表格行容器 */
  .models-container {
    flex: 1;
    overflow-y: auto;
    border: 1px solid var(--card-border);
    border-radius: 0 0 8px 8px;
    background: rgba(15, 21, 34, 0.65);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    display: flex;
    flex-direction: column;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
  }

  /* 单行模型条目 (.model-row) */
  .model-row {
    height: 38px;
    min-height: 38px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 12px;
    border-bottom: 1px solid var(--card-border);
    transition: background 0.15s ease, border-color 0.15s ease;
    user-select: none;
    position: relative;
    box-sizing: border-box;
  }
  .model-row:last-child { border-bottom: none; }
  .model-row:hover { background: rgba(30, 41, 59, 0.5); }
  .model-row.disabled { opacity: 0.45; background: rgba(9, 13, 22, 0.6); }
  .model-row.dragging { opacity: 0.28; }
  .model-row.drag-over { border-top: 2px solid var(--primary); }

  .model-row-handle {
    width: 20px;
    font-size: 12px;
    color: var(--text-muted);
    cursor: grab;
    flex-shrink: 0;
    line-height: 1;
    text-align: center;
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0.5;
    transition: opacity 0.15s;
  }
  .model-row:hover .model-row-handle { opacity: 1; color: var(--text-secondary); }

  .model-row-alias {
    width: 200px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
  }
  .alias-input {
    width: 100%;
    box-sizing: border-box;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 3px 6px;
    color: var(--text-primary);
    font-size: 12px;
    font-weight: 500;
    font-family: inherit;
    outline: none;
    transition: all 0.14s ease;
  }
  .alias-input:hover { background: rgba(30, 41, 59, 0.6); border-color: rgba(255, 255, 255, 0.12); }
  .alias-input:focus {
    background: var(--bg);
    border-color: var(--primary);
    box-shadow: 0 0 0 2px var(--primary-glow);
  }

  .model-row-id {
    flex: 1;
    min-width: 140px;
    box-sizing: border-box;
    font-family: var(--mono-family);
    font-size: 11.5px;
    color: #cbd5e1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    padding: 2px 7px;
    background: rgba(9, 13, 22, 0.7);
    border-radius: 4px;
    border: 1px solid var(--card-border);
    line-height: 1.4;
  }

  .model-meta-badges {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    margin-left: 6px;
    flex-shrink: 0;
  }
  .meta-badge {
    font-size: 9.5px;
    font-family: var(--mono-family);
    padding: 1px 5px;
    border-radius: 4px;
    line-height: 1.3;
    white-space: nowrap;
  }
  .meta-badge.badge-ctx {
    background: rgba(59, 130, 246, 0.14);
    color: #93c5fd;
    border: 1px solid rgba(59, 130, 246, 0.28);
  }
  .meta-badge.badge-img {
    background: rgba(16, 185, 129, 0.14);
    color: #6ee7b7;
    border: 1px solid rgba(16, 185, 129, 0.28);
  }
  .meta-badge.badge-reason {
    background: rgba(168, 85, 247, 0.15);
    color: #d8b4fe;
    border: 1px solid rgba(168, 85, 247, 0.32);
  }

  .model-row-status {
    width: 70px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .disabled-badge {
    font-size: 10px;
    font-family: var(--mono-family);
    padding: 1px 6px;
    border-radius: 4px;
    background: rgba(239, 68, 68, 0.14);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.32);
    white-space: nowrap;
  }
  .fail-streak {
    font-size: 10px;
    font-family: var(--mono-family);
    padding: 1px 5px;
    border-radius: 4px;
    background: rgba(245, 158, 11, 0.14);
    color: #fcd34d;
    border: 1px solid rgba(245, 158, 11, 0.32);
    white-space: nowrap;
  }

  /* 默认置顶模型行（琥珀金光拟态） */
  .model-row.pinned {
    background: linear-gradient(90deg, rgba(245, 158, 11, 0.14) 0%, rgba(245, 158, 11, 0.02) 50%);
    box-shadow: inset 3px 0 0 var(--accent-amber);
  }
  .model-row.pinned .model-row-handle {
    color: var(--accent-amber);
    opacity: 1;
    cursor: not-allowed;
  }
  .btn.is-on {
    background: rgba(16, 185, 129, 0.2);
    color: #6ee7b7;
    border-color: rgba(16, 185, 129, 0.5);
    box-shadow: 0 0 10px rgba(16, 185, 129, 0.25);
  }

  .model-row-actions {
    width: 290px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 4px;
  }
  .model-row-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 3px 7px;
    border-radius: 4px;
    border: 1px solid var(--card-border);
    background: rgba(255, 255, 255, 0.03);
    color: var(--text-secondary);
    font-size: 11px;
    cursor: pointer;
    line-height: 1.3;
    white-space: nowrap;
    transition: all 0.14s ease;
  }
  .model-row-btn:hover {
    background: var(--subcard);
    border-color: rgba(255, 255, 255, 0.18);
    color: var(--text-primary);
  }
  .model-row-btn.test-btn {
    color: #93c5fd;
    border-color: rgba(59, 130, 246, 0.3);
    background: rgba(59, 130, 246, 0.1);
  }
  .model-row-btn.test-btn:hover {
    background: rgba(59, 130, 246, 0.24);
    border-color: #93c5fd;
    box-shadow: 0 0 8px rgba(59, 130, 246, 0.3);
  }
  .model-row-btn.test-btn.testing { opacity: 0.5; cursor: wait; }
  .model-row-btn.test-btn.ok {
    color: #6ee7b7;
    background: rgba(16, 185, 129, 0.16);
    border-color: rgba(16, 185, 129, 0.45);
    box-shadow: 0 0 8px var(--accent-green-glow);
  }
  .model-row-btn.test-btn.fail {
    color: #fca5a5;
    background: rgba(239, 68, 68, 0.16);
    border-color: rgba(239, 68, 68, 0.45);
    box-shadow: 0 0 8px var(--accent-rose-glow);
  }
  .model-row-btn.test-btn.warn {
    color: #fde68a;
    background: rgba(245, 158, 11, 0.16);
    border-color: rgba(245, 158, 11, 0.45);
    box-shadow: 0 0 8px rgba(245, 158, 11, 0.3);
  }
  .model-row-btn.default-btn {
    color: var(--accent-amber);
    border-color: rgba(245, 158, 11, 0.3);
  }
  .model-row-btn.default-btn.is-default {
    background: rgba(245, 158, 11, 0.2);
    border-color: var(--accent-amber);
    color: #fcd34d;
    font-weight: 600;
    box-shadow: 0 0 8px var(--accent-amber-glow);
  }
  .model-row-btn.del-btn:hover {
    background: var(--accent-rose);
    color: #FFF;
    border-color: var(--accent-rose);
  }

  /* ==================== 侧滑抽屉面板 (Retina Drawer) ==================== */
  .provider-drawer {
    position: absolute;
    inset: 0;
    z-index: 50;
    pointer-events: none;
    visibility: hidden;
    transition: visibility 0s 0.24s;
    overflow: hidden;
  }
  .provider-drawer.open {
    pointer-events: auto;
    visibility: visible;
    transition: visibility 0s 0s;
  }
  .drawer-backdrop {
    position: absolute;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.24s ease;
  }
  .provider-drawer.open .drawer-backdrop {
    opacity: 1;
    pointer-events: auto;
  }
  .drawer-panel {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    width: 380px;
    max-width: calc(100vw - 260px);
    background: rgba(14, 20, 32, 0.96);
    backdrop-filter: blur(28px) saturate(190%);
    -webkit-backdrop-filter: blur(28px) saturate(190%);
    border-left: 1px solid var(--card-border);
    display: flex;
    flex-direction: column;
    box-shadow: -10px 0 36px rgba(0, 0, 0, 0.65);
    transform: translateX(100%);
    transition: transform 0.24s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .provider-drawer.open .drawer-panel {
    transform: translateX(0);
  }
  .drawer-header {
    height: 46px;
    padding: 0 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--card-border);
    font-size: 13.5px;
    font-weight: 600;
    flex-shrink: 0;
    background: rgba(18, 24, 38, 0.85);
  }
  .drawer-close {
    width: 28px;
    height: 28px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--text-muted);
    border-radius: 6px;
    cursor: pointer;
    font-size: 14px;
    transition: all 0.14s ease;
  }
  .drawer-close:hover { background: rgba(255, 255, 255, 0.1); color: var(--text-primary); }

  .drawer-body {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-height: 0;
  }

  .drawer-footer {
    flex-shrink: 0;
    padding: 12px 16px;
    background: rgba(12, 17, 28, 0.98);
    border-top: 1px solid var(--card-border);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .view-panel {
    display: flex;
    flex-direction: column;
    gap: 10px;
    background: var(--subcard);
    border: 1px solid var(--card-border);
    border-radius: 8px;
    padding: 14px;
  }
  .view-field { display: flex; flex-direction: column; gap: 3px; }
  .view-label { font-size: 10.5px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.6px; }
  .view-value { font-size: 12px; color: var(--text-primary); font-family: var(--mono-family); word-break: break-all; }
  .view-value.empty { color: var(--text-muted); font-style: italic; }

  .edit-panel {
    display: none;
    flex-direction: column;
    gap: 12px;
    background: var(--subcard);
    border: 1px solid var(--card-border);
    border-radius: 8px;
    padding: 14px;
  }
  .form-group { display: flex; flex-direction: column; gap: 5px; }
  .form-group label { font-size: 11px; font-weight: 600; color: var(--text-secondary); }
  .input-wrapper { position: relative; display: flex; align-items: center; }
  .input-wrapper .input { width: 100%; padding-right: 32px; box-sizing: border-box; }
  .toggle-pwd {
    position: absolute;
    right: 8px;
    cursor: pointer;
    font-size: 13px;
    color: var(--text-muted);
    user-select: none;
    transition: color 0.15s;
  }
  .toggle-pwd:hover { color: var(--text-primary); }

  /* ==================== 底部状态栏 (Footer) ==================== */
  /* ==================== 底部运行日志面板 ==================== */
  .log-panel {
    flex-shrink: 0;
    background: rgba(10, 15, 26, 0.96);
    border-top: 1px solid var(--card-border);
    display: flex;
    flex-direction: column;
    position: relative;
    z-index: 35;
    transition: box-shadow 0.2s ease;
  }
  .log-panel.open {
    box-shadow: 0 -12px 32px rgba(0, 0, 0, 0.45);
  }
  .log-panel-head {
    height: 30px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 12px;
    cursor: pointer;
    user-select: none;
    background: rgba(255, 255, 255, 0.025);
    border-bottom: 1px solid transparent;
    font-size: 11px;
    color: var(--text-muted);
  }
  .log-panel.open .log-panel-head {
    border-bottom-color: var(--card-border);
    background: rgba(255, 255, 255, 0.045);
  }
  .log-panel-head:hover { background: rgba(255, 255, 255, 0.07); }
  .log-title {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-weight: 600;
    color: var(--text-secondary);
    white-space: nowrap;
    flex-shrink: 0;
  }
  .log-badge {
    min-width: 16px;
    height: 16px;
    padding: 0 5px;
    border-radius: 8px;
    background: rgba(148, 163, 184, 0.18);
    color: #cbd5e1;
    font-size: 10px;
    line-height: 16px;
    text-align: center;
    font-family: var(--mono-family);
  }
  .log-badge.err {
    background: rgba(239, 68, 68, 0.22);
    color: #fca5a5;
  }
  .log-last {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--mono-family);
    font-size: 10.5px;
    color: var(--text-muted);
    opacity: 0.9;
  }
  .log-actions { display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0; }
  .log-btn {
    background: transparent;
    border: 1px solid var(--card-border);
    color: var(--text-secondary);
    font-size: 10.5px;
    padding: 2px 7px;
    border-radius: 5px;
    cursor: pointer;
    transition: all 0.14s ease;
    font-family: var(--font-family);
  }
  .log-btn:hover {
    background: rgba(99, 102, 241, 0.18);
    border-color: rgba(99, 102, 241, 0.5);
    color: #e2e8f0;
  }
  .log-panel-body {
    display: none;
    height: 180px;
    max-height: 32vh;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 8px 12px 10px 12px;
    font-family: var(--mono-family);
    font-size: 11px;
    line-height: 1.55;
    color: #cbd5e1;
    user-select: text;
    background: #060910;
  }
  .log-panel.open .log-panel-body { display: block; }
  .log-line {
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
    padding: 1px 0;
    border-left: 2px solid transparent;
    padding-left: 7px;
  }
  .log-line .log-ts { color: #64748b; margin-right: 6px; }
  .log-line.err { color: #fca5a5; border-left-color: #ef4444; background: rgba(239, 68, 68, 0.07); }
  .log-line.warn { color: #fcd34d; border-left-color: #f59e0b; background: rgba(245, 158, 11, 0.06); }
  .log-line.ok { color: #86efac; border-left-color: #10b981; }
  .log-line.info { color: #cbd5e1; }
  .log-line.sys { color: #a5b4fc; border-left-color: #6366f1; }
  .log-empty { color: #64748b; font-style: italic; padding: 6px 4px; }

  footer {
    height: 28px;
    background: rgba(14, 20, 32, 0.9);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border-top: 1px solid var(--card-border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 14px;
    font-size: 11px;
    color: var(--text-muted);
    flex-shrink: 0;
    position: relative;
    z-index: 30;
  }
  .footer-left, .footer-right { display: flex; align-items: center; gap: 14px; }
  .footer-left { min-width: 0; flex: 1 1 auto; overflow: hidden; }
  #statusMsg {
    display: inline-block;
    max-width: 46vw;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    vertical-align: middle;
  }
  #statusMsg.has-detail {
    cursor: pointer;
    text-decoration: underline dotted rgba(148, 163, 184, 0.45);
    text-underline-offset: 3px;
  }
  #statusMsg.has-detail:hover { filter: brightness(1.35); }
  .footer-log-btn {
    background: transparent;
    border: 1px solid var(--card-border);
    color: var(--text-secondary);
    font-size: 10.5px;
    padding: 2px 7px;
    border-radius: 5px;
    cursor: pointer;
    transition: all 0.14s ease;
  }
  .footer-log-btn:hover {
    background: rgba(99, 102, 241, 0.2);
    border-color: rgba(99, 102, 241, 0.5);
    color: #e2e8f0;
  }
  .footer-log-btn.has-err { border-color: rgba(239, 68, 68, 0.55); color: #fca5a5; }
  kbd {
    font-family: var(--mono-family);
    font-size: 10px;
    padding: 1px 5px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid var(--card-border);
    color: var(--text-secondary);
  }

  /* ==================== 对话框 (Retina Modal Box) ==================== */
  .modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.72);
    z-index: 200;
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(12px) saturate(180%);
    -webkit-backdrop-filter: blur(12px) saturate(180%);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.22s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .modal-backdrop.open {
    opacity: 1;
    pointer-events: auto;
  }
  .modal-box {
    background: rgba(15, 21, 35, 0.94);
    backdrop-filter: blur(32px) saturate(200%);
    -webkit-backdrop-filter: blur(32px) saturate(200%);
    border: 1px solid rgba(255, 255, 255, 0.12);
    box-shadow: var(--modal-shadow);
    border-radius: 12px;
    width: 480px;
    max-width: 92vw;
    max-height: 88vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    transform: scale(0.95) translateY(8px);
    transition: transform 0.22s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.22s ease;
  }
  .modal-backdrop.open .modal-box {
    transform: scale(1) translateY(0);
  }
  .modal-header {
    height: 44px;
    padding: 0 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: rgba(255, 255, 255, 0.03);
    border-bottom: 1px solid var(--card-border);
    font-size: 13.5px;
    font-weight: 600;
    font-family: var(--mono-family);
    color: var(--text-primary);
    flex-shrink: 0;
  }
  .modal-header .drawer-close {
    width: 28px;
    height: 28px;
    border-radius: 6px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: none;
    color: var(--text-muted);
    font-size: 14px;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .modal-header .drawer-close:hover {
    background: rgba(255, 255, 255, 0.1);
    color: var(--text-primary);
  }
  .modal-body {
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    font-size: 12px;
    color: var(--text-secondary);
    line-height: 1.6;
    min-height: 0;
    overflow-y: auto;
  }

  /* 极客暗夜终端框 (UI Skill 规范 Terminal Box) */
  .modal-report {
    font-family: var(--mono-family);
    font-size: 11.5px;
    line-height: 1.65;
    color: #cbd5e1;
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
    overflow-x: hidden;
    overflow-y: auto;
    max-height: 64vh;
    user-select: text;
    padding: 14px 16px;
    background: #060910;
    border: 1px solid var(--card-border);
    border-radius: 8px;
    tab-size: 2;
    box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.5);
  }
  .modal-hint {
    font-size: 11px;
    color: var(--text-secondary);
    opacity: 0.75;
    line-height: 1.5;
  }

  /* 顺序自检：左右两栏对照 */
  .modal-html {
    font-size: 12.5px;
    line-height: 1.55;
    max-height: 60vh;
    overflow-y: auto;
    overflow-x: hidden;
  }
  /* 从对端导入：服务商勾选器 */
  .ip-toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    font-size: 11.5px;
    color: var(--text-secondary);
  }
  .ip-toolbar .ip-tip { flex: 1; opacity: .8; }
  .ip-mini {
    flex: none;
    background: rgba(148, 163, 184, .12);
    color: var(--text-secondary);
    border: 1px solid var(--card-border);
    border-radius: 5px;
    padding: 3px 9px;
    font-size: 11px;
    cursor: pointer;
    transition: background .15s ease;
  }
  .ip-mini:hover { background: rgba(148, 163, 184, .24); }
  .ip-list { display: flex; flex-direction: column; gap: 4px; }
  .ip-row {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 7px 10px;
    border-radius: 6px;
    background: rgba(148, 163, 184, .06);
    border: 1px solid transparent;
    cursor: pointer;
    transition: background .15s ease, border-color .15s ease;
  }
  .ip-row:hover { background: rgba(148, 163, 184, .13); border-color: var(--card-border); }
  .ip-row input { accent-color: #10B981; flex: none; margin: 0; }
  .ip-name {
    flex: 1;
    min-width: 0;
    font-weight: 600;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .ip-id { flex: none; font-family: var(--mono-family); font-size: 10.5px; opacity: .6; }
  .ip-count { flex: none; font-size: 10.5px; opacity: .7; }
  .ip-badge { flex: none; font-size: 10px; padding: 1px 6px; border-radius: 4px; }
  .ip-badge-new { background: rgba(16, 185, 129, .15); color: #6EE7B7; border: 1px solid rgba(16, 185, 129, .35); }
  .ip-badge-warn { background: rgba(245, 158, 11, .15); color: #FCD34D; border: 1px solid rgba(245, 158, 11, .35); }
  .oc { min-width: 0; }
  .oc-verdict {
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 600;
    margin-bottom: 12px;
    border: 1px solid transparent;
    line-height: 1.5;
  }
  .oc-verdict.ok   { background: rgba(16, 185, 129, 0.14); border-color: rgba(16, 185, 129, 0.42); color: #6ee7b7; box-shadow: 0 0 12px var(--accent-green-glow); }
  .oc-verdict.warn { background: rgba(245, 158, 11, 0.14); border-color: rgba(245, 158, 11, 0.42); color: #fcd34d; box-shadow: 0 0 12px var(--accent-amber-glow); }
  .oc-verdict.info { background: rgba(99, 102, 241, 0.14); border-color: rgba(99, 102, 241, 0.42); color: #a5b4fc; box-shadow: 0 0 12px var(--primary-glow); }
  .oc-tbl {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 46px;
    border: 1px solid var(--card-border);
    border-radius: 8px;
    overflow: hidden;
    background: rgba(0, 0, 0, 0.25);
  }
  .oc-hd, .oc-tr { display: contents; }
  .oc-c {
    padding: 9px 12px;
    border-top: 1px solid var(--card-border);
    min-width: 0;
  }
  .oc-hd .oc-c {
    border-top: 0;
    background: rgba(255, 255, 255, 0.05);
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary);
    letter-spacing: .4px;
  }
  .oc-tr.bad .oc-c { background: rgba(245, 158, 11, 0.1); }
  .oc-n { display: block; font-size: 10px; font-weight: 400; opacity: .7; margin-top: 2px; }
  .oc-top { display: flex; align-items: center; gap: 6px; min-width: 0; }
  .oc-i {
    font-size: 10px;
    color: var(--text-secondary);
    background: rgba(255, 255, 255, 0.08);
    border-radius: 4px;
    padding: 1px 5px;
    flex: none;
  }
  .oc-top b { font-size: 12px; word-break: break-all; }
  .oc-m { font-size: 10.5px; color: var(--text-secondary); margin-top: 4px; line-height: 1.55; word-break: break-word; }
  .oc-star { color: #fcd34d; font-weight: 600; }
  .oc-chip {
    font-size: 9.5px;
    color: #fcd34d;
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-radius: 4px;
    padding: 0 4px;
    white-space: nowrap;
  }
  .oc-empty { opacity: .6; font-style: italic; }
  .oc-res { text-align: center; }
  .oc-badge { font-size: 13px; }
  .oc-sum { margin-top: 12px; display: grid; gap: 6px; }
  .oc-sr {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    font-size: 11.5px;
    padding: 7px 12px;
    background: rgba(0, 0, 0, 0.35);
    border-radius: 6px;
    border: 1px solid var(--card-border);
  }
  .oc-sr span { color: var(--text-secondary); }
  .oc-sr b { font-weight: 600; text-align: right; }
  .oc-sr b.ok   { color: #6ee7b7; }
  .oc-sr b.bad  { color: #fcd34d; }
  .oc-sr b.info { color: #a5b4fc; }
  .oc-foot { margin-top: 10px; font-size: 10.5px; color: var(--text-secondary); opacity: .75; line-height: 1.6; word-break: break-all; }
  .modal-footer {
    padding: 12px 18px 14px 18px;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
    background: rgba(18, 24, 38, 0.85);
    border-top: 1px solid var(--card-border);
  }

  /* 二级弹窗卡片式单选框选项 */
  .modal-option-card {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 12px 14px;
    background: var(--subcard);
    border: 1px solid var(--card-border);
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.16s ease;
  }
  .modal-option-card:hover {
    border-color: rgba(255, 255, 255, 0.16);
    background: rgba(255, 255, 255, 0.05);
  }
  .modal-option-card.selected {
    border-color: var(--primary);
    background: rgba(99, 102, 241, 0.12);
    box-shadow: 0 0 12px var(--primary-glow);
  }

  /* Model row Key badge button */
  .model-row-btn.key-btn {
    font-family: inherit;
    color: var(--text-muted);
    border-color: var(--card-border);
  }
  .model-row-btn.key-btn:hover {
    color: var(--text-primary);
    border-color: rgba(255, 255, 255, 0.2);
  }
  .model-row-btn.key-btn.is-custom {
    color: #fcd34d;
    background: rgba(245, 158, 11, 0.15);
    border-color: rgba(245, 158, 11, 0.38);
  }
  .model-row-btn.key-btn.is-pool {
    color: #93c5fd;
    background: rgba(59, 130, 246, 0.15);
    border-color: rgba(59, 130, 246, 0.38);
  }

  /* Key Pool List styling */
  .key-pool-item {
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--subcard);
    border: 1px solid var(--card-border);
    padding: 7px 10px;
    border-radius: 6px;
    transition: border-color 0.15s;
  }
  .key-pool-item:hover {
    border-color: rgba(255, 255, 255, 0.16);
  }

  /* Keepcompat stubs */
  .model-tag { display: none; }
  .column-provider { display: none; }

  /* 8 方向无边框缩放把手 */
  .resize-handle {
    position: fixed;
    z-index: 9999;
    background: transparent;
  }
  .rh-top { top: 0; left: 12px; right: 12px; height: 8px; cursor: ns-resize; }
  .rh-bottom { bottom: 0; left: 12px; right: 12px; height: 8px; cursor: ns-resize; }
  .rh-left { top: 12px; bottom: 12px; left: 0; width: 8px; cursor: ew-resize; }
  .rh-right { top: 12px; bottom: 12px; right: 0; width: 8px; cursor: ew-resize; }
  .rh-top-left { top: 0; left: 0; width: 14px; height: 14px; cursor: nwse-resize; }
  .rh-top-right { top: 0; right: 0; width: 14px; height: 14px; cursor: nesw-resize; }
  .rh-bottom-left { bottom: 0; left: 0; width: 14px; height: 14px; cursor: nesw-resize; }
  .rh-bottom-right { bottom: 0; right: 0; width: 22px; height: 22px; cursor: nwse-resize; }

  .window-resize-grip {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    cursor: nwse-resize;
    color: var(--text-muted);
    opacity: 0.7;
    margin-left: 6px;
    border-radius: 4px;
    transition: all 0.16s ease;
  }
  .window-resize-grip:hover {
    opacity: 1;
    color: #a5b4fc;
    background: rgba(99, 102, 241, 0.15);
  }

  .summary-pill {
    display: inline-flex;
    align-items: center;
    padding: 2px 7px;
    border-radius: 8px;
    background: rgba(99, 102, 241, 0.14);
    border: 1px solid rgba(99, 102, 241, 0.28);
    color: #a5b4fc;
    font-size: 11px;
    font-family: var(--mono-family);
    white-space: nowrap;
    cursor: help;
  }
</style>
</head>
<body>

<header class="pywebview-drag-region" ondblclick="handleHeaderDblClick(event)">
  <div class="header-left pywebview-no-drag-region">
    <div class="logo-title pywebview-drag-region" title="按住拖动窗口 · 双击最大化/还原">
      <div class="brand-mark" style="font-size: 15px; font-weight: 800; font-family: var(--b-sans);">π</div>
      <div class="brand-text">
        <span class="brand-title">模型管理工作台</span>
        <span class="sub-en">UNIFIED MODEL STUDIO</span>
      </div>
    </div>
    <div class="target-tabs pywebview-no-drag-region" id="targetTabs" style="display:none;"></div>
  </div>

  <div class="header-right pywebview-no-drag-region">
    <div class="header-actions pywebview-no-drag-region" style="display: flex; gap: 6px;">
      <button class="btn btn-purple" id="rescanBuiltinsBtn" onclick="rescanBuiltins()" title="重新扫描 auth.json / models-store.json / 环境变量中的内置服务商">
        <span>🔄 扫描</span>
      </button>
      <button class="btn btn-primary" id="saveBtn" onclick="saveAll()" title="保存配置 (Ctrl+S)">
        <span>💾 保存</span>
      </button>
      <button class="btn btn-indigo" id="importCounterpartBtn" onclick="importFromCounterpart()" style="display:none;" title="从对端导入模型配置">
        <span>📥 导入配置</span>
      </button>
      <button class="btn btn-secondary" id="exportBtn" onclick="exportAllProviders()" title="导出全部服务商为 TXT">
        <span>📦 导出</span>
      </button>
      <button class="btn btn-emerald" id="restartPiBtn" onclick="restartPi()" title="重启 Pi 交互终端">
        <span>🔄 重启</span>
      </button>
      <button class="btn btn-indigo" id="orderReportBtn" onclick="showOrderReport()" title="左右两栏对照：工具窗口顺序 ↔ Pi 实际顺序（逐行给出✅/⚠️结果）">
        <span>🔍 自检</span>
      </button>
      <button class="btn btn-secondary" id="logBtn" onclick="showErrorLog()" title="查看完整错误日志（可复制 / 直接打开日志文件）">
        <span>📜 日志</span>
      </button>
      <button class="btn btn-purple" id="orderPatchBtn" onclick="showPiOrderPatchDialog()" title="让 Pi 的厂商分组顺序跟随工具（可一键还原原版）">
        <span>🧩 补丁</span>
      </button>
    </div>
    
    <div class="window-controls pywebview-no-drag-region">
      <div class="win-btn" title="最小化" onclick="window.pywebview.api.minimize_window()">—</div>
      <div class="win-btn" title="最大化/还原" onclick="window.pywebview.api.maximize_window()">▢</div>
      <div class="win-btn close" title="关闭" onclick="window.pywebview.api.close_window()">✕</div>
    </div>
  </div>
</header>

<div class="layout">
  <!-- 栏目1: 紧凑服务商侧栏 (260px) -->
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <div style="display: flex; align-items: center; gap: 6px;">
        <span>服务商</span>
        <span class="count-chip" id="sidebarCount">0</span>
      </div>
      <button class="btn btn-secondary" style="padding: 2px 7px; font-size: 11px;" onclick="newProvider()">➕ 新建</button>
      <button class="btn btn-secondary" id="providerOrderToggle" style="padding: 2px 7px; font-size: 11px;"
              onclick="toggleUiPref('alignProviderOrder')" title="按 Pi 的字母序显示厂商">⇅ Pi 字母序</button>
    </div>
    <div class="sidebar-search">
      <input class="search-input" id="providerSearch" placeholder="🔍 搜索服务商..." oninput="renderSidebar()">
    </div>
    <div class="provider-list" id="providerList"></div>
  </aside>

  <!-- 侧栏拖拽调宽 -->
  <div class="col-resizer" id="resizerSidebar"></div>

  <!-- 栏目2: 全宽模型管理工作区 -->
  <section class="column-models">
    <!-- 顶部统一工作栏：无论切换到哪个 Tab，都共享同一顶栏背景与边框，绝无突兀错位 -->
    <div class="model-work-tabs">
      <div class="model-tab-buttons">
        <div class="model-tab-btn active" id="tabBtnModels" onclick="switchModelWorkTab('models')">
          <span>📋 已配置模型</span>
          <span class="model-tab-badge" id="modelsTabBadge">0</span>
        </div>
        <div class="model-tab-btn" id="tabBtnFetch" onclick="switchModelWorkTab('fetch')">
          <span>📥 拉取预览</span>
          <span class="model-tab-badge" id="fetchTabBadge" style="display: none;">0</span>
        </div>
      </div>

      <!-- Tab 1 专属操作按钮组 -->
      <div id="modelsBarActions" style="display: flex; gap: 5px; align-items: center; flex: 1;">
        <input class="search-input" id="modelSearch" style="width: 110px; height: 28px; font-size: 11.5px; padding: 0 8px;" placeholder="🔍 过滤模型/别名..." oninput="renderModels()">
        <button class="btn btn-secondary" id="pinDefaultToggle" onclick="toggleUiPref('pinDefaultModel')" title="把 Pi 默认模型置顶显示（仅显示，不改动保存顺序）">⭐ 默认置顶</button>
        <button class="btn btn-indigo" id="testAllBtn" onclick="testAllModels()" title="依次测活当前服务商的所有模型">⚡ 全部测活</button>
        <button class="btn btn-secondary" id="openDrawerBtn" onclick="openDrawer(selectedPid)" title="查看或编辑服务商连接参数与密钥">⚙️ 厂商设置</button>
      </div>

      <!-- Tab 2 专属操作按钮组：与 Tab 1 严格等高同级，紧凑排布，自适应防溢出 -->
      <div id="fetchBarActions" style="display: none; gap: 5px; align-items: center; flex: 1;">
        <span id="fetchSummary" style="margin-right: 2px; flex-shrink: 0;"></span>
        <button class="btn btn-emerald" onclick="fetchRemoteModelsFromTab()" title="从当前 Base URL 重新拉取可用模型">🔄 拉取</button>
        <input class="search-input" id="previewSearch" style="width: 90px; height: 28px; font-size: 11.5px; padding: 0 8px;" placeholder="🔍 过滤..." oninput="renderFetchPreview()">
        <button class="btn btn-emerald" onclick="commitFetchedModels()" title="将勾选的模型批量添加到当前服务商">✅ 导入选中</button>
        <button class="btn btn-indigo" onclick="testAllFetched()" title="依次测活所有拉取到的模型">⚡ 测活</button>
        <button class="btn btn-ghost" onclick="toggleAllFetched(true)" title="选中全部模型">☑ 全选</button>
        <button class="btn btn-ghost" onclick="toggleAllFetched(false)" title="取消所有勾选">☐ 清空</button>
        <button class="btn btn-ghost" onclick="switchModelWorkTab('models')" title="返回已配置模型列表">✕ 返回</button>
      </div>
    </div>

    <!-- Tab 1: 已配置模型单行表格 -->
    <div class="model-view-pane active" id="paneModels">
      <div class="model-add-bar">
        <input class="input" id="newModelId" placeholder="模型 ID (例如: deepseek-chat, gpt-4o)" style="flex: 1.2;">
        <input class="input" id="newModelName" placeholder="显示别名 (可选)" style="flex: 1;">
        <button class="btn btn-primary" onclick="addModelManual()">➕ 添加模型</button>
      </div>

      <!-- 不滚动的表头 -->
      <div class="model-table-header" id="modelTableHeader">
        <span class="th-handle"></span>
        <span class="th-alias">别名 (点击编辑)</span>
        <span class="th-id">模型 ID</span>
        <span class="th-status">状态</span>
        <span class="th-actions">操作</span>
      </div>

      <!-- 滚动的单行表格行容器 -->
      <div class="models-container" id="modelsContainer">
        <div style="color: var(--b-text-3); font-size: 12px; padding: 20px; text-align: center;">暂无模型，点击「厂商设置 → 自动拉取」或上方手动添加</div>
      </div>
    </div>

    <!-- Tab 2: 远程拉取结果预览（直接顶齐表头，浑然一体） -->
    <div class="model-view-pane" id="paneFetch">
      <!-- 不滚动的拉取表头 -->
      <div class="model-table-header" id="fetchTableHeader">
        <span class="th-handle" style="width: 20px; text-align: center;">☑</span>
        <span class="th-alias" style="width: 200px;">别名 (可直接编辑)</span>
        <span class="th-id">模型 ID</span>
        <span class="th-status" style="width: 70px;">状态</span>
        <span class="th-actions" style="width: 100px;">操作</span>
      </div>

      <!-- 滚动的单行拉取结果列表 -->
      <div class="models-container" id="fetchPreviewList"></div>
    </div>
  </section>

  <!-- 右滑厂商抽屉 (#providerDrawer, 370px) -->
  <div class="provider-drawer" id="providerDrawer">
    <div class="drawer-backdrop" onclick="closeDrawer()"></div>
    <div class="drawer-panel">
      <div class="drawer-header">
        <span id="drawerTitle">🛠️ 服务商连接</span>
        <div style="display: flex; align-items: center; gap: 6px;">
          <div id="providerModeActions" style="display: flex; gap: 6px;">
            <button class="btn btn-secondary" id="editProviderBtn" onclick="toggleProviderEditMode(true)" title="编辑服务商参数">✏️ 编辑</button>
            <button class="btn btn-primary" id="saveProviderBtn" style="display: none;" onclick="saveProviderEdit()" title="保存修改">✓ 完成</button>
            <button class="btn btn-ghost" id="cancelProviderBtn" style="display: none;" onclick="cancelProviderEdit()" title="取消修改">✕ 取消</button>
          </div>
          <button class="drawer-close" onclick="closeDrawer()" title="关闭抽屉 (Esc)">✕</button>
        </div>
      </div>

      <div class="drawer-body">
        <!-- 查看模式 -->
        <div class="view-panel" id="providerViewPanel">
          <div class="view-field">
            <span class="view-label">服务商 ID</span>
            <span class="view-value" id="vId">-</span>
          </div>
          <div class="view-field">
            <span class="view-label">显示名称</span>
            <span class="view-value" id="vName">-</span>
          </div>
          <div class="view-field">
            <span class="view-label">Base URL 端点</span>
            <span class="view-value" id="vBaseUrl">-</span>
          </div>
          <div class="view-field">
            <span class="view-label">默认 API 密钥 (Key)</span>
            <div style="display: flex; align-items: center; justify-content: space-between;">
              <span class="view-value" id="vApiKey" style="letter-spacing: 0.05em;">-</span>
              <button class="btn btn-ghost" style="padding: 1px 4px; font-size: 11px;" onclick="toggleViewKeyMask()" id="viewMaskBtn" title="显隐 Key">👁️</button>
            </div>
          </div>
          <div class="view-field">
            <span class="view-label">密钥池 (Key Pool)</span>
            <div id="vKeyPoolSummary" style="font-size: 11.5px; color: var(--b-text-2); margin-top: 2px;">未配置</div>
          </div>
          <div class="view-field">
            <span class="view-label">协议类型</span>
            <span class="view-value" id="vApi">-</span>
          </div>
        </div>

        <!-- 编辑模式 -->
        <div class="edit-panel" id="providerEditPanel">
          <div class="form-group">
            <label>服务商 ID <span style="color: var(--b-accent);">*</span></label>
            <input class="input" id="pId" placeholder="例如: deepseek, grok, openrouter">
          </div>
          <div class="form-group">
            <label>显示名称 (可选)</label>
            <input class="input" id="pName" placeholder="例如: DeepSeek Official">
          </div>
          <div class="form-group">
            <label>Base URL <span style="color: var(--b-accent);">*</span></label>
            <input class="input" id="pBaseUrl" placeholder="例如: https://api.deepseek.com/v1">
          </div>
          <div class="form-group">
            <label>API 密钥 (API Key)</label>
            <div class="input-wrapper">
              <input class="input" id="pApiKey" type="password" placeholder="例如: sk-..." oninput="syncCurrentFormToMemory()">
              <span class="toggle-pwd" id="toggleEditApiKeyBtn" onclick="toggleEditApiKeyVisibility()" title="显隐 Key">👁️</span>
            </div>
          </div>
          <div class="form-group">
            <label>API 协议类型</label>
            <select class="input" id="pApi" onchange="syncCurrentFormToMemory(); renderSidebar();">
              <option value="openai-completions">openai-completions (OpenAI 补全)</option>
              <option value="openai-responses">openai-responses (OpenAI Responses)</option>
              <option value="anthropic-messages">anthropic-messages (Claude 原生)</option>
              <option value="google-generative-ai">google-generative-ai (Gemini 原生)</option>
            </select>
          </div>

          <!-- Key 池管理模块 -->
          <div class="form-group" style="margin-top: 6px; padding-top: 10px; border-top: 1px dashed var(--b-line);">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px;">
              <label style="font-size: 11px; font-weight: 600; color: var(--b-text);">🔑 厂商密钥池 (多Key)</label>
              <button type="button" class="btn btn-secondary" style="padding: 1px 6px; font-size: 11px;" onclick="addKeyPoolItem()">➕ 新增 Key</button>
            </div>
            <div style="font-size: 10.5px; color: var(--b-text-3); margin-bottom: 6px;">为不同分组/模型分配不同的 API 密钥，可在模型列表中直接绑定。</div>
            <div id="keyPoolContainer" style="display: flex; flex-direction: column; gap: 6px; max-height: 180px; overflow-y: auto;">
              <!-- 动态渲染密钥池条目 -->
            </div>
          </div>
        </div>
      </div>

      <!-- 固定抽屉底部操作栏：永不被外部截断，永不随表单滚动走样 -->
      <div class="drawer-footer">
        <button class="btn btn-emerald" style="width: 100%;" onclick="fetchRemoteModels()">🔄 自动拉取远程模型</button>
        <button class="btn btn-secondary" style="width: 100%;" onclick="exportCurrentProvider()" title="导出当前服务商为 TXT">📤 导出当前服务商</button>
        <button class="btn btn-rose" id="deleteProviderBtn" style="width: 100%;" onclick="deleteCurrentProvider()" title="删除此服务商">🗑️ 删除服务商</button>
      </div>
    </div>
  </div>
</div>

<!-- 拉取模型时选择密钥弹窗 -->
<div class="modal-backdrop" id="fetchKeyModal" onclick="handleFetchKeyModalBackdrop(event)">
  <div class="modal-box" style="width: 380px;">
    <div class="modal-header">
      <span>🔄 选择用于拉取的 API 密钥</span>
      <button class="drawer-close" onclick="closeFetchKeyModal()">✕</button>
    </div>
    <div class="modal-body">
      <div style="font-size: 11.5px; color: var(--b-text-3); margin-bottom: 4px;">
        不同密钥可能有不同的可用模型列表。拉取成功后，系统会自动为这些模型绑定您选择的密钥。
      </div>
      <div id="fetchKeyModalOptions" style="display: flex; flex-direction: column; gap: 8px;">
        <!-- 动态填充 -->
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeFetchKeyModal()">取消</button>
      <button class="btn btn-emerald" onclick="confirmFetchRemoteModels()">✓ 开始拉取</button>
    </div>
  </div>
</div>

<!-- 模型独立 API Key / 密钥池绑定 弹窗配置 -->
<div class="modal-backdrop" id="modelKeyModal" onclick="handleModelKeyModalBackdrop(event)">
  <div class="modal-box">
    <div class="modal-header">
      <span id="modelKeyModalTitle">🔑 模型密钥配置</span>
      <button class="drawer-close" onclick="closeModelKeyModal()">✕</button>
    </div>
    <div class="modal-body">
      <div style="font-size: 11px; color: var(--b-text-3);">
        目标模型: <b id="modelKeyTargetId" style="color: var(--b-text); font-family: var(--b-mono);"></b>
      </div>

      <div style="display: flex; flex-direction: column; gap: 8px; margin-top: 4px;">
        <label class="modal-option-card" id="cardKeyModeDefault">
          <input type="radio" name="modelKeyMode" value="default" id="keyModeDefault" style="margin-top: 2px;" onchange="updateModelKeyModalUI()">
          <div style="display: flex; flex-direction: column; gap: 2px;">
            <span style="font-weight: 500; color: var(--b-text);">继承厂商默认 API Key</span>
            <span id="modelKeyDefaultPreview" style="color: var(--b-text-3); font-size: 11px;"></span>
          </div>
        </label>

        <label class="modal-option-card" id="cardKeyModePool">
          <input type="radio" name="modelKeyMode" value="pool" id="keyModePool" style="margin-top: 2px;" onchange="updateModelKeyModalUI()">
          <div style="display: flex; flex-direction: column; gap: 6px; width: 100%;">
            <span style="font-weight: 500; color: var(--b-text);">绑定厂商密钥池中的 Key</span>
            <div id="modelKeyPoolWrap" style="display: none;">
              <select class="input" id="modelKeyPoolSelect" style="width: 100%; font-family: var(--b-mono); font-size: 11.5px;" onchange="updateModelKeyModalUI()">
                <option value="">(请选择密钥池中的 Key)</option>
              </select>
            </div>
          </div>
        </label>

        <label class="modal-option-card" id="cardKeyModeCustom">
          <input type="radio" name="modelKeyMode" value="custom" id="keyModeCustom" style="margin-top: 2px;" onchange="updateModelKeyModalUI()">
          <div style="display: flex; flex-direction: column; gap: 6px; width: 100%;">
            <span style="font-weight: 500; color: var(--b-text);">为此模型单独设置自定义 API Key</span>
            <div id="modelKeyCustomWrap" style="display: none;">
              <div class="input-wrapper">
                <input class="input" type="password" id="modelKeyCustomInput" placeholder="输入该模型的专属 API Key (如 sk-...)" style="font-family: var(--b-mono); font-size: 11.5px;">
                <span class="toggle-pwd" id="toggleModelKeyCustomBtn" onclick="toggleModelKeyCustomVisibility()" title="显隐 Key">👁️</span>
              </div>
              <div style="font-size: 10.5px; color: var(--b-text-3); margin-top: 4px;">
                保存时将以 <code style="color: var(--b-accent);">headers.Authorization = "Bearer ..."</code> 写入模型配置。
              </div>
            </div>
          </div>
        </label>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModelKeyModal()">取消</button>
      <button class="btn btn-primary" onclick="saveModelKeyModal()">✓ 应用此配置</button>
    </div>
  </div>
</div>

<!-- 全局统一 Confirm / Alert 消息弹窗 -->
<div class="modal-backdrop" id="globalDialogModal" onclick="handleGlobalDialogBackdrop(event)">
  <div class="modal-box" id="globalDialogBox" style="width: 440px;">
    <div class="modal-header">
      <span id="globalDialogTitle" style="display: flex; align-items: center; gap: 8px;">
        <span id="globalDialogIcon">⚠️</span>
        <span id="globalDialogTitleText">提示</span>
      </span>
      <button class="drawer-close" onclick="closeGlobalDialog(false)">✕</button>
    </div>
    <div class="modal-body" style="padding: 20px 18px;">
      <div id="globalDialogHint" class="modal-hint" style="display:none;"></div>
      <div id="globalDialogMessage" style="font-size: 12.5px; line-height: 1.65; color: var(--b-text); white-space: pre-wrap; word-break: break-word;"></div>
    </div>
    <div class="modal-footer" id="globalDialogFooter">
      <button class="btn btn-secondary" id="globalDialogCopyBtn" style="display:none; margin-right:auto;" onclick="handleDialogCopy()">📋 复制全文</button>
      <button class="btn btn-secondary" id="globalDialogAltBtn" style="display:none;" onclick="handleDialogAlt()">备选</button>
      <button class="btn btn-ghost" id="globalDialogCancelBtn" onclick="closeGlobalDialog(false)">取消</button>
      <button class="btn btn-emerald" id="globalDialogConfirmBtn" onclick="closeGlobalDialog(true)">确认</button>
    </div>
  </div>
</div>

<!-- 底部运行日志面板（可展开，完整显示报错） -->
<div class="log-panel" id="logPanel">
  <div class="log-panel-head" onclick="toggleLogPanel()" title="点击展开/收起完整日志">
    <span class="log-title">📜 运行日志 <span class="log-badge" id="logBadge">0</span></span>
    <span class="log-last" id="logLast">暂无日志</span>
    <span class="log-actions">
      <button class="log-btn" onclick="event.stopPropagation();copyLogPanel()" title="复制面板内全部日志">📋 复制</button>
      <button class="log-btn" onclick="event.stopPropagation();clearLogPanel()" title="清空面板与日志文件">🗑 清空</button>
      <button class="log-btn" onclick="event.stopPropagation();openLogFile()" title="用系统编辑器打开日志文件">📂 文件</button>
      <button class="log-btn" id="logToggleBtn" onclick="event.stopPropagation();toggleLogPanel()" title="展开/收起">▲ 展开</button>
    </span>
  </div>
  <div class="log-panel-body" id="logPanelBody"></div>
</div>

<footer>
  <div class="footer-left">
    <span id="statusMsg">● 就绪</span>
  </div>
  <div class="footer-right">
    <button class="footer-log-btn" id="footerLogBtn" onclick="toggleLogPanel()" title="展开底部完整日志（报错不再被截断）">📜 日志</button>
    <span id="defaultModelStatus" style="color: var(--b-amber);">★ 默认: 正在获取...</span>
    <span id="pathDisplay">📁 正在加载...</span>
    <span><kbd>Ctrl+S</kbd> 保存</span>
    <div class="window-resize-grip" onmousedown="initWindowResize(event, 'se')" title="按住拖拽调整窗口大小">
      <svg width="10" height="10" viewBox="0 0 10 10">
        <path d="M8 2L2 8M8 5L5 8M8 8L8 8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
      </svg>
    </div>
  </div>
</footer>

<!-- 8 方向全屏无边框拖拽缩放把手 -->
<div class="resize-handle rh-top" onmousedown="initWindowResize(event, 'n')"></div>
<div class="resize-handle rh-bottom" onmousedown="initWindowResize(event, 's')"></div>
<div class="resize-handle rh-left" onmousedown="initWindowResize(event, 'w')"></div>
<div class="resize-handle rh-right" onmousedown="initWindowResize(event, 'e')"></div>
<div class="resize-handle rh-top-left" onmousedown="initWindowResize(event, 'nw')"></div>
<div class="resize-handle rh-top-right" onmousedown="initWindowResize(event, 'ne')"></div>
<div class="resize-handle rh-bottom-left" onmousedown="initWindowResize(event, 'sw')"></div>
<div class="resize-handle rh-bottom-right" onmousedown="initWindowResize(event, 'se')"></div>

<script>
let currentConfig = { providers: {} };
let selectedPid = null;
let currentConfigPath = null;
let currentEditable = true;
let fetchedPreview = [];
let currentDefaultProvider = '';
let currentDefaultModel = '';
// 工具界面偏好（与 Pi 顺序语义相关，持久化在 ~/.pi/agent/model-manager-settings.json）
let uiPrefs = { pinDefaultModel: true, alignProviderOrder: false, patchPiOrder: false };
let isProviderEditing = false;
let isViewKeyMasked = true;
// 密钥池编辑区当前是为哪个服务商渲染的（null = 未渲染/已清空），
// 用于防止把空/脏数据回写到其他服务商
let keyPoolRenderedPid = null;

// ==========================================
// 全局统一 Modal 弹窗 Engine (替代原生 alert / confirm)
// ==========================================
let globalDialogResolver = null;
let globalDialogAltHandler = null;

// 长文本弹窗支持：等宽字体 / 独立滚动区 / 一键复制全文
let globalDialogCopyText = '';

// 错误信息一旦偏长（>160 字或带换行），自动升级为「等宽 + 宽版 + 可复制」日志视图，
// 避免关键报错被窄弹窗挤成看不全的碎片。
function wantsLogView(opts) {
  if (!opts || opts.html) return false;
  if (opts.mono) return true;
  const text = String(opts.message || opts.text || '');
  const isError = opts.type === 'error';
  return isError && (text.length > 160 || text.indexOf(String.fromCharCode(10)) >= 0);
}

function applyDialogBody(opts, msgEl) {
  const text = opts.message || opts.text || '';
  const copyText = opts.copyText || text;
  const monoView = !opts.html && wantsLogView(opts);
  if (!msgEl) return;
  if (opts.html) {
    msgEl.className = 'modal-html';
    msgEl.innerHTML = String(opts.html);
    msgEl.style.fontSize = '';
    msgEl.style.lineHeight = '';
    msgEl.style.whiteSpace = '';
    msgEl.style.wordBreak = '';
  } else if (monoView) {
    msgEl.innerText = text;
    msgEl.className = 'modal-report';
    msgEl.style.fontSize = '';
    msgEl.style.lineHeight = '';
    msgEl.style.whiteSpace = '';
    msgEl.style.wordBreak = '';
  } else {
    msgEl.innerText = text;
    msgEl.className = '';
    msgEl.style.fontSize = '12.5px';
    msgEl.style.lineHeight = '1.65';
    msgEl.style.whiteSpace = 'pre-wrap';
    msgEl.style.wordBreak = 'break-word';
  }

  const hintEl = $id('globalDialogHint');
  if (hintEl) {
    const lines = copyText ? copyText.split(String.fromCharCode(10)).length : 0;
    const auto = (monoView && lines > 12)
      ? ('共 ' + lines + ' 行，内容较长：可在框内上下滚动查看，或点左下角「📋 复制全文」整段复制。')
      : '';
    const hint = [auto, opts.hint].filter(Boolean).join('  ·  ');
    hintEl.innerText = hint || '';
    hintEl.style.display = hint ? 'block' : 'none';
  }

  globalDialogCopyText = copyText;
  const copyBtn = $id('globalDialogCopyBtn');
  if (copyBtn) {
    if ((opts.allowCopy || monoView) && copyText) {
      copyBtn.style.display = 'inline-flex';
      copyBtn.innerText = '📋 复制全文';
    } else {
      copyBtn.style.display = 'none';
    }
  }
}

function handleDialogCopy() {
  const btn = $id('globalDialogCopyBtn');
  const text = globalDialogCopyText || '';
  let done = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text);
      done = true;
    }
  } catch (e) { done = false; }
  if (!done) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done = true;
    } catch (e) { done = false; }
  }
  if (btn) {
    btn.innerText = done ? '✅ 已复制全文' : '⚠️ 复制失败，请手动选择文本';
    setTimeout(function () { if (btn) btn.innerText = '📋 复制全文'; }, 1800);
  }
}

function handleDialogAlt() {
  const handler = globalDialogAltHandler;
  globalDialogAltHandler = null;
  closeGlobalDialog(false);
  if (typeof handler === 'function') {
    try { handler(); } catch (e) { console.error(e); }
  }
}

function showConfirm(options) {
  return new Promise((resolve) => {
    const opts = typeof options === 'string' ? { message: options } : (options || {});
    globalDialogResolver = resolve;

    const modal = $id('globalDialogModal');
    const iconEl = $id('globalDialogIcon');
    const titleEl = $id('globalDialogTitleText');
    const msgEl = $id('globalDialogMessage');
    const cancelBtn = $id('globalDialogCancelBtn');
    const confirmBtn = $id('globalDialogConfirmBtn');

    if (iconEl) iconEl.innerText = opts.icon || (opts.danger ? '🗑️' : '⚠️');
    if (titleEl) titleEl.innerText = opts.title || '确认提示';
    applyDialogBody(opts, msgEl);
    const box = $id('globalDialogBox');
    if (box) box.style.width = (opts.wide || wantsLogView(opts)) ? '860px' : '440px';

    if (cancelBtn) {
      cancelBtn.style.display = 'inline-flex';
      cancelBtn.innerText = opts.cancelText || '取消';
    }

    if (confirmBtn) {
      confirmBtn.innerText = opts.confirmText || '确认';
      confirmBtn.className = 'btn ' + (opts.confirmClass || (opts.danger ? 'btn-rose' : 'btn-emerald'));
    }

    // 可选第三个按钮（仅 showConfirm 支持；showAlert 会隐藏它）
    const altBtn = $id('globalDialogAltBtn');
    globalDialogAltHandler = typeof opts.onAlt === 'function' ? opts.onAlt : null;
    if (altBtn) {
      if (opts.altText) {
        altBtn.style.display = 'inline-flex';
        altBtn.innerText = opts.altText;
        altBtn.className = 'btn ' + (opts.altClass || 'btn-secondary');
      } else {
        altBtn.style.display = 'none';
      }
    }

    if (modal) {
      modal.classList.add('open');
      setTimeout(() => confirmBtn && confirmBtn.focus(), 50);
    }
  });
}

function showAlert(options) {
  return new Promise((resolve) => {
    const opts = typeof options === 'string' ? { message: options } : (options || {});
    globalDialogResolver = () => resolve();

    const modal = $id('globalDialogModal');
    const iconEl = $id('globalDialogIcon');
    const titleEl = $id('globalDialogTitleText');
    const msgEl = $id('globalDialogMessage');
    const cancelBtn = $id('globalDialogCancelBtn');
    const confirmBtn = $id('globalDialogConfirmBtn');

    if (iconEl) iconEl.innerText = opts.icon || (opts.type === 'error' ? '❌' : opts.type === 'success' ? '✅' : 'ℹ️');
    if (titleEl) titleEl.innerText = opts.title || (opts.type === 'error' ? '错误' : opts.type === 'success' ? '成功' : '提示');
    const box = $id('globalDialogBox');
    if (box) box.style.width = (opts.wide || wantsLogView(opts)) ? '860px' : '440px';
    applyDialogBody(opts, msgEl);

    if (cancelBtn) cancelBtn.style.display = 'none';

    const altBtn = $id('globalDialogAltBtn');
    globalDialogAltHandler = null;
    if (altBtn) altBtn.style.display = 'none';

    if (confirmBtn) {
      confirmBtn.innerText = opts.okText || '确定';
      confirmBtn.className = 'btn ' + (opts.type === 'error' ? 'btn-rose' : 'btn-primary');
    }

    if (modal) {
      modal.classList.add('open');
      setTimeout(() => confirmBtn && confirmBtn.focus(), 50);
    }
  });
}

function closeGlobalDialog(result) {
  const modal = $id('globalDialogModal');
  if (modal) modal.classList.remove('open');
  if (globalDialogResolver) {
    const res = globalDialogResolver;
    globalDialogResolver = null;
    res(Boolean(result));
  }
}

function handleGlobalDialogBackdrop(e) {
  if (e.target && e.target.id === 'globalDialogModal') {
    closeGlobalDialog(false);
  }
}

// 拦截全局 window.alert
window.alert = function(msg) {
  showAlert({ message: String(msg) });
};

// 快捷键拦截 (Esc 键关闭弹窗/抽屉，Enter 键确认弹窗)
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const dialogModal = $id('globalDialogModal');
    if (dialogModal && dialogModal.classList.contains('open')) {
      closeGlobalDialog(false);
      return;
    }
    const fetchModal = $id('fetchKeyModal');
    if (fetchModal && fetchModal.classList.contains('open')) {
      closeFetchKeyModal();
      return;
    }
    const modelModal = $id('modelKeyModal');
    if (modelModal && modelModal.classList.contains('open')) {
      closeModelKeyModal();
      return;
    }
    closeDrawer();
  } else if (e.key === 'Enter') {
    const dialogModal = $id('globalDialogModal');
    if (dialogModal && dialogModal.classList.contains('open')) {
      e.preventDefault();
      closeGlobalDialog(true);
      return;
    }
  }
});

function $id(id) { return document.getElementById(id); }

function el(tag, cls, text) {
  const elem = document.createElement(tag);
  if (cls) elem.className = cls;
  if (text !== undefined) elem.innerText = text;
  return elem;
}

function emptyState(title, sub) {
  const wrap = el('div', 'empty-wrap');
  wrap.style.cssText = 'color:var(--b-text-3); font-size:12px; text-align:center; padding:30px 10px; line-height:1.6;';
  wrap.appendChild(el('div', '', title));
  if (sub) {
    const s = el('div', '', sub);
    s.style.cssText = 'color:var(--b-text-4); font-size:11px; margin-top:4px;';
    wrap.appendChild(s);
  }
  return wrap;
}

// Drawer Controls
function openDrawer(pid) {
  const drawer = $id('providerDrawer');
  if (drawer) drawer.classList.add('open');
  if (pid) {
    const titleEl = $id('drawerTitle');
    const p = (currentConfig.providers || {})[pid] || {};
    if (titleEl) titleEl.textContent = '🛠️ ' + (p.name || pid);
  }
}

function closeDrawer() {
  const drawer = $id('providerDrawer');
  if (drawer) drawer.classList.remove('open');
}

// Default Model Sync
async function refreshDefaultModel() {
  try {
    const res = await window.pywebview.api.get_default_model(currentConfigPath);
    if (res && res.success) {
      currentDefaultProvider = res.defaultProvider || '';
      currentDefaultModel = res.defaultModel || '';
      const statusEl = $id('defaultModelStatus');
      if (statusEl) {
        statusEl.textContent = currentDefaultModel
          ? `★ 默认: ${currentDefaultProvider}/${currentDefaultModel}`
          : '☆ 默认: 未设置';
      }
    }
  } catch (e) {}
}

async function setDefaultModel(providerId, modelId) {
  try {
    const isDsh = currentConfigPath && (currentConfigPath.includes('cordis.patch.yml') || currentConfigPath.includes('.dsh'));
    const targetLabel = isDsh ? 'DSH' : 'Pi';
    setStatus(`正在将 [${modelId}] 设为 ${targetLabel} 默认模型...`, '#F59E0B');
    const res = await window.pywebview.api.set_default_model(providerId, modelId, currentConfigPath);
    if (res && res.success) {
      currentDefaultProvider = providerId;
      currentDefaultModel = modelId;
      const statusEl = $id('defaultModelStatus');
      if (statusEl) statusEl.textContent = `★ 默认: ${providerId}/${modelId}`;
      setStatus(`★ 已成功将 [${modelId}] 设为 ${targetLabel} 默认模型`, '#10B981');
      const p = (currentConfig.providers || {})[providerId];
      if (p) renderModels(p.models || []);
    } else {
      setStatus(`设置默认模型失败: ${res ? res.error : '未知错误'}`, '#EF4444');
    }
  } catch (e) {
    setStatus(`设置默认模型异常: ${e}`, '#EF4444');
  }
}

// ==========================================
// 顺序对齐（工具 ↔ Pi）
// ------------------------------------------
// Pi 侧一共存在三种顺序，工具只能控制「厂商内部的模型顺序」：
//   1. 厂商分组顺序：/model 选择器与 `pi --list-models` 都按厂商 ID 的字母序分组
//      （Pi 源码硬编码 localeCompare），models.json 的键顺序对 Pi 无效。
//   2. 厂商内部的模型顺序：自定义厂商 = models.json 数组顺序（工具拖拽生效，持久化后
//      重启 Pi 即按此顺序显示）；内置厂商 = Pi 原生目录顺序（目录内模型锁定拖拽）。
//   3. 默认/当前模型置顶：/model 选择器把「当前模型」放第 1、「默认模型」放第 2。
// 两个开关都是「显示层偏好」，不会改动 models.json 的保存顺序。
async function loadUiPrefs() {
  try {
    const res = await window.pywebview.api.get_tool_prefs();
    if (res && res.success && res.prefs) uiPrefs = Object.assign(uiPrefs, res.prefs);
  } catch (e) {}
  syncOrderToggleButtons();
  updateOrderPatchBtn();
}

// ==========================================
// Pi 排序补丁（让 Pi 的厂商分组顺序跟随工具）
// ==========================================
async function updateOrderPatchBtn(status) {
  const btn = $id('orderPatchBtn');
  if (!btn) return;
  try {
    let st = status || null;
    if (!st) {
      const res = await window.pywebview.api.get_order_patch_status();
      st = (res && res.success && res.status) || {};
    }
    const on = st.patchedCount > 0 && st.patchedCount === st.targetCount;
    btn.classList.toggle('is-on', !!on);
    btn.title = on
      ? '已打补丁：Pi 按工具的厂商顺序分组' + (st.versionChanged ? '（Pi 已升级，需重打）' : '')
      : '点击进入：让 Pi 的厂商分组顺序跟随工具（可一键还原原版）';
  } catch (e) { /* 忽略 */ }
}

function buildPatchStatusText(st) {
  const lines = [];
  lines.push('目标：让 Pi 的 /model 选择器与 `pi --list-models` 按「工具里的厂商顺序」分组。');
  lines.push('原理：把 Pi 硬编码的 `a.provider.localeCompare(b.provider)` 换成读取顺序文件的排名比较；');
  lines.push('      顺序文件由工具在每次保存后自动刷新（就是窗口里看到的厂商顺序）。');
  lines.push('');
  lines.push('── 当前状态 ──');
  lines.push('· 自动维护（保存后 / Pi 升级后自动重打）: ' + (st.enabled ? '✅ 已开启' : '⭕ 未开启'));
  lines.push('· Pi 安装目录: ' + (st.packageDir || '（未找到）'));
  lines.push('· Pi 版本: ' + (st.piVersion || '未知') + (st.stateVersion ? '（补丁记录版本 ' + st.stateVersion + '）' : ''));
  lines.push('· 补丁文件: ' + (st.patchedCount || 0) + '/' + (st.targetCount || 0) + (st.fullyPatched ? '  ✅ 已完整打入' : '  ⚠️ 未完整'));
  (st.targets || []).forEach(function (t) { lines.push('    ' + (t.patched ? '✅' : '⬜') + ' ' + t.rel); });
  if (st.versionChanged) lines.push('· ⚠️ Pi 已升级，锚点可能变化 → 点「打补丁 / 重打」即可自动重打');
  lines.push('· 顺序文件: ' + (st.orderFilePath || ''));
  lines.push('  当前顺序（' + ((st.order || []).length) + ' 项）: ' + ((st.order || []).join(' → ') || '（空，将回退为 Pi 原生字母序）'));
  lines.push('· 原版备份: ' + (st.backupDir || ''));
  if (st.error) lines.push('· ⚠️ ' + st.error);
  lines.push('');
  lines.push('── 说明 ──');
  lines.push('· 补丁可随时一键还原（还原后 Pi 恢复原生字母序，工具侧“⇅ Pi 字母序”与之对应）。');
  lines.push('· Pi 通过 npm 升级会覆盖 dist 目录，但自动维护会在工具保存时或启动时重新打入。');
  return lines.join('\\n');
}

async function showPiOrderPatchDialog() {
  setStatus('正在读取 Pi 排序补丁状态...', '#F59E0B');
  let st = {};
  try {
    const res = await window.pywebview.api.get_order_patch_status();
    if (!res || !res.success) {
      const msg = (res && res.error) || '未知错误';
      setStatus('补丁状态读取失败: ' + msg, '#EF4444');
      await showAlert({ title: 'Pi 顺序补丁', icon: '❌', type: 'error', message: msg, wide: true });
      return;
    }
    st = res.status || {};
  } catch (e) {
    setStatus('补丁状态读取异常: ' + e, '#EF4444');
    return;
  }
  setStatus('Pi 排序补丁: ' + (st.patchedCount || 0) + '/' + (st.targetCount || 0) + ' 个文件已打', '#10B981');
  const ok = await showConfirm({
    title: '🧩 Pi 顺序补丁（厂商分组顺序跟随工具）',
    icon: '🧩',
    wide: true,
    mono: true,
    allowCopy: true,
    message: buildPatchStatusText(st),
    confirmText: st.fullyPatched ? '🔁 重新打补丁' : '🧩 打补丁 / 立即生效',
    confirmClass: 'btn-purple',
    cancelText: '关闭',
    altText: st.patchedCount > 0 ? '🩹 还原原版' : null,
    altClass: 'btn-secondary',
    onAlt: async function () {
      setStatus('正在还原 Pi 原版排序逻辑...', '#F59E0B');
      try {
        const r = await window.pywebview.api.revert_order_patch();
        const rr = (r && r.result) || {};
        if (r && r.success) {
          uiPrefs.patchPiOrder = false;
          setStatus('已还原 Pi 原版排序逻辑（工具自动维护已关闭）', '#10B981');
          await showAlert({ title: '已还原原版', icon: '🩹', type: 'success', wide: true,
            message: '已还原 ' + (((rr.reverted) || []).length) + ' 个文件，Pi 恢复原生字母序分组。\\n\\n' +
              '· 原版备份仍保留在: ' + ((r.status && r.status.backupDir) || '') + '\\n' +
              '· 随时可再次点「🧩 顺序补丁」重新打入' });
        } else {
          setStatus('还原失败: ' + ((r && r.error) || '未知错误'), '#EF4444');
          await showAlert({ title: '还原失败', icon: '❌', type: 'error', wide: true,
            message: ((r && r.error) || '未知错误') + '\\n\\n' + JSON.stringify(rr.failed || [], null, 2) });
        }
      } catch (e) {
        setStatus('还原异常: ' + e, '#EF4444');
      }
      updateOrderPatchBtn();
    }
  });
  if (!ok) { updateOrderPatchBtn(); return; }
  setStatus('正在给 Pi 打入排序补丁...', '#F59E0B');
  try {
    const r = await window.pywebview.api.apply_order_patch();
    const rr = (r && r.result) || {};
    updateOrderPatchBtn();
    if (r && r.success) {
      uiPrefs.patchPiOrder = true;
      setStatus('✅ Pi 排序补丁已生效（已开启自动维护）', '#10B981');
      await showAlert({ title: '补丁已生效', icon: '✅', type: 'success', wide: true,
        message: '✅ 已给 Pi 打入顺序补丁，并开启自动维护。\\n\\n' +
          '· 已打文件: ' + (((rr.applied) || []).length) + ' 个（跳过已打 ' + (((rr.skipped) || []).length) + ' 个）\\n' +
          '· 替换锚点: ' + (rr.replacements || 0) + ' 处\\n' +
          '· 厂商顺序: ' + (((rr.order) || []).join(' → ') || '（空）') + '\\n\\n' +
          '生效方式：重启 Pi（或重开 /model 选择器）后，Pi 的厂商分组顺序即为工具里的顺序。' });
    } else {
      setStatus('补丁失败: ' + ((r && r.error) || '未知错误'), '#EF4444');
      await showAlert({ title: '补丁未生效', icon: '❌', type: 'error', wide: true,
        message: ((r && r.error) || '未知错误') + '\\n\\n失败明细:\\n' + JSON.stringify(rr.failed || [], null, 2) });
    }
  } catch (e) {
    setStatus('补丁异常: ' + e, '#EF4444');
  }
}

function syncOrderToggleButtons() {
  const pinBtn = $id('pinDefaultToggle');
  if (pinBtn) {
    pinBtn.classList.toggle('is-on', !!uiPrefs.pinDefaultModel);
    pinBtn.title = uiPrefs.pinDefaultModel
      ? '已开启：Pi 默认模型在该厂商列表顶部置顶显示（仅显示层，不改动保存顺序）'
      : '点击开启：把 Pi 默认模型置顶显示，与 Pi 的 /model 选择器观感一致';
  }
  const orderBtn = $id('providerOrderToggle');
  if (orderBtn) {
    orderBtn.classList.toggle('is-on', !!uiPrefs.alignProviderOrder);
    orderBtn.title = uiPrefs.alignProviderOrder
      ? '已开启：厂商按 Pi 的字母序显示（Pi 侧固定按厂商名排序，不可配置）'
      : '点击开启：厂商按 Pi 的字母序显示（当前为自定义拖拽顺序，仅影响工具视图）';
  }
}

async function toggleUiPref(name) {
  uiPrefs[name] = !uiPrefs[name];
  syncOrderToggleButtons();
  try { await window.pywebview.api.set_tool_pref(name, uiPrefs[name]); } catch (e) {}
  renderSidebar();
  if (selectedPid && currentConfig.providers[selectedPid]) {
    renderModels(currentConfig.providers[selectedPid].models || []);
  }
  if (name === 'pinDefaultModel') {
    setStatus(uiPrefs.pinDefaultModel
      ? '⭐ 已开启默认模型置顶显示（仅显示层，保存顺序不变）'
      : '已关闭默认模型置顶显示（列表 = 保存顺序）', '#10B981');
  } else if (name === 'alignProviderOrder') {
    setStatus(uiPrefs.alignProviderOrder
      ? '⇅ 厂商列已切换为 Pi 的字母序（Pi 侧固定按厂商名分组）'
      : '厂商列已恢复为自定义拖拽顺序', '#10B981');
    // 顺序文件与窗口显示保持一致（开启字母序时，Pi 侧补丁也跟随字母序）
    await refreshOrderFileIfPatched();
  } else {
    setStatus(name + ' 已更新为 ' + (uiPrefs[name] ? '开启' : '关闭'), '#10B981');
  }
}

// 仅在「补丁正在生效」时刷新顺序文件，避免无谓写盘
async function refreshOrderFileIfPatched() {
  try {
    const res = await window.pywebview.api.get_order_patch_status();
    if (res && res.success && res.status && res.status.patchedCount > 0) {
      await window.pywebview.api.ensure_order_patch();
    }
  } catch (e) { /* 忽略 */ }
}

async function showErrorLog() {
  let res = null;
  try {
    res = await window.pywebview.api.get_error_log(600);
  } catch (e) {
    res = { success: false, error: String(e) };
  }
  const NL = String.fromCharCode(10);
  const path = (res && res.path) || '';
  const text = (res && res.text) || '';
  const body = (res && res.success === false)
    ? ('读取日志失败: ' + (res.error || '未知错误') + (path ? NL + path : ''))
    : (text || '（日志为空：目前没有记录到错误）');
  await showConfirm({
    title: '运行日志 / 错误回溯',
    icon: '📜',
    message: body,
    mono: true,
    wide: true,
    allowCopy: true,
    okText: '关闭',
    altText: '📂 打开日志文件',
    onAlt: () => { try { window.pywebview.api.open_log_file(); } catch (e) {} },
    hint: (path ? '日志文件: ' + path + '  ·  ' : '')
      + '最新记录在最后 · 共 ' + ((res && res.totalLines) || 0) + ' 行 · 上限 1.5MB 自动轮转'
  });
}

async function showOrderReport() {  setStatus('正在核对「工具顺序 ↔ Pi 顺序」...', '#F59E0B');
  try {
    const alpha = !!(uiPrefs && uiPrefs.alignProviderOrder);
    const res = await window.pywebview.api.get_order_compare(alpha);
    if (!res || !res.success) {
      setStatus('顺序自检失败: ' + ((res && res.error) || '未知错误'), '#EF4444');
      await showAlert({ title: '顺序自检失败', icon: '❌', type: 'error', message: (res && res.error) || '未知错误' });
      return;
    }
    setStatus('顺序自检完成', '#10B981');
    await showAlert({
      title: res.title || '顺序自检 · 工具 ↔ Pi',
      icon: '🔍',
      html: res.html,
      copyText: res.reportText || '',
      wide: true,
      allowCopy: true,
      hint: res.hint || '',
      okText: '关闭'
    });
  } catch (e) {
    setStatus('顺序自检异常: ' + e, '#EF4444');
  }
}

// Rescan Built-ins
async function rescanBuiltins() {
  setStatus('正在扫描 Pi 内置服务商与模型目录...', '#F59E0B');
  try {
    const res = await window.pywebview.api.rescan_builtins();
    if (res && res.success && res.builtins) {
      const bKeys = Object.keys(res.builtins);
      let addedModels = 0;
      for (const [bPid, bProv] of Object.entries(res.builtins)) {
        const existing = currentConfig.providers[bPid];
        if (!existing) {
          currentConfig.providers[bPid] = bProv;
          addedModels += (bProv.models || []).length;
          continue;
        }
        existing._isBuiltin = true;
        if (!existing.apiKey && bProv.apiKey) existing.apiKey = bProv.apiKey;
        const byId = {};
        (existing.models || []).forEach(m => { byId[m.id] = m; });
        const merged = [];
        (bProv.models || []).forEach(bm => {
          if (byId[bm.id]) {
            const mergedModel = Object.assign({}, bm, byId[bm.id]);
            mergedModel._isBuiltinModel = true;
            merged.push(mergedModel);
            delete byId[bm.id];
          } else {
            const copy = Object.assign({}, bm);
            copy._isBuiltinModel = true;
            merged.push(copy);
            addedModels++;
          }
        });
        Object.keys(byId).forEach(mid => merged.push(byId[mid]));
        existing.models = merged;
      }
      renderSidebar();
      if (selectedPid && currentConfig.providers[selectedPid]) {
        renderModels(currentConfig.providers[selectedPid].models || []);
      }
      setStatus(`✅ 已同步 ${bKeys.length} 个内置服务商，新增 ${addedModels} 个模型 (记得点 💾 保存)`, '#10B981');
    } else {
      setStatus('扫描内置服务商未返回数据', '#F59E0B');
    }
  } catch (e) {
    setStatus('扫描内置服务商失败: ' + e, '#EF4444');
  }
}

// Render Sidebar (.p-row)
function renderSidebar() {
  const container = $id('providerList');
  if (!container) return;
  container.innerHTML = '';

  const activeEntries = Object.entries(currentConfig.providers || {});

  const countChip = $id('sidebarCount');
  if (countChip) countChip.innerText = String(activeEntries.length);

  const searchInput = $id('providerSearch');
  const query = searchInput ? searchInput.value.trim().toLowerCase() : '';
  
  let pids = activeEntries.map(([pid]) => pid);
  // 开关开启时：厂商按 Pi 的字母序显示（Pi 侧固定按厂商 ID localeCompare 排序）
  if (uiPrefs.alignProviderOrder) {
    pids = pids.slice().sort((a, b) => String(a).localeCompare(String(b)));
  }
  if (query) {
    pids = pids.filter(pid => {
      const p = currentConfig.providers[pid] || {};
      return pid.toLowerCase().includes(query) || (p.name && p.name.toLowerCase().includes(query));
    });
  }

  if (pids.length === 0 && activeEntries.length === 0) {
    container.appendChild(emptyState(query ? '未找到匹配服务商' : '暂无服务商配置', query ? '' : '点击右上角《➕ 新建》添加'));
    return;
  }

  pids.forEach((pid) => {
    const p = currentConfig.providers[pid] || {};
    const count = (p.models || []).length;
    const isActive = pid === selectedPid;
    const row = el('div', 'p-row' + (isActive ? ' active' : ''));
    row.dataset.pid = pid;

    row.addEventListener('dragstart', (e) => {
      if (row.draggable !== true) { e.preventDefault(); return; }
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', pid);
      row.classList.add('dragging');
      document.body.classList.add('dragging-cursor');
    });
    row.addEventListener('dragend', () => {
      row.draggable = false;
      row.classList.remove('dragging');
      document.body.classList.remove('dragging-cursor');
      document.querySelectorAll('.p-row').forEach(r => r.classList.remove('drag-over'));
    });
    row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('drag-over'); });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', async (e) => {
      e.preventDefault();
      row.classList.remove('drag-over');
      const srcPid = e.dataTransfer.getData('text/plain');
      if (srcPid && srcPid !== pid) await moveProviderToTarget(srcPid, pid);
    });

    row.onclick = () => selectProvider(pid);
    row.ondblclick = () => { selectProvider(pid); openDrawer(pid); toggleProviderEditMode(true); };

    const handle = el('span', 'p-row-handle', '≡');
    if (uiPrefs.alignProviderOrder) {
      // Pi 侧厂商分组顺序固定为字母序，此时拖拽不会对 Pi 产生任何影响
      handle.title = '已开启「⇅ Pi 字母序」：Pi 侧固定按厂商名排序，拖拽不会影响 Pi（可点侧栏标题的开关关闭）';
      handle.style.opacity = '0.3';
      handle.style.cursor = 'not-allowed';
    } else {
      handle.title = '按住拖拽调整服务商顺序（仅影响工具视图：Pi 侧始终按厂商名分组）';
      handle.addEventListener('pointerdown', () => { row.draggable = true; });
    }

    const dotClass = p.api === 'anthropic-messages' ? 'proto-claude-dot'
      : p.api === 'google-generative-ai' ? 'proto-gemini-dot'
      : p.api === 'openai-responses' ? 'proto-resp-dot'
      : 'proto-openai-dot';
    const dot = el('span', 'p-row-dot ' + dotClass);
    dot.title = p.api || 'openai-completions';

    const nameEl = el('span', 'p-row-name', p.name || pid);
    nameEl.title = (p.name || pid) + (p.name ? ' (' + pid + ')' : '');

    const badges = el('span', 'p-row-badges');
    if (p._isBuiltin) {
      const bBadge = el('span', 'p-proto proto-builtin', p._dshBuiltin ? 'DSH内置' : '内置');
      bBadge.title = p._dshBuiltin
        ? 'DSH base 层自带路由（' + (p._dshEntryId || '') + '），不在 pi-ai 配置里：只能禁用，不能删除'
        : 'Pi 原生内置服务商';
      badges.appendChild(bBadge);
      if (p._dshDisabled) {
        const dBadge = el('span', 'p-proto', '已禁用');
        dBadge.style.cssText = 'background:rgba(239,68,68,.15); color:#FCA5A5; border:1px solid rgba(239,68,68,.35);';
        dBadge.title = '已在 cordis.patch.yml 写 disabled: true，重启 DSH 后从模型列表消失';
        badges.appendChild(dBadge);
      }
    }
    badges.appendChild(el('span', 'p-count', String(count)));

    const actions = el('span', 'p-row-actions');
    const editBtn = el('button', 'p-row-btn', '⚙️');
    editBtn.title = '打开服务商连接抽屉';
    editBtn.onclick = (e) => { e.stopPropagation(); selectProvider(pid); openDrawer(pid); };

    // 内置厂商由 Pi 原生目录提供，不可删除也不可隐藏 -> 不提供删除按钮
    if (p._isBuiltin) {
      actions.append(editBtn);
    } else {
      const delBtn = el('button', 'p-row-btn del', '🗑️');
      delBtn.title = '删除服务商 [' + (p.name || pid) + ']';
      delBtn.onclick = (e) => { e.stopPropagation(); deleteProvider(pid); };
      actions.append(editBtn, delBtn);
    }

    row.append(handle, dot, nameEl, badges, actions);
    container.appendChild(row);
  });
}

async function moveProviderToTarget(srcPid, targetPid) {
  const providers = currentConfig.providers || {};
  const keys = Object.keys(providers);
  const fromIdx = keys.indexOf(srcPid);
  const toIdx = keys.indexOf(targetPid);
  if (fromIdx < 0 || toIdx < 0 || fromIdx === toIdx) return;
  const newKeys = [...keys];
  const [removed] = newKeys.splice(fromIdx, 1);
  newKeys.splice(toIdx, 0, removed);
  const reordered = {};
  for (const k of newKeys) reordered[k] = providers[k];
  currentConfig.providers = reordered;
  renderSidebar();
  await saveAll();
}

async function deleteProvider(pid) {
  const p = currentConfig.providers[pid];
  if (!p) return;

  if (p._dshBuiltin) {
    await toggleDshBuiltin(pid);
    return;
  }

  if (p._isBuiltin) {
    // 内置厂商的模型目录由 Pi 自身提供，工具无法删除或隐藏它
    showAlert('无法删除', `[${pid}] 是 Pi 原生内置服务商，其模型目录由 Pi 自身提供，无法从工具中删除或隐藏。\\n\\n如需调整连接参数，请在右侧抽屉中编辑。`);
    return;
  }

  const ok = await showConfirm({
    title: '删除服务商',
    icon: '🗑️',
    danger: true,
    message: `确定删除服务商 [${p.name || pid}] 吗？`,
    confirmText: '确定删除'
  });
  if (!ok) return;
  delete currentConfig.providers[pid];
  if (selectedPid === pid) {
    const keys = Object.keys(currentConfig.providers);
    if (keys.length > 0) selectProvider(keys[0]);
    else newProvider();
  }
  renderSidebar();
  saveAll();
  setStatus(`🗑️ 已删除服务商 [${pid}]`, '#EF4444');
}


/**
 * DSH 内置路由（deepseek-official / deepseek-account）既不在 pi-ai providers 里，
 * 也没有可删的配置节点；能做的只有给对应 base 插件条目写 disabled 补丁。
 */
async function toggleDshBuiltin(pid) {
  const p = (currentConfig.providers || {})[pid];
  if (!p || !p._dshBuiltin) return;
  const entryId = p._dshEntryId || '';
  const label = p.name || pid;
  const disabling = !p._dshDisabled;

  const isDefaultProv = (currentDefaultProvider || '') === pid;
  const extra = [];
  if (entryId === 'llm-deepseek-account') {
    extra.push('⚠️ 这是「DeepSeek 账号」登录路由，禁用后已登录账号的模型会一并消失。');
  }
  if (isDefaultProv) {
    extra.push(`⚠️ 当前默认模型就指向 ${pid}，禁用后需先把默认模型改到其他服务商。`);
  }
  extra.push('改动写入 cordis.patch.yml 的 disabled 补丁，**重启 DSH 后生效**（本工具不会替你重启）。');

  const ok = await showConfirm({
    icon: disabling ? '⏸️' : '▶️',
    title: (disabling ? '禁用' : '恢复') + ' DSH 内置服务商',
    message: `[${label}] (${pid}) 是 DSH base 层自带路由，不在 pi-ai 配置里，无法从配置中删除。\\n\\n`
      + (disabling
          ? `可以给插件条目 [${entryId}] 打上 disabled: true，DSH 下次启动就不再挂载它。`
          : `将移除 [${entryId}] 上的 disabled 补丁，DSH 下次启动恢复该路由。`)
      + '\\n\\n' + extra.join('\\n'),
    confirmText: disabling ? '禁用' : '恢复',
    confirmClass: disabling ? 'btn-rose' : 'btn-emerald'
  });
  if (!ok) return;

  const res = await window.pywebview.api.set_dsh_builtin_disabled(entryId, disabling);
  if (!res || !res.success) {
    setStatus(`操作失败: ${(res && res.error) || '未知错误'}`, '#EF4444');
    return;
  }
  p._dshDisabled = disabling;
  renderSidebar();
  updateProviderViewPanel(pid);
  setStatus(disabling
    ? `⏸️ 已禁用内置路由 [${label}]（${entryId}）。重启 DSH 后生效。`
    : `▶️ 已恢复内置路由 [${label}]（${entryId}）。重启 DSH 后生效。`, disabling ? '#F59E0B' : '#10B981');
}


// Select Provider
function selectProvider(pid) {
  selectedPid = pid;
  if (window.pywebview && window.pywebview.api && window.pywebview.api.set_selected_provider) {
    window.pywebview.api.set_selected_provider(pid).catch(() => {});
  }
  fetchedPreview = [];
  renderSidebar();
  const p = (currentConfig.providers || {})[pid] || {};
  if ($id('pId')) $id('pId').value = pid || '';
  if ($id('pName')) $id('pName').value = p.name || '';
  if ($id('pBaseUrl')) $id('pBaseUrl').value = p.baseUrl || '';
  if ($id('pApiKey')) $id('pApiKey').value = p.apiKey || (p.apiKeys && p.apiKeys[0] ? p.apiKeys[0].key : '') || '';
  if ($id('pApi')) $id('pApi').value = p.api || 'openai-completions';

  updateProviderViewPanel(pid);
  toggleProviderEditMode(false);
  renderModels(p.models || []);
}

function toggleEditApiKeyVisibility() {
  const inp = $id('pApiKey');
  const btn = $id('toggleEditApiKeyBtn');
  if (!inp) return;
  if (inp.type === 'password') {
    inp.type = 'text';
    if (btn) btn.textContent = '🙈';
  } else {
    inp.type = 'password';
    if (btn) btn.textContent = '👁️';
  }
}

// Provider View & Edit Panels
function toggleViewKeyMask() {
  isViewKeyMasked = !isViewKeyMasked;
  const btn = $id('viewMaskBtn');
  if (btn) btn.textContent = isViewKeyMasked ? '👁️' : '🙈';
  if (selectedPid) updateProviderViewPanel(selectedPid);
}

function updateProviderViewPanel(pid) {
  const p = (pid && (currentConfig.providers || {})[pid]) ? currentConfig.providers[pid] : null;
  const vId = $id('vId');
  const vName = $id('vName');
  const vBaseUrl = $id('vBaseUrl');
  const vApiKey = $id('vApiKey');
  const vKeyPoolSummary = $id('vKeyPoolSummary');
  const vApi = $id('vApi');

  if (!p) {
    if (vId) vId.innerText = '-';
    if (vName) { vName.innerText = '未选择服务商'; vName.className = 'view-value empty'; }
    if (vBaseUrl) { vBaseUrl.innerText = '-'; vBaseUrl.className = 'view-value empty'; }
    if (vApiKey) { vApiKey.innerText = '-'; vApiKey.className = 'view-value empty'; }
    if (vKeyPoolSummary) vKeyPoolSummary.innerHTML = '未配置';
    if (vApi) { vApi.innerText = '-'; vApi.className = 'view-value empty'; }
    return;
  }

  if (vId) { vId.innerText = pid; vId.className = 'view-value'; }
  if (vName) {
    vName.innerText = p.name || '（未设置别名）';
    vName.className = p.name ? 'view-value' : 'view-value empty';
  }
  if (vBaseUrl) {
    vBaseUrl.innerText = p.baseUrl || '（未配置 Base URL）';
    vBaseUrl.className = p.baseUrl ? 'view-value' : 'view-value empty';
  }
  if (vApiKey) {
    if (!p.apiKey) {
      vApiKey.innerText = '（无需或未填写 Key）';
      vApiKey.className = 'view-value empty';
    } else if (isViewKeyMasked) {
      const raw = p.apiKey;
      vApiKey.innerText = raw.length > 8 ? raw.slice(0, 4) + '••••••••' + raw.slice(-3) : '••••••••';
      vApiKey.className = 'view-value';
    } else {
      vApiKey.innerText = p.apiKey;
      vApiKey.className = 'view-value';
    }
  }

  if (vKeyPoolSummary) {
    if (p._dshBuiltin) {
      const stateBadge = p._dshDisabled
        ? '<span style="color:#FCA5A5;">已禁用（待重启生效）</span>'
        : '<span style="color:#6EE7B7;">启用中</span>';
      vKeyPoolSummary.innerHTML = `DSH 内置路由：<b>${escapeHtml(p._dshEntryId || '')}</b>`
        + `<br>插件：${escapeHtml(p._dshPlugin || '')}`
        + `<br>凭据引用：${escapeHtml(p._dshKeyRef || '（无，走账号登录）')}`
        + `<br>状态：${stateBadge}`
        + `<br><span style="opacity:.7;">${escapeHtml(p._dshNote || '')}</span>`;
    } else {
      const pool = p.apiKeys || [];
      if (!pool || pool.length === 0) {
        vKeyPoolSummary.innerHTML = '<span style="color:var(--b-text-4); font-style:italic;">未配置密钥池</span>';
      } else {
        const badges = pool.map(k => {
          const kname = k.name || k.id;
          return `<span style="display:inline-block; font-family:var(--b-mono); font-size:10px; padding:1px 5px; margin:2px 3px 2px 0; border-radius:2px; background:rgba(59,130,246,0.15); color:#93C5FD; border:1px solid rgba(59,130,246,0.3);">${escapeHtml(kname)}</span>`;
        }).join('');
        vKeyPoolSummary.innerHTML = `已维护 <b>${pool.length}</b> 组密钥:<br>${badges}`;
      }
    }
  }

  if (vApi) {
    vApi.innerText = p.api || 'openai-completions';
    vApi.className = 'view-value';
  }

  const delBtn = $id('deleteProviderBtn');
  if (delBtn) {
    if (p._dshBuiltin) {
      // DSH 内置路由：不能删，只能打 disabled 补丁
      delBtn.style.display = 'block';
      delBtn.innerText = p._dshDisabled ? '▶️ 恢复内置路由' : '⏸️ 禁用内置路由';
      delBtn.className = p._dshDisabled ? 'btn btn-emerald' : 'btn btn-rose';
      delBtn.title = p._dshDisabled
        ? '移除 disabled 补丁，重启 DSH 后恢复该路由'
        : '写入 disabled 补丁，重启 DSH 后从模型列表消失';
    } else if (p._isBuiltin) {
      // Pi 内置厂商由原生目录提供，不可删除也不可隐藏
      delBtn.style.display = 'none';
    } else {
      delBtn.style.display = 'block';
      delBtn.innerText = '🗑️ 删除服务商';
      delBtn.className = 'btn btn-rose';
      delBtn.title = '删除此服务商';
    }
  }


}

function escapeHtml(text) {
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function toggleProviderEditMode(editing) {
  isProviderEditing = editing;
  const viewPanel = $id('providerViewPanel');
  const editPanel = $id('providerEditPanel');
  const editBtn = $id('editProviderBtn');
  const saveBtn = $id('saveProviderBtn');
  const cancelBtn = $id('cancelProviderBtn');

  if (editing) {
    const cur = (currentConfig.providers || {})[selectedPid] || {};
    if (cur._dshBuiltin) {
      // DSH 内置路由由 base 层插件提供，不在 pi-ai 配置里
      showAlert({
        title: 'DSH 内置服务商',
        icon: '🔒',
        message: `[${selectedPid}] 由 DSH base 层插件（${cur._dshPlugin || ''}）提供，不在 pi-ai 配置里，工具无法编辑或删除。\\n\\n右侧面板底部可以禁用/恢复它（写 disabled 补丁，重启 DSH 生效）。`
      });
      return;
    }
    if (viewPanel) viewPanel.style.display = 'none';
    if (editPanel) editPanel.style.display = 'flex';
    if (editBtn) editBtn.style.display = 'none';
    if (saveBtn) saveBtn.style.display = 'inline-flex';
    if (cancelBtn) cancelBtn.style.display = 'inline-flex';
    renderKeyPoolEditor(selectedPid);
    if ($id('pId')) $id('pId').focus();
  } else {
    // 退出编辑模式后清空密钥池编辑区，避免残留其他厂商的条目被误同步到当前厂商
    keyPoolRenderedPid = null;
    const poolContainer = $id('keyPoolContainer');
    if (poolContainer) poolContainer.innerHTML = '';
    if (viewPanel) viewPanel.style.display = 'flex';
    if (editPanel) editPanel.style.display = 'none';
    if (editBtn) editBtn.style.display = 'inline-flex';
    if (saveBtn) saveBtn.style.display = 'none';
    if (cancelBtn) cancelBtn.style.display = 'none';
    if (selectedPid) updateProviderViewPanel(selectedPid);
  }
}

async function saveProviderEdit() {
  const pIdInput = $id('pId');
  const rawId = pIdInput ? pIdInput.value : '';
  const pid = normalizeProviderId(rawId);
  if (!pid) return showAlert({ title: '校验失败', icon: '⚠️', message: '服务商 ID 不能为空', type: 'error' });

  if (!selectedPid && currentConfig.providers[pid]) {
    return showAlert({ title: '校验失败', icon: '⚠️', message: `服务商 ID [${pid}] 已存在，请使用其他 ID`, type: 'error' });
  }

  // 修复问题1：不再提前覆盖 selectedPid，交由 syncCurrentFormToMemory 内部处理重命名逻辑
  const finalPid = syncCurrentFormToMemory();
  const saved = await saveAll();
  if (!saved) return;
  toggleProviderEditMode(false);
  renderSidebar();
  updateProviderViewPanel(finalPid);
  setStatus(`✅ 已保存服务商 [${finalPid}]`, '#10B981');
}

function cancelProviderEdit() {
  if (selectedPid && currentConfig.providers[selectedPid]) {
    selectProvider(selectedPid);
  } else {
    const keys = Object.keys(currentConfig.providers);
    if (keys.length > 0) selectProvider(keys[0]);
    else toggleProviderEditMode(false);
  }
  toggleProviderEditMode(false);
}

function normalizeProviderId(raw) {
  return String(raw || '').trim().replace(/[^A-Za-z0-9_.-]+/g, '-');
}

function syncCurrentFormToMemory() {
  const pIdInput = $id('pId');
  if (!pIdInput) return selectedPid;
  const rawId = pIdInput.value;
  const pid = normalizeProviderId(rawId);
  if (!pid) return selectedPid || '';

  if (selectedPid && selectedPid !== pid) {
    if (currentConfig.providers[pid]) {
      showAlert({ title: '校验失败', icon: '⚠️', message: '该服务商 ID 已存在', type: 'error' });
      pIdInput.value = selectedPid;
      return selectedPid;
    }
    currentConfig.providers[pid] = currentConfig.providers[selectedPid] || { models: [] };
    delete currentConfig.providers[selectedPid];
    selectedPid = pid;
  } else if (!selectedPid) {
    selectedPid = pid;
  }

  if (!currentConfig.providers[pid]) {
    currentConfig.providers[pid] = { models: [] };
  }
  const p = currentConfig.providers[pid];
  const nameInput = $id('pName');
  const baseInput = $id('pBaseUrl');
  const apiInput = $id('pApi');
  const apiKeyInput = $id('pApiKey');
  const displayName = nameInput ? nameInput.value.trim() : (p.name || '');
  const baseUrl = baseInput ? baseInput.value.trim().replace(new RegExp('/+$'), '') : (p.baseUrl || '');
  const api = apiInput ? apiInput.value : (p.api || '');
  const directApiKey = apiKeyInput ? apiKeyInput.value.trim() : '';

  if (displayName) p.name = displayName; else delete p.name;
  if (baseUrl) p.baseUrl = baseUrl; else delete p.baseUrl;
  if (api) p.api = api; else delete p.api;

  // 同步密钥与密钥池
  if (isProviderEditing) {
    const poolItems = document.querySelectorAll('#keyPoolContainer .key-pool-item');
    const nextPool = [];
    poolItems.forEach(item => {
      const kidInput = item.querySelector('.key-id-input');
      const knameInput = item.querySelector('.key-name-input');
      const ksecretInput = item.querySelector('.key-secret-input');
      const kid = kidInput ? kidInput.value.trim() : '';
      const ksecret = ksecretInput ? ksecretInput.value.trim() : '';
      const kname = knameInput ? knameInput.value.trim() : '';
      if (kid && ksecret) {
        const poolEntry = { id: kid, key: ksecret };
        if (kname) poolEntry.name = kname;
        nextPool.push(poolEntry);
      }
    });
    if (nextPool.length > 0) {
      p.apiKeys = nextPool;
      if (directApiKey) {
        p.apiKey = directApiKey;
      } else {
        p.apiKey = nextPool[0].key;
        // 只在输入框不在编辑焦点时回显（避免打字/删除过程中被整段回填覆盖）
        if (apiKeyInput && document.activeElement !== apiKeyInput) apiKeyInput.value = nextPool[0].key;
      }
    } else {
      delete p.apiKeys;
      if (directApiKey) {
        p.apiKey = directApiKey;
      } else {
        delete p.apiKey;
      }
    }
  } else {
    if (directApiKey) {
      p.apiKey = directApiKey;
    }
  }

  if (!p.models) p.models = [];
  return pid;
}

function renderKeyPoolEditor(pid) {
  keyPoolRenderedPid = pid || null;
  const container = $id('keyPoolContainer');
  if (!container) return;
  container.innerHTML = '';
  const p = (pid && (currentConfig.providers || {})[pid]) ? currentConfig.providers[pid] : {};
  
  // 如果 p 存在 apiKey 但 apiKeys 为空，则为其初始化一个默认的 apiKeys 池条目
  let pool = p.apiKeys || [];
  if (pool.length === 0 && p.apiKey) {
    pool = [{ id: 'default', name: '主密钥', key: p.apiKey }];
  }

  if (pool.length === 0) {
    const tip = el('div', 'empty-pool-tip', '暂未添加密钥，请点击右上方 ➕ 新增 Key');
    tip.style.cssText = 'color: var(--b-text-4); font-size: 11px; font-style: italic; padding: 4px 0; text-align: center;';
    container.appendChild(tip);
    return;
  }
  pool.forEach((item, idx) => {
    container.appendChild(createKeyPoolRowElement(item.id, item.name || '', item.key || '', idx));
  });
}

function createKeyPoolRowElement(idVal, nameVal, secretVal, index) {
  const row = el('div', 'key-pool-item');
  row.dataset.index = index;

  const colId = el('input', 'input key-id-input');
  colId.style.cssText = 'width: 80px; font-family: var(--b-mono); font-size: 11px; padding: 2px 4px;';
  colId.placeholder = '标识 (如 key-1)';
  colId.value = idVal || '';
  colId.title = 'Key 的唯一内部 ID 标识';

  const colName = el('input', 'input key-name-input');
  colName.style.cssText = 'flex: 1; min-width: 60px; font-size: 11px; padding: 2px 4px;';
  colName.placeholder = '别名 (如 VIP Key)';
  colName.value = nameVal || '';
  colName.title = 'Key 的备注说明';

  const secWrap = el('div', 'input-wrapper');
  secWrap.style.cssText = 'flex: 1.4; min-width: 90px;';
  const colSec = el('input', 'input key-secret-input');
  colSec.type = 'password';
  colSec.style.cssText = 'width: 100%; font-family: var(--b-mono); font-size: 11px; padding: 2px 24px 2px 4px;';
  colSec.placeholder = 'sk-...';
  colSec.value = secretVal || '';
  
  const eye = el('span', 'toggle-pwd', '👁️');
  eye.style.fontSize = '10px';
  eye.style.right = '4px';
  eye.onclick = () => {
    if (colSec.type === 'password') {
      colSec.type = 'text';
      eye.textContent = '🙈';
    } else {
      colSec.type = 'password';
      eye.textContent = '👁️';
    }
  };
  secWrap.append(colSec, eye);

  const delBtn = el('button', 'btn btn-ghost', '✕');
  delBtn.type = 'button';
  delBtn.style.cssText = 'padding: 1px 5px; font-size: 11px; color: var(--b-text-3);';
  delBtn.title = '移除此 Key';
  delBtn.onclick = () => {
    row.remove();
    syncCurrentFormToMemory();
    const remain = document.querySelectorAll('#keyPoolContainer .key-pool-item');
    if (remain.length === 0) {
      renderKeyPoolEditor(selectedPid);
    }
  };

  row.append(colId, colName, secWrap, delBtn);
  return row;
}

function addKeyPoolItem() {
  const container = $id('keyPoolContainer');
  if (!container) return;
  // If showing placeholder tip, clear it
  const tip = container.querySelector('.empty-pool-tip');
  if (tip) tip.remove();

  const items = container.querySelectorAll('.key-pool-item');
  const nextIdx = items.length + 1;
  const newRow = createKeyPoolRowElement(`key-${nextIdx}`, `密钥 ${nextIdx}`, '', items.length);
  container.appendChild(newRow);
  const secretInp = newRow.querySelector('.key-secret-input');
  if (secretInp) secretInp.focus();
}

// toggleApiKeyVisibility 不再需要了
function toggleApiKeyVisibility() {}

// Render Single-line Model Rows (.model-row)
function renderModels(models) {
  const pid = selectedPid || syncCurrentFormToMemory();
  const p = (pid && (currentConfig.providers || {})[pid]) ? currentConfig.providers[pid] : {};
  const actualList = models !== undefined ? models : (p.models || []);

  const tabBadge = $id('modelsTabBadge');
  if (tabBadge) tabBadge.innerText = String(actualList.length);

  const container = $id('modelsContainer');
  if (!container) return;
  container.innerHTML = '';

  if (!actualList || actualList.length === 0) {
    container.appendChild(emptyState('暂无模型配置', '点击上方《➕ 添加模型》或《⚙️ 厂商设置 → 自动拉取》'));
    return;
  }

  const searchInput = $id('modelSearch');
  const query = searchInput ? searchInput.value.trim().toLowerCase() : '';
  // 复制一份再操作：displayList 可能与 p.models 共享引用，不能原地排序
  let displayList = actualList.slice();
  if (query) {
    displayList = displayList.filter(m => {
      const id = String(m.id || '').toLowerCase();
      const name = String(m.name || '').toLowerCase();
      return id.includes(query) || name.includes(query);
    });
  }

  // Pi 的 /model 选择器会把「当前模型」与「默认模型」置顶显示。
  // 工具侧对应地把 Pi 默认模型置顶（仅显示层，不修改保存顺序）。
  const pinnedId = (uiPrefs.pinDefaultModel && currentDefaultProvider && currentDefaultProvider === pid)
    ? currentDefaultModel : '';
  if (pinnedId) {
    const idx = displayList.findIndex(m => m.id === pinnedId);
    if (idx > 0) {
      const pinnedRow = displayList.splice(idx, 1)[0];
      displayList.unshift(pinnedRow);
    }
  }

  if (displayList.length === 0) {
    container.appendChild(emptyState('未搜索到匹配模型', `关键词: "${query}"`));
    return;
  }

  displayList.forEach((m) => {
    const isPinned = Boolean(pinnedId) && m.id === pinnedId;
    const row = el('div', 'model-row' + (m.disabled ? ' disabled' : '') + (isPinned ? ' pinned' : ''));
    row.dataset.mid = m.id;
    if (isPinned) {
      row.title = 'Pi 的 /model 选择器会把默认模型置顶显示：此处仅为显示层置顶，保存位置不变';
    }

    // Drag-and-drop ordering
    row.addEventListener('dragstart', (e) => {
      if (row.draggable !== true) { e.preventDefault(); return; }
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', m.id);
      row.classList.add('dragging');
      document.body.classList.add('dragging-cursor');
    });
    row.addEventListener('dragend', () => {
      row.draggable = false;
      row.classList.remove('dragging');
      document.body.classList.remove('dragging-cursor');
      document.querySelectorAll('.model-row').forEach(r => r.classList.remove('drag-over'));
    });
    row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('drag-over'); });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', async (e) => {
      e.preventDefault();
      row.classList.remove('drag-over');
      const srcMid = e.dataTransfer.getData('text/plain');
      if (!srcMid || srcMid === m.id) return;
      if (isPinned) {
        setStatus('⭐ 默认模型行在 Pi 侧始终置顶显示：请关闭「⭐ 默认置顶」后再调整它的保存位置', '#F59E0B');
        return;
      }
      await moveModelToTarget(pid, srcMid, m.id);
    });

    // Handle：内置模型的顺序由 Pi 原生目录决定，禁止拖拽以保证两边顺序一致
    const handle = el('span', 'model-row-handle', '≡');
    if (isPinned) {
      handle.innerText = '⭐';
      handle.title = 'Pi 默认模型：/model 选择器总是把它置顶显示（关闭「⭐ 默认置顶」后可拖拽调整保存位置）';
    } else if (m._isBuiltinModel) {
      handle.title = '内置模型顺序由 Pi 原生目录决定，不可调整';
      handle.style.opacity = '0.3';
      handle.style.cursor = 'not-allowed';
    } else {
      handle.title = '按住拖拽调整模型顺序';
      handle.addEventListener('pointerdown', () => { row.draggable = true; });
    }

    // Alias Input
    const aliasWrap = el('div', 'model-row-alias');
    const aliasInput = el('input', 'alias-input');
    aliasInput.type = 'text';
    aliasInput.value = m.name || m.id;
    aliasInput.placeholder = m.id;
    aliasInput.title = '点击直接修改别名，按回车保存';
    aliasInput.onchange = () => updateModelAlias(m.id, aliasInput.value);
    aliasWrap.appendChild(aliasInput);

    // Model ID
    const idEl = el('span', 'model-row-id', m.id);
    idEl.title = m.id;

    // Badges Container
    const badgesWrap = el('div', 'model-meta-badges');
    const ctx = m.contextWindow ? Math.round(m.contextWindow / 1000) + 'K' : '';
    if (ctx) {
      const bCtx = el('span', 'meta-badge badge-ctx', ctx);
      bCtx.title = `上下文窗口: ${m.contextWindow.toLocaleString()} tokens`;
      badgesWrap.appendChild(bCtx);
    }
    const hasImage = Array.isArray(m.input) && m.input.includes('image');
    if (hasImage) {
      const bImg = el('span', 'meta-badge badge-img', '📷 视觉');
      bImg.title = '支持图像/多模态输入 (input: ["text", "image"])';
      badgesWrap.appendChild(bImg);
    }
    if (m.reasoning) {
      const bRea = el('span', 'meta-badge badge-reason', '🧠 思考');
      bRea.title = '具备深度推理思考能力 (reasoning: true)';
      badgesWrap.appendChild(bRea);
    }
    idEl.appendChild(badgesWrap);

    // Status Badge
    const statusWrap = el('div', 'model-row-status');
    if (m.disabled) {
      statusWrap.appendChild(el('span', 'disabled-badge', '已禁用'));
    } else if (m._failStreak > 0) {
      statusWrap.appendChild(el('span', 'fail-streak', `⚠ ${m._failStreak}`));
    }

    // Actions
    const actions = el('div', 'model-row-actions');

    // Key Configuration Button (🔑)
    const keyRef = m.apiKeyRef;
    const headers = m.headers || {};
    const hasCustomKey = Boolean(headers.Authorization || headers['x-api-key']);
    let keyBtnClass = 'model-row-btn key-btn';
    let keyBtnText = '🔑 Key';
    let keyBtnTitle = '使用厂商默认 API Key (点击切换或单独配置)';

    const poolList = p.apiKeys || [];
    const poolMap = {};
    poolList.forEach(k => { poolMap[k.id] = k; });

    if (keyRef && poolMap[keyRef]) {
      keyBtnClass += ' is-pool';
      const kInfo = poolMap[keyRef];
      keyBtnText = `🔑 ${kInfo.name || keyRef}`;
      keyBtnTitle = `已绑定厂商密钥池: ${kInfo.name || keyRef} (点击修改)`;
    } else if (hasCustomKey) {
      keyBtnClass += ' is-custom';
      keyBtnText = '🔑 独立Key';
      keyBtnTitle = '已配置当前模型专属独立 API Key (点击修改)';
    }

    const keyBtn = el('button', keyBtnClass, keyBtnText);
    keyBtn.title = keyBtnTitle;
    keyBtn.onclick = () => openModelKeyModal(m.id);

    // Test Button
    const testBtn = el('button', 'model-row-btn test-btn', '⚡ 测活');
    testBtn.title = '发送最小请求验证 API 是否可用';
    testBtn.onclick = () => testModel(testBtn, m.id);

    // Default Button
    const isDefault = currentDefaultProvider === pid && currentDefaultModel === m.id;
    const defBtn = el('button', 'model-row-btn default-btn' + (isDefault ? ' is-default' : ''), isDefault ? '★ 默认' : '☆ 设默认');
    defBtn.title = isDefault ? '当前已是 Pi 默认模型' : `点击将 [${m.id}] 设为 Pi 默认模型`;
    defBtn.onclick = () => setDefaultModel(pid, m.id);

    // Delete Button
    const delBtn = el('button', 'model-row-btn del-btn', '✕');
    if (m._isBuiltinModel) {
      // 内置模型的目录由 Pi 原生提供：不可删除，也不提供“禁用”
      // （Pi 的 models.json schema 没有 disabled 字段，设了也不会生效）
      delBtn.disabled = true;
      delBtn.title = `[${m.id}] 是 Pi 内置模型，目录由 Pi 原生提供，不可删除或隐藏。`;
      delBtn.style.opacity = '0.25';
      delBtn.style.cursor = 'not-allowed';
      actions.append(keyBtn, testBtn, defBtn, delBtn);
    } else {
      const toggleBtn = el('button', 'model-row-btn', m.disabled ? '🔓 启用' : '⏸ 禁用');
      toggleBtn.title = m.disabled ? '点击重新启用此模型' : '点击禁用此模型（保存后从 Pi 中移除）';
      toggleBtn.onclick = () => toggleModelEnabled(m.id);
      delBtn.title = `删除模型 [${m.id}]`;
      delBtn.onclick = () => removeModel(m.id);
      actions.append(keyBtn, testBtn, defBtn, toggleBtn, delBtn);
    }

    row.append(handle, aliasWrap, idEl, statusWrap, actions);
    container.appendChild(row);
  });
}

async function moveModelToTarget(pid, srcMid, targetMid) {
  const p = (currentConfig.providers || {})[pid];
  if (!p || !p.models) return;
  const models = [...p.models];
  const fromIdx = models.findIndex(m => m.id === srcMid);
  const toIdx = models.findIndex(m => m.id === targetMid);
  if (fromIdx < 0 || toIdx < 0 || fromIdx === toIdx) return;
  const [removed] = models.splice(fromIdx, 1);
  models.splice(toIdx, 0, removed);
  p.models = models;
  renderModels(p.models);
  await saveAll();
}

function updateModelAlias(mid, newAlias) {
  const pid = syncCurrentFormToMemory();
  if (!pid) return;
  const p = currentConfig.providers[pid];
  if (!p || !p.models) return;
  const trimmed = String(newAlias || '').trim();
  const m = p.models.find(x => x.id === mid);
  if (!m) return;
  if (!trimmed || trimmed === mid) delete m.name;
  else m.name = trimmed;
  renderSidebar();
}

function toggleModelEnabled(mid) {
  const pid = syncCurrentFormToMemory();
  if (!pid) return;
  const p = currentConfig.providers[pid];
  if (!p || !p.models) return;
  const m = p.models.find(x => x.id === mid);
  if (!m) return;
  if (m.disabled) {
    delete m.disabled;
    m._failStreak = 0;
    m._lastError = null;
    setStatus(`✅ [${mid}] 已重新启用（点 💾 保存后 Pi 侧恢复显示）`, '#10B981');
  } else {
    m.disabled = true;
    setStatus(`⏸ [${mid}] 已禁用：点 💾 保存后该模型将从 Pi 中移除`, '#F59E0B');
  }
  renderModels(p.models);
}

async function addModelManual() {
  const mid = $id('newModelId').value.trim();
  const mname = $id('newModelName').value.trim();
  if (!mid) return showAlert({ title: '校验失败', icon: '⚠️', message: '请输入模型 ID', type: 'error' });
  const pid = syncCurrentFormToMemory();
  if (!pid) return showAlert({ title: '校验失败', icon: '⚠️', message: '请先选择或新建服务商', type: 'error' });
  const p = currentConfig.providers[pid];
  if (p && p._dshBuiltin) {
    return showAlert({
      title: 'DSH 内置服务商',
      icon: '🔒',
      message: `[${pid}] 的模型目录由 DSH base 层插件（${p._dshPlugin || ''}）自行提供，模型列表不可编辑，也无法写入 pi-ai 配置。\\n\\n如要自定义 DeepSeek 官方端点，请新建一个 openai-completions / anthropic-messages 服务商。`
    });
  }
  if (p && p._isBuiltin) {
    return showAlert({
      title: '内置服务商',
      icon: '🔒',
      message: `[${pid}] 的模型目录由 Pi 原生提供，不支持手动添加模型。`
    });
  }
  if (!p.models) p.models = [];
  const list = p.models.filter(x => x.id !== mid);
  const nextModel = { id: mid };
  if (mname) nextModel.name = mname;

  // 自动推断 contextWindow / maxTokens / input / reasoning
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.infer_specs) {
      const res = await window.pywebview.api.infer_specs(mid);
      if (res && res.success && res.specs) {
        Object.assign(nextModel, res.specs);
      }
    }
  } catch (e) {
    console.error('infer_specs failed', e);
  }

  list.push(nextModel);
  p.models = list;
  $id('newModelId').value = '';
  $id('newModelName').value = '';
  renderModels(p.models);
  renderSidebar();
  $id('newModelId').focus();
  const caps = [];
  if (nextModel.contextWindow) caps.push(`${Math.round(nextModel.contextWindow / 1000)}K`);
  if (Array.isArray(nextModel.input) && nextModel.input.includes('image')) caps.push('视觉');
  if (nextModel.reasoning) caps.push('思考');
  const capStr = caps.length ? ` [${caps.join(' · ')}]` : '';
  setStatus(`已添加模型 [${mid}]${capStr}，点击「💾 保存」持久化`, '#10B981');
}

function removeModel(mid) {
  const pid = syncCurrentFormToMemory();
  if (!pid) return;
  const p = currentConfig.providers[pid];
  p.models = (p.models || []).filter(x => x.id !== mid);
  renderModels(p.models);
  renderSidebar();
}

function newProvider() {
  selectedPid = null;
  fetchedPreview = [];
  renderSidebar();
  if ($id('pId')) $id('pId').value = '';
  if ($id('pName')) $id('pName').value = '';
  if ($id('pBaseUrl')) $id('pBaseUrl').value = '';
  if ($id('pApiKey')) $id('pApiKey').value = '';
  if ($id('pApi')) $id('pApi').value = 'openai-completions';
  updateProviderViewPanel(null);
  openDrawer();
  toggleProviderEditMode(true);
  renderModels([]);
  if ($id('pId')) $id('pId').focus();
}

function deleteCurrentProvider() {
  if (!selectedPid) return;
  deleteProvider(selectedPid);
  closeDrawer();
}


// Tab Switching
function switchModelWorkTab(tab) {
  const btnModels = $id('tabBtnModels');
  const btnFetch = $id('tabBtnFetch');
  const paneModels = $id('paneModels');
  const paneFetch = $id('paneFetch');
  const modelsActions = $id('modelsBarActions');
  const fetchActions = $id('fetchBarActions');
  
  if (tab === 'fetch') {
    if (btnModels) btnModels.classList.remove('active');
    if (btnFetch) btnFetch.classList.add('active');
    if (paneModels) paneModels.classList.remove('active');
    if (paneFetch) paneFetch.classList.add('active');
    if (modelsActions) modelsActions.style.display = 'none';
    if (fetchActions) fetchActions.style.display = 'flex';
  } else {
    if (btnModels) btnModels.classList.add('active');
    if (btnFetch) btnFetch.classList.remove('active');
    if (paneModels) paneModels.classList.add('active');
    if (paneFetch) paneFetch.classList.remove('active');
    if (modelsActions) modelsActions.style.display = 'flex';
    if (fetchActions) fetchActions.style.display = 'none';
  }
}

// Model-specific API Key Modal & Resolution
let activeModalModelId = null;

function getEffectiveModelAuth(pid, model) {
  const p = (pid && (currentConfig.providers || {})[pid]) ? currentConfig.providers[pid] : {};
  const pool = p.apiKeys || [];
  const poolMap = {};
  pool.forEach(k => { poolMap[k.id] = k; });

  const headers = model.headers ? { ...model.headers } : {};
  let effectiveKey = (p.apiKey || '').trim();
  let keySource = 'default';
  let keySourceName = '厂商默认';

  if (model.apiKeyRef && poolMap[model.apiKeyRef]) {
    const item = poolMap[model.apiKeyRef];
    effectiveKey = (item.key || '').trim();
    keySource = 'pool';
    keySourceName = item.name || item.id;
    if (effectiveKey) {
      headers['Authorization'] = `Bearer ${effectiveKey}`;
    }
  } else if (headers.Authorization) {
    keySource = 'custom';
    keySourceName = '独立专属Key';
    const match = headers.Authorization.match(/^Bearer[ ]+(.+)$/i);
    if (match) {
      effectiveKey = match[1].trim();
    }
  } else if (headers['x-api-key']) {
    keySource = 'custom';
    keySourceName = '独立专属Key';
    effectiveKey = headers['x-api-key'].trim();
  }

  return {
    apiKey: effectiveKey,
    headers: headers,
    source: keySource,
    sourceName: keySourceName
  };
}

function openModelKeyModal(mid) {
  const pid = syncCurrentFormToMemory();
  if (!pid) return showAlert({ title: '校验失败', icon: '⚠️', message: '请先选择服务商', type: 'error' });
  const p = currentConfig.providers[pid] || {};
  const model = (p.models || []).find(m => m.id === mid);
  if (!model) return;

  activeModalModelId = mid;
  const modal = $id('modelKeyModal');
  const targetIdEl = $id('modelKeyTargetId');
  const defPreviewEl = $id('modelKeyDefaultPreview');
  const poolSelect = $id('modelKeyPoolSelect');
  const customInput = $id('modelKeyCustomInput');

  if (targetIdEl) targetIdEl.textContent = `${mid} (${model.name || mid})`;
  
  const defaultKey = (p.apiKey || '').trim();
  if (defPreviewEl) {
    if (defaultKey) {
      const masked = defaultKey.length > 8 ? defaultKey.slice(0, 4) + '••••' + defaultKey.slice(-3) : '••••••••';
      defPreviewEl.textContent = `[${masked}]`;
    } else {
      defPreviewEl.textContent = '(当前未配置默认 Key)';
    }
  }

  // Populate Key Pool Select
  const pool = p.apiKeys || [];
  if (poolSelect) {
    poolSelect.innerHTML = '';
    if (pool.length === 0) {
      const opt = el('option', '', '(厂商暂未在「厂商设置」中添加密钥池)');
      opt.value = '';
      poolSelect.appendChild(opt);
      poolSelect.disabled = true;
    } else {
      poolSelect.disabled = false;
      const defOpt = el('option', '', '-- 请选择绑定的池密钥 --');
      defOpt.value = '';
      poolSelect.appendChild(defOpt);
      pool.forEach(k => {
        const masked = k.key && k.key.length > 8 ? k.key.slice(0, 4) + '••••' + k.key.slice(-3) : '••••••••';
        const opt = el('option', '', `${k.name || k.id} [${k.id}] (${masked})`);
        opt.value = k.id;
        poolSelect.appendChild(opt);
      });
    }
  }

  // Determine current mode
  const headers = model.headers || {};
  let currentKeyVal = '';
  if (headers.Authorization) {
    const match = headers.Authorization.match(/^Bearer[ ]+(.+)$/i);
    currentKeyVal = match ? match[1] : headers.Authorization;
  } else if (headers['x-api-key']) {
    currentKeyVal = headers['x-api-key'];
  }

  if (model.apiKeyRef) {
    $id('keyModePool').checked = true;
    if (poolSelect) poolSelect.value = model.apiKeyRef;
    if (customInput) customInput.value = '';
  } else if (currentKeyVal) {
    $id('keyModeCustom').checked = true;
    if (customInput) customInput.value = currentKeyVal;
  } else {
    $id('keyModeDefault').checked = true;
    if (customInput) customInput.value = '';
  }

  updateModelKeyModalUI();
  if (modal) modal.classList.add('open');
}

function updateModelKeyModalUI() {
  const isDefault = $id('keyModeDefault') && $id('keyModeDefault').checked;
  const isPool = $id('keyModePool') && $id('keyModePool').checked;
  const isCustom = $id('keyModeCustom') && $id('keyModeCustom').checked;

  const poolWrap = $id('modelKeyPoolWrap');
  const customWrap = $id('modelKeyCustomWrap');

  if (poolWrap) poolWrap.style.display = isPool ? 'block' : 'none';
  if (customWrap) customWrap.style.display = isCustom ? 'block' : 'none';

  if (isCustom) {
    const input = $id('modelKeyCustomInput');
    if (input) setTimeout(() => input.focus(), 50);
  }
}

function toggleModelKeyCustomVisibility() {
  const input = $id('modelKeyCustomInput');
  const btn = $id('toggleModelKeyCustomBtn');
  if (!input) return;
  if (input.type === 'password') {
    input.type = 'text';
    if (btn) btn.textContent = '🙈';
  } else {
    input.type = 'password';
    if (btn) btn.textContent = '👁️';
  }
}

function handleModelKeyModalBackdrop(e) {
  if (e.target && e.target.id === 'modelKeyModal') {
    closeModelKeyModal();
  }
}

function closeModelKeyModal() {
  const modal = $id('modelKeyModal');
  if (modal) modal.classList.remove('open');
  activeModalModelId = null;
}

async function saveModelKeyModal() {
  if (!activeModalModelId) return closeModelKeyModal();
  const pid = syncCurrentFormToMemory();
  if (!pid) return;
  const p = currentConfig.providers[pid] || {};
  const model = (p.models || []).find(m => m.id === activeModalModelId);
  if (!model) return closeModelKeyModal();

  const isDefault = $id('keyModeDefault') && $id('keyModeDefault').checked;
  const isPool = $id('keyModePool') && $id('keyModePool').checked;
  const isCustom = $id('keyModeCustom') && $id('keyModeCustom').checked;

  if (isDefault) {
    delete model.apiKeyRef;
    if (model.headers) {
      delete model.headers['Authorization'];
      delete model.headers['x-api-key'];
      if (Object.keys(model.headers).length === 0) delete model.headers;
    }
    setStatus(`[${model.id}] 已重置为继承厂商默认 Key`, '#10B981');
  } else if (isPool) {
    const poolSelect = $id('modelKeyPoolSelect');
    const kid = poolSelect ? poolSelect.value : '';
    if (!kid) return showAlert({ title: '配置校验', icon: '⚠️', message: '请选择要绑定的密钥池条目' });
    const poolList = p.apiKeys || [];
    const poolItem = poolList.find(k => k.id === kid);
    if (!poolItem) return showAlert({ title: '配置校验', icon: '⚠️', message: '选中的密钥不存在', type: 'error' });

    model.apiKeyRef = kid;
    if (!model.headers) model.headers = {};
    model.headers['Authorization'] = `Bearer ${poolItem.key}`;
    setStatus(`[${model.id}] 已绑定密钥池: ${poolItem.name || kid}`, '#10B981');
  } else if (isCustom) {
    const customInput = $id('modelKeyCustomInput');
    const secret = customInput ? customInput.value.trim() : '';
    if (!secret) return showAlert({ title: '配置校验', icon: '⚠️', message: '请输入专属自定义 API Key' });

    delete model.apiKeyRef;
    if (!model.headers) model.headers = {};
    model.headers['Authorization'] = `Bearer ${secret}`;
    setStatus(`[${model.id}] 已配置独立专属 API Key`, '#10B981');
  }

  closeModelKeyModal();
  renderModels(p.models);
  await saveAll();
}

// Model Testing
function currentTestContext() {
  const pid = selectedPid || syncCurrentFormToMemory();
  const p = (pid && currentConfig.providers && currentConfig.providers[pid]) || {};
  const formBaseUrl = $id('pBaseUrl') ? $id('pBaseUrl').value.trim() : '';
  const formApi = $id('pApi') ? $id('pApi').value : '';
  const formApiKey = $id('pApiKey') ? $id('pApiKey').value.trim() : '';
  return {
    baseUrl: formBaseUrl || p.baseUrl || '',
    apiKey: formApiKey || p.apiKey || (p.apiKeys && p.apiKeys[0] ? p.apiKeys[0].key : '') || '',
    api: formApi || p.api || 'openai-completions',
  };
}

function formatModelError(res, baseUrl, mid, sourceName) {
  const NL = String.fromCharCode(10);
  const lines = [];
  lines.push('模型: ' + mid);
  if (baseUrl) lines.push('端点: ' + baseUrl);
  if (sourceName) lines.push('凭据: ' + sourceName);
  if (res && res.endpoint) lines.push('请求地址: ' + res.endpoint);
  if (res && res.status) lines.push('HTTP 状态: ' + res.status);
  lines.push('');
  lines.push('错误详情:');
  lines.push((res && res.error) ? res.error : '未知错误');
  if (res && res.traceback) {
    lines.push('');
    lines.push('Python Traceback:');
    lines.push(res.traceback);
  }
  if (res && res.logPath) {
    lines.push('');
    lines.push('完整日志: ' + res.logPath);
  }
  return lines.join(NL);
}

async function testModel(btn, mid) {
  const ctx = currentTestContext();
  if (!ctx.baseUrl) return showAlert({ title: '配置校验', icon: '⚠️', message: '请先填写 Base URL 端点' });
  
  const pid = selectedPid || syncCurrentFormToMemory();
  const p = (pid && (currentConfig.providers || {})[pid]) ? currentConfig.providers[pid] : {};
  const model = (p.models || []).find(m => m.id === mid) || { id: mid };
  const auth = getEffectiveModelAuth(pid, model);

  btn.classList.add('testing');
  btn.classList.remove('ok', 'fail', 'warn');
  btn.innerHTML = '⏳ 测活中';
  btn.title = `正在深度检测 API 连通性与 Agent 工具支持 (Key: ${auth.sourceName})...`;
  try {
    const res = await window.pywebview.api.test_model(ctx.baseUrl, auth.apiKey, ctx.api, mid, auth.headers);
    if (res.success) {
      if (res.agent_ready === false) {
        btn.classList.add('warn');
        btn.innerHTML = `⚠ ${res.latency_ms}ms`;
        btn.title = `基础可用，但${res.warning || '不支持工具调用'} [${auth.sourceName}]`;
        setStatus(`⚠️ [${mid}] 仅支持纯聊 · ${res.warning || '不支持 Agent 工具'} (${auth.sourceName})`, '#F59E0B');
      } else {
        btn.classList.add('ok');
        btn.innerHTML = `✓ ${res.latency_ms}ms`;
        btn.title = `测活成功 · 全功能就绪(流式+工具已通过) · 延迟 ${res.latency_ms}ms [${auth.sourceName}]`;
        setStatus(`✅ [${mid}] 测活成功(全功能可用) · ${res.latency_ms}ms (${auth.sourceName})`, '#10B981');
      }
    } else {
      btn.classList.add('fail');
      btn.innerHTML = '✗ 失败';
      const errText = formatModelError(res, ctx.baseUrl, mid, auth.sourceName);
      btn.title = errText;
      setStatus(`❌ [${mid}] 测活失败: ${res.error || '未知错误'} (${auth.sourceName})`, '#EF4444');
      appendLog('err', `测活失败 · ${mid}`, errText);
      toggleLogPanel(true);
    }
  } catch (e) {
    btn.classList.add('fail');
    btn.innerHTML = '✗ 异常';
    setStatus(`❌ [${mid}] 测活异常: ${e}`, '#EF4444');
    appendLog('err', `测活异常 · ${mid}`, String(e && e.stack ? e.stack : e));
    toggleLogPanel(true);
  }
  setTimeout(() => btn.classList.remove('testing'), 400);
}

let testAllInProgress = false;

async function testAllModels() {
  if (testAllInProgress) return;
  const pid = syncCurrentFormToMemory();
  if (!pid) return showAlert({ title: '未选择服务商', icon: '⚠️', message: '请先选择或配置服务商' });
  const p = currentConfig.providers[pid];
  const models = (p && p.models) || [];
  if (models.length === 0) return showAlert({ title: '暂无模型', icon: 'ℹ️', message: '当前服务商暂无模型' });
  const ctx = currentTestContext();
  if (!ctx.baseUrl) return showAlert({ title: '配置校验', icon: '⚠️', message: '请先填写 Base URL 端点' });

  testAllInProgress = true;
  setStatus(`⏳ 正在依次测活 ${models.length} 个模型...`, '#F59E0B');

  const rows = document.querySelectorAll('#modelsContainer .model-row');
  let okCount = 0, failCount = 0;
  const failures = [];
  for (let i = 0; i < models.length; i++) {
    const m = models[i];
    const row = rows[i];
    const btn = row ? row.querySelector('.test-btn') : null;
    if (btn) {
      btn.classList.add('testing');
      btn.classList.remove('ok', 'fail');
      btn.innerHTML = '⏳ 测活中';
    }
    const auth = getEffectiveModelAuth(pid, m);
    let res;
    try {
      res = await window.pywebview.api.test_model(ctx.baseUrl, auth.apiKey, ctx.api, m.id, auth.headers);
    } catch (e) {
      res = { success: false, error: String(e) };
    }
    if (res.success) {
      if (btn) {
        if (res.agent_ready === false) {
          btn.classList.add('warn');
          btn.innerHTML = `⚠ ${res.latency_ms}ms`;
          btn.title = `基础可用，但${res.warning || '不支持工具调用'} [${auth.sourceName}]`;
        } else {
          btn.classList.add('ok');
          btn.innerHTML = `✓ ${res.latency_ms}ms`;
          btn.title = `测活成功 · 全功能就绪 [${auth.sourceName}]`;
        }
      }
      okCount++;
      m._failStreak = 0;
    } else {
      const errText = formatModelError(res, ctx.baseUrl, m.id, auth.sourceName);
      if (btn) { btn.classList.add('fail'); btn.innerHTML = '✗ 失败'; btn.title = errText; }
      failCount++;
      failures.push({ id: m.id, status: res.status || 0, error: res.error || '未知错误', detail: errText });
      m._failStreak = (m._failStreak || 0) + 1;
    }
    if (btn) btn.classList.remove('testing');
  }
  testAllInProgress = false;
  renderModels(p.models);
  setStatus(`✅ 全部测活完成：${okCount} 可用 / ${failCount} 失败`, failCount > 0 ? '#F59E0B' : '#10B981');
  if (failCount > 0) {
    const NL = String.fromCharCode(10);
    const body = [`全部测活结果: 可用 ${okCount} / 失败 ${failCount} / 共 ${models.length}`, ''];
    failures.forEach((f, i) => {
      body.push(`【${i + 1}】${f.id}` + (f.status ? `  (HTTP ${f.status})` : ''));
      body.push(f.detail);
      body.push('');
    });
    appendLog('err', `全部测活完成：${failCount} 个失败`, body.join(NL));
    toggleLogPanel(true);
  }
}

// Remote Fetch
let currentFetchParams = null;

function fetchRemoteModelsFromTab() {
  if (!selectedPid) return showAlert({ title: '未选择服务商', icon: '⚠️', message: '请先在左侧选择一个服务商' });
  const p = currentConfig.providers[selectedPid] || {};
  const baseUrl = ($id('pBaseUrl') ? $id('pBaseUrl').value.trim() : '') || p.baseUrl || '';
  if (!baseUrl) {
    showAlert({ title: '配置校验', icon: '⚠️', message: '该服务商尚未配置 Base URL，请先在「厂商设置」中填写并保存。' });
    return openDrawer(selectedPid);
  }
  
  const pool = p.apiKeys || [];
  const formKey = ($id('pApiKey') ? $id('pApiKey').value.trim() : '') || p.apiKey || '';

  if (pool.length <= 1) {
    // 只有 0 或 1 个 Key，直接拉取
    const mainKey = formKey || (pool.length === 1 ? pool[0].key : '');
    const refId = pool.length === 1 ? pool[0].id : 'default';
    const refName = pool.length === 1 ? (pool[0].name || pool[0].id) : '主 Key';
    currentFetchParams = { key: mainKey, ref: refId, refName: refName, tempBaseUrl: baseUrl };
    confirmFetchRemoteModels();
    return;
  }

  // 有多 Key，弹出选择框
  const modal = $id('fetchKeyModal');
  const optsContainer = $id('fetchKeyModalOptions');
  optsContainer.innerHTML = '';

  // Pool Keys 选项
  pool.forEach((k, idx) => {
    const card = el('label', 'modal-option-card' + (idx === 0 ? ' selected' : ''));
    const rdo = el('input');
    rdo.type = 'radio';
    rdo.name = 'fetchKeyChoice';
    rdo.value = k.id;
    rdo.style.cssText = 'margin-top: 2px;';
    if (idx === 0) rdo.checked = true;
    rdo.dataset.secret = k.key || '';
    rdo.dataset.refname = k.name || k.id;

    rdo.onchange = () => {
      document.querySelectorAll('#fetchKeyModalOptions .modal-option-card').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
    };

    let ksecret = k.key || '';
    let preview = ksecret.length > 8 ? ksecret.slice(0,4) + '••••' + ksecret.slice(-3) : '••••';
    const wrap = el('div');
    wrap.style.cssText = 'display: flex; flex-direction: column; gap: 2px; font-size: 11.5px;';
    const title = el('span', '', `🔑 ${k.name || k.id}`);
    title.style.cssText = 'font-weight: 500; color: var(--b-text);';
    const sub = el('span', '', `Key: ${preview}`);
    sub.style.cssText = 'color: var(--b-text-3); font-family: var(--b-mono); font-size: 10.5px;';
    wrap.append(title, sub);
    card.append(rdo, wrap);
    optsContainer.appendChild(card);
  });

  currentFetchParams = { tempBaseUrl: baseUrl }; // Store baseUrl for the modal to use
  modal.classList.add('open');
}

function fetchRemoteModels() {
  const pid = syncCurrentFormToMemory();
  const p = (pid && currentConfig.providers && currentConfig.providers[pid]) || {};
  const baseUrl = ($id('pBaseUrl') ? $id('pBaseUrl').value.trim() : '') || p.baseUrl || '';
  if (!baseUrl) return showAlert({ title: '配置校验', icon: '⚠️', message: '请先填写 Base URL 端点' });
  
  const pool = p.apiKeys || [];
  const formKey = ($id('pApiKey') ? $id('pApiKey').value.trim() : '') || p.apiKey || '';

  if (pool.length <= 1) {
    // 只有 0 或 1 个 Key，直接拉取
    const mainKey = formKey || (pool.length === 1 ? pool[0].key : '');
    const refId = pool.length === 1 ? pool[0].id : 'default';
    const refName = pool.length === 1 ? (pool[0].name || pool[0].id) : '主 Key';
    currentFetchParams = { key: mainKey, ref: refId, refName: refName, tempBaseUrl: baseUrl };
    confirmFetchRemoteModels();
    return;
  }

  // 有多 Key，弹出选择框
  const modal = $id('fetchKeyModal');
  const optsContainer = $id('fetchKeyModalOptions');
  optsContainer.innerHTML = '';

  // Pool Keys 选项 (第一个默认勾选)
  pool.forEach((k, idx) => {
    const card = el('label', 'modal-option-card' + (idx === 0 ? ' selected' : ''));
    const rdo = el('input');
    rdo.type = 'radio';
    rdo.name = 'fetchKeyChoice';
    rdo.value = k.id;
    rdo.style.cssText = 'margin-top: 2px;';
    if (idx === 0) rdo.checked = true;
    rdo.dataset.secret = k.key || '';
    rdo.dataset.refname = k.name || k.id;

    rdo.onchange = () => {
      document.querySelectorAll('#fetchKeyModalOptions .modal-option-card').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
    };

    let ksecret = k.key || '';
    let preview = ksecret.length > 8 ? ksecret.slice(0,4) + '••••' + ksecret.slice(-3) : '••••';
    const wrap = el('div');
    wrap.style.cssText = 'display: flex; flex-direction: column; gap: 2px; font-size: 11.5px;';
    const title = el('span', '', `🔑 ${k.name || k.id}`);
    title.style.cssText = 'font-weight: 500; color: var(--b-text);';
    const sub = el('span', '', `Key: ${preview}`);
    sub.style.cssText = 'color: var(--b-text-3); font-family: var(--b-mono); font-size: 10.5px;';
    wrap.append(title, sub);
    card.append(rdo, wrap);
    optsContainer.appendChild(card);
  });

  currentFetchParams = { tempBaseUrl: baseUrl }; // Store baseUrl for the modal to use
  modal.classList.add('open');
}

function closeFetchKeyModal() {
  const modal = $id('fetchKeyModal');
  if (modal) modal.classList.remove('open');
}

function handleFetchKeyModalBackdrop(e) {
  if (e.target && e.target.id === 'fetchKeyModal') closeFetchKeyModal();
}

async function confirmFetchRemoteModels() {
  let baseUrl = ($id('pBaseUrl') ? $id('pBaseUrl').value : '').trim();
  let apiKey = '';
  let refId = 'default';
  let refName = '主 Key';

  const modal = $id('fetchKeyModal');
  if (modal && modal.classList.contains('open')) {
    const checked = document.querySelector('input[name="fetchKeyChoice"]:checked');
    if (checked) {
      apiKey = checked.dataset.secret || '';
      refId = checked.value;
      refName = checked.dataset.refname;
    }
    closeFetchKeyModal();
  } else if (currentFetchParams) {
    apiKey = currentFetchParams.key;
    refId = currentFetchParams.ref;
    refName = currentFetchParams.refName;
  }

  // Use the stored baseUrl if it's not set from the drawer input (e.g., when called from the tab)
  if (!baseUrl && currentFetchParams && currentFetchParams.tempBaseUrl) {
    baseUrl = currentFetchParams.tempBaseUrl;
  }

  setStatus(`正在使用 ${refName} 拉取远程模型列表...`, '#F59E0B');
  try {
    const res = await window.pywebview.api.fetch_models(baseUrl, apiKey);
    if (!res.success) throw new Error(res.error || '拉取失败');
    
    // 如果是通过左侧列表选择（没有打开抽屉），需要获取 selectedPid
    const pid = syncCurrentFormToMemory() || selectedPid;
    const existing = (currentConfig.providers[pid] || {}).models || [];
    const existingIds = new Set(existing.map(m => m.id));
    
    fetchedPreview = res.models.map(m => ({
      id: m.id,
      name: m.name || m.id,
      contextWindow: m.contextWindow,
      maxTokens: m.maxTokens,
      input: m.input,
      reasoning: m.reasoning,
      selected: !existingIds.has(m.id),
      added: existingIds.has(m.id),
      sourceRefId: refId,
      sourceRefName: refName
    }));
    renderFetchPreview();
    switchModelWorkTab('fetch');
    closeDrawer();
    setStatus(`成功拉取到 ${res.models.length} 个模型 (${refName})，请勾选后添加`, '#10B981');
  } catch (err) {
    showAlert({ title: '拉取失败', icon: '❌', message: '拉取模型失败: ' + err.message, type: 'error' });
    setStatus('拉取模型失败: ' + err.message, '#EF4444');
  }
}

function renderFetchPreview() {
  const fetchBadge = $id('fetchTabBadge');
  const list = $id('fetchPreviewList');
  const summary = $id('fetchSummary');
  if (!fetchedPreview || fetchedPreview.length === 0) {
    if (fetchBadge) fetchBadge.style.display = 'none';
    if (list) list.innerHTML = '<div style="color:var(--b-text-3); font-size:12px; text-align:center; padding:20px 0;">暂无拉取结果</div>';
    return;
  }
  if (fetchBadge) {
    fetchBadge.style.display = 'inline-block';
    fetchBadge.innerText = String(fetchedPreview.length);
  }
  list.innerHTML = '';
  const selectedCount = fetchedPreview.filter(m => m.selected && !m.added).length;
  const addedCount = fetchedPreview.filter(m => m.added).length;
  if (summary) {
    summary.innerHTML = `<span class="summary-pill" title="共拉取 ${fetchedPreview.length} 个模型 · 已在列表 ${addedCount} 个">已选 ${selectedCount}/${fetchedPreview.length}</span>`;
  }

  const searchInput = $id('previewSearch');
  const query = searchInput ? searchInput.value.trim().toLowerCase() : '';

  fetchedPreview.forEach((m, idx) => {
    if (query) {
      const id = String(m.id || '').toLowerCase();
      const name = String(m.name || '').toLowerCase();
      if (!id.includes(query) && !name.includes(query)) return;
    }
    const row = el('div', 'model-row fetch-row' + (m.added ? ' added' : ''));
    row.dataset.mid = m.id;

    // Checkbox column (20px)
    const chkWrap = el('div', 'model-row-handle');
    const chk = el('input', 'checkbox');
    chk.type = 'checkbox';
    chk.checked = m.selected;
    chk.disabled = m.added;
    chk.style.cursor = m.added ? 'not-allowed' : 'pointer';
    chk.style.accentColor = 'var(--b-accent)';
    chk.onchange = () => { fetchedPreview[idx].selected = chk.checked; updateFetchSummary(); };
    chkWrap.appendChild(chk);

    // Alias column (200px)
    const aliasWrap = el('div', 'model-row-alias');
    const aliasInp = el('input', 'alias-input');
    aliasInp.type = 'text';
    aliasInp.value = m.name || m.id;
    aliasInp.placeholder = m.id;
    aliasInp.title = '修改导入后的显示别名';
    aliasInp.onchange = () => { fetchedPreview[idx].name = aliasInp.value.trim() || fetchedPreview[idx].id; };
    aliasWrap.appendChild(aliasInp);

    // Model ID column (flex: 1)
    const idWrap = el('div');
    idWrap.style.cssText = 'flex: 1; display: flex; align-items: center; gap: 6px; padding-left: 6px; box-sizing: border-box; min-width: 140px; overflow: hidden;';
    const idSpan = el('span', 'model-row-id', m.id);
    idSpan.title = m.id;

    // Badges in fetched view
    const badgesWrap = el('div', 'model-meta-badges');
    const ctx = m.contextWindow ? Math.round(m.contextWindow / 1000) + 'K' : '';
    if (ctx) {
      const bCtx = el('span', 'meta-badge badge-ctx', ctx);
      bCtx.title = `推断上下文: ${m.contextWindow.toLocaleString()} tokens`;
      badgesWrap.appendChild(bCtx);
    }
    const hasImage = Array.isArray(m.input) && m.input.includes('image');
    if (hasImage) {
      const bImg = el('span', 'meta-badge badge-img', '📷 视觉');
      bImg.title = '支持图像/多模态输入';
      badgesWrap.appendChild(bImg);
    }
    if (m.reasoning) {
      const bRea = el('span', 'meta-badge badge-reason', '🧠 思考');
      bRea.title = '具备深度推理思考能力';
      badgesWrap.appendChild(bRea);
    }
    idSpan.appendChild(badgesWrap);

    idWrap.appendChild(idSpan);

    if (m.sourceRefId && m.sourceRefId !== 'default') {
      const keyBadge = el('span', '', `🔑 ${m.sourceRefName}`);
      keyBadge.style.cssText = 'font-size: 10px; padding: 1px 4px; border-radius: 2px; color: #93C5FD; background: rgba(59,130,246,0.15); border: 1px solid rgba(59,130,246,0.3); white-space: nowrap;';
      idWrap.appendChild(keyBadge);
    }

    // Status column (70px)
    const statusWrap = el('div', 'model-row-status');
    if (m.added) {
      statusWrap.appendChild(el('span', 'disabled-badge', '已在列表'));
    }

    // Actions column (100px)
    const actions = el('div', 'model-row-actions');
    actions.style.width = '100px';

    const testBtn = el('button', 'model-row-btn test-btn', '⚡ 测活');
    testBtn.title = '测活：验证该模型 API 是否连通';
    testBtn.onclick = (e) => { e.stopPropagation(); testSingleFetched(testBtn, m.id); };
    actions.appendChild(testBtn);

    row.append(chkWrap, aliasWrap, idWrap, statusWrap, actions);
    list.appendChild(row);
  });
}

async function testSingleFetched(btn, mid) {
  const ctx = currentTestContext();
  if (!ctx.baseUrl) return showAlert({ title: '配置校验', icon: '⚠️', message: '请先填写 Base URL 端点' });
  
  const pid = syncCurrentFormToMemory();
  const p = currentConfig.providers[pid] || {};
  const fetchedItem = fetchedPreview.find(m => m.id === mid);
  
  let testKey = ctx.apiKey;
  let customHeaders = null;
  
  if (fetchedItem && fetchedItem.sourceRefId && fetchedItem.sourceRefId !== 'default') {
    const poolItem = (p.apiKeys || []).find(k => k.id === fetchedItem.sourceRefId);
    if (poolItem && poolItem.key) {
      testKey = poolItem.key;
      customHeaders = { Authorization: `Bearer ${poolItem.key}` };
    }
  }

  btn.classList.add('testing');
  btn.classList.remove('ok', 'fail', 'warn');
  btn.innerHTML = '⏳ 测活中';
  btn.title = '正在深度检测 API 连通性与 Agent 工具支持...';
  try {
    const res = await window.pywebview.api.test_model(ctx.baseUrl, testKey, ctx.api, mid, customHeaders);
    if (res.success) {
      if (res.agent_ready === false) {
        btn.classList.add('warn');
        btn.innerHTML = `⚠ ${res.latency_ms}ms`;
        btn.title = `基础可用，但${res.warning || '不支持工具调用'}`;
        setStatus(`⚠️ [${mid}] 仅支持纯聊 · ${res.warning || '不支持 Agent 工具'}`, '#F59E0B');
      } else {
        btn.classList.add('ok');
        btn.innerHTML = `✓ ${res.latency_ms}ms`;
        btn.title = `测活成功 · 全功能就绪(流式+工具已通过) · 延迟 ${res.latency_ms}ms`;
        setStatus(`✅ [${mid}] 测活成功(全功能可用) · ${res.latency_ms}ms`, '#10B981');
      }
    } else {
      btn.classList.add('fail');
      btn.innerHTML = '✗ 失败';
      btn.title = `失败: ${res.error || '未知错误'}`;
      setStatus(`❌ [${mid}] 测活失败: ${res.error || '未知错误'}`, '#EF4444');
    }
  } catch (e) {
    btn.classList.add('fail');
    btn.innerHTML = '✗ 异常';
    setStatus(`❌ [${mid}] 测活异常`, '#EF4444');
  }
  setTimeout(() => btn.classList.remove('testing'), 400);
}

function updateFetchSummary() {
  const summary = $id('fetchSummary');
  if (!summary || fetchedPreview.length === 0) return;
  const selectedCount = fetchedPreview.filter(m => m.selected && !m.added).length;
  const addedCount = fetchedPreview.filter(m => m.added).length;
  summary.innerHTML = `<span class="summary-pill" title="共拉取 ${fetchedPreview.length} 个模型 · 已在列表 ${addedCount} 个">已选 ${selectedCount}/${fetchedPreview.length}</span>`;
}

function toggleAllFetched(val) {
  fetchedPreview.forEach(m => { if (!m.added) m.selected = val; });
  renderFetchPreview();
}

async function testAllFetched() {
  const ctx = currentTestContext();
  if (!ctx.baseUrl) return showAlert({ title: '配置校验', icon: '⚠️', message: '请先填写 Base URL 端点' });
  setStatus(`正在批量测活 ${fetchedPreview.length} 个模型...`, '#F59E0B');
  let okCount = 0, failCount = 0;
  
  const pid = syncCurrentFormToMemory();
  const p = currentConfig.providers[pid] || {};

  const rows = document.querySelectorAll('#fetchPreviewList .fetch-row');
  for (const row of rows) {
    const mid = row.dataset.mid;
    if (!mid) continue;
    const btn = row.querySelector('.test-btn');
    if (btn) {
      btn.classList.add('testing');
      btn.classList.remove('ok', 'fail', 'warn');
      btn.innerHTML = '⏳ 测活中';
    }

    const fetchedItem = fetchedPreview.find(m => m.id === mid);
    let testKey = ctx.apiKey;
    let customHeaders = null;
    
    if (fetchedItem && fetchedItem.sourceRefId && fetchedItem.sourceRefId !== 'default') {
      const poolItem = (p.apiKeys || []).find(k => k.id === fetchedItem.sourceRefId);
      if (poolItem && poolItem.key) {
        testKey = poolItem.key;
        customHeaders = { Authorization: `Bearer ${poolItem.key}` };
      }
    }

    try {
      const res = await window.pywebview.api.test_model(ctx.baseUrl, testKey, ctx.api, mid, customHeaders);
      if (res.success) {
        okCount++;
        if (btn) {
          if (res.agent_ready === false) {
            btn.classList.add('warn');
            btn.innerHTML = `⚠ ${res.latency_ms}ms`;
            btn.title = `基础可用，但${res.warning || '不支持工具调用'}`;
          } else {
            btn.classList.add('ok');
            btn.innerHTML = `✓ ${res.latency_ms}ms`;
            btn.title = `测活成功 · 全功能就绪`;
          }
        }
      } else {
        failCount++;
        if (btn) {
          btn.classList.add('fail');
          btn.innerHTML = '✗ 失败';
          btn.title = `失败: ${res.error || '未知错误'}`;
        }
      }
    } catch (e) {
      failCount++;
      if (btn) {
        btn.classList.add('fail');
        btn.innerHTML = '✗ 异常';
      }
    }
    if (btn) btn.classList.remove('testing');
  }
  setStatus(`批量测活完成：${okCount} 可用 / ${failCount} 失败`, failCount > 0 ? '#F59E0B' : '#10B981');
}

function commitFetchedModels() {
  const pid = syncCurrentFormToMemory();
  if (!pid) return showAlert({ title: '未选择服务商', icon: '⚠️', message: '请先选择服务商' });
  const p = currentConfig.providers[pid];
  if (!p) return;
  const list = (p.models || []).slice();
  let addedCount = 0;
  fetchedPreview.forEach(m => {
    if (m.added || !m.selected) return;
    if (list.some(x => x.id === m.id)) return;
    const next = { id: m.id };
    const trimmed = String(m.name || '').trim();
    if (trimmed && trimmed !== m.id) next.name = trimmed;
    if (m.contextWindow) next.contextWindow = m.contextWindow;
    if (m.maxTokens) next.maxTokens = m.maxTokens;
    if (m.input) next.input = m.input;
    if (m.reasoning) next.reasoning = m.reasoning;

    if (m.sourceRefId && m.sourceRefId !== 'default') {
      next.apiKeyRef = m.sourceRefId;
      const pool = p.apiKeys || [];
      const poolItem = pool.find(k => k.id === m.sourceRefId);
      if (poolItem && poolItem.key) {
        next.headers = { Authorization: `Bearer ${poolItem.key}` };
      }
    }

    list.push(next);
    m.added = true;
    m.selected = false;
    addedCount++;
  });
  p.models = list;
  renderModels(p.models);
  renderSidebar();
  renderFetchPreview();
  switchModelWorkTab('models');
  if (addedCount > 0) {
    setStatus(`✅ 已添加 ${addedCount} 个模型到 [${pid}]，点击「💾 保存」持久化`, '#10B981');
  } else {
    setStatus('未选中可添加的模型', '#F59E0B');
  }
}

let currentTargets = [];
const targetStates = {};

function updateTopActionButtons(isDsh) {
  const rescanBtn = $id('rescanBuiltinsBtn');
  const orderReportBtn = $id('orderReportBtn');
  const orderPatchBtn = $id('orderPatchBtn');
  const restartPiBtn = $id('restartPiBtn');
  const importCounterpartBtn = $id('importCounterpartBtn');
  // DSH 的 llm-pi-ai 会把空字符串的 baseURL 当作致命错误，但“留空”本身是合法的
  // （目录路由用内置端点），所以用 placeholder 把这条语义写在输入框上。
  const baseUrlInp = $id('pBaseUrl');
  if (baseUrlInp) {
    baseUrlInp.placeholder = isDsh
      ? '留空 = 使用 DSH 内置目录默认端点'
      : '例如: https://api.deepseek.com/v1';
  }

  if (isDsh) {
    if (rescanBtn) rescanBtn.style.display = 'none';
    if (orderReportBtn) orderReportBtn.style.display = 'none';
    if (orderPatchBtn) orderPatchBtn.style.display = 'none';
    if (restartPiBtn) restartPiBtn.style.display = 'none';
    if (importCounterpartBtn) {
      importCounterpartBtn.style.display = 'inline-flex';
      importCounterpartBtn.innerHTML = '<span>📥 从 Pi 导入</span>';
      importCounterpartBtn.title = '从 Pi (models.json) 选择服务商导入到当前 DSH 编辑区（可单个/多选）';
    }
  } else {
    if (rescanBtn) rescanBtn.style.display = 'inline-flex';
    if (orderReportBtn) orderReportBtn.style.display = 'inline-flex';
    if (orderPatchBtn) orderPatchBtn.style.display = 'inline-flex';
    if (restartPiBtn) restartPiBtn.style.display = 'inline-flex';
    if (importCounterpartBtn) {
      importCounterpartBtn.style.display = 'none';
    }
  }
}

function captureCurrentTargetState() {
  if (!currentConfigPath) return;
  try {
    syncCurrentFormToMemory();
  } catch (e) {}

  const isCreating = (selectedPid === null || !currentConfig.providers || !currentConfig.providers[selectedPid]);
  const draft = {
    pId: $id('pId') ? $id('pId').value : '',
    pName: $id('pName') ? $id('pName').value : '',
    pBaseUrl: $id('pBaseUrl') ? $id('pBaseUrl').value : '',
    pApiKey: $id('pApiKey') ? $id('pApiKey').value : '',
    pApi: $id('pApi') ? $id('pApi').value : 'openai-completions',
  };

  targetStates[currentConfigPath] = {
    config: JSON.parse(JSON.stringify(currentConfig || { providers: {} })),
    selectedPid: selectedPid,
    isProviderEditing: isProviderEditing,
    isCreatingNew: isCreating,
    formDraft: draft
  };
}

async function restoreTargetState(targetPath) {
  const savedState = targetStates[targetPath];
  if (!savedState || !savedState.config) {
    return false;
  }

  currentConfig = savedState.config;
  currentConfigPath = targetPath;
  selectedPid = savedState.selectedPid;

  const isDsh = currentConfigPath && (currentConfigPath.includes('cordis.patch.yml') || currentConfigPath.includes('.dsh'));
  if ($id('pathDisplay')) {
    $id('pathDisplay').innerText = isDsh ? `🪐 [DSH] ${targetPath}` : `⚡ [Pi] ${targetPath}`;
  }
  updateTopActionButtons(isDsh);

  renderSidebar();
  await refreshDefaultModel();
  await initTargets();

  if (savedState.isCreatingNew) {
    newProvider();
    if (savedState.formDraft) {
      if ($id('pId')) $id('pId').value = savedState.formDraft.pId || '';
      if ($id('pName')) $id('pName').value = savedState.formDraft.pName || '';
      if ($id('pBaseUrl')) $id('pBaseUrl').value = savedState.formDraft.pBaseUrl || '';
      if ($id('pApiKey')) $id('pApiKey').value = savedState.formDraft.pApiKey || '';
      if ($id('pApi')) $id('pApi').value = savedState.formDraft.pApi || 'openai-completions';
    }
  } else if (selectedPid && currentConfig.providers && currentConfig.providers[selectedPid]) {
    selectProvider(selectedPid);
    if (savedState.isProviderEditing) {
      toggleProviderEditMode(true);
      if (savedState.formDraft) {
        if ($id('pId') && savedState.formDraft.pId) $id('pId').value = savedState.formDraft.pId;
        if ($id('pName')) $id('pName').value = savedState.formDraft.pName || '';
        if ($id('pBaseUrl')) $id('pBaseUrl').value = savedState.formDraft.pBaseUrl || '';
        if ($id('pApiKey')) $id('pApiKey').value = savedState.formDraft.pApiKey || '';
        if ($id('pApi')) $id('pApi').value = savedState.formDraft.pApi || 'openai-completions';
      }
    }
  } else {
    const pids = Object.keys(currentConfig.providers || {});
    if (pids.length > 0) selectProvider(pids[0]);
    else newProvider();
  }

  setStatus('已恢复工作区草稿', '#10B981');
  return true;
}

async function initTargets() {
  try {
    const res = await window.pywebview.api.discover_configs();
    currentTargets = (res && res.targets) || [];
    const container = $id('targetTabs');
    if (!container) return;
    if (currentTargets.length <= 1) {
      container.style.display = 'none';
      return;
    }
    container.innerHTML = currentTargets.map(t => {
      const isActive = (t.path === currentConfigPath) || (!currentConfigPath && t.id === 'pi');
      return `<button class="target-tab-btn ${isActive ? 'active' : ''}" title="${t.title || t.label}" onclick="switchTarget('${t.id}')">
        <span>${t.label}</span>
        <span class="badge-count">${t.providerCount || 0}</span>
      </button>`;
    }).join('');
    container.style.display = 'inline-flex';
  } catch (e) {
    console.error('Init targets failed:', e);
  }
}

async function switchTarget(targetId) {
  const target = currentTargets.find(t => t.id === targetId);
  if (!target || target.path === currentConfigPath) return;

  // 1. 暂存当前目标未保存的草稿状态
  captureCurrentTargetState();

  // 2. 尝试从草稿箱恢复，若没有草稿则首次从磁盘读取
  const restored = await restoreTargetState(target.path);
  if (!restored) {
    await loadData(target.path);
  }
}

// DSH 落盘时路由 id 统一小写，Pi 侧 id 区分大小写；比较前先归一化
function routeIdOf(pid) {
  return String(pid || '').trim().toLowerCase();
}

// 从对端导入：服务商勾选器（返回选中的 pid 数组；取消返回 null）
async function pickImportProviders(sourceName, targetName, importedProvs, existingByRoute) {
  const pids = Object.keys(importedProvs);
  const selection = {};
  pids.forEach(pid => { selection[pid] = true; });

  const rows = pids.map(pid => {
    const p = importedProvs[pid] || {};
    const modelCount = Array.isArray(p.models) ? p.models.length : 0;
    const clashPid = existingByRoute[routeIdOf(pid)];
    let badge = '<span class="ip-badge ip-badge-new">新增</span>';
    if (clashPid) {
      badge = clashPid === pid
        ? '<span class="ip-badge ip-badge-warn">已存在</span>'
        : `<span class="ip-badge ip-badge-warn">与 ${escapeHtml(clashPid)} 同 ID</span>`;
    }
    return `<label class="ip-row">
        <input type="checkbox" data-import-pid="${escapeHtml(pid)}" checked>
        <span class="ip-name">${escapeHtml(p.name || pid)}</span>
        <span class="ip-id">${escapeHtml(pid)}</span>
        <span class="ip-count">${modelCount} 模型</span>
        ${badge}
      </label>`;
  }).join('');

  const html = `<div class="ip-toolbar">
      <span class="ip-tip">${escapeHtml(sourceName)} → ${escapeHtml(targetName)}：勾选要导入的服务商（共 ${pids.length} 个）</span>
      <button type="button" class="ip-mini" data-pick="all">全选</button>
      <button type="button" class="ip-mini" data-pick="none">全不选</button>
    </div>
    <div class="ip-list">${rows}</div>`;

  const dlg = showConfirm({
    icon: '📥',
    title: `从 ${sourceName} 导入服务商`,
    html,
    confirmText: '导入选中',
    confirmClass: 'btn-emerald',
    wide: true
  });

  // 弹窗已同步渲染完成，此处挂监听（不依赖内联 onclick，避免 pid 特殊字符注入）
  const box = $id('globalDialogMessage');
  if (box) {
    box.querySelectorAll('input[data-import-pid]').forEach(cb => {
      cb.addEventListener('change', () => {
        selection[cb.getAttribute('data-import-pid')] = cb.checked;
      });
    });
    box.querySelectorAll('[data-pick]').forEach(btn => {
      btn.addEventListener('click', () => {
        const want = btn.getAttribute('data-pick') === 'all';
        box.querySelectorAll('input[data-import-pid]').forEach(cb => {
          cb.checked = want;
          selection[cb.getAttribute('data-import-pid')] = want;
        });
      });
    });
  }

  const ok = await dlg;
  if (!ok) return null;
  return pids.filter(pid => selection[pid]);
}

async function importFromCounterpart() {
  const isDsh = currentConfigPath && (currentConfigPath.includes('cordis.patch.yml') || currentConfigPath.includes('.dsh'));
  const targetName = isDsh ? "DSH" : "Pi";
  const sourceName = isDsh ? "Pi" : "DSH";

  setStatus(`正在从 ${sourceName} 读取配置...`, '#F59E0B');
  const res = await window.pywebview.api.import_from_counterpart(currentConfigPath);
  if (!res || !res.success || !res.config || !res.config.providers) {
    setStatus(`导入失败: ${(res && res.error) || '未读取到数据'}`, '#EF4444');
    return;
  }

  const importedProvs = res.config.providers;
  const importedKeys = Object.keys(importedProvs);
  if (importedKeys.length === 0) {
    setStatus(`⚠️ ${sourceName} 中没有可导入的服务商`, '#F59E0B');
    return;
  }

  const currentProvs = currentConfig.providers || {};
  const existingByRoute = {};
  Object.keys(currentProvs).forEach(pid => { existingByRoute[routeIdOf(pid)] = pid; });

  // 1. 选择要导入的服务商（默认全选；可只勾单个厂商）
  const picked = await pickImportProviders(sourceName, targetName, importedProvs, existingByRoute);
  if (picked === null) {
    setStatus('已取消导入', '#94A3B8');
    return;
  }
  if (picked.length === 0) {
    setStatus('未勾选任何服务商，已取消导入', '#F59E0B');
    return;
  }

  // 2. 仅有冲突时才追问覆盖策略（三态：覆盖 / 跳过冲突项 / 取消）
  const conflicts = picked.filter(pid => existingByRoute[routeIdOf(pid)]);
  let choice = conflicts.length === 0 ? 'overwrite' : 'cancel';
  if (conflicts.length > 0) {
    const shown = conflicts.slice(0, 10).join('、') + (conflicts.length > 10 ? ` 等 ${conflicts.length} 个` : '');
    const overwriteAll = await showConfirm({
      icon: '⚠️',
      title: '服务商 ID 冲突',
      message: `以下服务商在 ${targetName} 中已存在：${shown}\\n\\n覆盖：用 ${sourceName} 的配置替换 ${targetName} 同名服务商（原配置将被覆盖）。\\n跳过：保留 ${targetName} 现有配置，只导入新增服务商。`,
      confirmText: '覆盖冲突项',
      confirmClass: 'btn-rose',
      altText: '跳过冲突项',
      onAlt: () => { choice = 'skip'; }
    });
    if (overwriteAll) choice = 'overwrite';
    if (choice === 'cancel') {
      setStatus('已取消导入', '#94A3B8');
      return;
    }
  }

  // 3. 合并（只处理勾选项）
  let addCount = 0;
  let overwriteCount = 0;
  let skipCount = 0;
  for (const pid of picked) {
    const route = routeIdOf(pid);
    const existingPid = existingByRoute[route];
    if (existingPid) {
      if (choice !== 'overwrite') { skipCount++; continue; }
      // 覆盖时清掉大小写不同的旧键，避免 Pi 的 CPAMP 与 DSH 的 cpamp 并存
      if (existingPid !== pid) delete currentProvs[existingPid];
      currentProvs[pid] = JSON.parse(JSON.stringify(importedProvs[pid]));
      overwriteCount++;
      continue;
    }
    existingByRoute[route] = pid;
    currentProvs[pid] = JSON.parse(JSON.stringify(importedProvs[pid]));
    addCount++;
  }

  currentConfig.providers = currentProvs;
  renderSidebar();
  const firstPicked = picked[0];
  if (currentProvs[firstPicked]) {
    selectedPid = firstPicked;
    updateProviderViewPanel(firstPicked);
  }
  const parts = [];
  if (addCount) parts.push(`新增 ${addCount} 个`);
  if (overwriteCount) parts.push(`覆盖 ${overwriteCount} 个`);
  if (skipCount) parts.push(`跳过 ${skipCount} 个`);
  if (parts.length === 0) {
    setStatus('没有可导入的服务商（全部被跳过）', '#F59E0B');
  } else {
    setStatus(`✅ 已从 ${sourceName} 导入：${parts.join('，')}。请点击「💾 保存」写入 ${targetName}。`, '#10B981');
  }
  await initTargets();
}

// Config Load & Save
async function loadData(targetPath = null) {
  setStatus('正在加载配置...', '#F59E0B');
  const data = await window.pywebview.api.get_config(targetPath);
  currentConfig = data.config || { providers: {} };
  currentConfigPath = data.path;
  const isDsh = currentConfigPath && (currentConfigPath.includes('cordis.patch.yml') || currentConfigPath.includes('.dsh'));
  if ($id('pathDisplay')) {
    $id('pathDisplay').innerText = isDsh ? `🪐 [DSH] ${data.path}` : `⚡ [Pi] ${data.path}`;
  }

  updateTopActionButtons(isDsh);

  await loadUiPrefs();
  renderSidebar();
  await refreshDefaultModel();
  await initTargets();

  if (!isDsh) {
    // 启动时自动维护 Pi 排序补丁（仅在 Pi 模式下执行）
    try {
      const p = await window.pywebview.api.ensure_order_patch();
      const st = (p && p.status) || {};
      updateOrderPatchBtn(st);
      if (p && p.status && p.status.action === 'patched') {
        setStatus('🧩 Pi 顺序补丁已自动重打（Pi 版本变化或补丁缺失）', '#10B981');
      } else if (p && p.status && p.status.action === 'patch-failed') {
        setStatus('⚠️ Pi 顺序补丁自动重打失败: ' + (st.error || '未知错误') + '（点 🧩 顺序补丁 查看）', '#EF4444');
      }
    } catch (e) { /* 忽略 */ }
  }

  const keys = Object.keys(currentConfig.providers || {});
  const preferredPid = (data.selectedProviderId && currentConfig.providers[data.selectedProviderId])
    ? data.selectedProviderId
    : (keys.length > 0 ? keys[0] : null);

  if (preferredPid) selectProvider(preferredPid);
  else newProvider();
  setStatus('已就绪', '#10B981');
}

async function saveAll() {
  const pid = syncCurrentFormToMemory();
  setStatus('正在保存全局配置 (Ctrl+S)...', '#F59E0B');
  const res = await window.pywebview.api.save_config(currentConfig, currentConfigPath);
  if (res.success) {
    const isDsh = res.target === 'dsh' || (currentConfigPath && (currentConfigPath.includes('cordis.patch.yml') || currentConfigPath.includes('.dsh')));
    const countText = `${res.providerCount || 0} providers / ${res.modelCount || 0} models`;
    if (isDsh) {
      setStatus(`✅ 已保存 DSH 配置 (${countText}) · cordis.patch.yml + .credentials.yaml`, '#10B981');
    } else {
      setStatus(`✅ 已保存配置 (${countText})`, '#10B981');
    }
    renderSidebar();
    if (pid) {
      selectedPid = pid;
      updateProviderViewPanel(pid);
    }
    if (currentConfigPath) {
      targetStates[currentConfigPath] = {
        config: JSON.parse(JSON.stringify(currentConfig || { providers: {} })),
        selectedPid: selectedPid,
        isProviderEditing: false,
        isCreatingNew: false,
        formDraft: null
      };
    }
    await initTargets();

    // 保存后：同步 Pi 排序补丁（顺序文件 / 必要时自动重打）
    const op = res.orderPatch;
    if (op && !isDsh) {
      uiPrefs.patchPiOrder = op.action !== 'disabled';
      if (op.action === 'patched' || op.action === 'order-refreshed') {
        setStatus(`✅ 已保存 (${countText}) · 🧩 Pi 顺序补丁已同步为当前厂商顺序`,
          op.fullyPatched ? '#10B981' : '#F59E0B');
      } else if (op.action === 'patch-failed' || op.action === 'error') {
        setStatus(`✅ 已保存 (${countText}) · ⚠️ 补丁重打失败: ${op.error || '未知错误'}`, '#EF4444');
      }
      updateOrderPatchBtn();
    }
    return true;
  } else {
    setStatus('❌ 保存失败: ' + res.error, '#EF4444');
    return false;
  }
}

async function restartPi() {
  const saved = await saveAll();
  if (!saved) return;
  setStatus('正在启动新的 Pi 交互终端...', '#3B82F6');
  await window.pywebview.api.restart_pi();
  setStatus('已在新终端启动 Pi', '#10B981');
}

async function exportCurrentProvider() {
  if (!selectedPid) return setStatus('请先选择服务商', '#F59E0B');
  syncCurrentFormToMemory();
  setStatus('正在导出服务商 TXT...', '#F59E0B');
  const res = await window.pywebview.api.export_provider_txt(currentConfig, currentConfigPath, selectedPid);
  if (res.success) setStatus(`已导出 TXT：${res.path}`, '#10B981');
  else setStatus('导出失败: ' + res.error, '#EF4444');
}

async function exportAllProviders() {
  syncCurrentFormToMemory();
  setStatus('正在导出全部服务商 TXT...', '#F59E0B');
  const res = await window.pywebview.api.export_all_providers_txt(currentConfig, currentConfigPath);
  if (res.success) setStatus(`已导出全部 TXT：${res.path}`, '#10B981');
  else setStatus('导出失败: ' + res.error, '#EF4444');
}

// ==========================================================
// 底部运行日志面板（完整保留报错，不再被页脚裁切）
// ==========================================================
const LOG_LIMIT = 800;
let logEntries = [];
let logErrCount = 0;
let logUnread = 0;

function logTimestamp() {
  const d = new Date();
  const p2 = (n) => String(n).padStart(2, '0');
  return p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
}

function appendLog(level, message, detail) {
  const NL = String.fromCharCode(10);
  const lv = String(level || 'info').toLowerCase();
  let text = String(message === null || message === undefined ? '' : message);
  if (detail) {
    const d = String(detail);
    text += (text && text.slice(-1) !== NL ? NL : '') + d;
  }
  const entry = { ts: logTimestamp(), level: lv, text: text };
  logEntries.push(entry);
  if (logEntries.length > LOG_LIMIT) logEntries = logEntries.slice(-LOG_LIMIT);
  if (lv === 'err') logErrCount++;
  const panel = $id('logPanel');
  const isOpen = panel && panel.classList.contains('open');
  if (!isOpen) logUnread++;
  renderLog(entry);
  updateLogChrome();
  if (lv === 'err' || lv === 'warn') {
    safeLogError('ui:' + lv, message, detail);
  }
  return entry;
}

function logLineEl(entry) {
  const div = document.createElement('div');
  div.className = 'log-line ' + entry.level;
  const ts = document.createElement('span');
  ts.className = 'log-ts';
  ts.textContent = entry.ts;
  div.appendChild(ts);
  div.appendChild(document.createTextNode(entry.text));
  return div;
}

function renderLog(appendOnly) {
  const body = $id('logPanelBody');
  if (!body) return;
  const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
  if (!appendOnly) {
    body.textContent = '';
    if (logEntries.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'log-empty';
      empty.textContent = '暂无日志。测活/保存等操作的完整报错会实时出现在这里。';
      body.appendChild(empty);
      return;
    }
    logEntries.forEach((e) => body.appendChild(logLineEl(e)));
  } else {
    const empty = body.querySelector('.log-empty');
    if (empty) empty.remove();
    body.appendChild(logLineEl(appendOnly));
  }
  if (nearBottom || appendOnly) body.scrollTop = body.scrollHeight;
}

function updateLogChrome() {
  const badge = $id('logBadge');
  const last = $id('logLast');
  const btn = $id('footerLogBtn');
  const panel = $id('logPanel');
  const isOpen = panel && panel.classList.contains('open');
  if (badge) {
    badge.textContent = String(logEntries.length);
    badge.classList.toggle('err', logErrCount > 0);
  }
  if (last) {
    const e = logEntries[logEntries.length - 1];
    last.textContent = e ? (e.text.split(String.fromCharCode(10))[0] || '') : '暂无日志';
    last.title = e ? e.text : '';
  }
  if (btn) {
    btn.textContent = isOpen ? '▼ 日志' : '📜 日志' + (logUnread > 0 ? ' (' + logUnread + ')' : '');
    btn.classList.toggle('has-err', logErrCount > 0);
  }
  const toggle = $id('logToggleBtn');
  if (toggle) toggle.textContent = isOpen ? '▼ 收起' : '▲ 展开';
}

function toggleLogPanel(force) {
  const panel = $id('logPanel');
  if (!panel) return;
  const wantOpen = (typeof force === 'boolean') ? force : !panel.classList.contains('open');
  panel.classList.toggle('open', wantOpen);
  // 同步全局标记：让抽屉/遮罩等 fixed 元素让位，避免日志面板遮住抽屉底部按钮
  document.body.classList.toggle('log-open', wantOpen);
  if (wantOpen) {
    logUnread = 0;
    renderLog();
    const body = $id('logPanelBody');
    if (body) body.scrollTop = body.scrollHeight;
  }
  updateLogChrome();
}

function logPanelText() {
  return logEntries.map((e) => '[' + e.ts + '] [' + e.level.toUpperCase() + '] ' + e.text).join(String.fromCharCode(10));
}

function copyTextToClipboard(text, btn) {
  let done = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text);
      done = true;
    }
  } catch (e) { done = false; }
  if (!done) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done = true;
    } catch (e) { done = false; }
  }
  if (btn) {
    const old = btn.textContent;
    btn.textContent = done ? '✅ 已复制' : '⚠️ 复制失败';
    setTimeout(() => { btn.textContent = old || '📋 复制'; }, 1600);
  }
  return done;
}

function copyLogPanel() {
  const text = logPanelText();
  if (!text) { setStatus('日志为空，无可复制内容', '#F59E0B'); return; }
  copyTextToClipboard(text, null);
  setStatus('已复制 ' + logEntries.length + ' 条日志到剪贴板', '#10B981');
}

async function clearLogPanel() {
  logEntries = [];
  logErrCount = 0;
  logUnread = 0;
  renderLog();
  updateLogChrome();
  try { await window.pywebview.api.clear_error_log(); } catch (e) {}
  setStatus('日志已清空', '#10B981');
}

async function openLogFile() {
  try {
    const r = await window.pywebview.api.open_log_file();
    if (r && r.success === false) setStatus('打开日志失败: ' + (r.error || '未知错误'), '#EF4444');
  } catch (e) {
    setStatus('打开日志异常: ' + e, '#EF4444');
  }
}

async function loadHistoryLog() {
  try {
    const r = await window.pywebview.api.get_error_log(80);
    if (!r || !r.success || !r.text) return;
    appendLog('sys', '历史日志（最近 ' + r.totalLines + ' 行，文件: ' + r.path + '）', r.text.trim());
  } catch (e) {}
}

function setStatus(msg, color = '#10B981') {
  const el = $id('statusMsg');
  const text = String(msg === null || msg === undefined ? '' : msg);
  lastStatusText = text;
  if (el) {
    el.innerText = '● ' + text;
    el.style.color = color;
    el.title = text.length > 32 ? (text + String.fromCharCode(10, 10) + '点击展开底部完整日志') : text;
    el.classList.toggle('has-detail', text.length > 32);
  }
  // 同步写入底部日志面板：长报错不再只活在被裁切的页脚里
  const lv = (color === '#EF4444') ? 'err'
    : (color === '#F59E0B') ? 'warn'
    : (color === '#10B981') ? 'ok' : 'info';
  appendLog(lv, text);
}

// 点状态栏 → 展开底部完整日志
let lastStatusText = '';
function showStatusDetail() {
  toggleLogPanel(true);
}

// Resizer logic
function initResizers() {
  const resizer = $id('resizerSidebar');
  const sidebar = $id('sidebar');
  if (!resizer || !sidebar) return;
  let isResizing = false;
  let startX = 0;
  let startW = 0;

  resizer.addEventListener('mousedown', (e) => {
    isResizing = true;
    startX = e.clientX;
    startW = sidebar.offsetWidth;
    resizer.classList.add('resizing');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  window.addEventListener('mousemove', (e) => {
    if (!isResizing) return;
    const newW = Math.min(380, Math.max(220, startW + (e.clientX - startX)));
    sidebar.style.width = newW + 'px';
  });

  window.addEventListener('mouseup', () => {
    if (isResizing) {
      isResizing = false;
      resizer.classList.remove('resizing');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
  });
}

// Global Shortcuts
window.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    saveAll();
  }
  if (e.key === 'Escape') {
    closeDrawer();
  }
});

// 全局 JS 异常回写后端日志文件（避免弹窗一闪而过后无迹可寻）
function safeLogError(source, message, detail) {
  try {
    const api = window.pywebview && window.pywebview.api;
    if (api && api.log_error) {
      api.log_error(String(source), String(message), String(detail || ''));
    }
  } catch (e) {}
}

window.addEventListener('error', (ev) => {
  if (!ev) return;
  const detail = (ev.error && ev.error.stack) ? ev.error.stack : (ev.message || '');
  safeLogError('window.error', (ev.message || 'JS error') + ' @ ' + (ev.filename || '') + ':' + (ev.lineno || 0), detail);
});

window.addEventListener('unhandledrejection', (ev) => {
  const r = ev ? ev.reason : null;
  safeLogError('unhandledrejection', (r && r.message) ? r.message : String(r), (r && r.stack) ? r.stack : '');
});

// ==========================================
// 全方位窗口拖拽缩放 (8-Way Fluid Resize Controller)
// ==========================================
let _resizeState = null;

function initWindowResize(e, direction) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();

  _resizeState = {
    dir: direction || 'se',
    startX: e.screenX,
    startY: e.screenY,
    startW: window.outerWidth || window.innerWidth,
    startH: window.outerHeight || window.innerHeight,
    busy: false,
    nextW: null,
    nextH: null
  };

  let mask = document.getElementById('resizeEventMask');
  if (!mask) {
    mask = document.createElement('div');
    mask.id = 'resizeEventMask';
    mask.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:999999;background:transparent;user-select:none;';
    document.body.appendChild(mask);
  }
  const cursorMap = {
    'e': 'ew-resize', 'w': 'ew-resize',
    's': 'ns-resize', 'n': 'ns-resize',
    'se': 'nwse-resize', 'nw': 'nwse-resize',
    'sw': 'nesw-resize', 'ne': 'nesw-resize'
  };
  mask.style.cursor = cursorMap[direction] || 'nwse-resize';
  mask.style.display = 'block';

  window.addEventListener('mousemove', onWindowResizeDragging);
  window.addEventListener('mouseup', onWindowResizeDragEnd);
}

function onWindowResizeDragging(e) {
  if (!_resizeState) return;
  const dx = e.screenX - _resizeState.startX;
  const dy = e.screenY - _resizeState.startY;
  const dir = _resizeState.dir;

  let targetW = _resizeState.startW;
  let targetH = _resizeState.startH;

  if (dir.indexOf('e') !== -1) {
    targetW = Math.max(960, _resizeState.startW + dx);
  } else if (dir.indexOf('w') !== -1) {
    targetW = Math.max(960, _resizeState.startW - dx);
  }

  if (dir.indexOf('s') !== -1) {
    targetH = Math.max(580, _resizeState.startH + dy);
  } else if (dir.indexOf('n') !== -1) {
    targetH = Math.max(580, _resizeState.startH - dy);
  }

  _resizeState.nextW = targetW;
  _resizeState.nextH = targetH;

  if (!_resizeState.busy) {
    _resizeState.busy = true;
    requestAnimationFrame(doDispatchResize);
  }
}

async function doDispatchResize() {
  if (!_resizeState) return;
  const w = _resizeState.nextW;
  const h = _resizeState.nextH;
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.resize_window) {
      await window.pywebview.api.resize_window(w, h);
    }
  } catch (err) {}
  if (_resizeState) {
    _resizeState.busy = false;
    if (_resizeState.nextW !== w || _resizeState.nextH !== h) {
      _resizeState.busy = true;
      requestAnimationFrame(doDispatchResize);
    }
  }
}

function onWindowResizeDragEnd() {
  if (!_resizeState) return;
  const mask = document.getElementById('resizeEventMask');
  if (mask) mask.style.display = 'none';

  window.removeEventListener('mousemove', onWindowResizeDragging);
  window.removeEventListener('mouseup', onWindowResizeDragEnd);

  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_window_size) {
      window.pywebview.api.save_window_size(window.innerWidth, window.innerHeight);
    }
  } catch (err) {}

  _resizeState = null;
}

function handleHeaderDblClick(e) {
  if (e.target && e.target.closest('.pywebview-no-drag-region')) return;
  toggleWindowMaximize();
}

function toggleWindowMaximize() {
  try {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.maximize_window();
    }
  } catch (e) {}
}

let _winResizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(_winResizeTimer);
  _winResizeTimer = setTimeout(() => {
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.save_window_size) {
        window.pywebview.api.save_window_size(window.innerWidth, window.innerHeight);
      }
    } catch (e) {}
  }, 500);
});

window.addEventListener('pywebviewready', () => {
  initResizers();
  const statusEl = $id('statusMsg');
  if (statusEl) statusEl.addEventListener('click', showStatusDetail);
  renderLog();
  updateLogChrome();
  loadHistoryLog();
  loadData();
});
</script>

</body>
</html>
"""

def scan_tool_js_escapes(src_path=None):
    """体检：扫描 ``HTML_CONTENT`` **源码区域**中被 Python 提前解释掉的单反斜杠转义。

    背景：``HTML_CONTENT`` 是普通（非 raw）三引号字符串，源码里 JS 的 ``'\\n'``
    （单反斜杠）会被 Python 先变成真换行 → 单引号字符串未闭合 → 整个 <script>
    解析失败（界面停在静态初始态、状态栏仍是初始文字，且浏览器不报可见错误）。

    注意：必须扫**源码文本**而不是运行时字符串 —— 运行时字符串里出现单反斜杠 n
    正是我们想要的结果（合法 JS 转义）。
    返回 [(源码行号, 转义, 片段), ...]。
    """
    try:
        raw = Path(src_path or __file__).read_text(encoding="utf-8")
    except Exception:
        return []
    m = re.search(r'HTML_CONTENT\s*=\s*"""(.*?)"""', raw, re.S)
    if not m:
        return []
    region = m.group(1)
    base = raw[: m.start(1)].count("\n") + 1
    pat = re.compile(r"(?<!\\)\\[ntrfv0abxsNuU]")
    bad = []
    for i, line in enumerate(region.split("\n")):
        mm = pat.search(line)
        if mm:
            bad.append((base + i, mm.group(0), line.strip()[:120]))
    return bad


def scan_ui_missing_helpers():
    """体检：内嵌 JS 里调用的界面助手（show*/open*）是否都有定义。

    背景：``importFromCounterpart`` 曾调用从未定义的 ``showGlobalConfirm()``，
    点击后只抛 ``ReferenceError``（被 unhandledrejection 钩子写进日志），
    界面上表现为「导入按钮点了没反应」。语法门禁查不出这类幽灵调用，
    所以这里单独做一次定义存在性核对。

    返回未被定义就被调用的助手名列表。
    """
    try:
        m = re.search(r"<script>(.*?)</script>", HTML_CONTENT, re.S)
    except Exception:
        return []
    if not m:
        return []
    js = m.group(1)
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", js))
    defined |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", js))
    called = set(re.findall(r"\b((?:show|open)[A-Z][\w$]*)\s*\(", js))
    return sorted(name for name in called if name not in defined)


def verify_tool_html_js():
    """工具自身内嵌 JS 的语法门禁（node --check）。返回 (ok, msg)。

    无 node 时跳过（返回 True），不阻塞启动。
    """
    try:
        m = re.search(r"<script>(.*?)</script>", HTML_CONTENT, re.S)
    except Exception as e:
        return True, "自检异常（已跳过）: %s" % e
    if not m:
        return True, "未找到内嵌脚本块"
    tmp = Path(os.environ.get("TEMP", ".")) / "_pi_model_manager_selfcheck.js"
    try:
        _atomic_write_text(tmp, m.group(1))
        ok, msg = verify_js_syntax(tmp)
        if not ok:
            # 把 node 的行号换算回 HTML 内容行号（脚本块起始行 + 相对行号）
            offset = HTML_CONTENT[:m.start(1)].count("\n")
            rel = 0
            mm = re.search(r"(\d+)", msg or "")
            if mm:
                rel = int(mm.group(1))
            prefix = "（HTML 内容第 %d 行附近；JS 第 %d 行）" % (offset + rel, rel)
            return False, prefix + " " + msg
        return True, ""
    except Exception as e:
        return True, "自检异常（已跳过）: %s" % e
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def _run_selftest():
    """打包后/无 GUI 自检入口（exe --selftest）：不弹窗、不建窗口，退出码 0=通过。"""
    install_exception_hooks()
    ok = True
    lines = []
    try:
        bad = scan_tool_js_escapes()
        missing = scan_ui_missing_helpers()
        ok_js, msg_js = verify_tool_html_js()
        if bad:
            ok = False
            lines.append("内嵌 JS 转义隐患 %d 处: %s" % (len(bad), bad[:3]))
        if missing:
            ok = False
            lines.append("内嵌 JS 调用了未定义的函数: %s" % ", ".join(missing[:8]))
        if not ok_js:
            ok = False
            lines.append("内嵌 JS 语法校验失败: %s" % clip_text(msg_js, 2000))
        else:
            lines.append("内嵌 JS 语法校验: ✅")
        if not missing:
            lines.append("界面助手定义: ✅")
    except Exception as e:
        ok = False
        lines.append("自检异常: %r" % e)
    try:
        cfg = load_config(DEFAULT_MODELS_PATH)
        lines.append("配置读取: %d 个服务商 @ %s" % (len(cfg.get("providers") or {}), DEFAULT_MODELS_PATH))
    except Exception as e:
        ok = False
        lines.append("配置读取失败: %r" % e)
    lines.append("日志文件: %s" % LOG_PATH)
    write_log("INFO", "selftest", ("通过" if ok else "失败") + " | " + " | ".join(lines))
    return 0 if ok else 1


def main():
    install_exception_hooks()
    write_log("INFO", "app", "启动 Pi Model Manager (python %s)" % sys.version.split()[0])
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    # 启动前自检：内嵌 JS 一旦被 Python 提前转义（如 '\n' 变成真换行），
    # 整个脚本会解析失败 → 界面停在静态态。这里主动报错，不再静默失效。
    try:
        bad_esc = scan_tool_js_escapes()
        missing_helpers = scan_ui_missing_helpers()
        ok_js, msg_js = verify_tool_html_js()
    except Exception:
        bad_esc, missing_helpers, ok_js, msg_js = [], [], True, ""
    if bad_esc or missing_helpers or not ok_js:
        detail = ""
        if bad_esc:
            detail += "\n\n源码中有 %d 处会被 Python 提前解释的转义（应写成双反斜杠）：\n" % len(bad_esc)
            detail += "\n".join("  第 %d 行 %s | %s" % (ln, esc, txt) for ln, esc, txt in bad_esc[:8])
        if missing_helpers:
            detail += "\n\n以下函数被调用但从未定义（点击后会抛 ReferenceError，表现为“按钮没反应”）：\n"
            detail += "\n".join("  " + name + "()" for name in missing_helpers[:8])
        if not ok_js:
            detail += "\n\nnode --check 结果：\n" + (msg_js or "")
        for _line in detail.splitlines():
            if _line.strip():
                write_log("ERROR", "ui:selfcheck", _line.strip())
        msg = "⚠️ 工具内嵌 JS 自检失败，界面可能停在静态初始态。\n" + detail
        try:
            print(msg, file=sys.stderr, flush=True)
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "Pi Model Manager 自检警告", 0x30)
        except Exception:
            pass

    # 读取上次持久化的窗口大小（默认 1280x760 宽屏，彻底消除按钮折行截断）
    settings = load_tool_settings()
    try:
        init_w = int(settings.get("windowWidth", 1280))
        init_h = int(settings.get("windowHeight", 760))
    except Exception:
        init_w, init_h = 1280, 760
    init_w = max(960, min(2560, init_w))
    init_h = max(580, min(1600, init_h))

    api = ApiBridge()
    window = webview.create_window(
        title="模型配置",
        html=HTML_CONTENT,
        js_api=api,
        width=init_w,
        height=init_h,
        min_size=(960, 580),
        frameless=True,
        easy_drag=False,
        background_color="#0F172A"
    )
    api.set_window(window)

    def on_window_ready(win):
        try:
            time.sleep(0.4)
            native = getattr(win, "native", None)
            if native and hasattr(native, "Handle"):
                hwnd = int(native.Handle.ToInt32())
                GWL_STYLE = -16
                WS_THICKFRAME = 0x00040000
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
                if not (style & WS_THICKFRAME):
                    style |= WS_THICKFRAME
                    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
                    SWP_FRAMECHANGED = 0x0020
                    SWP_NOMOVE = 0x0002
                    SWP_NOSIZE = 0x0001
                    SWP_NOZORDER = 0x0004
                    ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                        SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)
        except Exception:
            pass

    webview.start(on_window_ready, (window,))


if __name__ == "__main__":
    main()
