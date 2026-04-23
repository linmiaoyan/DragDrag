import os
import json
import re
from flask import Flask, request, send_from_directory, jsonify, Response, make_response, render_template
from werkzeug.utils import secure_filename
import urllib.parse
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, '../download')
SECTIONS_DATA_FILE = os.path.join(BASE_DIR, 'sections_data.json')

# 确保必要的目录存在
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

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

@app.route('/')
def index():
    return render_template('index.html')

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
        base_name, ext = os.path.splitext(safe_filename)
        final_filename = safe_filename
        counter = 1
        while os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], final_filename)):
            final_filename = f"{base_name}_{counter}{ext}"
            counter += 1
        
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
    # 解码URL编码的文件名
    decoded_filename = urllib.parse.unquote(filename, encoding='utf-8')
    
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
            global sections_data
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

def main():
    app.run(host='0.0.0.0', port=80, debug=True)

if __name__ == '__main__':
    main()