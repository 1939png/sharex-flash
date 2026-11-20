# -*- coding: utf-8 -*-
"""OBS 录屏后端：以独立进程拉起 OBS Studio，再通过 obs-websocket v5 协议控制录制。

许可证说明
----------
本模块不包含、不链接、不再分发任何 OBS 源码，仅通过 obs-websocket 公开协议
驱动一个独立运行的 OBS 进程，属于"聚合调用"而非"衍生作品"，因此本项目整体
继续适用 MIT。OBS 本体为 GPL-2.0，以独立目录分发时需保留其许可证文件，
见 third_party/obs-portable/COPYING。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

try:
    import websocket  # websocket-client
except Exception:  # pragma: no cover - 缺依赖时给出可读提示
    websocket = None

SCENE_NAME = "ShareX Flash"
INPUT_NAME = "ShareX Flash 屏幕"
PROFILE_NAME = "ShareX Flash"
DEFAULT_PORT = 4455
WS_RPC_VERSION = 1

# 画质 / 编码器 / 格式：配置值 -> OBS SimpleOutput 取值
REC_QUALITY = {
    "stream": "Stream",
    "high": "High Quality",
    "indistinguishable": "Indistinguishable",
}
REC_ENCODER = {
    "auto": None,           # 交给 OBS 自己选
    "x264": "x264",
    "nvenc": "nvenc",
    "qsv": "qsv",
    "amd": "amd",
}
REC_FORMAT = {
    "mp4": "mp4",
    "mkv": "mkv",
    "hybrid_mp4": "hybrid_mp4",
}
# 分辨率档位：None 表示跟随所选显示器
REC_RESOLUTION = {
    "native": None,
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}


class ObsError(RuntimeError):
    """OBS 后端可预期的失败（找不到程序、连不上、录制失败等）。"""


# ---------------------------------------------------------------- 路径与探测

def app_root():
    """项目根目录：源码运行时是 src 的上级，打包后是可执行文件所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def portable_dir():
    return os.path.join(app_root(), "third_party", "obs-portable")


def _kill_stale_obs(obs_root):
    """杀掉可执行文件位于 obs_root 下的残留 obs64，避免它占住配置目录和端口。"""
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        PROCESS_QUERY_LIMITED = 0x1000
        PROCESS_TERMINATE = 0x0001
        max_pids = 1024
        pids = (wt.DWORD * max_pids)()
        used = wt.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids),
                                   ctypes.byref(used)):
            return 0
        killed = 0
        root_l = os.path.normcase(os.path.abspath(obs_root))
        for i in range(used.value // ctypes.sizeof(wt.DWORD)):
            pid = pids[i]
            if not pid:
                continue
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED | PROCESS_TERMINATE,
                                     False, pid)
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    path = os.path.normcase(buf.value)
                    if path.startswith(root_l + os.sep):
                        if kernel32.TerminateProcess(h, 1):
                            killed += 1
            finally:
                kernel32.CloseHandle(h)
        if killed:
            time.sleep(1.2)      # 等它把配置文件句柄放开
        return killed
    except Exception:
        return 0


def _candidate_exes():
    root = portable_dir()
    yield os.path.join(root, "bin", "64bit", "obs64.exe")
    yield os.path.join(root, "bin", "32bit", "obs32.exe")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not base:
            continue
        yield os.path.join(base, "obs-studio", "bin", "64bit", "obs64.exe")
        yield os.path.join(base, "obs-studio", "bin", "32bit", "obs32.exe")


def find_obs_exe(explicit=""):
    """按 用户指定 -> 内置便携版 -> 系统安装 的顺序定位 OBS 主程序。"""
    if explicit:
        path = explicit.strip().strip('"')
        if os.path.isfile(path):
            return os.path.abspath(path)
    for path in _candidate_exes():
        if os.path.isfile(path):
            return os.path.abspath(path)
    return ""


def is_portable_exe(path):
    """仅当 OBS 位于 third_party 内置目录时才使用 --portable，避免污染用户配置。"""
    try:
        return os.path.commonpath([os.path.abspath(path), portable_dir()]) == portable_dir()
    except ValueError:
        return False


def _obs_data_dirs(obs_root):
    """OBS 便携模式的配置目录（不同版本落在 data/ 或 config/，两处都预写更稳妥）。"""
    return (os.path.join(obs_root, "data", "obs-studio"),
            os.path.join(obs_root, "config", "obs-studio"))


def _ini_set(path, section, key, value):
    """在 ini 里改一个键，保留其它内容（OBS 自己写回的窗口状态等）。"""
    lines = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except Exception:
            lines = []
    out, in_section, done = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not done:
                out.append("%s=%s" % (key, value))
                done = True
            in_section = stripped[1:-1].strip().lower() == section.lower()
            out.append(line)
            continue
        if in_section and stripped.split("=")[0].strip().lower() == key.lower():
            if not done:
                out.append("%s=%s" % (key, value))
                done = True
            continue
        out.append(line)
    if not done:
        if not any(("[%s]" % section).lower() == l.strip().lower() for l in out):
            out.append("[%s]" % section)
        out.append("%s=%s" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def _seed_profile(data_dir, cfg):
    """把输出目录/编码格式/画质写进 profile：这些在 OBS 拉起前落盘最稳。

    视频分辨率与帧率不在这里写——ini 会被 OBS 退出时回写覆盖，
    改走 obs-websocket 的 SetVideoSettings（见 ObsRecorder._apply_video）。
    """
    basic = os.path.join(data_dir, "basic")
    profile_dir = os.path.join(basic, "profiles", PROFILE_NAME)
    os.makedirs(profile_dir, exist_ok=True)
    _ini_set(os.path.join(data_dir, "global.ini"), "Basic", "Profile", PROFILE_NAME)
    _ini_set(os.path.join(data_dir, "global.ini"), "Basic", "ProfileDir", PROFILE_NAME)
    _ini_set(os.path.join(data_dir, "global.ini"), "General", "FirstRun", "false")

    basic_ini = os.path.join(profile_dir, "basic.ini")
    _ini_set(basic_ini, "General", "Name", PROFILE_NAME)

    _ini_set(basic_ini, "Audio", "SampleRate", 48000)
    _ini_set(basic_ini, "Audio", "ChannelSetup", "Stereo")
    _ini_set(basic_ini, "Output", "Mode", "Simple")
    quality = REC_QUALITY.get(cfg.get("rec_quality", "high"))
    if quality:
        _ini_set(basic_ini, "SimpleOutput", "RecQuality", quality)
    encoder = REC_ENCODER.get(cfg.get("rec_encoder", "auto"))
    if encoder:
        _ini_set(basic_ini, "SimpleOutput", "RecEncoder", encoder)
    fmt = REC_FORMAT.get(cfg.get("rec_format", "hybrid_mp4"))
    if fmt:
        _ini_set(basic_ini, "SimpleOutput", "RecFormat2", fmt)
    out_dir = cfg.get("rec_out_dir") or ""
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        # 简单输出模式的录制目录键名是 FilePath（RecFilePath 属于高级输出模式）
        _ini_set(basic_ini, "SimpleOutput", "FilePath", out_dir)
        _ini_set(basic_ini, "SimpleOutput", "RecFilePath", out_dir)
    _ini_set(basic_ini, "SimpleOutput", "RecRB", "false")


def _force_no_systray(user_ini):
    """OBS 的托盘图标开关写在 user.ini 里；每次启动都压成 false（幂等）。

    真正生效的是 [BasicWindow] 段，但历史上也出现过写进 [General] 的版本，
    所以这两组键出现几次就改几次，缺了再补到 [BasicWindow]。
    """
    want = {"systrayenabled": "SysTrayEnabled=false",
            "systraywhenstarted": "SysTrayWhenStarted=false"}
    try:
        with open(user_ini, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except Exception:
        lines = []
    out, applied, sections = [], set(), []
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            sections.append(s[1:-1].lower())
            out.append(line)
            continue
        if "=" in s:
            k = s.split("=", 1)[0].strip().lower()
            if k in want:
                out.append(want[k])
                applied.add(k)
                continue
        out.append(line)
    missing = [k for k in want if k not in applied]
    if missing:
        anchor = "basicwindow" if "basicwindow" in sections else "general"
        if anchor not in sections:
            out.insert(0, "[BasicWindow]")
            sections.insert(0, "basicwindow")
        idx = next(i for i, l in enumerate(out) if l.strip().lower() == "[" + anchor + "]")
        end = len(out)
        for j in range(idx + 1, len(out)):
            s = out[j].strip()
            if s.startswith("[") and s.endswith("]"):
                end = j
                break
        out[end:end] = [want[k] for k in missing]
    with open(user_ini, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def seed_portable_config(obs_root, port, cfg=None):
    """预写 OBS 配置：跳过首次运行向导，并开启 obs-websocket 服务。"""
    seeded = []
    for data_dir in _obs_data_dirs(obs_root):
        try:
            plugin_dir = os.path.join(data_dir, "plugin_config", "obs-websocket")
            os.makedirs(plugin_dir, exist_ok=True)
            # .sentinel 是 OBS 的运行标记（目录，内含 run_* 文件）：上次非正常退出会残留，
            # 导致下次启动弹"崩溃恢复"对话框并卡住等待用户点击。
            sentinel = os.path.join(data_dir, ".sentinel")
            if os.path.isdir(sentinel):
                shutil.rmtree(sentinel, ignore_errors=True)
            elif os.path.exists(sentinel):
                try:
                    os.remove(sentinel)
                except Exception:
                    pass
            user_ini = os.path.join(data_dir, "user.ini")
            if not os.path.isfile(user_ini):
                with open(user_ini, "w", encoding="utf-8") as f:
                    f.write("[General]\nFirstRun=false\n")
            _force_no_systray(user_ini)
            ws_conf = os.path.join(plugin_dir, "config.json")
            payload = {
                "alerts_enabled": False,
                "auth_required": False,
                "first_load": False,
                "server_enabled": True,
                "server_port": int(port),
                "server_password": "",
            }
            with open(ws_conf, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            if cfg:
                _seed_profile(data_dir, cfg)
            seeded.append(data_dir)
        except Exception:
            continue
    return seeded


def port_in_use(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except Exception:
        return False


def pick_free_port(start=DEFAULT_PORT, span=12):
    for port in range(start, start + span):
        if not port_in_use(port):
            return port
    return start


# ---------------------------------------------------------------- 显示器

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wt.DWORD)]


def list_monitors():
    """返回按左上角排序的显示器列表 [(index, left, top, width, height)]。"""
    user32 = ctypes.windll.user32
    found = []

    def _cb(hmon, _hdc, _rect, _data):
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            found.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return 1

    proto = ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC, ctypes.POINTER(_RECT), wt.LPARAM)
    try:
        user32.EnumDisplayMonitors(None, None, proto(_cb), 0)
    except Exception:
        pass
    if not found:
        rect = _virtual_rect()
        found = [(rect[0], rect[1], rect[2], rect[3])]
    found.sort(key=lambda m: (m[0], m[1]))
    return [(i,) + m for i, m in enumerate(found)]


def _virtual_rect():
    user32 = ctypes.windll.user32
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def monitor_under_cursor(monitors=None):
    """返回鼠标所在显示器的序号，用于"跟随鼠标"采集。"""
    pt = wt.POINT()
    try:
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    except Exception:
        return 0
    for item in (monitors or list_monitors()):
        idx, left, top, w, h = item
        if left <= pt.x < left + w and top <= pt.y < top + h:
            return idx
    return 0


# ---------------------------------------------------------------- WebSocket 客户端

def auth_string(password, challenge, salt):
    """obs-websocket v5 口令摘要：base64(sha256(sha256(pwd+salt)+challenge))。"""
    secret = hashlib.sha256((password + salt).encode("utf-8")).digest()
    digest = hashlib.sha256(secret + challenge.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


class ObsWs(object):
    """最小 obs-websocket v5 客户端：Hello/Identify 握手 + 请求响应 + 事件回调。"""

    def __init__(self):
        self._ws = None
        self._alive = False
        self._send_lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._pending = {}
        self._reader = None
        self._events = {}

    # -- 连接
    def connect(self, host="127.0.0.1", port=DEFAULT_PORT, password="", timeout=8.0):
        if websocket is None:
            raise ObsError("缺少依赖 websocket-client，请执行 pip install -r requirements.txt")
        url = "ws://%s:%d" % (host, int(port))
        try:
            self._ws = websocket.create_connection(
                url, timeout=timeout, subprotocols=["obswebsocket.json"])
        except Exception as e:
            raise ObsError("连接 OBS WebSocket 失败（%s）：%s" % (url, e))

        hello = self._recv_once(timeout)
        if not isinstance(hello, dict) or hello.get("op") != 0:
            self.close()
            raise ObsError("OBS WebSocket 握手异常：%r" % (hello,))
        info = hello.get("d") or {}
        identify = {"rpcVersion": WS_RPC_VERSION}
        auth = info.get("authentication")
        if auth:
            identify["authentication"] = auth_string(
                password, auth.get("challenge", ""), auth.get("salt", ""))
        self._send_json({"op": 1, "d": identify})
        reply = self._recv_once(timeout)
        if not isinstance(reply, dict) or reply.get("op") != 2:
            detail = ""
            if isinstance(reply, dict) and reply.get("op") == 5:
                detail = "（收到事件而非认证结果）"
            self.close()
            raise ObsError("OBS 拒绝 WebSocket 认证%s，请检查口令设置" % detail)

        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, name="obs-ws", daemon=True)
        self._reader.start()

    def _recv_once(self, timeout):
        try:
            self._ws.settimeout(timeout)
            raw = self._ws.recv()
        except Exception as e:
            raise ObsError("读取 OBS WebSocket 数据失败：%s" % e)
        try:
            return json.loads(raw)
        except Exception:
            raise ObsError("OBS WebSocket 返回了非 JSON 数据")

    def _send_json(self, payload):
        raw = json.dumps(payload, ensure_ascii=True)
        with self._send_lock:
            self._ws.send(raw)

    def _read_loop(self):
        while self._alive:
            try:
                raw = self._ws.recv()
            except Exception:
                if self._alive:
                    time.sleep(0.05)
                    try:
                        self._ws.settimeout(10.0)
                        continue
                    except Exception:
                        pass
                break
            if not raw:
                self._alive = False
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            op = msg.get("op")
            body = msg.get("d") or {}
            if op == 7:  # RequestResponse
                rid = body.get("requestId")
                with self._req_lock:
                    slot = self._pending.pop(rid, None)
                if slot is not None:
                    slot["payload"] = msg
                    slot["event"].set()
            elif op == 5:  # Event
                handler = self._events.get(body.get("eventType"))
                if handler:
                    try:
                        handler(body.get("eventData") or {})
                    except Exception:
                        pass
        self._alive = False

    # -- 请求
    def request(self, request_type, data=None, timeout=10.0):
        # 207 = RequestNotReady：OBS 前端还没加载完，同样的请求隔一会儿再发就能过
        for attempt in range(6):
            try:
                return self._request_once(request_type, data, timeout)
            except ObsError as e:
                if attempt < 5 and "（207）" in str(e):
                    time.sleep(0.5)
                    continue
                raise

    def _request_once(self, request_type, data=None, timeout=10.0):
        if not self._alive:
            raise ObsError("OBS WebSocket 连接已断开")
        rid = uuid.uuid4().hex
        slot = {"event": threading.Event(), "payload": None}
        with self._req_lock:
            self._pending[rid] = slot
        self._send_json({"op": 6, "d": {
            "requestType": request_type,
            "requestId": rid,
            "requestData": data or {},
        }})
        if not slot["event"].wait(timeout):
            with self._req_lock:
                self._pending.pop(rid, None)
            raise ObsError("OBS 请求超时：%s" % request_type)
        body = (slot["payload"] or {}).get("d") or {}
        status = body.get("requestStatus") or {}
        if not status.get("result"):
            raise ObsError("%s 执行失败（%s）：%s" % (
                request_type, status.get("code"), status.get("comment") or "未知原因"))
        return body.get("responseData") or {}

    def on_event(self, event_type, handler):
        self._events[event_type] = handler

    @property
    def alive(self):
        return self._alive

    def close(self):
        self._alive = False
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None


# ---------------------------------------------------------------- 录制器

class ObsRecorder(object):
    """面向 ShareX Flash 的 OBS 录制控制器（线程安全，方法可跨线程调用）。"""

    def __init__(self, log=None):
        self._log = log or (lambda msg: None)
        self._ws = None
        self._proc = None
        self._port = DEFAULT_PORT
        self._exe = ""
        self._portable = False
        self._state = "idle"          # idle | recording | paused
        self._out_path = ""
        self._out_dir = ""
        self._lock = threading.RLock()
        self._last_error = ""

    # -- 状态
    @property
    def state(self):
        return self._state

    @property
    def last_error(self):
        return self._last_error

    @property
    def out_path(self):
        return self._out_path

    def describe(self, explicit=""):
        """给 UI 用的后端探测结果。"""
        exe = find_obs_exe(explicit)
        return {
            "exe": exe,
            "found": bool(exe),
            "portable": bool(exe) and is_portable_exe(exe),
            "exe_path": portable_dir(),
        }

    # -- 进程
    def _launch(self, exe, portable, port, cfg):
        obs_root = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        # 上一轮异常退出常留下一个占着 portable 配置目录的 obs64，它会让新实例
        # 抢不到配置/端口，表现就是“等 WebSocket 端口超时”。先清掉自家这个目录下的残留。
        _kill_stale_obs(obs_root)
        args = [exe]
        if portable:
            seed_portable_config(obs_root, port, cfg)
            args.append("--portable")
        # 不加 --minimize-to-tray：那会给托盘登记一个 OBS 图标，用户明确不要；
        # 改为启动后用 SW_HIDE + _hide_windows 压住窗口，托盘里就不会多出 OBS
        args.append("--disable-shutdown-check")
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE：OBS 初始化时会自己 ShowWindow，随后由 _hide_windows 兜住
        creation = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creation |= subprocess.CREATE_NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(
            args, cwd=os.path.dirname(exe), startupinfo=si, creationflags=creation,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 光靠启动参数不够稳：OBS 启动过程中会主动显示主窗口，这里持续压一段时间
        threading.Thread(target=self._hide_windows, args=(self._proc.pid,),
                         name="obs-hide", daemon=True).start()
        self._log("已启动 OBS：%s（portable=%s, port=%d）" % (exe, portable, port))

    @staticmethod
    def _hide_windows(pid, seconds=12.0):
        """把属于 OBS 进程的可见顶层窗口藏掉，录制期间完全不出现界面。"""
        try:
            user32 = ctypes.windll.user32
            winfun = ctypes.WINFUNCTYPE(ctypes.c_int, wt.HWND, wt.LPARAM)
            user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
            user32.IsWindowVisible.argtypes = [wt.HWND]
            user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
            user32.EnumWindows.argtypes = [winfun, wt.LPARAM]
        except Exception:
            return
        deadline = time.monotonic() + seconds
        # 启动头两秒窗口创建最密，压得紧一点，之后放慢避免空转
        while time.monotonic() < deadline:
            try:
                def _cb(hwnd, _lparam):
                    try:
                        wpid = wt.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                        if wpid.value == pid and user32.IsWindowVisible(hwnd):
                            user32.ShowWindow(hwnd, 0)  # SW_HIDE
                    except Exception:
                        pass
                    return 1

                user32.EnumWindows(winfun(_cb), 0)
            except Exception:
                pass
            time.sleep(0.08 if time.monotonic() - (deadline - seconds) < 2.5 else 0.25)

    def _wait_port(self, port, timeout=45.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise ObsError("OBS 进程已退出（返回码 %s），常见原因：显卡驱动过旧或首次启动弹窗被关闭"
                               % self._proc.returncode)
            if port_in_use(port):
                return True
            time.sleep(0.3)
        self._kill_proc()                    # 超时不留垃圾进程，否则下一轮又撞端口
        raise ObsError("等待 OBS WebSocket 端口 %d 超时（45 秒）" % port)

    def _kill_proc(self):
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
        except Exception:
            pass

    def _connect(self, port, timeout=20.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                ws = ObsWs()
                ws.connect(port=port, timeout=6.0)
                return ws
            except ObsError as e:
                last = e
                time.sleep(0.5)
        raise last or ObsError("连接 OBS WebSocket 失败")

    def ensure(self, cfg):
        """确保 OBS 进程与 WebSocket 就绪；返回是否由本次调用新启动。"""
        with self._lock:
            if self._ws is not None and self._ws.alive:
                return False
            exe = find_obs_exe(cfg.get("rec_obs_path", ""))
            if not exe:
                raise ObsError(
                    "未找到 OBS。请先运行 `python tools/fetch_obs.py` 下载内置便携版，"
                    "或在设置里指定 obs64.exe 路径（默认查找 third_party/obs-portable）")
            portable = is_portable_exe(exe)
            _kill_stale_obs(os.path.dirname(os.path.dirname(os.path.dirname(exe))))
            self._port = pick_free_port(DEFAULT_PORT)
            self._exe, self._portable = exe, portable
            self._launch(exe, portable, self._port, cfg)
            self._wait_port(self._port)
            self._ws = self._connect(self._port)
            try:
                ver = self._ws.request("GetVersion", {}, timeout=8.0)
                self._log("已连接 OBS %s / obs-websocket %s" % (
                    ver.get("obsVersion"), ver.get("obsWebSocketVersion")))
            except ObsError:
                pass
            self._state = "idle"
            self._wait_frontend()
            return True

    def _wait_frontend(self, timeout=12.0):
        """WebSocket 端口往往比 OBS 前端就绪更早，早发请求会拿到 207（not ready）。"""
        began = time.monotonic()
        while time.monotonic() - began < timeout:
            try:
                self._ws.request("GetCurrentProgramScene", {}, timeout=5.0)
                if time.monotonic() - began > 2.0:
                    self._log("OBS 前端就绪较慢（%.1fs），已等待" % (time.monotonic() - began))
                return True
            except ObsError:
                time.sleep(0.4)
        self._log("⚠ OBS 前端 %.0fs 内未就绪，继续尝试配置" % timeout)
        return False

    # -- 配置
    def _set_profile(self, category, name, value):
        try:
            self._ws.request("SetProfileParameter", {
                "parameterCategory": category,
                "parameterName": name,
                "parameterValue": str(value),
            }, timeout=8.0)
            return True
        except ObsError as e:
            self._log("配置项 %s/%s=%s 未能生效：%s" % (category, name, value, e))
            return False

    def _apply_video(self, cfg):
        monitors = list_monitors()
        target = cfg.get("rec_monitor", 0)
        if isinstance(target, int) and target < 0:
            target = monitor_under_cursor(monitors)
        if not isinstance(target, int) or target >= len(monitors):
            target = 0
        native = monitors[target][3:] if monitors else (1920, 1080)
        res = REC_RESOLUTION.get(cfg.get("rec_resolution", "native"))
        width, height = res if res else native
        # OBS 要求偶数分辨率，否则部分编码器直接拒绝
        width = max(2, (int(width) // 2) * 2)
        height = max(2, (int(height) // 2) * 2)
        fps = int(cfg.get("rec_fps", 30) or 30)
        # 写 ini 不生效（OBS 只在自己退出时回写），走 SetVideoSettings 立刻改画布
        self._ws.request("SetVideoSettings", {
            "baseWidth": width, "baseHeight": height,
            "outputWidth": width, "outputHeight": height,
            "fpsNumerator": fps, "fpsDenominator": 1,
        }, timeout=10.0)
        try:
            actual = self._ws.request("GetVideoSettings", {}, timeout=8.0)
            if actual.get("outputWidth") != width or actual.get("outputHeight") != height \
                    or actual.get("fpsNumerator") != fps:
                self._log("视频参数未完全生效：实际 %sx%s@%s" % (
                    actual.get("outputWidth"), actual.get("outputHeight"),
                    actual.get("fpsNumerator")))
        except ObsError:
            pass
        self._set_profile("Audio", "SampleRate", 48000)
        self._set_profile("Audio", "ChannelSetup", "Stereo")
        return target, monitors

    def _apply_output(self, cfg):
        self._set_profile("Output", "Mode", "Simple")
        out_dir = cfg.get("rec_out_dir") or ""
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            self._out_dir = out_dir
            # 简单输出模式的录制目录键名是 FilePath（RecFilePath 属于高级输出模式）
            self._set_profile("SimpleOutput", "FilePath", out_dir)
            self._set_profile("SimpleOutput", "RecFilePath", out_dir)
        quality = REC_QUALITY.get(cfg.get("rec_quality", "high"))
        if quality:
            self._set_profile("SimpleOutput", "RecQuality", quality)
        encoder = REC_ENCODER.get(cfg.get("rec_encoder", "auto"))
        if encoder:
            self._set_profile("SimpleOutput", "RecEncoder", encoder)
        fmt = REC_FORMAT.get(cfg.get("rec_format", "hybrid_mp4"))
        if fmt:
            self._set_profile("SimpleOutput", "RecFormat2", fmt)
        self._set_profile("SimpleOutput", "RecRB", "false")

    def _ensure_scene(self):
        scenes = (self._ws.request("GetSceneList", {}, timeout=8.0).get("scenes") or [])
        names = [s.get("sceneName") for s in scenes]
        if SCENE_NAME not in names:
            self._ws.request("CreateScene", {"sceneName": SCENE_NAME}, timeout=8.0)
        self._ws.request("SetCurrentProgramScene", {"sceneName": SCENE_NAME}, timeout=8.0)

    def _ensure_input(self):
        inputs = (self._ws.request("GetInputList", {}, timeout=8.0).get("inputs") or [])
        names = [i.get("inputName") for i in inputs]
        if INPUT_NAME in names:
            return
        self._ws.request("CreateInput", {
            "sceneName": SCENE_NAME,
            "inputName": INPUT_NAME,
            "inputKind": "monitor_capture",
            "inputSettings": {"capture_cursor": True},
            "sceneItemEnabled": True,
        }, timeout=10.0)

    @staticmethod
    def _item_origin(item_name):
        """从 "显示器 1: 2560x1440 @ 0,0" 里解析显示器坐标。"""
        match = re.search(r"@\s*(-?\d+)\s*,\s*(-?\d+)", item_name or "")
        if not match:
            return None
        return (int(match.group(1)), int(match.group(2)))

    def _apply_monitor(self, index, monitors, show_cursor=True):
        settings = {"capture_cursor": bool(show_cursor)}
        applied = False
        for prop in ("monitor_id", "monitor"):
            try:
                items = self._ws.request("GetInputPropertiesListPropertyItems", {
                    "inputName": INPUT_NAME, "propertyName": prop,
                }, timeout=8.0).get("propertyItems") or []
            except ObsError:
                continue
            items = [i for i in items if i.get("itemEnabled", True)]
            if not items:
                continue
            chosen = None
            if monitors and index < len(monitors):
                origin = monitors[index][1:3]
                for item in items:
                    if self._item_origin(item.get("itemName")) == origin:
                        chosen = item
                        break
            if chosen is None:
                chosen = items[index] if index < len(items) else items[0]
            settings[prop] = chosen.get("itemValue")
            applied = True
            break
        if not applied:
            self._log("未能确定显示器属性名，使用 OBS 当前默认采集目标")
            return
        self._ws.request("SetInputSettings", {
            "inputName": INPUT_NAME,
            "inputSettings": settings,
            "overlay": True,
        }, timeout=8.0)

    def _apply_audio(self, mode):
        """按"系统声音 / 麦克风 / 两者 / 静音"控制 OBS 全局音频源。"""
        inputs = (self._ws.request("GetInputList", {}, timeout=8.0).get("inputs") or [])
        for item in inputs:
            kind = (item.get("unversionedInputKind") or item.get("inputKind") or "").lower()
            name = item.get("inputName")
            is_system = "wasapi_output_capture" in kind
            is_mic = "wasapi_input_capture" in kind
            if not (is_system or is_mic):
                continue
            if mode == "both":
                muted = False
            elif mode == "system":
                muted = is_mic
            elif mode == "mic":
                muted = is_system
            else:  # none
                muted = True
            try:
                self._ws.request("SetInputMute", {
                    "inputName": name, "inputMuted": bool(muted),
                }, timeout=6.0)
            except ObsError as e:
                self._log("设置音频源 %s 失败：%s" % (name, e))

    def configure(self, cfg):
        """把所有录制参数写进 OBS，返回实际采集的显示器序号。"""
        with self._lock:
            index, monitors = self._apply_video(cfg)
            self._apply_output(cfg)
            self._ensure_scene()
            self._ensure_input()
            self._apply_monitor(index, monitors, cfg.get("rec_cursor", True))
            self._apply_audio(cfg.get("rec_audio", "system"))
            return index

    # -- 录制
    def start(self, cfg):
        with self._lock:
            if self._state in ("recording", "paused"):
                # 只有“连接还活着 + 进程还在”才算真的在录；
                # 否则是 OBS 被外部关掉留下的残留状态，必须清掉再重开
                if self._ws is not None and self._ws.alive and self.alive():
                    return False
                self._state = "idle"
            self.ensure(cfg)
            self.configure(cfg)
            self._out_path = ""
            try:
                self._ws.request("StartRecord", {}, timeout=15.0)
            except ObsError as e:
                # hybrid_mp4 在个别旧版本上不可用，退回 mkv 再试一次
                if REC_FORMAT.get(cfg.get("rec_format", "hybrid_mp4")):
                    self._log("启动录制失败，回退 mkv 重试：%s" % e)
                    self._set_profile("SimpleOutput", "RecFormat2", "mkv")
                    self._ws.request("StartRecord", {}, timeout=15.0)
                else:
                    raise
            # StartRecord 是异步的：输出真正跑起来要一两百毫秒，轮询比立刻断言稳
            deadline = time.time() + 3.0
            active = False
            while time.time() < deadline:
                status = self._ws.request("GetRecordStatus", {}, timeout=8.0)
                if status.get("outputActive"):
                    active = True
                    break
                time.sleep(0.2)
            if not active:
                raise ObsError("OBS 未进入录制状态，请检查编码器是否可用")
            self._state = "recording"
            return True

    def pause(self):
        with self._lock:
            if self._state != "recording":
                return False
            self._ws.request("PauseRecord", {}, timeout=8.0)
            self._state = "paused"
            return True

    def resume(self):
        with self._lock:
            if self._state != "paused":
                return False
            self._ws.request("ResumeRecord", {}, timeout=8.0)
            self._state = "recording"
            return True

    def stop(self):
        """停止录制并返回文件绝对路径（拿不到时返回空字符串）。"""
        data = None
        with self._lock:
            if self._state not in ("recording", "paused"):
                return ""
            try:
                data = self._ws.request("StopRecord", {}, timeout=30.0)
            except Exception as e:
                # OBS 偶发 not ready，直接让它自己收尾退出，否则录制会一直挂着
                self._log("StopRecord 失败：%s，改为关闭 OBS 收尾" % e)
            finally:
                self._state = "idle"
            path = (data or {}).get("outputPath") or ""
            self._out_path = path or self._fallback_path()
        if data is None:
            try:
                self.shutdown(stop_first=False)
            except Exception as e:
                self._log("兜底关闭 OBS 失败：%s" % e)
        if self._out_path:
            self._wait_file_settled(self._out_path, timeout=8.0 if data is None else 3.0)
        return self._out_path

    def alive(self):
        """OBS 进程是否还在；用来区分“查询失败”和“真的退出了”。"""
        proc = self._proc
        return proc is not None and proc.poll() is None

    def reset(self):
        """OBS 被外部关掉后清空内部状态，避免下一次操作被残留的“录制中”挡住。"""
        with self._lock:
            ws = self._ws
            self._ws = None
            self._proc = None
            self._state = "idle"
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    @staticmethod
    def _wait_file_settled(path, timeout=3.0):
        """封装是异步落盘的：等文件出现且大小稳定，否则上层立刻退出 OBS 会拿到 0 字节文件。"""
        deadline = time.time() + timeout
        last = -1
        while time.time() < deadline:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size > 0 and size == last:
                return True
            last = size
            time.sleep(0.2)
        return False

    def _fallback_path(self):
        """StopRecord 未回传路径时，按扩展名在输出目录里找最新文件。"""
        exts = (".mp4", ".mkv", ".mov", ".flv", ".ts")
        newest, newest_ts = "", 0.0
        for folder in (self._out_dir,):
            if not folder or not os.path.isdir(folder):
                continue
            try:
                for name in os.listdir(folder):
                    if not name.lower().endswith(exts):
                        continue
                    full = os.path.join(folder, name)
                    ts = os.path.getmtime(full)
                    if ts > newest_ts:
                        newest, newest_ts = full, ts
            except Exception:
                continue
        return newest

    def status(self):
        with self._lock:
            if self._ws is None or not self._ws.alive:
                return {"active": False, "paused": False, "timecode": "", "state": self._state}
            try:
                data = self._ws.request("GetRecordStatus", {}, timeout=6.0)
            except ObsError as e:
                # 注意：这里不能谎报 active=False —— 上层会当成“OBS 已停止录制”，
                # 结果录制还在跑、界面却显示结束，再按热键又开了新录制。
                self._log("查询录制状态失败：%s" % e)
                return {"error": str(e), "state": self._state}
            return {
                "active": bool(data.get("outputActive")),
                "paused": bool(data.get("outputPaused")),
                "timecode": data.get("outputTimecode") or "",
                "bytes": data.get("outputBytes") or 0,
                "state": self._state,
            }

    def shutdown(self, stop_first=True):
        with self._lock:
            if stop_first and self._state in ("recording", "paused"):
                try:
                    self.stop()
                except Exception as e:
                    self._log("退出前停止录制失败：%s" % e)
            ws = self._ws
            self._ws = None
            proc = self._proc
            self._proc = None
            self._state = "idle"
        if proc is None or proc.poll() is not None:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            return
        # 先请 OBS 自己正常退出：强杀会留下 .sentinel，下次启动被判为崩溃
        quit_ok = False
        if ws is not None and ws.alive:
            try:
                ws.request("Quit", {}, timeout=5.0)
                proc.wait(timeout=12)
                quit_ok = proc.poll() is not None
            except Exception:
                quit_ok = False
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if not quit_ok and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------- 自检

def _selftest():
    print("== 显示器 ==")
    for item in list_monitors():
        print("  #%d %dx%d @ %d,%d" % (item[0], item[3], item[4], item[1], item[2]))
    print("== OBS 定位 ==")
    exe = find_obs_exe()
    print("  exe      : %s" % (exe or "未找到（运行 python tools/fetch_obs.py 下载）"))
    if exe:
        print("  portable : %s" % is_portable_exe(exe))
    print("== 协议 ==")
    # 用 obs-websocket 文档给出的算法做一次确定性校验
    digest = auth_string("secret", "challenge", "salt")
    print("  认证摘要 : %s (len=%d)" % (digest, len(digest)))
    assert len(digest) == 44 and digest.endswith("="), "认证摘要长度异常"
    print("== 端口 ==")
    print("  可用端口 : %d (4455 占用=%s)" % (pick_free_port(), port_in_use(4455)))
    print("自检通过")


if __name__ == "__main__":
    _selftest()
