import os
import json
import re
import hmac
import threading
import time
import uuid
from flask import Flask, request, send_from_directory, jsonify, Response, make_response, render_template
from werkzeug.utils import secure_filename
import urllib.parse
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_FOLDER = '/mnt/data/dragdrag/download'
SECTIONS_DATA_FILE = os.path.join(BASE_DIR, 'sections_data.json')
HMDM_FILES_FOLDER = '/opt/hmdm/volumes/files'

# ---------------------------------------------------------------
# 删除权限
#   「临时」组 = 默认分区（id 固定为 default），删除免密。
#   其余分组删除文件 / 删除分区都要求管理员密码。
#   口令来源（按优先级）：
#     1) 环境变量 DRAGDRAG_ADMIN_PASSWORD
#     2) 同目录 .admin_password 文件（不入库，权限 600）
#     3) 都没配 -> 一律拒绝删除（fail-closed），不会退化成免密
#   ⚠️ 仓库里不写死口令，部署时把真实口令放到上述任一处。（2026-11-10）
#
#   ⚠️ 判定只认 **分区 id**，不认名字 —— 名字可以随时改，
#      否则把正式组改名叫「临时」就能绕过。（2026-10-19）
# ---------------------------------------------------------------
def _load_admin_password():
    pw = os.environ.get('DRAGDRAG_ADMIN_PASSWORD', '').strip()
    if pw:
        return pw
    try:
        with open(os.path.join(BASE_DIR, '.admin_password'), 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


ADMIN_PASSWORD = _load_admin_password()
TEMP_SECTION_ID = 'default'

# 确保必要的目录存在
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# Optional: expose Headwind MDM client APK for QR code generation
if os.path.isdir(HMDM_FILES_FOLDER) and not os.path.exists(HMDM_FILES_FOLDER):
    os.makedirs(HMDM_FILES_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = DOWNLOAD_FOLDER

# 分区数据管理函数
def load_sections_data():
    """从文件加载分区数据"""
    if os.path.exists(SECTIONS_DATA_FILE):
        try:
            with open(SECTIONS_DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"加载分区数据失败: {e}")
    # 返回默认分区配置
    return [{"id": "default", "name": "默认分区", "files": []}]

def save_sections_data(sections):
    """保存分区数据到文件"""
    try:
        with open(SECTIONS_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(sections, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"保存分区数据失败: {e}")
        return False

# 初始化分区数据
sections_data = load_sections_data()


def is_temp_section(section):
    """是不是「临时」组（默认分区）。None 一律按「不是临时组」处理。"""
    return bool(section) and section.get('id') == TEMP_SECTION_ID


def sections_containing(filename):
    """列出所有包含该文件的分区（同一文件可能被拖拽登记在多个分区里）。"""
    return [s for s in sections_data if filename in s.get('files', [])]


def needs_admin_password(filename):
    """删除这个文件要不要密码。

    ⚠️ 判定依据是「文件实际归属哪些分区」，**不看前端传来的 sectionId** ——
       否则构造一个 sectionId=default 的请求就能把正式组的文件删掉。
       只要文件出现在任何一个非临时分区里，就必须过密码这一关。
       文件不属于任何分区（孤儿）时也按需要密码处理，从严。
    """
    holders = sections_containing(filename)
    if not holders:
        return True
    return any(not is_temp_section(s) for s in holders)


def password_ok(supplied):
    if not supplied:
        return False
    try:
        return hmac.compare_digest(str(supplied).encode('utf-8'),
                                   ADMIN_PASSWORD.encode('utf-8'))
    except Exception:
        return False


def request_password():
    """密码可放在 X-Admin-Password 头，也可放在 JSON/表单的 password 字段。"""
    pw = request.headers.get('X-Admin-Password', '')
    if not pw:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            pw = body.get('password') or ''
        if not pw:
            pw = request.form.get('password') or ''
    return pw


def password_error(message):
    return jsonify({
        "status": "error",
        "code": "PASSWORD_REQUIRED",
        "message": message,
    }), 403


def allowed_file(filename):
    return True

def safe_filename_keep_unicode(filename):
    """
    清理文件名，但保留中文字符和其他Unicode字符
    只移除危险字符（路径分隔符、控制字符等）
    """
    # 移除路径分隔符和其他危险字符
    # 保留：字母、数字、中文、日文、韩文等Unicode字符、点、连字符、下划线、空格
    # 移除：/ \ : * ? " < > | 以及控制字符
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', filename)
    # 移除开头和结尾的空格和点
    filename = filename.strip(' .')
    # 如果文件名被清空，使用默认名称
    if not filename:
        filename = 'unnamed_file'
    return filename

def unique_filename(desired_filename: str) -> str:
    """Ensure filename unique in upload folder by appending _N."""
    base_name, ext = os.path.splitext(desired_filename)
    final_filename = desired_filename
    counter = 1
    while os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], final_filename)):
        final_filename = f"{base_name}_{counter}{ext}"
        counter += 1
    return final_filename

# ---------------------------------------------------------------
# 共享剪贴板（2026-10-26）
#   页面最上方的一块公共便签：谁都可以加、改、删，用来临时贴一段文本。
#   • 存 clipboard_data.json；读写加锁 + 原子替换（.tmp → os.replace）
#   • 超过 TTL 的条目在每个请求里顺手清掉（默认 24 小时）
#   • 单条长度、条目总数都有上限，防刷屏
#   • 这里**不设密码**：它天生就是公开协作区，
#     和「删除正式分组的文件要管理员密码」是两套东西，别混在一起
# ---------------------------------------------------------------
CLIPBOARD_DATA_FILE = os.path.join(BASE_DIR, 'clipboard_data.json')
CLIPBOARD_TTL_HOURS = int(os.environ.get('DRAGDRAG_CLIPBOARD_TTL_HOURS', '24'))
CLIPBOARD_MAX_ITEMS = int(os.environ.get('DRAGDRAG_CLIPBOARD_MAX_ITEMS', '30'))
CLIPBOARD_MAX_CHARS = int(os.environ.get('DRAGDRAG_CLIPBOARD_MAX_CHARS', '4000'))
_clipboard_lock = threading.Lock()


def _clip_now():
    return int(time.time())


def _clip_load():
    """读盘。文件坏了就当空，不要因为一条坏数据把整页拖死。"""
    if os.path.exists(CLIPBOARD_DATA_FILE):
        try:
            with open(CLIPBOARD_DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return [it for it in data if isinstance(it, dict) and it.get('id')]
            if isinstance(data, dict) and isinstance(data.get('items'), list):
                return [it for it in data['items'] if isinstance(it, dict) and it.get('id')]
        except Exception as e:
            print(f"加载剪贴板失败: {e}")
    return []


def _clip_save(items):
    """原子写：先写 .tmp 再 os.replace，避免写到一半断电/并发读到半个文件。"""
    tmp = CLIPBOARD_DATA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CLIPBOARD_DATA_FILE)


def _clip_prune(items):
    """丢掉过期的，返回 (留下的, 有没有变)。"""
    deadline = _clip_now() - CLIPBOARD_TTL_HOURS * 3600
    kept = [it for it in items if int(it.get('ts') or 0) >= deadline]
    return kept, len(kept) != len(items)


def _clip_clean_text(raw):
    """统一换行、去首尾空白，顺便挡掉 '\x00' 这类会让 JS 出问题的字符。"""
    text = (raw or '').replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '')
    return text.strip()


def _clip_public(item):
    """只吐前端要用的字段，别把内部结构漏出去。"""
    return {
        'id': item['id'],
        'text': item.get('text', ''),
        'ts': int(item.get('ts') or 0),
        'mts': int(item.get('mts') or item.get('ts') or 0),
    }


def _clip_read_all():
    """加锁读 + 清过期。返回快照（不加锁的裸数据别在外面用）。"""
    with _clipboard_lock:
        items = _clip_load()
        items, changed = _clip_prune(items)
        if len(items) > CLIPBOARD_MAX_ITEMS:
            items = items[:CLIPBOARD_MAX_ITEMS]
            changed = True
        if changed:
            try:
                _clip_save(items)
            except Exception as e:
                print(f"清理剪贴板失败: {e}")
        return items


def clipboard_error(message, code=400, http=400):
    return jsonify({"status": "error", "code": code, "message": message}), http


def _clip_body():
    """前端只发 JSON，但顺手兼容表单，省得调接口时踩坑。"""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form.to_dict() or {}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/clipboard', methods=['GET'])
def clipboard_list():
    items = _clip_read_all()
    return jsonify({
        "status": "ok",
        "items": [_clip_public(it) for it in items],
        "ttl_hours": CLIPBOARD_TTL_HOURS,
        "max_items": CLIPBOARD_MAX_ITEMS,
        "max_chars": CLIPBOARD_MAX_CHARS,
        "server_ts": _clip_now(),
    })


@app.route('/api/clipboard', methods=['POST'])
def clipboard_add():
    text = _clip_clean_text(_clip_body().get('text'))
    if not text:
        return clipboard_error('内容不能为空', 'EMPTY_TEXT')
    if len(text) > CLIPBOARD_MAX_CHARS:
        return clipboard_error(f'太长了，最多 {CLIPBOARD_MAX_CHARS} 个字符', 'TOO_LONG')
    with _clipboard_lock:
        items = _clip_load()
        items, _ = _clip_prune(items)
        if len(items) >= CLIPBOARD_MAX_ITEMS:
            return clipboard_error(
                f'剪贴板已经满 {CLIPBOARD_MAX_ITEMS} 条了，先删掉几条旧的', 'FULL', http=429)
        now = _clip_now()
        item = {'id': uuid.uuid4().hex[:12], 'text': text, 'ts': now, 'mts': now}
        items.insert(0, item)          # 新的在最前
        try:
            _clip_save(items)
        except Exception as e:
            return clipboard_error(f'保存失败: {e}', 'SAVE_FAILED', 500)
    return jsonify({"status": "ok", "item": _clip_public(item)}), 201


@app.route('/api/clipboard/<cid>', methods=['POST', 'PUT', 'PATCH'])
def clipboard_update(cid):
    text = _clip_clean_text(_clip_body().get('text'))
    if not text:
        return clipboard_error('内容不能为空', 'EMPTY_TEXT')
    if len(text) > CLIPBOARD_MAX_CHARS:
        return clipboard_error(f'太长了，最多 {CLIPBOARD_MAX_CHARS} 个字符', 'TOO_LONG')
    with _clipboard_lock:
        items, _ = _clip_prune(_clip_load())
        hit = next((it for it in items if it.get('id') == cid), None)
        if not hit:
            # 过期被清掉也会走到这里，提示「已被清掉」比「不存在」更好懂
            return clipboard_error('这条已经不在剪贴板里了（可能已过期或被删）', 'NOT_FOUND', 404)
        hit['text'] = text
        hit['mts'] = _clip_now()
        try:
            _clip_save(items)
        except Exception as e:
            return clipboard_error(f'保存失败: {e}', 'SAVE_FAILED', 500)
    return jsonify({"status": "ok", "item": _clip_public(hit)})


@app.route('/api/clipboard/<cid>', methods=['DELETE'])
def clipboard_delete(cid):
    with _clipboard_lock:
        items = _clip_load()
        left = [it for it in items if it.get('id') != cid]
        if len(left) == len(items):
            return clipboard_error('这条已经不在剪贴板里了', 'NOT_FOUND', 404)
        try:
            _clip_save(left)
        except Exception as e:
            return clipboard_error(f'保存失败: {e}', 'SAVE_FAILED', 500)
    return jsonify({"status": "ok", "deleted": cid})


@app.route('/files', methods=['GET'])
def list_files():
    """获取文件列表，正确处理中文文件名编码"""
    files = []
    try:
        # 在Windows上，os.listdir可能返回GBK编码的文件名，需要转换为UTF-8
        raw_files = os.listdir(app.config['UPLOAD_FOLDER'])
        for f in raw_files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], f)
            if os.path.isfile(file_path):
                # 尝试将文件名正确解码
                try:
                    # 如果已经是str，直接使用；如果是bytes，尝试解码
                    if isinstance(f, bytes):
                        # Windows上可能是GBK编码
                        filename = f.decode('gbk')
                    else:
                        filename = f
                    files.append(filename)
                except Exception:
                    # 如果解码失败，尝试使用原始文件名
                    files.append(f if isinstance(f, str) else f.decode('utf-8', errors='ignore'))
    except Exception as e:
        print(f"获取文件列表失败: {e}")
    return jsonify(files)

@app.route('/sections', methods=['GET'])
def get_sections():
    """获取所有分区信息"""
    global sections_data
    # 确保文件系统中的文件与分区数据同步
    sync_files_with_sections()
    return jsonify(sections_data)

@app.route('/sections', methods=['POST'])
def update_sections():
    """更新分区数据"""
    global sections_data
    try:
        new_sections = request.json
        if new_sections and isinstance(new_sections, list):
            # 确保默认分区始终存在
            if not any(s['id'] == 'default' for s in new_sections):
                default_section = {"id": "default", "name": "默认分区", "files": []}
                new_sections.insert(0, default_section)

            # —— 权限：删掉「临时」以外的分区要管理员密码 ——
            # 拖拽移动文件、新建分区、重命名组都会走这个接口，
            # 只有「有分组真的消失了」才拦，不影响正常操作。
            old_ids = {s.get('id') for s in sections_data}
            new_ids = {s.get('id') for s in new_sections}
            removed = [s for s in sections_data if s.get('id') in (old_ids - new_ids)]
            removed_protected = [s for s in removed if not is_temp_section(s)]
            if removed_protected:
                names = '、'.join((s.get('name') or s.get('id')) for s in removed_protected)
                if not password_ok(request_password()):
                    return password_error(f"删除分区「{names}」需要管理员密码")

            sections_data = new_sections
            if save_sections_data(sections_data):
                return jsonify({"status": "success"})
            else:
                return jsonify({"status": "error", "message": "保存分区数据失败"}), 500
        return jsonify({"status": "error", "message": "无效的分区数据"}), 400
    except Exception as e:
        print(f"更新分区数据失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def sync_files_with_sections():
    """同步文件系统与分区数据"""
    global sections_data
    
    # 获取文件系统中的所有文件，正确处理编码
    fs_files = set()
    try:
        raw_files = os.listdir(app.config['UPLOAD_FOLDER'])
        for f in raw_files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], f)
            if os.path.isfile(file_path):
                try:
                    if isinstance(f, bytes):
                        filename = f.decode('gbk')
                    else:
                        filename = f
                    fs_files.add(filename)
                except Exception:
                    fs_files.add(f if isinstance(f, str) else f.decode('utf-8', errors='ignore'))
    except Exception as e:
        print(f"同步文件列表失败: {e}")
    
    # 获取分区数据中记录的所有文件
    section_files = set()
    for section in sections_data:
        section_files.update(section.get('files', []))
    
    # 找出文件系统中存在但未在任何分区中记录的文件
    new_files = fs_files - section_files
    if new_files:
        # 将新文件添加到默认分区
        default_section = next((s for s in sections_data if s['id'] == 'default'), None)
        if default_section:
            default_section['files'] = default_section.get('files', []) + list(new_files)
            save_sections_data(sections_data)
    
    # 移除分区数据中记录但文件系统中不存在的文件
    missing_files = section_files - fs_files
    if missing_files:
        for section in sections_data:
            section['files'] = [f for f in section.get('files', []) if f not in missing_files]
        save_sections_data(sections_data)

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return 'No file part', 400
    file = request.files['file']
    if file.filename == '':
        return 'No selected file', 400
    if file and allowed_file(file.filename):
        # 获取原始文件名
        original_filename = file.filename
        
        # 使用新的安全文件名函数，保留中文字符
        safe_filename = safe_filename_keep_unicode(original_filename)
        
        # 确保文件名不重复
        final_filename = unique_filename(safe_filename)
        
        # 保存文件，使用UTF-8编码处理文件名
        try:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], final_filename)
            file.save(file_path)
        except Exception as e:
            print(f"保存文件失败: {e}")
            # 如果保存失败，尝试使用URL编码的文件名
            try:
                encoded_filename = urllib.parse.quote(final_filename, safe='')
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], encoded_filename)
                file.save(file_path)
                final_filename = encoded_filename
            except Exception as e2:
                print(f"使用编码文件名保存也失败: {e2}")
                return f'File save failed: {str(e2)}', 500
        
        # 获取目标分区ID
        target_section = request.form.get('targetSection', 'default')
        
        # 将文件添加到指定分区（使用最终保存的文件名）
        global sections_data
        section_found = False
        for section in sections_data:
            if section['id'] == target_section:
                if final_filename not in section['files']:
                    section['files'].append(final_filename)
                section_found = True
                break
        
        # 如果指定分区不存在，添加到默认分区
        if not section_found:
            default_section = next((s for s in sections_data if s['id'] == 'default'), None)
            if default_section and final_filename not in default_section['files']:
                default_section['files'].append(final_filename)
        
        # 保存更新后的分区数据
        save_sections_data(sections_data)
        
        return 'File uploaded successfully', 200
    return 'Invalid file', 400

@app.route('/rename', methods=['POST'])
def rename_file():
    """Rename a file in UPLOAD_FOLDER and update section references."""
    global sections_data
    try:
        data = request.get_json(force=True, silent=True) or {}
        old_name = data.get('oldName') or data.get('old') or ''
        new_name = data.get('newName') or data.get('new') or ''

        if not isinstance(old_name, str) or not isinstance(new_name, str):
            return jsonify({"status": "error", "message": "参数无效"}), 400

        old_name = old_name.strip()
        new_name = new_name.strip()
        if not old_name or not new_name:
            return jsonify({"status": "error", "message": "文件名不能为空"}), 400

        # decode if client accidentally sends url-encoded names
        old_name = urllib.parse.unquote(old_name, encoding='utf-8', errors='ignore')
        new_name = urllib.parse.unquote(new_name, encoding='utf-8', errors='ignore')

        # Sanitize new filename (keep unicode) and keep extension if user omitted it
        old_base, old_ext = os.path.splitext(old_name)
        new_sanitized = safe_filename_keep_unicode(new_name)
        new_base, new_ext = os.path.splitext(new_sanitized)
        if not new_ext and old_ext:
            new_sanitized = new_sanitized + old_ext

        if new_sanitized == old_name:
            return jsonify({"status": "success", "filename": old_name})

        src = os.path.join(app.config['UPLOAD_FOLDER'], old_name)
        if not os.path.exists(src):
            return jsonify({"status": "error", "message": "原文件不存在"}), 404

        # Avoid overwrite, generate unique name if needed
        dst_name = unique_filename(new_sanitized) if os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], new_sanitized)) else new_sanitized
        dst = os.path.join(app.config['UPLOAD_FOLDER'], dst_name)

        os.rename(src, dst)

        # Update section references
        for section in sections_data:
            files = section.get('files', [])
            section['files'] = [dst_name if f == old_name else f for f in files]
        save_sections_data(sections_data)

        return jsonify({"status": "success", "filename": dst_name})
    except Exception as e:
        print(f"重命名失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    # 解码URL编码的文件名
    decoded_filename = urllib.parse.unquote(filename, encoding='utf-8')
    
    # 尝试找到文件（可能需要处理编码问题）
    file_path = None
    try:
        # 首先尝试直接使用解码后的文件名
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], decoded_filename)
        if not os.path.exists(file_path):
            # 如果不存在，尝试在文件系统中查找匹配的文件
            # 在Windows上，文件名可能是GBK编码
            try:
                if sys.platform == 'win32':
                    # Windows上尝试GBK编码
                    gbk_filename = decoded_filename.encode('utf-8').decode('gbk', errors='ignore')
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], gbk_filename)
            except:
                pass

        if file_path and os.path.exists(file_path):
            response = make_response(send_from_directory(app.config['UPLOAD_FOLDER'], os.path.basename(file_path)))
            # 设置Content-Disposition头，确保下载时显示正确的中文文件名
            response.headers['Content-Disposition'] = f'attachment; filename*=UTF-8\'\'{urllib.parse.quote(decoded_filename, safe="")}'
            return response
        else:
            return 'File not found', 404
    except Exception as e:
        print(f"下载文件失败: {e}")
        return f'Download error: {str(e)}', 500

@app.route('/delete/<filename>', methods=['POST'])
def delete_file(filename):
    global sections_data
    # 解码URL编码的文件名
    decoded_filename = urllib.parse.unquote(filename, encoding='utf-8')

    # —— 权限：只有「临时」组里的文件免密，其余要管理员密码 ——
    if needs_admin_password(decoded_filename):
        holders = sections_containing(decoded_filename)
        label = '、'.join((s.get('name') or s.get('id')) for s in holders) or '未知分区'
        if not password_ok(request_password()):
            return password_error(f"「{label}」不是临时组，删除需要管理员密码")

    # 尝试找到并删除文件
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], decoded_filename)
    
    # 如果直接路径不存在，尝试查找匹配的文件（处理编码问题）
    if not os.path.exists(file_path):
        try:
            if sys.platform == 'win32':
                # Windows上尝试GBK编码
                gbk_filename = decoded_filename.encode('utf-8').decode('gbk', errors='ignore')
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], gbk_filename)
        except:
            pass

    if os.path.exists(file_path):
        try:
            # 删除文件
            os.remove(file_path)
            
            # 从所有分区中移除该文件（使用原始解码后的文件名）
            for section in sections_data:
                if decoded_filename in section['files']:
                    section['files'].remove(decoded_filename)
            
            # 保存更新后的分区数据
            save_sections_data(sections_data)
            
            return 'File deleted', 200
        except Exception as e:
            print(f"删除文件失败: {e}")
            return f'Delete error: {str(e)}', 500
    return 'File not found', 404

@app.route('/files/<path:filename>', methods=['GET'])
def serve_files(filename):
    """
    Serve files under /opt/hmdm/volumes/files at /files/<name>.
    Headwind MDM QR-code generation expects its client APK at /files/hmdm-<ver>-os.apk.
    """
    if not os.path.isdir(HMDM_FILES_FOLDER):
        return 'Not found', 404
    # Prevent path traversal
    safe_name = safe_filename_keep_unicode(os.path.basename(filename))
    file_path = os.path.join(HMDM_FILES_FOLDER, safe_name)
    if not os.path.exists(file_path):
        return 'Not found', 404
    return send_from_directory(HMDM_FILES_FOLDER, safe_name, as_attachment=False)

def main():
    app.run(host='0.0.0.0', port=30001, debug=True)

if __name__ == '__main__':
    main()