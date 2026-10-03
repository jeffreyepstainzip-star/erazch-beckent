import os, re, uuid, glob, shutil, tempfile, threading, time
from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp

app = Flask(__name__)
TMP = os.path.join(tempfile.gettempdir(), "erazch_install")
os.makedirs(TMP, exist_ok=True)
YT_RE = re.compile(r"^(https?://)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.I)

# ffmpeg (pip orqali avtomatik keladi) - yuqori sifat va MP3 uchun
FFMPEG = None
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = shutil.which("ffmpeg")

# YouTube o'zgarib turadi, shuning uchun bir nechta usulni ketma-ket sinaymiz
CLIENTS = [None, ["android"], ["ios"], ["tv"], ["web_safari"]]

# YouTube bot-tekshiruvidan o'tish uchun cookies (Render "Secret Files" ichida cookies.txt)
COOKIES = None
for _src in ("/etc/secrets/cookies.txt", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")):
    if os.path.exists(_src):
        # yt-dlp cookie faylga yozishi mumkin, shuning uchun yozish mumkin bo'lgan joyga nusxalaymiz
        COOKIES = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        shutil.copyfile(_src, COOKIES)
        break

jobs = {}

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return r

def base_opts(client):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True,
         "retries": 5, "fragment_retries": 5, "socket_timeout": 20,
         "concurrent_fragment_downloads": 8, "nocheckcertificate": True}
    if FFMPEG:
        o["ffmpeg_location"] = FFMPEG
    if COOKIES:
        o["cookiefile"] = COOKIES
    if client:
        o["extractor_args"] = {"youtube": {"player_client": client}}
    return o

def cleaner():
    while True:
        now = time.time()
        for f in glob.glob(os.path.join(TMP, "*")):
            try:
                if now - os.path.getmtime(f) > 1800:
                    os.remove(f)
            except Exception:
                pass
        for k in list(jobs):
            if now - jobs[k]["t"] > 3600:
                jobs.pop(k, None)
        time.sleep(300)
threading.Thread(target=cleaner, daemon=True).start()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/info", methods=["POST", "OPTIONS"])
def info():
    if request.method == "OPTIONS":
        return "", 204
    url = (request.json or {}).get("url", "").strip()
    if not YT_RE.match(url):
        return jsonify(error="Faqat YouTube ssilkasini kiriting"), 400
    last = None
    for c in CLIENTS:
        try:
            with yt_dlp.YoutubeDL(base_opts(c)) as y:
                d = y.extract_info(url, download=False)
            return jsonify(title=d.get("title"), thumbnail=d.get("thumbnail"),
                           duration=d.get("duration"), channel=d.get("uploader"),
                           hq=bool(FFMPEG))
        except Exception as e:
            last = e
            print("INFO XATO [client=%s]: %s" % (c, str(e)[:300]), flush=True)
    return jsonify(error="Video topilmadi. Ssilkani tekshiring yoki keyinroq urinib ko'ring"), 400

def run_job(jid, url, kind, quality):
    job = jobs[jid]
    last = None
    for c in CLIENTS:
        try:
            uid = uuid.uuid4().hex
            o = base_opts(c)
            o["outtmpl"] = os.path.join(TMP, uid + ".%(ext)s")
            streams = 1
            if kind == "audio":
                if FFMPEG:
                    o["format"] = "bestaudio/best"
                    o["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                            "preferredcodec": "mp3", "preferredquality": "192"}]
                else:
                    o["format"] = "bestaudio[ext=m4a]/bestaudio"
            else:
                h = int(quality) if str(quality).isdigit() else 720
                if FFMPEG:
                    o["format"] = (f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                                   f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best")
                    o["merge_output_format"] = "mp4"
                    streams = 2
                else:
                    o["format"] = f"best[height<={h}][ext=mp4]/best[ext=mp4]/best"
            done = {"n": 0}

            def hook(d):
                if d["status"] == "downloading":
                    tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    p = (d.get("downloaded_bytes", 0) / tot) if tot else 0
                    job["progress"] = min(99, int((done["n"] + p) / streams * 100))
                elif d["status"] == "finished":
                    done["n"] += 1
                    job["progress"] = min(99, int(done["n"] / streams * 100))
                    if done["n"] >= streams:
                        job["status"] = "processing"
            o["progress_hooks"] = [hook]
            with yt_dlp.YoutubeDL(o) as y:
                d = y.extract_info(url, download=True)
            path = None
            rd = d.get("requested_downloads") or []
            if rd and rd[0].get("filepath") and os.path.exists(rd[0]["filepath"]):
                path = rd[0]["filepath"]
            if not path:
                fs = [f for f in glob.glob(os.path.join(TMP, uid + ".*"))
                      if not f.endswith((".part", ".ytdl"))]
                path = fs[0] if fs else None
            if not path:
                raise RuntimeError("fayl topilmadi")
            title = re.sub(r'[\\/:*?"<>|]', "", d.get("title", "video")).strip()[:80] or "video"
            job.update(status="done", progress=100, path=path,
                       name=title + os.path.splitext(path)[1])
            return
        except Exception as e:
            last = e
            print("YUKLASH XATO [client=%s]: %s" % (c, str(e)[:300]), flush=True)
    job.update(status="error", error="Yuklab bo'lmadi. Keyinroq yoki boshqa sifatda urinib ko'ring")

@app.route("/api/start", methods=["POST", "OPTIONS"])
def start():
    if request.method == "OPTIONS":
        return "", 204
    j = request.json or {}
    url = j.get("url", "").strip()
    if not YT_RE.match(url):
        return jsonify(error="Faqat YouTube ssilkasini kiriting"), 400
    jid = uuid.uuid4().hex
    jobs[jid] = {"status": "downloading", "progress": 0, "t": time.time()}
    threading.Thread(target=run_job, args=(jid, url, j.get("type", "video"),
                                           j.get("quality", "720")), daemon=True).start()
    return jsonify(id=jid)

@app.route("/api/status/<jid>")
def status(jid):
    j = jobs.get(jid)
    if not j:
        return jsonify(status="error", error="Topilmadi"), 404
    return jsonify(status=j["status"], progress=j.get("progress", 0), error=j.get("error"))

@app.route("/api/file/<jid>")
def file(jid):
    j = jobs.get(jid)
    if not j or j.get("status") != "done" or not os.path.exists(j["path"]):
        return "Fayl topilmadi", 404
    return send_file(j["path"], as_attachment=True, download_name=j["name"])

if __name__ == "__main__":
    print("ffmpeg:", "bor" if FFMPEG else "yo'q (sifat 720p gacha)")
    print("cookies:", "bor" if COOKIES else "yo'q")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
