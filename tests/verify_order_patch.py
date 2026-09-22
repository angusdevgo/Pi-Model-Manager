"""Pi 顺序补丁自检脚本（升级 Pi 后可随时复验）。

用法:  python tests/verify_order_patch.py
说明:  全流程在临时沙箱中进行，绝不触碰真实 Pi 安装目录；
       原版文件来源：补丁已打时取备份目录，未打补丁时取真实安装目录。
"""
import os, sys, json, shutil, pathlib, subprocess, hashlib, re, tempfile, time

REPO = pathlib.Path(__file__).resolve().parent.parent
REAL = pathlib.Path(os.environ.get(
    "PI_REAL_PACKAGE_DIR",
    pathlib.Path(os.environ.get("APPDATA", ""), "npm", "node_modules",
                 "@earendil-works", "pi-coding-agent")))
BACKUP = pathlib.Path(os.environ.get("PI_CODING_AGENT_DIR", pathlib.Path.home() / ".pi" / "agent")) / "pi-order-patch-backup"
SB = pathlib.Path(tempfile.mkdtemp(prefix="pi-order-patch-test-"))
pkg = (SB / "pkg").resolve()
agent = (SB / "agent").resolve()
agent.mkdir(parents=True, exist_ok=True)
targets = ["dist/bundle/chunks/chunk-4DKZACXI.js", "dist/cli/list-models.js",
           "dist/modes/interactive/components/model-selector.js",
           "dist/modes/interactive/components/settings-selector.js"]
# 目标清单：优先按内容扫描真实安装目录，扫描不到再回退到上面这份已知清单
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("dapp", REPO / "desktop_app.py")
    _dapp = importlib.util.module_from_spec(spec)
    _saved = os.environ.get("PI_PACKAGE_DIR")
    os.environ["PI_PACKAGE_DIR"] = str(REAL)
    spec.loader.exec_module(_dapp)
    if _saved is None:
        os.environ.pop("PI_PACKAGE_DIR", None)
    else:
        os.environ["PI_PACKAGE_DIR"] = _saved
    found = _dapp.find_pi_patch_targets(REAL, use_cache=False)
    if found:
        targets = [str(p.relative_to(REAL)).replace("\\", "/") for p in found]
except Exception as e:
    print("  (感知目标清单失败，使用内置清单: %s)" % e)

for rel in targets:
    bak = BACKUP / "__".join(pathlib.Path(rel).parts)
    srcf = bak if bak.exists() else REAL / rel
    assert srcf.exists(), "找不到原版文件: %s" % srcf
    (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(srcf, pkg / rel)
(pkg / "package.json").write_text(json.dumps({"name": "@earendil-works/pi-coding-agent", "version": "0.87.0", "type": "module"}), encoding="utf-8")

os.environ["PI_PACKAGE_DIR"] = str(pkg)
os.environ["PI_CODING_AGENT_DIR"] = str(agent)
sys.path.insert(0, str(REPO))
import desktop_app as D

def sha(p): return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()[:16]
orig_sha = {rel: sha(pkg / rel) for rel in targets}
ok = []
def chk(name, cond, extra=""):
    ok.append(cond)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("  " + str(extra) if extra else ""))

print("sandbox pkg :", pkg)
print("sandbox agent:", agent)
print("原版来源     :", "备份目录" if (BACKUP / "__".join(pathlib.Path(targets[0]).parts)).exists() else "真实安装目录")
print("\n[0] 初始状态")
st = D.pi_order_patch_status()
chk("目标文件 %d 个" % len(targets), st["targetCount"] == len(targets), st["targetCount"])
chk("初始 0 个已打", st["patchedCount"] == 0, st["patchedCount"])
chk("未安装补丁", st["fullyPatched"] is False)

print("\n[1] apply")
r = D.apply_pi_order_patch(None)
chk("success", r["success"], r.get("error"))
chk("applied=all skipped=0 failed=0",
    len(r["applied"]) == len(targets) and not r["skipped"] and not r["failed"] and r["replacements"] >= 6,
    "applied=%s skipped=%s failed=%s repl=%s" % (len(r["applied"]), len(r["skipped"]), len(r["failed"]), r["replacements"]))
for rel in targets:
    t = D.read_pi_source(pkg / rel)
    chk("原生锚点清零 " + rel.split("/")[-1], len(D.PI_PROVIDER_CMP_RE.findall(t)) == 0)
    chk("助手已注入 " + rel.split("/")[-1], "__piomCmp$(" in t and t.lstrip().startswith("/* pi-model-manager"))
    _sok, _smsg = D.verify_js_syntax(str(pkg / rel))
    chk("node --check 通过 " + rel.split("/")[-1], _sok, _smsg)
    b = D.PI_PATCH_BACKUP_DIR / "__".join(pathlib.Path(rel).parts)
    chk("备份与原始字节一致 " + rel.split("/")[-1], sha(b) == orig_sha[rel], sha(b) + " vs " + orig_sha[rel])

print("\n[2] 幂等性：重复 apply")
before = {rel: sha(pkg / rel) for rel in targets}
r2 = D.apply_pi_order_patch(None)
chk("skipped=all applied=0", len(r2["skipped"]) == len(targets) and not r2["applied"], "skipped=%s" % len(r2["skipped"]))
chk("文件未被改写", all(sha(pkg / rel) == before[rel] for rel in targets))
chk("助手只注入一次", all(D.read_pi_source(pkg / rel).count(D.PI_PATCH_START) == 1 and D.read_pi_source(pkg / rel).count(D.PI_PATCH_END) == 1 for rel in targets))

print("\n[3] 模拟 Pi 升级（dist 被覆盖 + 版本号变化）")
for rel in targets:
    shutil.copy2(BACKUP / "__".join(pathlib.Path(rel).parts), pkg / rel)
D.set_tool_pref("patchPiOrder", True)
chk("沙箱内开启自动维护", D.get_tool_prefs()["patchPiOrder"] is True)
js = json.loads((pkg / "package.json").read_text(encoding="utf-8")); js["version"] = "0.88.0"
(pkg / "package.json").write_text(json.dumps(js), encoding="utf-8")
st = D.pi_order_patch_status()
chk("检测到补丁丢失", st["patchedCount"] == 0)
chk("检测到版本变化", st["versionChanged"] is True, st["stateVersion"])
e = D.ensure_pi_order_patch(None)
chk("ensure 自动重打", e["action"] == "patched" and e["fullyPatched"], e["action"])
new_sha = {rel: sha(pkg / rel) for rel in targets}
chk("重打后文件已变更", new_sha != orig_sha)
for rel in targets:
    chk("备份 == 升级后原版 " + rel.split("/")[-1], sha(D.PI_PATCH_BACKUP_DIR / "__".join(pathlib.Path(rel).parts)) == orig_sha[rel])

print("\n[4] 比较器行为（node 实跑注入的助手）")
OF = D.PI_PROVIDER_ORDER_PATH
D.write_pi_provider_order(None, ["CPAMP", "WB2API", "Xiaomi", "DeepSeek", "ooioo", "Sensenova"])
src = D.read_pi_source(pkg / targets[0])
helper = src[src.index(D.PI_PATCH_START):src.index(D.PI_PATCH_END) + len(D.PI_PATCH_END)]
print("   helper bytes:", len(helper))
harness = helper + """
const mk = (a) => a.map(p => ({provider: p, id: "m"}));
const show = (a) => a.slice().sort((x, y) => __piomCmp$(x.provider, y.provider)).map(o => o.provider).join(",");
console.log("saved-order  :", show(mk(["Xiaomi","CPAMP","Sensenova","WB2API","DeepSeek","ooioo"])));
console.log("with-unknown :", show(mk(["zzz","openai","Xiaomi","CPAMP","Anthropic"])));
console.log("case-fold    :", show(mk(["sensENova","cpamp","Xiaomi"])));
"""
hp = SB / "h.mjs"; hp.write_text(harness, encoding="utf-8")
env = dict(os.environ); env["PI_CODING_AGENT_DIR"] = str(agent)
out = subprocess.run(["node", str(hp)], capture_output=True, text=True, env=env, timeout=60)
print("   " + out.stdout.strip().replace("\n", "\n   "))
chk("按顺序文件排序", "saved-order  : CPAMP,WB2API,Xiaomi,DeepSeek,ooioo,Sensenova" in out.stdout)
chk("未知厂商按字母序沉底", "with-unknown : CPAMP,Xiaomi,Anthropic,openai,zzz" in out.stdout)
chk("大小写不敏感回退", "case-fold    : cpamp,Xiaomi,sensENova" in out.stdout)
chk("助手无 import/require 顶层语句", not re.search(r"^(import|const require\s*=)", helper, re.M))

print("\n[5] 顺序文件缺失/损坏 → 回退 Pi 原生字母序")
OF.write_text("{ not json", encoding="utf-8")
out2 = subprocess.run(["node", str(hp)], capture_output=True, text=True, env=env, timeout=60)
print("   " + out2.stdout.strip().replace("\n", "\n   "))
chk("损坏文件回退字母序", "saved-order  : CPAMP,DeepSeek,ooioo,Sensenova,WB2API,Xiaomi" in out2.stdout)
OF.unlink()
out3 = subprocess.run(["node", str(hp)], capture_output=True, text=True, env=env, timeout=60)
chk("文件不存在回退字母序", "CPAMP,DeepSeek,ooioo,Sensenova,WB2API,Xiaomi" in out3.stdout)
D.write_pi_provider_order(None, ["CPAMP", "WB2API", "Xiaomi", "DeepSeek", "ooioo", "Sensenova"])

print("\n[6] 还原原版（字节级）")
rv = D.revert_pi_order_patch()
chk("success 且 reverted=all", rv["success"] and len(rv["reverted"]) == len(targets), "reverted=%s failed=%s" % (len(rv["reverted"]), rv["failed"]))
for rel in targets:
    chk("还原字节一致 " + rel.split("/")[-1], sha(pkg / rel) == orig_sha[rel], sha(pkg / rel) + " vs " + orig_sha[rel])
    chk("助手已移除 " + rel.split("/")[-1], "__piom" not in D.read_pi_source(pkg / rel) and "pi-model-manager:provider-order-patch" not in D.read_pi_source(pkg / rel))
    chk("锚点已还原 " + rel.split("/")[-1], len(D.PI_PROVIDER_CMP_RE.findall(D.read_pi_source(pkg / rel))) == len(D.PI_PROVIDER_CMP_RE.findall((BACKUP / "__".join(pathlib.Path(rel).parts)).read_text(encoding="utf-8", newline=""))))
chk("state 文件已删除", not D.PI_PATCH_STATE_PATH.exists())
chk("备份仍保留", all((D.PI_PATCH_BACKUP_DIR / "__".join(pathlib.Path(rel).parts)).exists() for rel in targets))

print("\n[7] UI 依赖的 API 方法存在性")
api = D.ApiBridge()
for name in ["get_order_patch_status", "apply_order_patch", "revert_order_patch", "ensure_order_patch"]:
    chk("ApiBridge." + name, callable(getattr(api, name, None)))

print("\n[8] 工具自身门禁（内嵌 JS 语法 / 源码转义隐患）")
BS = chr(92)
esc = D.scan_tool_js_escapes()
chk("HTML_CONTENT 源码无被 Python 提前解释的转义", esc == [], esc[:2])
js_ok, js_msg = D.verify_tool_html_js()
chk("内嵌 JS node --check 通过", bool(js_ok), (js_msg or "")[:160])
blk = re.search(r"<script>(.*?)</script>", D.HTML_CONTENT, re.S)
chk("内嵌脚本块存在", bool(blk))
for fn in ["showPiOrderPatchDialog", "buildPatchStatusText", "updateOrderPatchBtn",
           "loadData", "renderSidebar", "selectProvider"]:
    chk("脚本含 " + fn, bool(blk) and fn in blk.group(1))
fake = SB / "fake_dapp.py"
raw_src = (REPO / "desktop_app.py").read_text(encoding="utf-8")
fake.write_text(raw_src.replace("lines.join('" + BS + BS + "n')", "lines.join('" + BS + "n')", 1),
                encoding="utf-8")
chk("隐患检出能力（模拟被破坏的源码）", len(D.scan_tool_js_escapes(fake)) == 1)

print("\n[9] 性能门禁（启动不卡顿：字面量预筛 + 缓存）")
_pkg_real = D.get_pi_package_dir()
t0 = time.perf_counter()
_found = D.find_pi_patch_targets(_pkg_real, use_cache=False)
_cold = time.perf_counter() - t0
chk("冷扫描 < 1.0s（实测 %.3fs）" % _cold, _cold < 1.0)
chk("冷扫描命中文件数 > 0", len(_found) > 0, len(_found))
t0 = time.perf_counter()
D.find_pi_patch_targets(_pkg_real, use_cache=True)
_warm = time.perf_counter() - t0
chk("热取缓存 < 0.3s（实测 %.3fs）" % _warm, _warm < 0.3)
chk("字面量预筛：真锚点命中", D.text_has_provider_cmp("x.provider.localeCompare(y.provider)"))
chk("字面量预筛：不误报（缺 provider 参数）", not D.text_has_provider_cmp("x.provider.localeCompare(y)"))
chk("字面量预筛：不误报（无关键串）", not D.text_has_provider_cmp("nothing to see here" + "z" * 5000))
chk("字面量预筛：大文本不误报", not D.text_has_provider_cmp("z" * 20000 + ".provider.localeCompare(" + "y" * 20000))
_ok_hit = [f for f in _found if D.text_has_provider_cmp(D.read_pi_source(f)) or D.PI_PATCH_START in D.read_pi_source(f)]
chk("扫出的文件均含锚点或已是补丁态", len(_ok_hit) == len(_found), "%d/%d" % (len(_ok_hit), len(_found)))

print("\n[10] 长报告弹窗显示完整性（可滚动 + 可复制）")
_html = D.HTML_CONTENT
chk("弹窗容器限高（不再溢出窗口）", "max-height: 88vh" in _html)
chk("内容区可滚动", re.search(r"\.modal-body\s*\{[^}]*overflow-y:\s*auto", _html, re.S) is not None)
chk("内容区 flex 滚动修正 min-height:0", re.search(r"\.modal-body\s*\{[^}]*min-height:\s*0", _html, re.S) is not None)
chk("报告等宽样式 .modal-report", ".modal-report" in _html and "white-space: pre;" in _html)
chk("报告区限高 56vh", re.search(r"\.modal-report\s*\{[^}]*max-height:\s*56vh", _html, re.S) is not None)
chk("复制全文按钮存在", 'id="globalDialogCopyBtn"' in _html)
chk("提示行元素存在", 'id="globalDialogHint"' in _html)
chk("脚本含 applyDialogBody", "function applyDialogBody" in _html)
chk("脚本含 handleDialogCopy", "function handleDialogCopy" in _html)
chk("两个弹窗调用 applyDialogBody", _html.count("applyDialogBody(opts, msgEl);") == 2)
chk("mono 模式 2 处（补丁弹窗 / 重打提示）", _html.count("mono: true") == 2, _html.count("mono: true"))
chk("allowCopy 模式 3 处", _html.count("allowCopy: true") == 3, _html.count("allowCopy: true"))
chk("updateOrderPatchBtn 可复用状态（省一次扫描）", "async function updateOrderPatchBtn(status)" in _html)
chk("ensure_order_patch 仅 2 处调用（启动 + 偏好切换刷新）", _html.count("api.ensure_order_patch()") == 2, _html.count("api.ensure_order_patch()"))
chk("启动块已去掉 uiPrefs 前置判断（后端控开关）", "if (uiPrefs.patchPiOrder) {" not in _html)
_rep = D.ApiBridge().get_order_report()
chk("报告返回 summary（用于标题结论）", bool(_rep.get("success") and _rep.get("summary")), (_rep.get("summary") or "")[:60])
_txt = _rep.get("text") or ""
chk("报告六节 + 结论齐全（后端不截断）", all(k in _txt for k in ["【1】", "【2】", "【3】", "【4】", "【5】", "【6】", "── 结论 ──"]))
chk("报告行数 > 30（确属长文，需弹窗滚动）", _txt.count(chr(10)) > 30, _txt.count(chr(10)))

print("\n[11] 顺序自检：左右两栏对照（左 = 工具窗口，右 = Pi 实际）")
_cfg = agent / "models.json"
_order_cfg = {"providers": {
    "zulu": {"models": [{"id": "z1"}, {"id": "z2"}]},
    "alpha": {"models": [{"id": "a1"}]},
    "mike": {"models": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]},
}}
_cfg.write_text(json.dumps(_order_cfg), encoding="utf-8")
_cmp = D.build_order_compare(str(_cfg))
chk("对照数据可生成", bool(_cmp.get("success")))
chk("左栏 = 工具窗口顺序（models.json 键序）", [r["leftId"] for r in _cmp["rows"]] == ["zulu", "alpha", "mike"])
chk("行数 = 厂商数", len(_cmp["rows"]) == 3, len(_cmp["rows"]))
chk("每行带 leftModels 名称", _cmp["rows"][0]["leftModels"] == ["z1", "z2"])
chk("汇总 6 项", len(_cmp["summary"]) == 6, len(_cmp["summary"]))
chk("含禁用模型小节（值 = 无）", any(s["label"].startswith("禁用模型") for s in _cmp["summary"]))
chk("含工具自身体检小节", any(s["label"] == "工具自身体检" for s in _cmp["summary"]))
_h_raw = D.build_order_compare_html(_cmp)
chk("HTML 含两栏表格 .oc-tbl", "oc-tbl" in _h_raw)
chk("HTML 两栏用 display:contents 行", ".oc-hd, .oc-tr { display: contents; }" in D.HTML_CONTENT)
chk("HTML 左右栅头含厂商/模型计数", ("厂商 ·" in _h_raw) and ("模型" in _h_raw))
chk("HTML 汇总行数 = 6", _h_raw.count("oc-sr") >= 6, _h_raw.count("oc-sr"))

# 补丁关闭 → Pi 原生字母序，应出现逐行⚠️与 warn 结论
_status_real = D.pi_order_patch_status
D.pi_order_patch_status = lambda: {"enabled": False, "fullyPatched": False, "targetCount": 4, "patchedCount": 0,
                                   "versionChanged": False, "order": [], "piVersion": "x", "targets": []}
_c_off = D.build_order_compare(str(_cfg))
_h_off = D.build_order_compare_html(_c_off)
chk("补丁关闭：右栏 = Pi 字母序", [r["rightId"] for r in _c_off["rows"]] == ["alpha", "mike", "zulu"])
chk("patch-off: rows fully misaligned", _c_off["sameCount"] == 0, _c_off["sameCount"])
chk("补丁关闭：结论为 warn", _c_off["verdict"]["cls"] == "warn")
chk("patch-off: mismatched rows carry bad class", _h_off.count("oc-tr bad") == 3, _h_off.count("oc-tr bad"))
chk("patch-off: per-row warn badge = 3", _h_off.count('oc-badge">' + chr(9888)) == 3, _h_off.count('oc-badge">' + chr(9888)))
chk("patch-off: no ok badge", _h_off.count('oc-badge">' + chr(9989)) == 0, _h_off.count('oc-badge">' + chr(9989)))

# 补丁开启但顺序文件过期（与工具当前顺序不同）→ 应直接报「顺序文件过期」
D.pi_order_patch_status = lambda: {"enabled": True, "fullyPatched": True, "targetCount": 4, "patchedCount": 4,
                                   "versionChanged": False, "order": ["mike", "zulu", "alpha"],
                                   "piVersion": "x", "targets": []}
_c_stale = D.build_order_compare(str(_cfg))
chk("顺序文件过期：staleOrderFile = True", bool(_c_stale["staleOrderFile"]))
chk("顺序文件过期：结论提示重新同步", "同步" in _c_stale["verdict"]["text"])

# 补丁开启且顺序文件一致 → 全 ✅
D.pi_order_patch_status = lambda: {"enabled": True, "fullyPatched": True, "targetCount": 4, "patchedCount": 4,
                                   "versionChanged": False, "order": ["zulu", "alpha", "mike"],
                                   "piVersion": "x", "targets": []}
_c_on = D.build_order_compare(str(_cfg))
_h_on = D.build_order_compare_html(_c_on)
chk("补丁开启且同步：右栏 = 左栏", [r["rightId"] for r in _c_on["rows"]] == [r["leftId"] for r in _c_on["rows"]])
chk("补丁开启且同步：结论为 ok", _c_on["verdict"]["cls"] == "ok")
chk("patch-on+synced: no bad row", "oc-tr bad" not in _h_on)
chk("patch-on+synced: per-row ok badge = 3", _h_on.count('oc-badge">' + chr(9989)) == 3, _h_on.count('oc-badge">' + chr(9989)))
chk("patch-on+synced: no warn badge", _h_on.count('oc-badge">' + chr(9888)) == 0, _h_on.count('oc-badge">' + chr(9888)))
chk("补丁开启：右栏标题标注补丁生效", "补丁生效" in _c_on["piLabel"])
D.pi_order_patch_status = _status_real

# XSS：厂商名/模型名必须转义
_xss_cfg = {"providers": {"<img src=x onerror=alert(1)>": {"models": [{"id": "a\"><script>x</script>"}]}}}
(agent / "models.json").write_text(json.dumps(_xss_cfg), encoding="utf-8")
_h_x = D.build_order_compare_html(D.build_order_compare(str(agent / "models.json")))
chk("厂商名/模型名已 HTML 转义（防注入）", ("&lt;img src=x" in _h_x) and ("<img src=x" not in _h_x))
chk("模型名尖括号已转义", "<script>" not in _h_x)

_api = D.ApiBridge()
chk("ApiBridge 暴露 get_order_compare", hasattr(_api, "get_order_compare"))
_oc = _api.get_order_compare(False)
chk("get_order_compare 返回 html", bool(_oc.get("success") and len(_oc.get("html") or "") > 500), len(_oc.get("html") or ""))
chk("get_order_compare 带回完整文本报告（供复制）", "【6】" in (_oc.get("reportText") or ""))
chk("get_order_compare 支持字母序视图参数", bool(_api.get_order_compare(True).get("success")))
chk("前端改用 get_order_compare", ("api.get_order_compare(alpha)" in _html))
chk("前端新增 .modal-html 样式", ".modal-html" in _html and ("oc-verdict" in _html))

print("\n==== 结果: %d/%d 全部通过 ====" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
