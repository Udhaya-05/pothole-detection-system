from flask import Flask, render_template, Response, request, jsonify, send_from_directory
import socket, threading, cv2, datetime, os, sqlite3, smtplib, urllib.request
import numpy as np

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import torch
from torchvision import transforms
from PIL import Image

# ================= MODEL =================
from torchvision.models import resnet18

ESP32_URL = "http://192.168.137.170/cam-lo.jpg"

#Enter your mail and app password
EMAIL = "" 
PASS = ""
TO = ""


model = resnet18(pretrained=False)
model.fc = torch.nn.Linear(model.fc.in_features, 2)

model.load_state_dict(torch.load("pothole_model.pth", map_location=torch.device("cpu")))

model.eval()
transform = transforms.Compose([
    transforms.Resize((224,224)),
    transforms.ToTensor(),
    transforms.Normalize(
        mean=[0.485, 0.456, 0.406],
        std=[0.229, 0.224, 0.225]
    )
])

# 🔥 ADD THIS FUNCTION (NEW)
def predict_frame(frame):
    try:
        img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(img)
        img = transform(img).unsqueeze(0)

        with torch.no_grad():
            output = model(img)
            pred = torch.argmax(output, dim=1).item()

        return "Pothole" if pred == 1 else "Normal"

    except Exception as e:
        print("Frame Predict Error:", e)
        return "Normal"

app = Flask(__name__)



latest_data = {"Ax": 0, "Ay": 0, "lat": 0, "lon": 0}

esp_running = False
camera_running = False

# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect("pothole.db")
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS potholes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        Ax REAL,
        Ay REAL,
        lat REAL,
        lon REAL,
        image TEXT
    )
    """)

    conn.commit()
    conn.close()

# ================= SAVE IMAGE =================
def save_image(frame):
    os.makedirs("results", exist_ok=True)
    filename = datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".jpg"
    path = os.path.join("results", filename)
    cv2.imwrite(path, frame)
    return path

# ================= EMAIL =================
def send_email(Ax, Ay, lat, lon, image_path):
    try:
        msg = MIMEMultipart()
        msg["Subject"] = "🚨 Pothole Alert"
        msg["From"] = EMAIL
        msg["To"] = TO

        body = f"Ax:{Ax} Ay:{Ay} Lat:{lat} Lon:{lon}"
        msg.attach(MIMEText(body, "plain"))

        if image_path:
            with open(image_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())

            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f"attachment; filename={os.path.basename(image_path)}")
            msg.attach(part)

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(EMAIL, PASS)
        server.send_message(msg)
        server.quit()

    except Exception as e:
        print("Email error:", e)

def handle_pothole(frame):
    try:
        img_path = save_image(frame)

        conn = sqlite3.connect("pothole.db")
        c = conn.cursor()
        c.execute("""
        INSERT INTO potholes (time, Ax, Ay, lat, lon, image)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(datetime.datetime.now()),
            latest_data["Ax"],
            latest_data["Ay"],
            latest_data["lat"],
            latest_data["lon"],
            img_path
        ))
        conn.commit()
        conn.close()

        send_email(
            latest_data["Ax"],
            latest_data["Ay"],
            latest_data["lat"],
            latest_data["lon"],
            img_path
        )

    except Exception as e:
        print("Background error:", e)
# ================= UDP =================
def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 5005))

    print("UDP Running...")

    while True:
        data, _ = sock.recvfrom(1024)

        try:
            Ax, Ay, lat, lon = map(float, data.decode().strip().split(","))

            latest_data.update({"Ax": Ax, "Ay": Ay, "lat": lat, "lon": lon})

            pothole = (abs(Ax) >= 0.40 or abs(Ay) >= 0.40)

            if not pothole:
                continue

            try:
                img_resp = urllib.request.urlopen(ESP32_URL, timeout=3)
                img_arr = np.array(bytearray(img_resp.read()), dtype=np.uint8)
                frame = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)

                img_path = save_image(frame) if frame is not None else None
            except:
                img_path = None

            conn = sqlite3.connect("pothole.db")
            c = conn.cursor()

            c.execute("""
            INSERT INTO potholes (time, Ax, Ay, lat, lon, image)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (str(datetime.datetime.now()), Ax, Ay, lat, lon, img_path))

            conn.commit()
            conn.close()

            send_email(Ax, Ay, lat, lon, img_path)

        except Exception as e:
            print("UDP error:", e)

# ================= ROUTES =================
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/data")
def data():
    conn = sqlite3.connect("pothole.db")
    c = conn.cursor()

    c.execute("SELECT * FROM potholes ORDER BY id DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()

    reports = []
    for r in rows:
        reports.append({
            "id": r[0],
            "time": r[1],
            "Ax": r[2],
            "Ay": r[3],
            "lat": r[4],
            "lon": r[5],
            "image": r[6].replace("\\", "/") if r[6] else None
        })

    return jsonify({
        "latest": latest_data,
        "reports": reports,
        "total": len(rows)
    })

@app.route("/results/<filename>")
def serve_img(filename):
    return send_from_directory("results", filename)

@app.route("/details/<int:id>")
def details(id):
    conn = sqlite3.connect("pothole.db")
    c = conn.cursor()

    c.execute("SELECT * FROM potholes WHERE id=?", (id,))
    r = c.fetchone()
    conn.close()

    if r:
        data = {
            "time": r[1],
            "Ax": r[2],
            "Ay": r[3],
            "lat": r[4],
            "lon": r[5],
            "image": r[6]
        }
        return render_template("details.html", d=data)

    return "Not Found"
def gen_laptop():
    global camera_running
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)

    frame_count = 0

    while camera_running:
        ret, frame = cap.read()
        if not ret:
            continue

        frame_count += 1

        if frame_count % 5 == 0:
            result = predict_frame(frame)
        else:
            result = "Normal"

        color = (0,255,0) if result=="Normal" else (0,0,255)

        cv2.putText(frame, result, (20,40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2)

        if result == "Pothole":
            threading.Thread(
                target=handle_pothole,
                args=(frame.copy(),),
                daemon=True
            ).start()

        ret, buf = cv2.imencode('.jpg', frame)
        if not ret:
            continue

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' +
               buf.tobytes() + b'\r\n')

    cap.release()

@app.route("/start_laptop")
def start_laptop():
    global camera_running
    camera_running = True
    return Response(gen_laptop(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/stop_laptop")
def stop_laptop():
    global camera_running
    camera_running = False
    return "Stopped"

def gen_esp():
    global esp_running
    frame_count = 0

    while esp_running:
        try:
            img_resp = urllib.request.urlopen(ESP32_URL, timeout=5)
            img_arr = np.array(bytearray(img_resp.read()), dtype=np.uint8)
            frame = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)

            if frame is None:
                continue

            frame_count += 1

            if frame_count % 5 == 0:
                result = predict_frame(frame)
            else:
                result = "Normal"

            color = (0,255,0) if result=="Normal" else (0,0,255)

            cv2.putText(frame, result, (20,40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2)

            if result == "Pothole":
                threading.Thread(
                    target=handle_pothole,
                    args=(frame.copy(),),
                    daemon=True
                ).start()

            ret, buf = cv2.imencode('.jpg', frame)
            if not ret:
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' +
                   buf.tobytes() + b'\r\n')

        except Exception as e:
            print("ESP Error:", e)

@app.route("/start_esp32")
def start_esp():
    global esp_running
    esp_running = True
    return Response(gen_esp(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/stop_esp32")
def stop_esp():
    global esp_running
    esp_running = False
    return "Stopped"

@app.route("/uploads/<filename>")
def serve_upload(filename):
    return send_from_directory("uploads", filename)
# ================= UPLOAD PREDICT =================
@app.route("/predict", methods=["POST"])
def predict():
    try:
        file = request.files["file"]

        os.makedirs("uploads", exist_ok=True)
        path = os.path.join("uploads", file.filename)
        file.save(path)

        img = Image.open(path).convert("RGB")
        img = transform(img).unsqueeze(0)

        with torch.no_grad():
            output = model(img)
            pred = torch.argmax(output, dim=1).item()

        result = "Pothole" if pred == 1 else "Normal"

        if result == "Pothole":
            conn = sqlite3.connect("pothole.db")
            c = conn.cursor()
            c.execute("""
            INSERT INTO potholes (time, Ax, Ay, lat, lon, image)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (str(datetime.datetime.now()),
                latest_data["Ax"],
                latest_data["Ay"],
                latest_data["lat"],
                latest_data["lon"],
                path))
            conn.commit()
            conn.close()

        return jsonify({"result": result})

    except Exception as e:
        print("Predict Error:", e)
        return jsonify({"result": "Error"})

# ================= MAIN =================
if __name__ == "__main__":
    init_db()
    threading.Thread(target=udp_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)