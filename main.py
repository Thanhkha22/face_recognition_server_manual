import os
import sqlite3
import datetime
import json
import io
import cv2
import numpy as np
import face_recognition
import requests
import uuid  # Thư viện tạo mã duy nhất
import threading
import time
from flask import Flask, request, jsonify, render_template, send_from_directory, Response, redirect, url_for, flash, send_file
from flask_mail import Mail, Message
from waitress import serve
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from PIL import Image

app = Flask(__name__)
# Sử dụng os.urandom(24) cho môi trường phát triển; trong sản xuất nên lưu secret key cố định
app.secret_key = '6112004'




# Cấu hình Mail
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'thanhkhazyd598@gmail.com'  # Thay bằng email của bạn
app.config['MAIL_PASSWORD'] = 'jhke mhgs dcup esxp'       # Thay bằng mật khẩu ứng dụng Gmail
app.config['MAIL_DEFAULT_SENDER'] = 'thanhkhazyd598@gmail.com'

# Cấu hình Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

mail = Mail(app)

UPLOAD_FOLDER = "uploads"
DB_NAME = "events.db"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ----------------- Khởi tạo cơ sở dữ liệu -----------------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Tạo bảng events
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            recognized_faces TEXT,
            count INTEGER,
            event_type TEXT
        )
    ''')
    # Tạo bảng faces để lưu thông tin khuôn mặt
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_id TEXT UNIQUE,
            name TEXT,
            dob TEXT,
            gender TEXT,
            timestamp TEXT
        )
    ''')
    # Tạo bảng users
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT
        )
    ''')
    conn.commit()
    conn.close()

# Gọi hàm khởi tạo cơ sở dữ liệu
init_db()

# Tạo tài khoản admin nếu chưa tồn tại
conn = sqlite3.connect(DB_NAME)
cursor = conn.cursor()
cursor.execute("SELECT * FROM users WHERE username=?", ("admin",))
if cursor.fetchone() is None:
    cursor.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                   ("admin", "123@abc", "admin"))
    conn.commit()
    print("Tài khoản admin đã được tạo!")
else:
    print("Tài khoản admin đã tồn tại!")
conn.close()

# ----------------- Các route chính -----------------
@app.route('/delete_event/<int:event_id>', methods=['DELETE'])
def delete_event(event_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
        return jsonify({"message": "Xóa sự kiện thành công!"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/download_invoice', methods=['GET'])
@login_required
def download_invoice():
    invoice_data = {
         "name": "Người dùng A",
         "entry": "2025-02-27 14:43:00",
         "exit": "2025-02-27 14:45:00",
         "fee": "10000"
    }
    content = f"Hóa đơn cho: {invoice_data['name']}\n"
    content += f"Check-in: {invoice_data['entry']}\n"
    content += f"Check-out: {invoice_data['exit']}\n"
    content += f"Phí phiên: {invoice_data['fee']} VNĐ\n"
    
    filename = "invoice.txt"
    with open(filename, "w", encoding="utf-8") as f:
         f.write(content)
    return send_file(filename, as_attachment=True)

@app.route('/calculate_fee', methods=['GET'])
@login_required
def calculate_fee():
    fee_rate = 5000 / 720  # VNĐ mỗi phút, với 12 tiếng tương đương 5000 đồng
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT timestamp, recognized_faces, event_type FROM events WHERE event_type IN ('Vào', 'Ra') ORDER BY timestamp ASC")
    rows = cursor.fetchall()
    conn.close()
    
    fees = {}
    for row in rows:
        timestamp_str, faces_json, event_type = row
        try:
            face_data = json.loads(faces_json)
        except:
            continue
        face_id = face_data.get("face_id")
        name = face_data.get("name")
        timestamp_dt = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        
        if face_id not in fees:
            fees[face_id] = {"name": name, "sessions": [], "current_entry": None, "total_fee": 0}
        
        if event_type == "Vào":
            fees[face_id]["current_entry"] = timestamp_dt
        elif event_type == "Ra":
            if fees[face_id]["current_entry"] is not None:
                entry_time = fees[face_id]["current_entry"]
                exit_time = timestamp_dt
                duration_seconds = (exit_time - entry_time).total_seconds()
                duration_minutes = duration_seconds / 60
                session_fee = duration_minutes * fee_rate
                fees[face_id]["sessions"].append({
                    "entry": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "exit": exit_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "duration_seconds": duration_seconds,
                    "fee": round(session_fee, 2)
                })
                fees[face_id]["total_fee"] += session_fee
                fees[face_id]["current_entry"] = None
    for face_id, data in fees.items():
        data["total_fee"] = round(data["total_fee"], 2)
        if "current_entry" in data:
            del data["current_entry"]
    return jsonify(fees)

@app.route('/upload', methods=['POST'])
def upload():
    try:
        image_data = request.data
        img = Image.open(io.BytesIO(image_data))
        img = img.convert("RGB")
        img_np = np.array(img)
        img_np = np.ascontiguousarray(img_np)

        # Phát hiện khuôn mặt
        face_locations = face_recognition.face_locations(img_np, model="hog")
        if not face_locations:
            return jsonify({"error": "Không phát hiện khuôn mặt"}), 400

        # Cắt khuôn mặt đầu tiên
        # Cắt khuôn mặt đầu tiên
        top, right, bottom, left = face_locations[0]

        # Tính chiều rộng / chiều cao vùng mặt
        face_height = bottom - top
        face_width = right - left

        # Thêm margin = 20% chiều cao / chiều rộng (tuỳ ý bạn)
        margin_h = int(face_height * 0.2)
        margin_w = int(face_width * 0.2)

        # Nới toạ độ, đồng thời đảm bảo không vượt ra ngoài kích thước ảnh
        top    = max(0, top - margin_h)
        bottom = min(img_np.shape[0], bottom + margin_h)
        left   = max(0, left - margin_w)
        right  = min(img_np.shape[1], right + margin_w)

        face_img = img_np[top:bottom, left:right]

        # Tính toán encoding của khuôn mặt mới
        encodings = face_recognition.face_encodings(face_img)
        if not encodings:
            return jsonify({"error": "Không thể lấy thông tin khuôn mặt"}), 400
        new_face_encoding = encodings[0]

        duplicate_found = False
        duplicate_face_path = None
        for file in os.listdir(UPLOAD_FOLDER):
            file_path = os.path.join(UPLOAD_FOLDER, file)
            try:
                existing_image = face_recognition.load_image_file(file_path)
                existing_face_locations = face_recognition.face_locations(existing_image, model="hog")
                if not existing_face_locations:
                    continue
                existing_encodings = face_recognition.face_encodings(existing_image, known_face_locations=existing_face_locations)
                for existing_encoding in existing_encodings:
                    results = face_recognition.compare_faces([existing_encoding], new_face_encoding, tolerance=0.4)
                    if results[0]:
                        duplicate_found = True
                        duplicate_face_path = file_path
                        break
                if duplicate_found:
                    break
            except Exception:
                continue

        if duplicate_found:
            base_name = os.path.basename(duplicate_face_path)
            splitted = base_name.split("_", 1)
            matched_face_id = splitted[0]
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("SELECT name, dob, gender FROM faces WHERE face_id=?", (matched_face_id,))
            row = c.fetchone()
            conn.close()
            if row:
                name, dob, gender = row
            else:
                name, dob, gender = "", "", ""
            face_url = url_for('uploaded_file', filename=base_name)
            return jsonify({
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
                "face_image": face_url,
                "face_id": matched_face_id,
                "name": name,
                "dob": dob,
                "gender": gender
            })

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        face_id = str(uuid.uuid4())
        face_filename = os.path.join(UPLOAD_FOLDER, f"{face_id}_{timestamp}.jpg")
        Image.fromarray(face_img).save(face_filename)
        face_url = url_for('uploaded_file', filename=os.path.basename(face_filename))
        return jsonify({
            "timestamp": timestamp,
            "face_image": face_url,
            "face_id": face_id
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/save_info', methods=['POST'])
def save_info():
    data = request.json
    face_id = data.get("face_id")
    name = data.get("name")
    dob = data.get("dob")
    gender = data.get("gender")
    face_image = data.get("face_image")
    event_type = data.get("event_type")
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not face_id or not face_image or not event_type:
        return jsonify({"error": "Thiếu dữ liệu cần thiết!"}), 400

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT face_id FROM faces WHERE face_id=?", (face_id,))
        exist_row = cursor.fetchone()
        if exist_row:
            face_info = {
                "face_id": face_id,
                "name": name,
                "dob": dob,
                "gender": gender,
                "face_image": face_image
            }
            cursor.execute(
                "INSERT INTO events (timestamp, recognized_faces, count, event_type) VALUES (?, ?, ?, ?)",
                (timestamp, json.dumps(face_info), 1, event_type)
            )
            conn.commit()
            message = "Khuôn mặt cũ, đã ghi nhận sự kiện {}.".format(event_type)
        else:
            cursor.execute(
                "INSERT INTO faces (face_id, name, dob, gender, timestamp) VALUES (?, ?, ?, ?, ?)",
                (face_id, name, dob, gender, timestamp)
            )
            conn.commit()
            face_info = {
                "face_id": face_id,
                "name": name,
                "dob": dob,
                "gender": gender,
                "face_image": face_image
            }
            cursor.execute(
                "INSERT INTO events (timestamp, recognized_faces, count, event_type) VALUES (?, ?, ?, ?)",
                (timestamp, json.dumps(face_info), 1, event_type)
            )
            conn.commit()
            message = "Thông tin khuôn mặt mới đã được lưu thành công!"
    except sqlite3.IntegrityError:
        message = "Mã khuôn mặt đã tồn tại!"
    finally:
        conn.close()

    return jsonify({"message": message}), 200

@app.route('/get_current_info', methods=['GET'])
def get_current_info():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM events WHERE event_type = 'Vào'")
    count_in = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM events WHERE event_type = 'Ra'")
    count_out = cursor.fetchone()[0]
    current_count = count_in - count_out
    cursor.execute("SELECT timestamp FROM events ORDER BY timestamp DESC LIMIT 1")
    last_update_row = cursor.fetchone()
    last_update = last_update_row[0] if last_update_row else "Chưa có dữ liệu"
    cursor.execute("SELECT timestamp, recognized_faces, event_type FROM events WHERE event_type IN ('Vào', 'Ra') ORDER BY timestamp DESC LIMIT 5")
    rows = cursor.fetchall()
    history = []
    for row in rows:
        try:
            recognized_faces = json.loads(row[1])
        except:
            recognized_faces = {}
        history.append({
            "timestamp": row[0],
            "recognized_faces": recognized_faces,
            "event_type": row[2]
        })
    conn.close()
    return jsonify({
        "current_count": current_count,
        "last_update": last_update,
        "history": history
    })


RECIPIENTS = ["khapt.22th@sv.dla.edu.vn", "phucnt.22th@sv.dla.edu.vn"]
@app.route("/send_support_email", methods=["POST"])
def send_support_email():
    data = request.json
    name = data.get("name")
    email = data.get("email")
    message = data.get("message")
    if not name or not email or not message:
        return jsonify({"error": "Thiếu thông tin"}), 400
    subject = f"Yêu cầu hỗ trợ từ {name}"
    body = f"Tên: {name}\nEmail: {email}\n\nNội dung:\n{message}"
    try:
        msg = Message(subject, recipients=RECIPIENTS, body=body)
        mail.send(msg)
        return jsonify({"message": "Email đã được gửi thành công!"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------- Flask-Login: Quản lý tài khoản -----------------
class User(UserMixin):
    def __init__(self, id, username, password, role):
        self.id = id
        self.username = username
        self.password = password
        self.role = role

def get_user_by_id(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password, role FROM users WHERE id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return User(*row)
    return None

def get_user_by_username(username):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password, role FROM users WHERE username=?", (username,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return User(*row)
    return None

@login_manager.user_loader
def load_user(user_id):
    return get_user_by_id(user_id)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = get_user_by_username(username)
        if user and user.password == password:
            login_user(user)
            flash("Đăng nhập thành công!", "success")
            return redirect(url_for('index'))
        else:
            flash("Tên đăng nhập hoặc mật khẩu không đúng!")
    return render_template('login.html')

@app.route('/create_account', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if not username or not password or not confirm_password:
            flash("Vui lòng điền đầy đủ thông tin!")
            return render_template('create_account.html')
        if password != confirm_password:
            flash("Mật khẩu xác nhận không khớp!")
            return render_template('create_account.html')
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                           (username, password, 'user'))
            conn.commit()
            flash("Tạo tài khoản thành công! Vui lòng đăng nhập.", "success")
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash("Tên đăng nhập đã tồn tại!")
        finally:
            conn.close()
    return render_template('create_account.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/account_management')
@login_required
def account_management():
    if current_user.role != "admin":
        flash("Bạn không có quyền truy cập trang này!")
        return redirect(url_for('index'))
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password, role FROM users")
    rows = cursor.fetchall()
    conn.close()
    users = [{"id": row[0], "username": row[1], "password": row[2], "role": row[3]} for row in rows]
    return render_template('account_management.html', users=users)

@app.route('/add_account', methods=['POST'])
@login_required
def add_account():
    if current_user.role != "admin":
        flash("Bạn không có quyền thực hiện thao tác này!")
        return redirect(url_for('index'))
    username = request.form.get('username')
    password = request.form.get('password')
    role = request.form.get('role')
    if not username or not password or not role:
        flash("Vui lòng điền đầy đủ thông tin!")
        return redirect(url_for('account_management'))
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                       (username, password, role))
        conn.commit()
        flash("Thêm tài khoản thành công!", "success")
    except sqlite3.IntegrityError:
        flash("Tên đăng nhập đã tồn tại!")
    finally:
        conn.close()
    return redirect(url_for('account_management'))

@app.route('/edit_account/<int:user_id>', methods=['GET', 'POST'])
@login_required
def edit_account(user_id):
    if current_user.role != "admin":
        flash("Bạn không có quyền thực hiện thao tác này!")
        return redirect(url_for('index'))
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')
        if not username or not password or not role:
            flash("Vui lòng điền đầy đủ thông tin!")
            return redirect(url_for('edit_account', user_id=user_id))
        try:
            cursor.execute("UPDATE users SET username=?, password=?, role=? WHERE id=?",
                           (username, password, role, user_id))
            conn.commit()
            flash("Cập nhật tài khoản thành công!", "success")
        except sqlite3.IntegrityError:
            flash("Tên đăng nhập đã tồn tại!")
        finally:
            conn.close()
        return redirect(url_for('account_management'))
    else:
        cursor.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            user = {"id": row[0], "username": row[1], "role": row[2]}
            return render_template('edit_account.html', user=user)
        else:
            flash("Không tìm thấy tài khoản!")
            return redirect(url_for('account_management'))

@app.route('/delete_account/<int:user_id>')
@login_required
def delete_account(user_id):
    if current_user.role != "admin":
        flash("Bạn không có quyền thực hiện thao tác này!")
        return redirect(url_for('index'))
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    flash("Xóa tài khoản thành công!", "success")
    return redirect(url_for('account_management'))

@app.route('/history')
@login_required
def history():
    return render_template('history.html')

@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html')

@app.route('/support')
@login_required
def support():
    return render_template('support.html')

@app.route('/face_management')
@login_required
def face_management():
    return render_template('face_management.html')

@app.route('/report')
@login_required
def report():
    return render_template('report.html')

@app.route('/footer')
@login_required
def footer():
    return render_template('footer.html')

@app.route('/header')
@login_required
def header():
    return render_template('header.html')

@app.route('/models/<path:filename>')
def serve_models(filename):
    return send_from_directory('models', filename)


@app.route('/report/daily_counts')
@login_required
def daily_counts():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT DATE(timestamp) AS day, COUNT(*) FROM events GROUP BY DATE(timestamp)")
    rows = cursor.fetchall()
    conn.close()
    data = [{"day": row[0], "count": row[1]} for row in rows]
    return jsonify(data)

@app.route('/report/in_out_counts')
@login_required
def in_out_counts():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT event_type, COUNT(*) FROM events GROUP BY event_type")
    rows = cursor.fetchall()
    conn.close()
    data = {row[0]: row[1] for row in rows}
    return jsonify(data)

@app.route('/update_event', methods=['POST'])
def update_event():
    data = request.json
    event_id = data.get('id')
    if not event_id:
        return jsonify({"error": "Thiếu ID sự kiện"}), 400

    recognized_faces = data.get('recognized_faces', {})
    event_type = data.get('event_type')

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT recognized_faces FROM events WHERE id=?", (event_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": f"Không tìm thấy sự kiện với ID = {event_id}"}), 404

    faces_json = json.dumps(recognized_faces)
    cursor.execute("""
        UPDATE events 
        SET recognized_faces = ?, event_type = ?
        WHERE id = ?
    """, (faces_json, event_type, event_id))
    conn.commit()
    conn.close()

    return jsonify({"message": "Cập nhật thành công!"}), 200

@app.route('/get_history', methods=['GET'])
def get_history():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, timestamp, recognized_faces, count, event_type FROM events ORDER BY timestamp DESC")
    rows = cursor.fetchall()
    conn.close()

    history = []
    for row in rows:
        try:
            faces = json.loads(row[2])
        except Exception:
            faces = {}
        history.append({
            "id": row[0],
            "timestamp": row[1],
            "recognized_faces": faces,
            "count": row[3],
            "event_type": row[4]
        })
    return jsonify(history)

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')

#trả về khuôn mặt đã biếtt
@app.route('/get_known_faces', methods=['GET'])
def get_known_faces():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT face_id, name FROM faces")
    rows = cursor.fetchall()
    conn.close()
    known_faces = []
    # Duyệt từng row, tìm file ảnh trong 'uploads'
    for face_id, name in rows:
        for file in os.listdir(UPLOAD_FOLDER):
            if file.startswith(face_id + "_"):
                image_url = url_for('uploaded_file', filename=file)
                known_faces.append({
                    "face_id": face_id,
                    "name": name,
                    "image_url": image_url
                })
                break
    return jsonify(known_faces)




# nhận diện thủ công 
# Hàm nạp danh sách khuôn mặt đã biết từ thư mục UPLOAD_FOLDER
def load_known_faces():
    known_face_encodings = []
    known_face_names = []
    for file in os.listdir(UPLOAD_FOLDER):
        # Chỉ xử lý file ảnh
        if not (file.lower().endswith(".jpg") or file.lower().endswith(".png")):
            continue
        file_path = os.path.join(UPLOAD_FOLDER, file)
        # Giả sử tên file có dạng: face_id_timestamp.jpg, lấy phần trước dấu "_" làm face_id
        face_id = file.split("_", 1)[0]
        try:
            image = face_recognition.load_image_file(file_path)
            encodings = face_recognition.face_encodings(image)
            if encodings:
                known_face_encodings.append(encodings[0])
                # Truy vấn DB để lấy tên theo face_id
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM faces WHERE face_id=?", (face_id,))
                row = cursor.fetchone()
                conn.close()
                if row and row[0]:
                    known_face_names.append(row[0])
                else:
                    known_face_names.append("Unknown")
            else:
                print(f"Không tìm thấy encoding trong file: {file}")
        except Exception as e:
            print(f"Lỗi khi tải ảnh {file}: {e}")
            continue
    print("Loaded {} known faces: {}".format(len(known_face_encodings), known_face_names))
    return known_face_encodings, known_face_names

# Gọi hàm nạp khuôn mặt trước khi chạy app
known_face_encodings, known_face_names = load_known_faces()

# Route /video_feed cập nhật với debug và bounding box theo độ giống
@app.route('/video_feed')
def video_feed():
    def generate():
        print("Đang chạy route /video_feed")
        cap = cv2.VideoCapture(1)
        if not cap.isOpened():
            print("Không mở được webcam!")
            return
        # Đặt độ phân giải thấp hơn cho việc debug (có thể chỉnh lại sau)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Không lấy được frame")
                break

            # Nếu cần, bạn có thể resize lại frame để tăng tốc xử lý
            frame = cv2.resize(frame, (640, 480))
            # Chuyển đổi từ BGR sang RGB để thư viện face_recognition làm việc
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            # Phát hiện vị trí khuôn mặt
            face_locations = face_recognition.face_locations(rgb_frame, model="hog")
            print("Số khuôn mặt tìm thấy:", len(face_locations))
            # Tính encoding cho các khuôn mặt được phát hiện
            face_encodings = face_recognition.face_encodings(rgb_frame, face_locations)
            
            # Xử lý từng khuôn mặt
            for (top, right, bottom, left), face_encoding in zip(face_locations, face_encodings):
                # Tính khoảng cách giữa encoding của khuôn mặt hiện tại và danh sách đã biết
                distances = face_recognition.face_distance(known_face_encodings, face_encoding)
                threshold = 0.25  # Điều chỉnh ngưỡng cho phù hợp
                if len(distances) > 0:
                    min_distance = np.min(distances)
                    best_match_index = np.argmin(distances)
                    print("Khoảng cách min:", min_distance)
                    # Nếu khoảng cách nhỏ hơn ngưỡng thì coi là match
                    if min_distance < threshold:
                        name = known_face_names[best_match_index]
                        color = (0, 255, 0)  # Khung xanh cho khuôn mặt đã biết
                        cv2.putText(frame, name, (left, top - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
                    else:
                        color = (0, 0, 255)  # Khung đỏ cho người lạ
                else:
                    color = (0, 0, 255)
                
                # Vẽ bounding box quanh khuôn mặt
                cv2.rectangle(frame, (left, top), (right, bottom), color, 2)

            # Mã hóa frame thành JPEG và gửi về client
            ret, jpeg = cv2.imencode('.jpg', frame)
            if ret:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        cap.release()
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

# ----------------- Tự động hóa nhận diện khuôn mặt (Background Thread) -----------------

# def auto_face_recognition():
#     cap = cv2.VideoCapture(1)  # Sử dụng webcam ngoài; điều chỉnh chỉ số nếu cần
#     cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
#     cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
#     while True:
#         ret, frame = cap.read()
#         if not ret:
#             continue
#         # Chuyển đổi frame sang RGB
#         rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
#         face_locations = face_recognition.face_locations(rgb_frame, model="hog")
#         if face_locations:
#             # Xử lý khuôn mặt đầu tiên
#             top, right, bottom, left = face_locations[0]
#             face_img = rgb_frame[top:bottom, left:right]
#             encodings = face_recognition.face_encodings(face_img)
#             if not encodings:
#                 continue
#             new_face_encoding = encodings[0]

#             duplicate_found = False
#             duplicate_face_path = None
#             for file in os.listdir(UPLOAD_FOLDER):
#                 file_path = os.path.join(UPLOAD_FOLDER, file)
#                 try:
#                     existing_image = face_recognition.load_image_file(file_path)
#                     existing_locations = face_recognition.face_locations(existing_image, model="hog")
#                     if not existing_locations:
#                         continue
#                     existing_encodings = face_recognition.face_encodings(existing_image, known_face_locations=existing_locations)
#                     for existing_encoding in existing_encodings:
#                         results = face_recognition.compare_faces([existing_encoding], new_face_encoding, tolerance=0.4)
#                         if results[0]:
#                             duplicate_found = True
#                             duplicate_face_path = file_path
#                             break
#                     if duplicate_found:
#                         break
#                 except Exception:
#                     continue

#             with app.app_context():
#                 timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#                 if duplicate_found:
#                     base_name = os.path.basename(duplicate_face_path)
#                     splitted = base_name.split("_", 1)
#                     matched_face_id = splitted[0]
#                     conn = sqlite3.connect(DB_NAME)
#                     c = conn.cursor()
#                     c.execute("SELECT name, dob, gender FROM faces WHERE face_id=?", (matched_face_id,))
#                     row = c.fetchone()
#                     conn.close()
#                     if row:
#                         name, dob, gender = row
#                     else:
#                         name, dob, gender = "", "", ""
#                     face_url = url_for('uploaded_file', filename=base_name)
#                     face_info = {
#                         "face_id": matched_face_id,
#                         "name": name,
#                         "dob": dob,
#                         "gender": gender,
#                         "face_image": face_url
#                     }
#                     # Giả sử khi phát hiện khuôn mặt đã tồn tại, ta ghi nhận sự kiện "Ra"
#                     conn = sqlite3.connect(DB_NAME)
#                     cursor = conn.cursor()
#                     cursor.execute(
#                         "INSERT INTO events (timestamp, recognized_faces, count, event_type) VALUES (?, ?, ?, ?)",
#                         (timestamp, json.dumps(face_info), 1, "Ra")
#                     )
#                     conn.commit()
#                     conn.close()
#                 else:
#                     # Nếu chưa có, lưu khuôn mặt mới và ghi nhận sự kiện "Vào"
#                     timestamp_filename = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
#                     face_id = str(uuid.uuid4())
#                     face_filename = os.path.join(UPLOAD_FOLDER, f"{face_id}_{timestamp_filename}.jpg")
#                     Image.fromarray(face_img).save(face_filename)
#                     face_url = url_for('uploaded_file', filename=os.path.basename(face_filename))
#                     conn = sqlite3.connect(DB_NAME)
#                     cursor = conn.cursor()
#                     cursor.execute(
#                         "INSERT INTO faces (face_id, name, dob, gender, timestamp) VALUES (?, ?, ?, ?, ?)",
#                         (face_id, "", "", "", timestamp)
#                     )
#                     conn.commit()
#                     face_info = {
#                         "face_id": face_id,
#                         "name": "",
#                         "dob": "",
#                         "gender": "",
#                         "face_image": face_url
#                     }
#                     cursor.execute(
#                         "INSERT INTO events (timestamp, recognized_faces, count, event_type) VALUES (?, ?, ?, ?)",
#                         (timestamp, json.dumps(face_info), 1, "Vào")
#                     )
#                     conn.commit()
#                     conn.close()
#         time.sleep(1)  # Điều chỉnh tần suất xử lý (1 giây) để không làm quá tải CPU
#     cap.release()

# # Khởi chạy background thread để tự động nhận diện khuôn mặt
# threading.Thread(target=auto_face_recognition, daemon=True).start()





# quét thư mục upload 
def load_known_faces():
    known_face_encodings = []
    known_face_names = []
    for file in os.listdir(UPLOAD_FOLDER):
        file_path = os.path.join(UPLOAD_FOLDER, file)
        # Giả sử tên file có dạng: face_id_timestamp.jpg
        face_id = file.split("_", 1)[0]
        try:
            image = face_recognition.load_image_file(file_path)
            encodings = face_recognition.face_encodings(image)
            if encodings:
                known_face_encodings.append(encodings[0])
                # Truy vấn DB để lấy tên theo face_id
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM faces WHERE face_id=?", (face_id,))
                row = cursor.fetchone()
                conn.close()
                if row and row[0]:
                    known_face_names.append(row[0])
                else:
                    known_face_names.append("Unknown")
        except Exception as e:
            print(f"Lỗi khi tải ảnh {file}: {e}")
            continue
    return known_face_encodings, known_face_names

# Tải các encoding khi khởi động ứng dụng (hoặc cập nhật định kỳ)
known_face_encodings, known_face_names = load_known_faces()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
