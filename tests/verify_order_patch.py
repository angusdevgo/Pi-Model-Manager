"""Pi 顺序补丁自检脚本（升级 Pi 后可随时复验）。

用法:  python tests/verify_order_patch.py
说明:  全流程在临时沙箱中进行，绝不触碰真实 Pi 安装目录；
       原版文件来源：补丁已打时取备份目录，未打补丁时取真实安装目录。
"""
import os, sys, json, shutil, pathlib, subprocess, hashlib, re, tempfile

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

print("\n==== 结果: %d/%d 全部通过 ====" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
