# DragDrag · 校园局域网文件传输站

一个 Flask 单文件服务：拖文件上传、按分组归类、下载，页面最上方还有一块**所有人可编辑的共享剪贴板**。
校内实际部署在 `10.10.16.166`，浏览器访问 `http://10.10.16.166/`（80 端口由 haproxy 转发到后端 **30001**）。

## 功能

| 模块 | 说明 |
|---|---|
| 文件站 | 拖拽上传、分组归类、重命名、下载、删除分组 |
| 共享剪贴板 | 页面最上方，多条文本便签，谁都能加 / 改 / 删 / 复制；6 秒轮询同步；**24 小时过期**、最多 30 条、单条 4000 字 |
| 删除保护 | 只有「临时」组（id 固定为 `default`）免密；**其余组删文件 / 删分区都要管理员口令** |

### 两条安全铁律（改代码时别踩）

1. **判权限看「这个文件实际登记在哪些组里」，绝不看前端传来的 `sectionId`** —— 否则伪造 `sectionId=default` 就能删掉正式组文件。
2. **`is_temp_section()` 只认分区 id（`default`），不认名字** —— 否则把正式组改名叫「临时」就绕过了。

剪贴板是**故意免密**的：它就是给所有人临时贴文本用的，和文件删除保护是两套独立逻辑。前端全程用 `textContent` 渲染，别人贴 `<img onerror=...>` 也只当纯文本。

## 仓库里有什么 / 没有什么

本仓库**只有服务代码**，不含任何人工上传的内容：

```
drag.py                      后端全部逻辑（Flask，约 640 行）
templates/index.html         前端单页（含样式与脚本）
sections_data.example.json   空分区模板，首次部署复制成 sections_data.json
requirements.txt             Flask / Werkzeug
deploy/dragdrag.service      systemd 单元样例
tools/                       回归测试（后端 + 真浏览器探针）
```

明确**不入库**（见 `.gitignore`）：

| 类别 | 线上位置 |
|---|---|
| 人工上传的文件本体（约 14 GB / 87 个：视频、安装包、APK、ISO、PPT…） | `/mnt/data/dragdrag/download`，本来就在仓库目录之外 |
| `sections_data.json` —— 分区与文件名索引，含内部资料名 | 服务目录下，运行期生成 |
| `clipboard_data.json` —— 用户贴的临时文本 | 服务目录下，运行期生成 |
| `.admin_password` —— 管理员口令明文 | 服务目录下，`chmod 600` |
| `drag.py.bak.migrate` / `.ipynb_checkpoints` / `__pycache__` / 截图 | 残留物 |

> 口令**不写死在代码里**。优先级：环境变量 `DRAGDRAG_ADMIN_PASSWORD` → 同目录 `.admin_password` 文件 → 都没配则「一律拒绝删除」（fail-closed，不会退化成免密）。

## 部署

```bash
# 1. 放代码
sudo mkdir -p /home/ubuntu/dragdrag && cd /home/ubuntu/dragdrag
#    把 drag.py / templates/ / requirements.txt 拷进来
cp sections_data.example.json sections_data.json

# 2. 依赖（也可用系统包 python3-flask）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. 上传文件库（路径写死在 drag.py 的 DOWNLOAD_FOLDER）
sudo mkdir -p /mnt/data/dragdrag/download

# 4. 管理员口令 —— 二选一，别提交
echo '<你的口令>' | sudo tee .admin_password && sudo chmod 600 .admin_password
#   或：在 systemd 的 [Service] 里加 Environment=DRAGDRAG_ADMIN_PASSWORD=<你的口令>

# 5. 起服务
sudo cp deploy/dragdrag.service /etc/systemd/system/   # 先改里面的路径
sudo systemctl daemon-reload
sudo systemctl enable --now dragdrag
```

前端 80 端口由 haproxy 转发到 30001，仓库里不含反向代理配置。

## 回归测试

在仓库根目录运行，都需要先设口令：

```bash
export DRAGDRAG_ADMIN_PASSWORD='<你的口令>'
export DRAGDRAG_BASE=http://127.0.0.1:30001     # 可选，默认见脚本

bash tools/test_pwd.sh            # 删除权限：临时组免密 / 正式组 403 / 伪造 sectionId 无效
python tools/test_clip.py         # 剪贴板后端：增删改查、并发、过期上限、XSS
node tools/probe_pwd.mjs          # 真浏览器：密码弹窗交互（需 Edge，CDP）
node tools/probe_clip.mjs         # 真浏览器：剪贴板卡片交互与移动端布局
```

`probe_*.mjs` 用 CDP 连 Edge，每次都用独立的 `--user-data-dir` 和调试端口，避免连到上一个残留实例。

## 维护备忘

- 数据是 JSON 文件 + `threading.Lock`，写盘走 `.tmp` → `os.replace` 原子替换；**服务只能单进程跑**（systemd 直接跑 `drag.py`，没有 gunicorn 多 worker）。
- 剪贴板过期是「下次有人访问时才清理」，不是定时任务。
- 改完 `drag.py` 或 `templates/index.html` 后 `sudo systemctl restart dragdrag`。
