#!/usr/bin/env python3
"""
JugginAI PC Model Manager + Reverse Proxy.

Stdlib-only (Python 3.8+). Binds to 127.0.0.1 only and is meant to be reached
through a cloudflared tunnel (pc.jugginai.net). Every endpoint requires
`Authorization: Bearer <token>`, where the token is read from
%USERPROFILE%\\.jugginai\\pc-bridge-token.

Endpoints
  GET    /health
  GET    /models/installed
  GET    /models/search?q=<query>[&limit=N]
  POST   /models/pull            {repo, file, engine: lmstudio|llamacpp, quant, method?: direct|lms}
  GET    /models/pull            list pull jobs
  GET    /models/pull/:id        job snapshot
  GET    /models/pull/:id/events SSE progress (percent, mbps, eta)
  DELETE /models/pull/:id        cancel
  POST   /models/load            {id, engine, ctx?, ngl?, port?}
  POST   /models/unload          {id, engine}
  DELETE /models/:id             remove a .gguf under %USERPROFILE%\\.llamacpp
  *      /lmstudio/v1/*          -> http://127.0.0.1:1234/v1/*
  *      /llamacpp/v1/*          -> http://127.0.0.1:8080/v1/*
"""

import hmac
import http.client
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
IS_WIN = os.name == "nt"
HOME = os.environ.get("USERPROFILE") or os.path.expanduser("~")
JDIR = os.environ.get("JUGGINAI_DIR") or os.path.join(HOME, ".jugginai")
TOKEN_FILE = os.path.join(JDIR, "pc-bridge-token")
HF_TOKEN_FILE = os.path.join(JDIR, "hf-token")
LLAMACPP_DIR = os.environ.get("LLAMACPP_DIR") or os.path.join(HOME, ".llamacpp")
LMSTUDIO_MODELS_DIR = os.environ.get("LMSTUDIO_MODELS_DIR") or os.path.join(HOME, ".lmstudio", "models")
BIND_HOST = "127.0.0.1"
BIND_PORT = int(os.environ.get("BRIDGE_PORT", "8899"))
LMSTUDIO_PORT = int(os.environ.get("LMSTUDIO_PORT", "1234"))
LLAMACPP_PORT = int(os.environ.get("LLAMACPP_PORT", "8080"))
LLAMA_SERVER_PORT = int(os.environ.get("LLAMA_SERVER_PORT", "8081"))
DISK_PATH = "C:\\" if IS_WIN else "/"
MAX_BODY = 64 * 1024 * 1024
VERSION = "1.0.0"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW, so pythonw never flashes consoles

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "authorization",
    "content-length",
}

os.makedirs(os.path.join(JDIR, "logs"), exist_ok=True)
logging.basicConfig(
    filename=os.path.join(JDIR, "logs", "bridge.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bridge")


def _read_secret(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    except OSError:
        return None


TOKEN = _read_secret(TOKEN_FILE)


def hf_token():
    return os.environ.get("HF_TOKEN") or _read_secret(HF_TOKEN_FILE)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def run(cmd, timeout=30):
    """Run a command without a console window. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "not found: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def tcp_up(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_lms():
    p = shutil.which("lms")
    if p:
        return p
    cand = os.path.join(HOME, ".lmstudio", "bin", "lms.exe" if IS_WIN else "lms")
    return cand if os.path.isfile(cand) else None


_llama_server_cache = {"path": None, "searched": False}


def _bounded_find(root, names, max_depth=7, max_dirs=40000):
    skip = {"node_modules", ".git", "__pycache__", "$recycle.bin", "windows",
            ".cache", "temp", "tmp", ".npm", ".nuget"}
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        seen += 1
        if seen > max_dirs:
            return None
        for n in filenames:
            if n.lower() in names:
                return os.path.join(dirpath, n)
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d.lower() not in skip]
    return None


def find_llama_server(refresh=False):
    if _llama_server_cache["searched"] and not refresh:
        p = _llama_server_cache["path"]
        if p and os.path.isfile(p):
            return p
        if not refresh:
            return p
    names = {"llama-server.exe", "llama-server"}
    p = shutil.which("llama-server")
    if not p:
        override = os.environ.get("LLAMA_SERVER_PATH")
        if override and os.path.isfile(override):
            p = override
    if not p:
        roots = []
        if IS_WIN:
            roots += [r"C:\Program Files\PAIR", r"C:\Program Files (x86)\PAIR"]
        roots += [LLAMACPP_DIR, HOME]
        for r in roots:
            if os.path.isdir(r):
                p = _bounded_find(r, names)
                if p:
                    break
    _llama_server_cache.update(path=p, searched=True)
    return p


def find_nvidia_smi():
    p = shutil.which("nvidia-smi")
    if p:
        return p
    if IS_WIN:
        for c in (r"C:\Windows\System32\nvidia-smi.exe",
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.isfile(c):
                return c
    return None


def gpu_info():
    smi = find_nvidia_smi()
    if not smi:
        return {"available": False, "error": "nvidia-smi not found"}
    rc, out, err = run([smi, "--query-gpu=memory.used,memory.total",
                        "--format=csv,noheader,nounits"], timeout=10)
    if rc != 0:
        return {"available": False, "error": (err or out).strip()[:300]}
    gpus = []
    for i, line in enumerate(l for l in out.splitlines() if l.strip()):
        parts = [x.strip() for x in line.split(",")]
        try:
            used, total = int(float(parts[0])), int(float(parts[1]))
            gpus.append({"index": i, "vram_used_mb": used, "vram_total_mb": total,
                         "vram_free_mb": total - used})
        except (ValueError, IndexError):
            gpus.append({"index": i, "raw": line})
    return {"available": bool(gpus), "gpus": gpus}


def total_vram_mb():
    g = gpu_info()
    try:
        return max(x.get("vram_total_mb", 0) for x in g.get("gpus", [])) or None
    except ValueError:
        return None


def disk_info():
    try:
        u = shutil.disk_usage(DISK_PATH)
        return {"path": DISK_PATH, "free_gb": round(u.free / 1e9, 2),
                "total_gb": round(u.total / 1e9, 2)}
    except OSError as e:
        return {"path": DISK_PATH, "error": str(e)}


def engine_models(port):
    try:
        d = http_json("http://127.0.0.1:%d/v1/models" % port, timeout=3)
        return {"online": True, "models": [m.get("id") for m in d.get("data", [])]}
    except Exception as e:  # noqa: BLE001
        return {"online": False, "error": str(e)[:200], "models": []}


QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])(IQ[1-4]_(?:XXS|XS|S|M|NL)|IQ[1-4]|Q[2-8]_K_(?:XL|S|M|L)|Q[2-8]_K|"
    r"Q[2-8]_[01]|Q[2-8]|TQ[12]_0|MXFP4|BF16|F16|FP16|F32|FP8)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def parse_quant(filename):
    m = QUANT_RE.search(os.path.basename(filename).replace(".gguf", ""))
    return m.group(1).upper() if m else None


def vram_fit(size_bytes, vram_mb):
    if not size_bytes:
        return "unknown"
    need_mb = size_bytes / 1e6 * 1.15 + 600  # weights + KV cache/context overhead
    if not vram_mb:
        return "unknown"
    if need_mb <= vram_mb * 0.9:
        return "fits"
    if need_mb <= vram_mb:
        return "tight"
    if size_bytes / 1e6 <= vram_mb * 2.5:
        return "partial_offload"
    return "too_large"


# --------------------------------------------------------------------------
# Managed llama-server child
# --------------------------------------------------------------------------
_llama = {"proc": None, "model": None, "port": None, "started": None, "log": None}
_llama_lock = threading.Lock()


def llama_status():
    p = _llama["proc"]
    alive = bool(p and p.poll() is None)
    return {"managed": alive, "model": _llama["model"] if alive else None,
            "port": _llama["port"] if alive else None,
            "pid": p.pid if alive else None,
            "exit_code": (p.returncode if p and not alive else None)}


def resolve_gguf(model_id):
    """Map an id (file name or relative path) to an absolute .gguf inside LLAMACPP_DIR."""
    if not model_id:
        return None
    root = os.path.realpath(LLAMACPP_DIR)
    cand = os.path.realpath(os.path.join(root, model_id))
    if not cand.startswith(root + os.sep) or not cand.lower().endswith(".gguf"):
        return None
    if os.path.isfile(cand):
        return cand
    # fall back to a basename search
    base = os.path.basename(model_id).lower()
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fn.lower() == base:
                return os.path.join(dp, fn)
    return None


# --------------------------------------------------------------------------
# Pull jobs
# --------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
SAFE_REPO = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")


class PullJob:
    def __init__(self, repo, file, engine, quant, method):
        self.id = uuid.uuid4().hex[:12]
        self.repo, self.file, self.engine, self.quant, self.method = repo, file, engine, quant, method
        self.status = "queued"
        self.bytes_done = 0
        self.bytes_total = None
        self.percent = 0.0
        self.mbps = 0.0
        self.eta = None
        self.error = None
        self.dest = None
        self.started = time.time()
        self.finished = None
        self.cancel = threading.Event()
        self.proc = None
        self.lock = threading.Lock()
        self.message = None

    def snapshot(self):
        with self.lock:
            return {
                "id": self.id, "repo": self.repo, "file": self.file, "engine": self.engine,
                "quant": self.quant, "method": self.method, "status": self.status,
                "percent": round(self.percent, 2), "mbps": round(self.mbps, 2),
                "eta": None if self.eta is None else int(self.eta),
                "bytes_done": self.bytes_done, "bytes_total": self.bytes_total,
                "dest": self.dest, "error": self.error, "message": self.message,
                "started": self.started, "finished": self.finished,
            }

    def done(self):
        return self.status in ("done", "error", "cancelled")

    def finish(self, status, error=None):
        with self.lock:
            self.status = status
            self.error = error
            self.finished = time.time()
            if status == "done":
                self.percent = 100.0
                self.eta = 0


def _direct_download(job):
    if job.engine == "llamacpp":
        dest_dir = LLAMACPP_DIR
    else:
        dest_dir = os.path.join(LMSTUDIO_MODELS_DIR, *job.repo.split("/"))
    dest = os.path.join(dest_dir, *job.file.split("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    job.dest = dest
    part = dest + ".part"
    url = "https://huggingface.co/%s/resolve/main/%s" % (
        urllib.parse.quote(job.repo), urllib.parse.quote(job.file))
    headers = {"User-Agent": "jugginai-pc-bridge/" + VERSION}
    tok = hf_token()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    resume_from = os.path.getsize(part) if os.path.exists(part) else 0
    if resume_from:
        headers["Range"] = "bytes=%d-" % resume_from
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        if resume_from and r.status != 206:
            resume_from = 0
        length = r.headers.get("Content-Length")
        total = (int(length) + resume_from) if length else None
        with job.lock:
            job.status = "downloading"
            job.bytes_total = total
            job.bytes_done = resume_from
        mode = "ab" if resume_from else "wb"
        t0, b0 = time.time(), resume_from
        win_t, win_b = t0, resume_from
        with open(part, mode) as f:
            while True:
                if job.cancel.is_set():
                    raise InterruptedError("cancelled")
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                now = time.time()
                with job.lock:
                    job.bytes_done += len(chunk)
                    if now - win_t >= 1.0:
                        job.mbps = (job.bytes_done - win_b) * 8 / 1e6 / (now - win_t)
                        win_t, win_b = now, job.bytes_done
                    elif job.mbps == 0 and now > t0:
                        job.mbps = (job.bytes_done - b0) * 8 / 1e6 / max(now - t0, 1e-3)
                    if total:
                        job.percent = job.bytes_done * 100.0 / total
                        rate = (job.bytes_done - b0) / max(now - t0, 1e-3)
                        job.eta = (total - job.bytes_done) / rate if rate > 0 else None
    os.replace(part, dest)


_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB)/s", re.IGNORECASE)


def _lms_get(job, lms):
    target = job.repo + ("@" + job.quant if job.quant else "")
    cmd = [lms, "get", target, "--yes"]
    job.message = " ".join(cmd[1:])
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    job.proc = p
    with job.lock:
        job.status = "downloading"
    buf = b""
    tail = []
    while True:
        if job.cancel.is_set():
            p.kill()
            raise InterruptedError("cancelled")
        b = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(1)
        if not b:
            break
        buf += b
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for line in parts:
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            tail = (tail + [s])[-20:]
            m = _PCT_RE.search(s)
            with job.lock:
                if m:
                    job.percent = min(float(m.group(1)), 100.0)
                sm = _SPEED_RE.search(s)
                if sm:
                    v = float(sm.group(1))
                    unit = sm.group(2).upper()
                    mul = {"KB": 0.008, "MB": 8, "GB": 8000}[unit]
                    job.mbps = v * mul
                job.message = s[:300]
    rc = p.wait()
    if rc != 0:
        raise RuntimeError("lms get exited %d: %s" % (rc, " | ".join(tail[-5:])))


def _run_job(job):
    try:
        lms = find_lms()
        if job.engine == "lmstudio" and lms and job.method != "direct":
            job.method = "lms"
            _lms_get(job, lms)
        else:
            job.method = "direct"
            _direct_download(job)
        job.finish("done")
        log.info("pull %s done: %s/%s", job.id, job.repo, job.file)
    except InterruptedError:
        if job.dest and os.path.exists(job.dest + ".part"):
            try:
                os.remove(job.dest + ".part")
            except OSError:
                pass
        job.finish("cancelled")
        log.info("pull %s cancelled", job.id)
    except urllib.error.HTTPError as e:
        job.finish("error", "HTTP %d from Hugging Face: %s" % (e.code, e.reason))
    except Exception as e:  # noqa: BLE001
        job.finish("error", str(e)[:500])
        log.exception("pull %s failed", job.id)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jugginai-pc-bridge/" + VERSION
    sys_version = ""

    # ---- plumbing --------------------------------------------------------
    def log_message(self, fmt, *args):  # pythonw has no stderr
        log.info("%s %s", self.address_string(), fmt % args)

    def send_json(self, code, obj):
        body = json.dumps(obj, indent=None, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def err(self, code, msg, **extra):
        d = {"error": msg}
        d.update(extra)
        self.send_json(code, d)

    def start_chunked(self, code, headers):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def write_chunk(self, data):
        if not data:
            return
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def end_chunked(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            out = bytearray()
            while True:
                line = self.rfile.readline(65537)
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                out += self.rfile.read(size)
                self.rfile.readline(3)
                if len(out) > MAX_BODY:
                    raise ValueError("body too large")
            return bytes(out)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def json_body(self):
        raw = self.read_body()
        if not raw:
            return {}
        d = json.loads(raw.decode("utf-8"))
        if not isinstance(d, dict):
            raise ValueError("JSON body must be an object")
        return d

    def authorized(self):
        if not TOKEN:
            return False
        h = self.headers.get("Authorization") or ""
        if not h.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(h[7:].strip().encode("utf-8"), TOKEN.encode("utf-8"))

    # ---- dispatch --------------------------------------------------------
    def handle_any(self):
        try:
            if not self.authorized():
                # drain body so the connection stays sane
                try:
                    self.read_body()
                except Exception:  # noqa: BLE001
                    pass
                return self.err(401, "unauthorized")
            u = urllib.parse.urlsplit(self.path)
            path = u.path.rstrip("/") or "/"
            q = urllib.parse.parse_qs(u.query)
            m = self.command

            if path.startswith("/lmstudio/v1/") or path == "/lmstudio/v1":
                return self.proxy(LMSTUDIO_PORT, "lmstudio", "/v1" + u.path[len("/lmstudio/v1"):], u.query)
            if path.startswith("/llamacpp/v1/") or path == "/llamacpp/v1":
                return self.proxy(LLAMACPP_PORT, "llamacpp", "/v1" + u.path[len("/llamacpp/v1"):], u.query)

            if m in ("GET", "HEAD") and path in ("/", "/health"):
                return self.h_health()
            if m == "GET" and path == "/models/installed":
                return self.h_installed()
            if m == "GET" and path == "/models/search":
                return self.h_search(q)
            if path == "/models/pull":
                if m == "POST":
                    return self.h_pull()
                if m == "GET":
                    with JOBS_LOCK:
                        jobs = [j.snapshot() for j in JOBS.values()]
                    return self.send_json(200, {"jobs": jobs})
            mm = re.match(r"^/models/pull/([0-9a-f]{12})(/events)?$", path)
            if mm:
                with JOBS_LOCK:
                    job = JOBS.get(mm.group(1))
                if not job:
                    return self.err(404, "no such pull job")
                if m == "GET" and mm.group(2):
                    return self.h_pull_events(job)
                if m == "GET":
                    return self.send_json(200, job.snapshot())
                if m == "DELETE":
                    job.cancel.set()
                    if job.proc and job.proc.poll() is None:
                        job.proc.kill()
                    return self.send_json(202, {"id": job.id, "status": "cancelling"})
            if m == "POST" and path == "/models/load":
                return self.h_load()
            if m == "POST" and path == "/models/unload":
                return self.h_unload()
            if m == "DELETE" and path.startswith("/models/") and not path.startswith("/models/pull"):
                return self.h_delete(urllib.parse.unquote(u.path[len("/models/"):]))
            return self.err(404, "not found", path=u.path, method=m)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except json.JSONDecodeError:
            self.err(400, "invalid JSON body")
        except ValueError as e:
            self.err(400, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("unhandled")
            try:
                self.err(500, "internal error", detail=str(e)[:300])
            except Exception:  # noqa: BLE001
                pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any

    # ---- endpoints -------------------------------------------------------
    def h_health(self):
        lm = tcp_up(LMSTUDIO_PORT)
        lc = tcp_up(LLAMACPP_PORT)
        self.send_json(200, {
            "status": "ok",
            "version": VERSION,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "engines": {
                "lmstudio": {"port": LMSTUDIO_PORT, "online": lm,
                             "backend_1235_online": tcp_up(1235)},
                "llamacpp": {"port": LLAMACPP_PORT, "online": lc,
                             "backend_8081_online": tcp_up(LLAMA_SERVER_PORT),
                             "managed_server": llama_status()},
            },
            "disk": disk_info(),
            "gpu": gpu_info(),
            "tools": {"lms": bool(find_lms()), "llama_server": find_llama_server(),
                      "nvidia_smi": bool(find_nvidia_smi())},
            "hf_token_configured": bool(hf_token()),
        })

    def h_installed(self):
        out = {"lmstudio": {}, "llamacpp": {}}
        lms = find_lms()
        if lms:
            rc, so, se = run([lms, "ls", "--json"], timeout=30)
            if rc == 0:
                try:
                    out["lmstudio"]["models"] = json.loads(so)
                except json.JSONDecodeError:
                    out["lmstudio"]["raw"] = so[:20000]
            else:
                out["lmstudio"]["error"] = (se or so).strip()[:500]
            rc, so, se = run([lms, "ps", "--json"], timeout=15)
            if rc == 0:
                try:
                    out["lmstudio"]["ps"] = json.loads(so)
                except json.JSONDecodeError:
                    pass
        else:
            out["lmstudio"]["error"] = "lms CLI not found on PATH or in ~/.lmstudio/bin"
        out["lmstudio"]["loaded"] = engine_models(LMSTUDIO_PORT)

        files = []
        if os.path.isdir(LLAMACPP_DIR):
            for dp, _, fns in os.walk(LLAMACPP_DIR):
                for fn in fns:
                    if fn.lower().endswith(".gguf"):
                        fp = os.path.join(dp, fn)
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            sz = None
                        files.append({"id": os.path.relpath(fp, LLAMACPP_DIR).replace("\\", "/"),
                                      "path": fp, "size_bytes": sz,
                                      "size_gb": round(sz / 1e9, 2) if sz else None,
                                      "quant": parse_quant(fn)})
        else:
            out["llamacpp"]["error"] = "directory not found: %s" % LLAMACPP_DIR
        loaded = engine_models(LLAMACPP_PORT)
        ms = llama_status()
        for f in files:
            stem = os.path.basename(f["id"])
            f["loaded"] = any(stem in (m or "") or (m or "") in stem for m in loaded["models"] if m) or \
                (ms["managed"] and ms["model"] == f["path"])
        out["llamacpp"].update(dir=LLAMACPP_DIR, models=files, loaded=loaded, managed_server=ms)
        self.send_json(200, out)

    def h_search(self, q):
        query = (q.get("q") or [""])[0].strip()
        if not query:
            return self.err(400, "missing q")
        try:
            limit = max(1, min(int((q.get("limit") or ["10"])[0]), 30))
        except ValueError:
            limit = 10
        detail_n = min(limit, 6)
        headers = {"User-Agent": "jugginai-pc-bridge/" + VERSION}
        tok = hf_token()
        if tok:
            headers["Authorization"] = "Bearer " + tok
        url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
            {"search": query, "filter": "gguf", "full": "true", "limit": limit,
             "sort": "downloads", "direction": "-1"})
        try:
            models = http_json(url, headers, timeout=20)
        except urllib.error.HTTPError as e:
            return self.err(502, "Hugging Face search failed: HTTP %d" % e.code)
        except Exception as e:  # noqa: BLE001
            return self.err(502, "Hugging Face search failed: %s" % e)
        vram = total_vram_mb()
        results = []
        for m in models:
            results.append({
                "repo": m.get("id") or m.get("modelId"),
                "downloads": m.get("downloads"), "likes": m.get("likes"),
                "updated": m.get("lastModified"), "pipeline": m.get("pipeline_tag"),
                "gated": m.get("gated"),
                "files": [{"file": s.get("rfilename"), "quant": parse_quant(s.get("rfilename", ""))}
                          for s in (m.get("siblings") or []) if s.get("rfilename", "").endswith(".gguf")],
            })

        def fill(r):
            try:
                tree = http_json("https://huggingface.co/api/models/%s/tree/main?recursive=true"
                                 % urllib.parse.quote(r["repo"]), headers, timeout=20)
                files = []
                for t in tree:
                    p = t.get("path", "")
                    if t.get("type") == "file" and p.lower().endswith(".gguf"):
                        sz = (t.get("lfs") or {}).get("size") or t.get("size")
                        files.append({"file": p, "size_bytes": sz,
                                      "size_gb": round(sz / 1e9, 2) if sz else None,
                                      "quant": parse_quant(p),
                                      "split": bool(re.search(r"-\d{5}-of-\d{5}\.gguf$", p)),
                                      "vram_fit": vram_fit(sz, vram)})
                r["files"] = sorted(files, key=lambda x: x["size_bytes"] or 0)
            except Exception as e:  # noqa: BLE001
                r["files_error"] = str(e)[:200]

        threads = [threading.Thread(target=fill, args=(r,)) for r in results[:detail_n]]
        for t in threads:
            t.start()
        for t in threads:
            t.join(25)
        self.send_json(200, {"query": query, "vram_total_mb": vram,
                             "detailed": detail_n, "results": results})

    def h_pull(self):
        b = self.json_body()
        repo = (b.get("repo") or "").strip()
        file = (b.get("file") or "").strip().replace("\\", "/")
        engine = (b.get("engine") or "llamacpp").strip().lower()
        quant = (b.get("quant") or "").strip() or None
        method = (b.get("method") or "").strip().lower() or None
        if not SAFE_REPO.match(repo):
            return self.err(400, "repo must look like owner/name")
        if engine not in ("lmstudio", "llamacpp"):
            return self.err(400, "engine must be lmstudio or llamacpp")
        needs_file = not (engine == "lmstudio" and find_lms() and method != "direct")
        if needs_file:
            if not file or file.startswith("/") or ".." in file.split("/") or ":" in file:
                return self.err(400, "file must be a relative path inside the repo")
        if quant and not re.match(r"^[\w.-]+$", quant):
            return self.err(400, "bad quant")
        job = PullJob(repo, file, engine, quant, method)
        with JOBS_LOCK:
            JOBS[job.id] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        log.info("pull %s started: %s %s/%s", job.id, engine, repo, file)
        self.send_json(202, {"id": job.id, "events": "/models/pull/%s/events" % job.id,
                             "status": job.status})

    def h_pull_events(self, job):
        self.start_chunked(200, [("Content-Type", "text/event-stream"),
                                 ("Cache-Control", "no-cache"),
                                 ("X-Accel-Buffering", "no")])
        last = None
        hb = time.time()
        while True:
            snap = snap_out = job.snapshot()
            key = (snap["status"], snap["percent"], snap["bytes_done"], snap["message"])
            if key != last:
                ev = "progress" if not job.done() else snap["status"]
                self.write_chunk(("event: %s\ndata: %s\n\n" % (ev, json.dumps(snap_out))).encode())
                last = key
                hb = time.time()
            elif time.time() - hb > 15:
                self.write_chunk(b": keepalive\n\n")
                hb = time.time()
            if job.done():
                break
            time.sleep(1.0)
        self.end_chunked()

    def h_load(self):
        b = self.json_body()
        mid = (b.get("id") or "").strip()
        engine = (b.get("engine") or "").strip().lower()
        if not mid:
            return self.err(400, "missing id")
        if engine == "lmstudio":
            lms = find_lms()
            if not lms:
                return self.err(501, "lms CLI not found (looked on PATH and ~/.lmstudio/bin/lms.exe)")
            cmd = [lms, "load", mid, "--yes"]
            if b.get("ctx"):
                cmd += ["--context-length", str(int(b["ctx"]))]
            if b.get("gpu"):
                cmd += ["--gpu", str(b["gpu"])]
            rc, so, se = run(cmd, timeout=600)
            return self.send_json(200 if rc == 0 else 502, {
                "engine": engine, "id": mid, "ok": rc == 0, "exit_code": rc,
                "output": (so + se).strip()[-2000:]})
        if engine == "llamacpp":
            exe = find_llama_server(refresh=True)
            if not exe:
                return self.err(501, "llama-server not found on PATH, under C:\\Program Files\\PAIR, "
                                     "or under %USERPROFILE%. Set LLAMA_SERVER_PATH or load it from PAIR.")
            path = resolve_gguf(mid)
            if not path:
                return self.err(404, "gguf not found under %s: %s" % (LLAMACPP_DIR, mid))
            port = int(b.get("port") or LLAMA_SERVER_PORT)
            with _llama_lock:
                p = _llama["proc"]
                if p and p.poll() is None:
                    return self.err(409, "a managed llama-server is already running; unload it first",
                                    status=llama_status())
                if tcp_up(port):
                    return self.err(409, "port %d is already in use (PAIR may already be serving a model there); "
                                         "unload it in PAIR or pass another port" % port)
                cmd = [exe, "-m", path, "--host", "127.0.0.1", "--port", str(port),
                       "-ngl", str(int(b.get("ngl", 99))),
                       "-c", str(int(b.get("ctx", 8192))),
                       "--alias", os.path.splitext(os.path.basename(path))[0]]
                logf = open(os.path.join(JDIR, "logs", "llama-server.log"), "ab")
                proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                                        cwd=os.path.dirname(exe))
                _llama.update(proc=proc, model=path, port=port, started=time.time(), log=logf)
            # wait up to 120s for it to come up
            deadline = time.time() + float(b.get("wait", 120))
            while time.time() < deadline:
                if proc.poll() is not None:
                    return self.err(502, "llama-server exited with code %s; see logs/llama-server.log"
                                    % proc.returncode)
                if tcp_up(port):
                    try:
                        http_json("http://127.0.0.1:%d/health" % port, timeout=2)
                        break
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(1)
            return self.send_json(200, {"engine": engine, "id": mid, "ok": True,
                                        "ready": tcp_up(port), "status": llama_status(),
                                        "note": "served on :%d; /llamacpp/v1 goes through :%d"
                                                % (port, LLAMACPP_PORT)})
        return self.err(400, "engine must be lmstudio or llamacpp")

    def h_unload(self):
        b = self.json_body()
        mid = (b.get("id") or "").strip()
        engine = (b.get("engine") or "").strip().lower()
        if engine == "lmstudio":
            lms = find_lms()
            if not lms:
                return self.err(501, "lms CLI not found")
            cmd = [lms, "unload", "--all"] if mid in ("", "*", "all") else [lms, "unload", mid]
            rc, so, se = run(cmd, timeout=120)
            return self.send_json(200 if rc == 0 else 502, {
                "engine": engine, "id": mid, "ok": rc == 0, "output": (so + se).strip()[-2000:]})
        if engine == "llamacpp":
            with _llama_lock:
                p = _llama["proc"]
                if not p or p.poll() is not None:
                    return self.err(409, "no managed llama-server is running (models started from PAIR "
                                         "must be stopped in PAIR)")
                p.terminate()
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
                if _llama["log"]:
                    _llama["log"].close()
                model = _llama["model"]
                _llama.update(proc=None, model=None, port=None, log=None)
            return self.send_json(200, {"engine": engine, "ok": True, "unloaded": model})
        return self.err(400, "engine must be lmstudio or llamacpp")

    def h_delete(self, mid):
        path = resolve_gguf(mid)
        if not path:
            return self.err(404, "gguf not found under %s: %s" % (LLAMACPP_DIR, mid))
        ms = llama_status()
        if ms["managed"] and ms["model"] == path:
            return self.err(409, "model is loaded in the managed llama-server; unload it first")
        try:
            os.remove(path)
        except OSError as e:
            return self.err(500, "delete failed: %s" % e)
        log.info("deleted %s", path)
        self.send_json(200, {"deleted": path})

    # ---- proxy -----------------------------------------------------------
    def proxy(self, port, name, upath, query):
        body = self.read_body()
        target = upath + ("?" + query if query else "")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Host"] = "127.0.0.1:%d" % port
        if body or self.command in ("POST", "PUT", "PATCH"):
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
        try:
            conn.request(self.command, target, body=body or None, headers=headers)
            resp = conn.getresponse()
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            conn.close()
            return self.err(503, "%s engine is down" % name, engine=name,
                            upstream="127.0.0.1:%d" % port, detail=str(e)[:200])
        try:
            out_headers = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP_BY_HOP]
            length = resp.getheader("Content-Length")
            if length is not None and "chunked" not in (resp.getheader("Transfer-Encoding") or "").lower():
                self.send_response(resp.status, resp.reason)
                for k, v in out_headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", length)
                self.end_headers()
                remaining = int(length)
                while remaining > 0:
                    chunk = resp.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                self.wfile.flush()
            else:
                self.send_response(resp.status, resp.reason)
                for k, v in out_headers:
                    self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.write_chunk(chunk)
                self.end_chunked()
        finally:
            conn.close()


def main():
    if not TOKEN:
        log.error("token file missing or empty: %s", TOKEN_FILE)
        sys.stderr and sys.stderr.write("token file missing or empty: %s\n" % TOKEN_FILE)
        sys.exit(2)
    srv = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    srv.daemon_threads = True
    # warm the llama-server lookup so the first /health call stays fast
    threading.Thread(target=find_llama_server, daemon=True).start()
    log.info("jugginai-pc-bridge %s listening on %s:%d", VERSION, BIND_HOST, BIND_PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        p = _llama["proc"]
        if p and p.poll() is None:
            p.terminate()


if __name__ == "__main__":
    main()
