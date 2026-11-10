"""共享剪贴板 后端自检（走 haproxy :80，和用户真实路径一致）"""
import json
import os
import urllib.request
import urllib.error
import concurrent.futures as cf

BASE = os.environ.get("DRAGDRAG_BASE", "http://10.10.16.166")
PW = os.environ.get("DRAGDRAG_ADMIN_PASSWORD", "")
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [OK] {name} {detail}")
    else:
        fail += 1
        print(f"  [FAIL] {name} {detail}")


def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


print("== 1. 初始状态 ==")
st, d = call("/api/clipboard")
check("GET /api/clipboard 200", st == 200, f"http {st}")
check("带 ttl/max 元信息", d.get("ttl_hours") == 24 and d.get("max_items") == 30 and d.get("max_chars") == 4000,
      str({k: d.get(k) for k in ("ttl_hours", "max_items", "max_chars")}))

print("== 2. 新增 / 读取 / 编辑 / 删除 ==")
st, d = call("/api/clipboard", "POST", {"text": "第一段：会议改到周三下午 3 点"})
check("新增 201", st == 201, f"http {st} {str(d)[:80]}")
item = d.get("item") or {}
cid = item.get("id")
check("返回 id / text / ts", bool(cid) and item.get("text", "").startswith("第一段") and item.get("ts", 0) > 0,
      str({k: item.get(k) for k in ("id", "ts")}))

st, d = call("/api/clipboard")
check("列表里能看到", any(x["id"] == cid for x in d.get("items", [])), f"共 {len(d.get('items', []))} 条")
check("最新的排最前", d["items"][0]["id"] == cid)

st, d = call(f"/api/clipboard/{cid}", "POST", {"text": "第一段（已改）：改到周四"})
check("编辑成功", st == 200 and "周四" in d.get("item", {}).get("text", ""), f"http {st}")
check("mts 已更新", d["item"]["mts"] >= d["item"]["ts"])

st, d = call("/api/clipboard", "POST", {"text": "第二段：临时口令 A1B2"})
cid2 = d.get("item", {}).get("id")

print("== 3. 校验与边界 ==")
st, d = call("/api/clipboard", "POST", {"text": "   "})
check("空内容被拒 400", st == 400 and d.get("code") == "EMPTY_TEXT", f"http {st} {d.get('code')}")
st, d = call("/api/clipboard", "POST", {"text": "x" * 4001})
check("超长被拒 400", st == 400 and d.get("code") == "TOO_LONG", f"http {st} {d.get('code')}")
st, d = call("/api/clipboard/notexist1234", "POST", {"text": "hi"})
check("改不存在的条目 404", st == 404, f"http {st}")
st, d = call("/api/clipboard/notexist1234", "DELETE")
check("删不存在的条目 404", st == 404, f"http {st}")

print("== 4. 不是 XSS 注入点（原样存、原样吐，不转义成 HTML）==")
payload = "<img src=x onerror=alert(1)> & <script>alert(2)</script>"
st, d = call("/api/clipboard", "POST", {"text": payload})
xid = d.get("item", {}).get("id")
check("恶意文本原样返回（前端用 textContent 才不会执行）",
      st == 201 and d.get("item", {}).get("text") == payload, f"http {st}")

print("== 5. 并发 20 条（验证不清档、不丢条目）==")
before = len(call("/api/clipboard")[1].get("items", []))


def add_one(i):
    return call("/api/clipboard", "POST", {"text": f"并发测试 {i}"})[0]


with cf.ThreadPoolExecutor(max_workers=10) as ex:
    codes = list(ex.map(add_one, range(20)))
after = call("/api/clipboard")[1].get("items", [])
check("20 条全部写入", all(c == 201 for c in codes) and len(after) == before + 20,
      f"codes={set(codes)} 条目 {before}→{len(after)}")
check("id 无重复", len({x['id'] for x in after}) == len(after))

print("== 6. 收尾：只删自己造的测试数据（别动别人的便签）==")
mine = {cid, cid2, xid}
for x in after:
    if x["id"] in mine or x["text"].startswith(("并发测试", "第一段", "第二段", payload[:10])):
        call(f"/api/clipboard/{x['id']}", "DELETE")
left = call("/api/clipboard")[1].get("items", [])
check("测试数据已清空（保留他人内容）", all(not x["text"].startswith("并发测试") for x in left),
      f"剩 {len(left)} 条")

print("== 7. 原有功能未被影响 ==")
for p in ("/", "/sections", "/files"):
    st, _ = call(p)
    check(f"{p} 仍 200", st == 200, f"http {st}")
html = urllib.request.urlopen(BASE + "/", timeout=15).read().decode()
check("页面含共享剪贴板", "共享剪贴板" in html and 'id="clip-card"' in html)
check("页面仍含管理员密码弹窗", 'id="pwd-mask"' in html)
check("页面源码不含明文管理员口令", not PW or PW not in html)
# 页面整体本来就有 X-Admin-Password（删文件/删分区用），所以只查「剪贴板那段脚本」里有没有
marker = "存的是 JSON"
check("剪贴板脚本存在", marker in html)
clip_js = html[html.index(marker):]
clip_js = clip_js[:clip_js.index("</script>")]
check("剪贴板那一段不碰管理员密码", "X-Admin-Password" not in clip_js and "pwd-" not in clip_js,
      f"片段长度 {len(clip_js)}")

print(f"\n===== {ok} 通过 / {fail} 失败 =====")
raise SystemExit(0 if fail == 0 else 1)
