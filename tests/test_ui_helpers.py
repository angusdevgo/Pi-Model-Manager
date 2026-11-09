# -*- coding: utf-8 -*-
"""内嵌 JS 静态门禁回归测试。

用法:  python tests/test_ui_helpers.py

背景（真实故障）：
    DSH 页签的「📥 从 Pi 导入」按钮点击后毫无反应，日志里只有
    ``ReferenceError: showGlobalConfirm is not defined`` —— 该函数从未定义过。
    node --check 只查语法，查不出这类“幽灵函数调用”，所以这里补一条定义存在性门禁。

覆盖:
  1. 内嵌 JS 中 show*/open* 助手全部有定义
  2. HTML_CONTENT 源码里没有会被 Python 提前解释的单反斜杠转义
  3. node --check 语法通过（无 node 时自动跳过）
"""
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

# GUI / 重依赖 stub（本测试只做静态体检，不需要 pywebview / yaml）
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

import desktop_app as dapp  # noqa: E402

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %-46s %s" % (name, detail))
    else:
        FAIL += 1
        print("  FAIL  %-46s %s" % (name, detail))


def main():
    print("=" * 60)
    print("内嵌 JS 静态门禁")
    print("=" * 60)

    missing = dapp.scan_ui_missing_helpers()
    check("show*/open* 助手均有定义", not missing, "缺失: %s" % ", ".join(missing))

    bad_esc = dapp.scan_tool_js_escapes()
    check("无提前解释的转义", not bad_esc, "命中: %s" % (bad_esc[:3],))

    ok_js, msg = dapp.verify_tool_html_js()
    check("node --check 语法", ok_js, msg or "")

    # 门禁本身要能真正抓住幽灵调用（防止空转）
    original = dapp.HTML_CONTENT
    try:
        dapp.HTML_CONTENT = "<script>function showReal(){} showReal(); showGhostHelper();</script>"
        detected = dapp.scan_ui_missing_helpers()
        check("门禁能捕获未定义调用", detected == ["showGhostHelper"], "detected=%s" % detected)
    finally:
        dapp.HTML_CONTENT = original

    print("=" * 60)
    print("PASS=%d  FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
