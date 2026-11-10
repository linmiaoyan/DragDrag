#!/bin/bash
# dragdrag 删除权限测试（只用测试文件，不动真实文件）
#
# 用法：DRAGDRAG_ADMIN_PASSWORD=<管理员口令> bash tools/test_pwd.sh
#   未设置时会尝试读仓库根目录的 .admin_password（该文件不入库）。
#   仓库与线上代码里都不写死口令。
# 可选：DRAGDRAG_BASE=http://127.0.0.1:30001 指定被测地址。

ROOT=$(cd "$(dirname "$0")/.." && pwd)
H=${DRAGDRAG_BASE:-http://127.0.0.1:30001}
PW=${DRAGDRAG_ADMIN_PASSWORD:-$(cat "$ROOT/.admin_password" 2>/dev/null)}
if [ -z "$PW" ]; then
  echo "请先设置 DRAGDRAG_ADMIN_PASSWORD（或创建 .admin_password）"; exit 2
fi
export PW DRAGDRAG_ADMIN_PASSWORD="$PW"

TS=$(date +%s)
A=/tmp/pwdtest-a-$TS.txt
B=/tmp/pwdtest-b-$TS.txt
echo "hello" > $A
echo "hello" > $B

pass=0; fail=0
chk() { # chk <期望> <实际> <说明>
  if [ "$1" = "$2" ]; then pass=$((pass+1)); echo "  ✅ $3  ($2)";
  else fail=$((fail+1)); echo "  ❌ $3  期望 $1 实际 $2"; fi
}

code() { curl -s -o /tmp/_resp.txt -w '%{http_code}' "$@"; }

# 正式分区的 id 每台部署都不同，运行时查，不写死
FORMAL=$(python3 -c "
import json, urllib.request
d = json.load(urllib.request.urlopen('$H/sections'))
print(next((s['id'] for s in d if s.get('id') != 'default'), ''))
")
if [ -z "$FORMAL" ]; then echo "找不到非临时分区，无法测试"; exit 2; fi
echo "被测地址 $H / 正式分区 $FORMAL"

echo
echo "=== A. 临时组（default）：免密删除 ==="
curl -s -F "file=@$A" -F "targetSection=default" $H/upload > /dev/null
c=$(code -X POST $H/delete/pwdtest-a-$TS.txt)
chk 200 "$c" "临时组文件不填密码可删除"

echo
echo "=== B. 正式组：必须密码 ==="
curl -s -F "file=@$B" -F "targetSection=$FORMAL" $H/upload > /dev/null

c=$(code -X POST $H/delete/pwdtest-b-$TS.txt)
chk 403 "$c" "不填密码被拒"
echo "     服务端说明: $(cat /tmp/_resp.txt)"

c=$(code -X POST -H 'X-Admin-Password: wrong' $H/delete/pwdtest-b-$TS.txt)
chk 403 "$c" "错误密码被拒"

c=$(code -X POST -H "X-Admin-Password: $PW" $H/delete/pwdtest-b-$TS.txt)
chk 200 "$c" "正确密码可删除"

echo
echo "=== C. 冒充 sectionId=default 想绕过 ==="
echo "hello" > $B
curl -s -F "file=@$B" -F "targetSection=$FORMAL" $H/upload > /dev/null
c=$(code -X POST -H 'Content-Type: application/json' \
      -d '{"sectionId":"default","password":""}' \
      $H/delete/pwdtest-b-$TS.txt)
chk 403 "$c" "文件在正式组里，谎报 sectionId 无效"

c=$(code -X POST -H "X-Admin-Password: $PW" $H/delete/pwdtest-b-$TS.txt)
chk 200 "$c" "带正确密码清理完毕"

echo
echo "=== D. 删除整个分区 ==="
python3 - <<'PY'
import json, os, urllib.request, urllib.error

BASE = os.environ.get('DRAGDRAG_BASE', 'http://127.0.0.1:30001')
PW = os.environ['DRAGDRAG_ADMIN_PASSWORD']

def get_sections():
    with urllib.request.urlopen(BASE + '/sections') as r:
        return json.loads(r.read().decode())

def post_sections(data, password=None):
    body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + '/sections', data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    if password:
        req.add_header('X-Admin-Password', password)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

base = get_sections()
n0 = len(base)

# 1) 新建一个测试分区（新建不涉及删除，不该被拦）
test_sec = {'id': 'section-pwdtest-999', 'name': '权限测试分区', 'files': []}
st, _ = post_sections(base + [test_sec])
print(f'  新建测试分区: HTTP {st}  组数 {n0} -> {len(get_sections())}')

# 2) 删掉它，不带密码 -> 403
now = get_sections()
without = [s for s in now if s['id'] != 'section-pwdtest-999']
st, msg = post_sections(without)
print(f'  删除分区不带密码: HTTP {st}  {msg[:80]}')

# 3) 带正确密码 -> 200
st, _ = post_sections(without, PW)
after = get_sections()
print(f'  删除分区带正确密码: HTTP {st}  组数 {len(after)}')

# 4) 数据是否完好
ok = len(after) == n0 and [s['id'] for s in after] == [s['id'] for s in base]
print(f'  分区结构已还原: {ok}')
print(f'  文件总数: {sum(len(s["files"]) for s in base)} -> {sum(len(s["files"]) for s in after)}')
PY

echo
echo "=== E. 普通保存（拖拽/新建）不被误拦 ==="
python3 - <<'PY'
import json, os, urllib.request, urllib.error
BASE = os.environ.get('DRAGDRAG_BASE', 'http://127.0.0.1:30001')
with urllib.request.urlopen(BASE + '/sections') as r:
    data = json.loads(r.read().decode())
req = urllib.request.Request(BASE + '/sections', data=json.dumps(data).encode(), method='POST')
req.add_header('Content-Type', 'application/json')
try:
    with urllib.request.urlopen(req) as r:
        print('  无变化保存: HTTP', r.status)
except urllib.error.HTTPError as e:
    print('  无变化保存: HTTP', e.code)
PY

echo
echo "=== 结果: 通过 $pass / 失败 $fail ==="
rm -f $A $B /tmp/_resp.txt
[ "$fail" -eq 0 ]
