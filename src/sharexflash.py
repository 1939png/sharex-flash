# -*- coding: utf-8 -*-
"""
Share X Flash
-------------------
一款轻量的 Windows 截图工具。
捕获后端采用与 ShareX 相同的 GDI BitBlt / CopyFromScreen 方案（Pillow ImageGrab 底层即 GDI 抓屏），
支持多显示器与高 DPI，无需安装 ShareX 本体即可独立运行。

功能：
  * 全屏 / 区域 / 当前窗口 三种截图方式，均可自定义多键快捷键
  * 保存目录、图片格式（PNG/JPG/BMP/WEBP/GIF）、文件名模板可配置
  * 截图"咔嚓"音效（内置合成，无外部素材）
  * 截图后右下角悬浮预览 10 秒（时长可调），始终置顶，可覆盖全屏应用
  * 悬浮窗三个按钮：打开图片 / 复制图片 / 删除图片
"""

import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import queue
import random
import re
import struct
import sys
import threading
import time
import traceback
import wave

import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

APP_NAME = "ShareXFlash"
APP_TITLE = "Share X Flash"
VERSION = "1.2.3"

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ShareXFlash")
_OLD_APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "QuickShot")


def _migrate_legacy_config():
    """从旧版 QuickShot 目录平滑迁移配置，避免用户重设。"""
    try:
        if os.path.isdir(APP_DIR) or not os.path.isdir(_OLD_APP_DIR):
            return
        os.makedirs(APP_DIR, exist_ok=True)
        for name in ("config.json",):
            s = os.path.join(_OLD_APP_DIR, name)
            if os.path.isfile(s):
                import shutil
                shutil.copy2(s, os.path.join(APP_DIR, name))
    except Exception:
        pass
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SOUND_PATH = os.path.join(APP_DIR, "shutter.wav")
LOG_PATH = os.path.join(APP_DIR, "sharexflash.log")

DEFAULT_CONFIG = {
    "hotkey_full": "Ctrl+Alt+A",
    "hotkey_region": "Ctrl+Alt+S",
    "hotkey_window": "Ctrl+Alt+W",
    "hotkey_panel": "Ctrl+Alt+O",
    "save_dir": os.path.join(os.path.expanduser("~"), "Pictures", "ShareXFlash"),
    "image_format": "png",
    "quality": 92,
    "name_template": "ShareX_{yyyy}{MM}{dd}_{HH}{mm}{ss}",
    "sound_enabled": True,
    "auto_copy": False,
    "preview_enabled": True,
    "preview_seconds": 10,
    "autostart": False,
}

FORMATS = [("PNG（无损，推荐）", "png"), ("JPG（体积小）", "jpg"),
           ("BMP（无压缩）", "bmp"), ("WEBP（高压缩）", "webp"), ("GIF（256色）", "gif")]

# ---------------------------------------------------------------- 基础工具


def resource_path(name):
    """打包(onefile)后资源在 sys._MEIPASS，源码运行时在脚本同级目录。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, name)
    if not os.path.isfile(p):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    return p


def log(msg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def enable_dpi_awareness():
    """开启 Per-Monitor V2 DPI 感知，保证高分屏下截到物理像素。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def load_config():
    _migrate_legacy_config()
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception as e:
        log("读取配置失败: %r" % (e,))
    return cfg


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 虚拟屏幕

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def virtual_screen_rect():
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wt.DWORD)]


def work_area_of_monitor_under_cursor():
    """返回鼠标所在显示器的可用工作区（已剔除任务栏）。"""
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    hmon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        r = mi.rcWork
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


# ---------------------------------------------------------------- 抓屏后端

def grab_screen():
    """GDI 抓屏（等价 ShareX 的 CopyFromScreen），覆盖全部显示器。"""
    from PIL import ImageGrab
    return ImageGrab.grab(all_screens=True)


def grab_active_window(full_img):
    """截取前台窗口（含 DWM 外框修正），从已抓好的全屏图中裁切。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None
    rect = RECT()
    got = False
    try:
        # DWMWA_EXTENDED_FRAME_BOUNDS = 9，规避 Win10 阴影边框
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect))
        got = (hr == 0)
    except Exception:
        got = False
    if not got:
        got = bool(user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)))
    if not got:
        return None, None
    vx, vy, vw, vh = virtual_screen_rect()
    left = max(0, rect.left - vx)
    top = max(0, rect.top - vy)
    right = min(vw, rect.right - vx)
    bottom = min(vh, rect.bottom - vy)
    if right - left < 8 or bottom - top < 8:
        return None, None
    return full_img.crop((left, top, right, bottom)), hwnd


def hwnd_of_own_windows():
    hs = []
    for name in ("ShareXFlashMainWindow",):
        try:
            h = user32.FindWindowW(None, APP_TITLE)
            if h:
                hs.append(h)
        except Exception:
            pass
    return hs


# ---------------------------------------------------------------- 剪贴板

def copy_image_to_clipboard(pil_img):
    """把 PIL 图片写入剪贴板（CF_DIB），不用 pywin32。"""
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]  # 去掉 BMP 文件头，保留 BITMAPINFOHEADER + 像素
    buf.close()

    user32.OpenClipboard.argtypes = [wt.HWND]
    user32.OpenClipboard.restype = wt.BOOL
    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wt.HGLOBAL
    kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    user32.SetClipboardData.restype = wt.HANDLE

    if not user32.OpenClipboard(None):
        raise RuntimeError("无法打开剪贴板（可能被其他程序占用）")
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not h:
            raise RuntimeError("内存分配失败")
        p = kernel32.GlobalLock(h)
        if not p:
            raise RuntimeError("锁定内存失败")
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(8, h):  # CF_DIB = 8
            raise RuntimeError("写入剪贴板失败")
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- 音效

def ensure_shutter_sound():
    """合成一段相机快门"咔嚓"音效，写入 wav 文件。"""
    if os.path.isfile(SOUND_PATH) and os.path.getsize(SOUND_PATH) > 2000:
        return SOUND_PATH
    os.makedirs(APP_DIR, exist_ok=True)
    sr = 44100
    dur = 0.18
    n = int(sr * dur)
    rnd = random.Random(20240607)
    frames = bytearray()
    events = ((0.000, 1.00, 150.0, 2400.0), (0.052, 0.72, 110.0, 1750.0))
    for i in range(n):
        t = i / sr
        v = 0.0
        for start, amp, decay, freq in events:
            if t >= start:
                d = t - start
                env = pow(2.718281828, -decay * d)
                noise = rnd.uniform(-1.0, 1.0)
                tone = 0.6 * __import__("math").sin(2 * 3.141592653589793 * freq * d)
                v += amp * env * (0.65 * noise + 0.55 * tone)
        v = max(-1.0, min(1.0, v * 0.55))
        frames += struct.pack("<h", int(v * 32000))
    with wave.open(SOUND_PATH, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return SOUND_PATH


def play_shutter():
    try:
        import winsound
        path = ensure_shutter_sound()
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as e:
        log("播放音效失败: %r" % (e,))


# ---------------------------------------------------------------- 全局快捷键

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312

VK_NAMED = {
    "PRINTSCREEN": 0x2C, "PRTSC": 0x2C, "PRTSCR": 0x2C, "SNAPSHOT": 0x2C, "SYSREQ": 0x2C,
    "INSERT": 0x2D, "INS": 0x2D, "DELETE": 0x2E, "DEL": 0x2E,
    "BACKSPACE": 0x08, "BACK": 0x08, "BS": 0x08,
    "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "PRIOR": 0x21, "NEXT": 0x22,
    "SPACE": 0x20, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D,
    "ESC": 0x1B, "ESCAPE": 0x1B,
    "PAUSE": 0x13, "BREAK": 0x13, "SCROLLLOCK": 0x91, "CAPSLOCK": 0x14,
    "NUMLOCK": 0x90, "APPS": 0x5D, "MENU": 0x5D, "HELP": 0x2F,
    "CLEAR": 0x0C, "SELECT": 0x29, "SLEEP": 0x5F,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "KP0": 0x60, "KP1": 0x61, "KP2": 0x62, "KP3": 0x63, "KP4": 0x64,
    "KP5": 0x65, "KP6": 0x66, "KP7": 0x67, "KP8": 0x68, "KP9": 0x69,
    "KPMULTIPLY": 0x6A, "KPADD": 0x6B, "KPSEPARATOR": 0x6C, "KPSUBTRACT": 0x6D,
    "KPDECIMAL": 0x6E, "KPDIVIDE": 0x6F,
    "MULTIPLY": 0x6A, "ADD": 0x6B, "SUBTRACT": 0x6D, "DECIMAL": 0x6E, "DIVIDE": 0x6F,
    "MUTE": 0xAD, "VOLUMEDOWN": 0xAE, "VOLUMEUP": 0xAF,
    "MEDIANEXT": 0xB0, "MEDIAPREV": 0xB1, "MEDIASTOP": 0xB2, "MEDIAPLAY": 0xB3,
    "BROWSERBACK": 0xA6, "BROWSERFORWARD": 0xA7, "BROWSERREFRESH": 0xA8,
    "BROWSERSTOP": 0xA9, "BROWSERSEARCH": 0xAA, "BROWSERHOME": 0xAC,
    "LAUNCHMAIL": 0xB4, "LAUNCHMEDIA": 0xB5, "LAUNCHAPP1": 0xB6, "LAUNCHAPP2": 0xB7,
}

# 虚拟键码 -> 规范名称（规范名排在前面，setdefault 保证优先取到）
_CANON_NAMES = [
    "PRINTSCREEN", "INSERT", "DELETE", "BACKSPACE", "HOME", "END", "PAGEUP", "PAGEDOWN",
    "SPACE", "TAB", "ENTER", "ESC", "PAUSE", "SCROLLLOCK", "CAPSLOCK", "NUMLOCK",
    "APPS", "HELP", "CLEAR", "SELECT", "SLEEP",
    "UP", "DOWN", "LEFT", "RIGHT",
    "`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/",
    "KP0", "KP1", "KP2", "KP3", "KP4", "KP5", "KP6", "KP7", "KP8", "KP9",
    "KPMULTIPLY", "KPADD", "KPSEPARATOR", "KPSUBTRACT", "KPDECIMAL", "KPDIVIDE",
    "MUTE", "VOLUMEDOWN", "VOLUMEUP", "MEDIANEXT", "MEDIAPREV", "MEDIASTOP",
    "MEDIAPLAY", "BROWSERBACK", "BROWSERFORWARD", "BROWSERREFRESH", "BROWSERSTOP",
    "BROWSERSEARCH", "BROWSERHOME", "LAUNCHMAIL", "LAUNCHMEDIA", "LAUNCHAPP1",
    "LAUNCHAPP2",
]
VK_CANON = {}
for _n in _CANON_NAMES:
    VK_CANON.setdefault(VK_NAMED[_n], _n)
del _n

# Shift 组合产生的 keysym（Tk 后备路径用）
KEYSYM_SYMBOL_VK = {
    "EXCLAM": "1", "AT": "2", "NUMBERSIGN": "3", "DOLLAR": "4", "PERCENT": "5",
    "ASCIIUCIRCUMFLEX": "6", "ASCIICIRCUM": "6", "AMPERSAND": "7", "ASTERISK": "8",
    "PARENLEFT": "9", "PARENRIGHT": "0", "UNDERSCORE": "-", "PLUS": "=",
    "BRACELEFT": "[", "BRACERIGHT": "]", "BAR": "\\", "COLON": ";", "QUOTEDBL": "'",
    "LESS": ",", "GREATER": ".", "QUESTION": "/", "TILDE": "`",
    "APOSTROPHE": "'", "QUOTELEFT": "`", "SEMICOLON": ";", "COMMA": ",", "PERIOD": ".",
    "SLASH": "/", "BACKSLASH": "\\", "EQUAL": "=", "MINUS": "-",
    "BRACKETLEFT": "[", "BRACKETRIGHT": "]", "BACKSPACE": "BACKSPACE",
    "KP_ENTER": "ENTER", "KP_ADD": "KPADD", "KP_SUBTRACT": "KPSUBTRACT",
    "KP_MULTIPLY": "KPMULTIPLY", "KP_DIVIDE": "KPDIVIDE", "KP_DECIMAL": "KPDECIMAL",
    "NUM_LOCK": "NUMLOCK", "CAPS_LOCK": "CAPSLOCK", "SCROLL_LOCK": "SCROLLLOCK",
    "MENU": "APPS", "SYSREQ": "PRINTSCREEN", "PRINT": "PRINTSCREEN",
    "PRIOR": "PAGEUP", "NEXT": "PAGEDOWN", "RETURN": "ENTER", "ESCAPE": "ESC",
}


def vk_to_token(vk):
    """虚拟键码 -> 配置里的按键 token；无法表达时返回 None。"""
    if not vk:
        return None
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x6F)
    return VK_CANON.get(vk)


def parse_hotkey(text):
    """'Ctrl+Alt+A' -> (mods, vk)；不合法抛 ValueError。"""
    if not text or not text.strip():
        raise ValueError("快捷键为空")
    parts = [p.strip() for p in re.split(r"\+", text) if p.strip()]
    if len(parts) < 1:
        raise ValueError("快捷键为空")
    mods = 0
    vk = None
    for p in parts:
        u = p.upper()
        if u in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif u == "ALT":
            mods |= MOD_ALT
        elif u == "SHIFT":
            mods |= MOD_SHIFT
        elif u in ("WIN", "WINDOWS", "SUPER"):
            mods |= MOD_WIN
        else:
            if vk is not None:
                raise ValueError("只能包含一个主按键：%s" % text)
            if len(u) == 1 and ("A" <= u <= "Z" or "0" <= u <= "9"):
                vk = ord(u)
            elif re.fullmatch(r"F([1-9]|1[0-9]|2[0-4])", u):
                vk = 0x6F + int(u[1:])
            elif u in VK_NAMED:
                vk = VK_NAMED[u]
            else:
                raise ValueError("无法识别的按键：%s" % p)
    if vk is None:
        raise ValueError("缺少主按键（如 Ctrl+Alt+A）")
    return mods, vk


class HotkeyManager:
    """独立线程注册全局热键，事件放入队列，由 Tk 主线程消费。"""

    def __init__(self):
        self._ids = {}
        self._sequence = 1000
        self._queue = queue.Queue()
        self._thread = None
        self._tid = None
        self.errors = {}

    def start(self, mapping):
        """mapping: {action: hotkey_text}"""
        self.stop()
        self.errors = {}
        self._thread = threading.Thread(target=self._run, args=(mapping,), daemon=True)
        self._thread.start()

    def stop(self):
        if self._tid:
            try:
                user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass
        self._tid = None

    def poll(self):
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items

    def _run(self, mapping):
        self._tid = kernel32.GetCurrentThreadId()
        hook_map = {}
        for action, hk in mapping.items():
            if not hk:
                continue
            try:
                mods, vk = parse_hotkey(hk)
            except ValueError as e:
                self.errors[action] = str(e)
                continue
            hid = self._sequence
            self._sequence += 1
            ok = user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk)
            if ok:
                self._ids[hid] = action
            else:
                # Windows 对 PrintScreen 等键的 RegisterHotKey 限制严格
                # （VK_SNAPSHOT 必须带 Alt 才能注册），退化为低级键盘钩子匹配
                hook_map[(mods, vk)] = (action, hk)
        proc = None
        hook = None
        if hook_map:
            try:
                user32.SetWindowsHookExW.restype = ctypes.c_void_p
                user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HotkeyRecorder.LOWLEVEL,
                                                     ctypes.c_void_p, wt.DWORD]
                user32.CallNextHookEx.restype = ctypes.c_ssize_t
                user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                  wt.WPARAM, wt.LPARAM]

                def _on_key(nCode, wParam, lParam):
                    try:
                        if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                            vk = int(kb.vkCode)
                            if vk not in MOD_VK_SET:
                                cur = set(HotkeyRecorder.pressed_mods())
                                mods = 0
                                if "Ctrl" in cur:
                                    mods |= MOD_CONTROL
                                if "Shift" in cur:
                                    mods |= MOD_SHIFT
                                if "Alt" in cur:
                                    mods |= MOD_ALT
                                if "Win" in cur:
                                    mods |= MOD_WIN
                                hit = hook_map.get((mods, vk))
                                if hit:
                                    self._queue.put(hit[0])
                                    return 1
                    except Exception:
                        pass
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)

                proc = HotkeyRecorder.LOWLEVEL(_on_key)
                hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, None, 0)
            except Exception:
                hook = None
            if not hook:
                for action, hk in hook_map.values():
                    self.errors[action] = "快捷键 %s 不可用（系统限制）" % hk
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                act = self._ids.get(int(msg.wParam))
                if act:
                    self._queue.put(act)
        if hook:
            try:
                user32.UnhookWindowsHookEx(ctypes.c_void_p(hook))
            except Exception:
                pass
        for hid in list(self._ids.keys()):
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass
        self._ids.clear()


# ---------------------------------------------------------------- 文件名

# ---------------------------------------------------------------- 录制用低级键盘钩子

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
WM_QUIT = 0x0012

# 修饰键虚拟码（泛键 + 左右分键）
MOD_VKS = {
    "Ctrl": (0x11, 0xA2, 0xA3),
    "Shift": (0x10, 0xA0, 0xA1),
    "Alt": (0x12, 0xA4, 0xA5),
    "Win": (0x5B, 0x5C),
}
MOD_VK_SET = {v for group in MOD_VKS.values() for v in group}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HotkeyRecorder:
    """录制阶段用 WH_KEYBOARD_LL 抓键。

    比 Tk 的 <KeyPress> 更可靠：PrintScreen、Win 组合、媒体键、小键盘键、NumLock 等
    全部可捕获；录制时吞掉这些键，避免按下 PrtSc 被系统截图工具抢走。
    """

    LOWLEVEL = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)

    def __init__(self, swallow=()):
        self.events = queue.Queue()
        self._thread = None
        self._tid = None
        self._proc = None
        self._swallow = set(swallow) | {0x2C, 0x5B, 0x5C}

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        tid = self._tid
        self._tid = None
        if tid:
            try:
                user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass

    @staticmethod
    def _down(vk):
        try:
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            return False

    @classmethod
    def pressed_mods(cls):
        return [name for name, vks in MOD_VKS.items() if any(cls._down(v) for v in vks)]

    def _callback(self, nCode, wParam, lParam):
        try:
            if nCode == 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self.events.put(("down", vk))
                else:
                    self.events.put(("up", vk))
                if vk in self._swallow:
                    return 1  # 吞掉，避免触发系统截图 / 开始菜单
        except Exception:
            pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _run(self):
        try:
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, self.LOWLEVEL,
                                                 ctypes.c_void_p, wt.DWORD]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              wt.WPARAM, wt.LPARAM]
            user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p,
                                           wt.UINT, wt.UINT]
            self._proc = self.LOWLEVEL(self._callback)
            self._tid = kernel32.GetCurrentThreadId()
            hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
            if not hook:
                self.events.put(("error", "SetWindowsHookEx 失败 code=%s"
                                 % kernel32.GetLastError()))
                return
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            user32.UnhookWindowsHookEx(ctypes.c_void_p(hook))
        except Exception as e:
            self.events.put(("error", repr(e)))


TOKEN_RE = re.compile(r"\{(yyyy|yy|MM|dd|HH|mm|ss|fff|n|W|H|fmt)\}")


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip() or "未命名"


def build_filename(template, ext, width, height, seq=1):
    now = time.localtime()
    ms = int((time.time() % 1) * 1000)
    values = {
        "yyyy": "%04d" % now.tm_year, "yy": "%02d" % (now.tm_year % 100),
        "MM": "%02d" % now.tm_mon, "dd": "%02d" % now.tm_mday,
        "HH": "%02d" % now.tm_hour, "mm": "%02d" % now.tm_min,
        "ss": "%02d" % now.tm_sec, "fff": "%03d" % ms,
        "n": str(seq), "W": str(width), "H": str(height), "fmt": ext,
    }
    out = TOKEN_RE.sub(lambda m: values[m.group(1)], template or DEFAULT_CONFIG["name_template"])
    return sanitize(out)


def unique_path(folder, base, ext):
    path = os.path.join(folder, "%s.%s" % (base, ext))
    i = 2
    while os.path.exists(path):
        path = os.path.join(folder, "%s_%d.%s" % (base, i, ext))
        i += 1
    return path


def save_image(img, cfg, seq=1):
    folder = os.path.expandvars(os.path.expanduser(cfg.get("save_dir") or DEFAULT_CONFIG["save_dir"]))
    os.makedirs(folder, exist_ok=True)
    ext = (cfg.get("image_format") or "png").lower()
    if ext == "jpeg":
        ext = "jpg"
    base = build_filename(cfg.get("name_template"), ext, img.width, img.height, seq)
    path = unique_path(folder, base, ext)
    fmt = {"jpg": "JPEG", "png": "PNG", "bmp": "BMP", "webp": "WEBP", "gif": "GIF"}.get(ext, "PNG")
    target = img
    if fmt in ("JPEG", "BMP"):
        target = img.convert("RGB")
    elif fmt == "GIF":
        target = img.convert("P", palette=1) if img.mode not in ("P", "L") else img
    kwargs = {}
    if fmt in ("JPEG", "WEBP"):
        q = int(cfg.get("quality") or 92)
        kwargs["quality"] = max(5, min(100, q))
        if fmt == "WEBP":
            kwargs["method"] = 4
    target.save(path, fmt, **kwargs)
    return path


# ---------------------------------------------------------------- 悬浮预览

class Toast:
    """右下角悬浮预览 A 版：下沿 34px 条带压在图片内部，半透明 + 背景模糊。

    - 条带：对底部 34px 区域做高斯模糊后叠 40% 深色，底图轮廓透出，不遮挡内容
    - 三按钮：纯图标、26x26、悬停反白、按下纯白；删除悬停转红
    - 删除：点一下立即弹出桌面确认框，无任何过渡与前置提示
    """

    current = None
    _all = []
    _icon_cache = {}

    # 版式常量（与设计稿 300x190 一致，条带叠加不占额外空间）
    W = 300
    H = 190
    BAR_H = 34
    BAR_ALPHA = 102          # 条带压暗：40%
    BAR_BLUR = 12
    BTN = 26
    BTN_GAP = 6
    BAR_PAD_R = 6
    BAR_PAD_L = 10
    ICON = 15
    RED = (255, 77, 77)

    _ICON_PATHS = {
        "open": (((11.2, 3.0), (17.0, 3.0), (17.0, 8.8)),
                 ((17.0, 3.0), (9.6, 10.4)),
                 ((14.6, 11.04), (14.6, 16.0), (4.0, 16.0), (4.0, 6.4), (8.96, 6.4))),
        "copy": (((7.2, 7.2), (16.3, 7.4), (16.3, 16.8), (7.2, 16.8), (7.2, 7.2)),
                 ((12.9, 4.6), (12.9, 3.2), (5.2, 3.2), (5.2, 11.1))),
        "check": (((4.6, 10.4), (8.3, 14.0), (15.4, 6.4)),),
        "delete": (((5.4, 5.4), (14.6, 14.6)), ((14.6, 5.4), (5.4, 14.6))),
    }

    @classmethod
    def close_all(cls):
        """收回所有仍在屏幕上的悬浮窗，返回是否有窗口被收回。"""
        closed_any = False
        for t in list(Toast._all):
            try:
                t.close()
                closed_any = True
            except Exception:
                pass
        Toast._all[:] = []
        Toast.current = None
        return closed_any

    # ---------- 字体与绘制辅助 ----------
    @staticmethod
    def _font(size, bold=False):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        names = ("msyhbd.ttc", "msyh.ttf", "simhei.ttf") if bold else ("msyh.ttc", "msyh.ttf", "simhei.ttf")
        for name in names:
            fp = os.path.join(windir, "Fonts", name)
            if os.path.exists(fp):
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    continue
        return ImageFont.load_default()

    @staticmethod
    def _ellipsis(draw, text, font, max_w):
        if draw.textlength(text, font=font) <= max_w:
            return text
        ell = "…"
        out = ""
        for ch in text:
            if draw.textlength(out + ch + ell, font=font) > max_w:
                break
            out += ch
        return (out + ell) if out else ell

    @classmethod
    def _icon(cls, name, color, size=None):
        """在 4 倍画布上按 20 网格坐标画线，再降采样得到抗锯齿图标。"""
        size = size or cls.ICON
        key = (name, color, size)
        hit = cls._icon_cache.get(key)
        if hit is not None:
            return hit
        ss = 4
        dim = size * ss
        per = dim / 20.0
        im = Image.new("RGBA", (dim, dim), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        w = max(2, int(round(1.7 * ss)))
        for poly in cls._ICON_PATHS.get(name, ()):
            pts = [(x * per, y * per) for x, y in poly]
            d.line(pts, fill=color, width=w, joint="curve")
        im = im.resize((size, size), Image.LANCZOS)
        cls._icon_cache[key] = im
        return im

    def _make_thumb(self, image):
        """等比铺满裁剪为 300x190。"""
        w, h = image.size
        scale = max(self.W / float(w), self.H / float(h))
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        img = image.convert("RGB").resize((nw, nh), Image.LANCZOS)
        left, top = (nw - self.W) // 2, (nh - self.H) // 2
        return img.crop((left, top, left + self.W, top + self.H))

    def _btn_rects(self):
        """三个按钮矩形（打开 / 复制 / 删除），从右往左排，垂直居中于条带。"""
        y0 = self.H - self.BAR_H + (self.BAR_H - self.BTN) // 2
        y1 = y0 + self.BTN
        x1 = self.W - self.BAR_PAD_R
        rects = []
        for _ in range(3):
            x0 = x1 - self.BTN
            rects.append((x0, y0, x1, y1))
            x1 = x0 - self.BTN_GAP
        rects.reverse()
        return rects

    def _sub_title(self, note=None):
        """副标题：分辨率 × 格式 · 文件大小。"""
        try:
            w, h = self.image.size
        except Exception:
            w, h = 0, 0
        ext = os.path.splitext(self.path)[1].lstrip(".").upper() or "PNG"
        try:
            size = os.path.getsize(self.path)
            if size >= 1024 * 1024:
                human = "%.2f MB" % (size / 1048576.0)
            else:
                human = "%d KB" % max(1, int(round(size / 1024.0)))
        except Exception:
            human = "-"
        return note or "%d × %d · %s · %s" % (w, h, ext, human)

    def _compose(self, thumb, hover=None, note=None, copied=False):
        """缩略图 + 半透明模糊条 + 图标按钮，合成为整图（一次成型，运行期不重绘）。"""
        W, H = self.W, self.H
        by0 = H - self.BAR_H
        base = thumb.convert("RGBA")

        # 1) 条带：底部区域模糊 + 40% 压暗，底图内容透出
        region = base.crop((0, by0, W, H)).filter(ImageFilter.GaussianBlur(self.BAR_BLUR))
        tint = Image.new("RGBA", region.size, (8, 8, 8, self.BAR_ALPHA))
        base.paste(Image.alpha_composite(region, tint), (0, by0))

        # 2) 半透明按钮层（必须独立图层合成，否则半透明会被直接覆盖）
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        dl = ImageDraw.Draw(layer)
        icon_on_bar = (238, 238, 238, 255)
        for rect, key in zip(self._btn_rects(), ("open", "copy", "delete")):
            active = (hover == key)
            if key == "delete":
                fill = (self.RED[0], self.RED[1], self.RED[2], 70) if active else (255, 255, 255, 26)
                outline = (self.RED[0], self.RED[1], self.RED[2], 255) if active else (255, 255, 255, 64)
            else:
                fill = (255, 255, 255, 242) if active else (255, 255, 255, 26)
                outline = (255, 255, 255, 255) if active else (255, 255, 255, 64)
            dl.rectangle([rect[0], rect[1], rect[2] - 1, rect[3] - 1], fill=fill, outline=outline, width=1)
        base = Image.alpha_composite(base, layer)

        # 3) 图标（不透明，直接贴）
        for rect, key in zip(self._btn_rects(), ("open", "copy", "delete")):
            active = (hover == key)
            name = key
            if key == "copy" and copied:
                active = True
                name = "check"
            if active and key != "delete":
                color = (17, 17, 17, 255)
            elif active and key == "delete":
                color = (255, 92, 92, 255)
            else:
                color = icon_on_bar
            ic = self._icon(name, color)
            base.paste(ic, (rect[0] + (self.BTN - ic.size[0]) // 2,
                            rect[1] + (self.BTN - ic.size[1]) // 2), ic)

        # 4) 文字：文件名 + 参数，带 1px 深色描影保证透亮底图时仍可读
        d = ImageDraw.Draw(base)
        info_x = self.BAR_PAD_L
        info_right = self._btn_rects()[0][0] - 8
        max_w = max(24, info_right - info_x)
        f_name = self._font(11, bold=True)
        f_sub = self._font(10)
        name = self._ellipsis(d, os.path.basename(self.path), f_name, max_w)
        sub = self._ellipsis(d, self._sub_title(note), f_sub, max_w)
        ny, sy = by0 + 3, by0 + 17
        d.text((info_x + 1, ny + 1), name, font=f_name, fill=(0, 0, 0, 255))
        d.text((info_x + 1, sy + 1), sub, font=f_sub, fill=(0, 0, 0, 255))
        d.text((info_x, ny), name, font=f_name, fill=(245, 245, 245, 255))
        sub_color = (124, 226, 168, 255) if note else (198, 198, 198, 255)
        d.text((info_x, sy), sub, font=f_sub, fill=sub_color)

        # 5) 1px 外框，让卡片与桌面分层（纯 2D，无圆角无投影）
        d.rectangle([0, 0, W - 1, H - 1], outline=(44, 44, 44, 255), width=1)
        return base

    # ---------- 倒计时进度条：Canvas 独立图元，只更新坐标，不重绘整图 ----------
    PROG_H = 2

    def _progress_geom(self):
        return 0, self.W, self.H - self.BAR_H

    def _build_progress(self):
        x0, x1, y = self._progress_geom()
        track = self.canvas.create_line(x0, y, x1, y, width=self.PROG_H,
                                        fill="#3A3A3A", tags="prog")
        fill = self.canvas.create_line(x0, y, x0 + self.PROG_H, y, width=self.PROG_H,
                                       fill="#FFFFFF", tags="prog")
        self._prog_ids = (track, fill)
        self._update_progress(self.remaining)

    def _update_progress(self, remaining):
        if not self._prog_ids:
            return
        x0, x1, y = self._progress_geom()
        _track, fill = self._prog_ids
        ratio = 0.0 if self.total <= 0 else max(0.0, min(1.0, remaining / float(self.total)))
        w = (x1 - x0) * ratio
        try:
            if w <= self.PROG_H + 1:
                self.canvas.itemconfigure(fill, state="hidden")
            else:
                self.canvas.itemconfigure(fill, state="normal")
                self.canvas.coords(fill, x0, y, x0 + w, y)
        except Exception:
            pass

    # ---------- 构造 ----------
    def __init__(self, app, image, path, seconds):
        Toast._all.append(self)
        self.app = app
        self.image = image
        self.path = path
        self.total = max(0.1, float(seconds))
        self.remaining = float(seconds)
        self.paused = False
        self.closed = False
        self._t0 = time.monotonic()
        self._pause_acc = 0.0
        self._pause_t0 = None
        self._prog_ids = None
        self._hover = None
        self._note = None
        self._copied = False
        self._confirm = None
        self._bursting = False
        self._burst_after = None
        self._burst_photo = None
        self._after_id = None
        self._note_after = None
        self._copy_after = None
        self._photo = None
        self._cache = {}

        self._thumb = self._make_thumb(image)
        self._cache[None] = self._compose(self._thumb)
        for key in ("open", "copy", "delete"):
            self._cache[key] = self._compose(self._thumb, hover=key)
        self._cache["copied"] = self._compose(self._thumb, hover="copy", copied=True)

        self.top = tk.Toplevel(app.root)
        self.top.overrideredirect(True)
        try:
            self.top.attributes("-topmost", True)
        except Exception:
            pass
        try:
            self.top.attributes("-toolwindow", True)
        except Exception:
            pass
        key_color = "#010203"
        self.top.configure(bg=key_color)
        try:
            self.top.attributes("-transparentcolor", key_color)
        except Exception:
            pass

        self.canvas = tk.Canvas(self.top, width=self.W, height=self.H, bg=key_color,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self._photo = ImageTk.PhotoImage(self._compose(self._thumb))
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._build_progress()

        # 底部右侧定位
        try:
            wx, wy, ww, wh = work_area_of_monitor_under_cursor()
        except Exception:
            wx, wy, ww, wh = 0, 0, self.W + 80, self.H + 80
        margin = 16
        self.top.geometry("%dx%d+%d+%d" % (self.W, self.H, wx + ww - self.W - margin, wy + wh - self.H - margin))

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", lambda e: self.open_image())
        self._pin_topmost()
        self._after_id = self.top.after(40, self._tick)

    def _frame(self):
        if self._note:
            # 轻量提示：仅替换副标题，重合成一次（点按频率低，可接受）
            return self._compose(self._thumb, hover=self._hover, note=self._note, copied=self._copied)
        if self._copied:
            return self._cache["copied"]
        return self._cache.get(self._hover) or self._cache[None]

    def _paint(self):
        try:
            self._photo = ImageTk.PhotoImage(self._frame())
            self.canvas.itemconfigure(1, image=self._photo)
        except Exception:
            pass
        self._update_progress(self.remaining)

    def _pin_topmost(self):
        try:
            self.top.attributes("-topmost", True)
            self.top.lift()
        except Exception:
            pass

    # ---------- 交互 ----------
    def _hit(self, x, y):
        if not (0 <= x <= self.W) or not (self.H - self.BAR_H <= y <= self.H):
            return None
        for rect, key in zip(self._btn_rects(), ("open", "copy", "delete")):
            if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                return key
        return None

    def _on_motion(self, event):
        key = self._hit(event.x, event.y)
        self._set_paused(key is not None)   # 仅指针停在按钮上时暂停，其余区域照常倒计时
        if key != self._hover:
            self._hover = key
            self._paint()

    def _on_leave(self, event):
        self._set_paused(False)
        if self._hover is not None:
            self._hover = None
            self._paint()

    def _on_click(self, event):
        key = self._hit(event.x, event.y)
        if key == "open":
            self.open_image()
        elif key == "copy":
            self.copy_image()
        elif key == "delete":
            self.delete_image()

    def _show_note(self, text):
        self._note = text
        self._paint()
        try:
            if self._note_after:
                self.top.after_cancel(self._note_after)
        except Exception:
            pass
        self._note_after = self.top.after(1200, self._clear_note)

    def _clear_note(self):
        self._note = None
        self._note_after = None
        self._paint()

    # ---------- 操作 ----------
    def open_image(self):
        try:
            os.startfile(self.path)
            self.close()
        except Exception:
            self._show_note("打开失败")

    def copy_image(self):
        try:
            copy_image_to_clipboard(self.image)
        except Exception:
            self._show_note("复制失败")
            return
        self._copied = True
        self._show_note("已复制到剪贴板")
        try:
            if self._copy_after:
                self.top.after_cancel(self._copy_after)
        except Exception:
            pass
        self._copy_after = self.top.after(1800, self._clear_copied)

    def _clear_copied(self):
        self._copy_after = None
        self._copied = False
        self._paint()

    def delete_image(self):
        """点一下直接删除：不弹任何确认框，文件即刻移出，卡片自身播放弹出特效后消失。"""
        if self.closed or self._bursting:
            return
        self._set_paused(True)
        self._bursting = True
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception:
            self._bursting = False
            self._set_paused(False)
            self._show_note("删除失败")
            return
        try:
            self.app.notify("已删除：%s" % self.path)
        except Exception:
            pass
        self._play_burst()

    # 弹出特效关键帧：（缩放, 不透明度, 垂直位移）——先回弹放大，再缩收淡出
    BURST_FRAMES = (
        (1.000, 1.00, 0),
        (1.048, 1.00, -3),
        (1.082, 0.97, -5),
        (1.030, 0.88, -7),
        (0.955, 0.74, -8),
        (0.870, 0.58, -9),
        (0.785, 0.40, -9),
        (0.700, 0.22, -9),
        (0.620, 0.08, -9),
    )
    BURST_INTERVAL = 22

    def _play_burst(self):
        """卡片弹出特效：以当前画面为底，逐帧缩放+淡出，播完销毁窗口。"""
        if self.closed:
            return
        try:
            base = self._frame().convert("RGBA")
        except Exception:
            self.close()
            return
        for aid in (self._after_id, self._note_after, self._copy_after):
            try:
                if aid:
                    self.top.after_cancel(aid)
            except Exception:
                pass
        self._after_id = None
        self._prog_ids = None
        try:
            self.canvas.delete("all")
            item = self.canvas.create_image(self.W // 2, self.H // 2, anchor="center")
            cx = self.top.winfo_rootx() + self.W / 2.0
            cy = self.top.winfo_rooty() + self.H / 2.0
        except Exception:
            self.close()
            return
        step = {"i": 0}

        def tick():
            if self.closed:
                return
            i = step["i"]
            if i >= len(self.BURST_FRAMES):
                self.close()
                return
            step["i"] = i + 1
            scale, opa, dy = self.BURST_FRAMES[i]
            w = max(2, int(round(self.W * scale)))
            h = max(2, int(round(self.H * scale)))
            try:
                im = base.resize((w, h), Image.BILINEAR)
                if opa < 1.0:
                    alpha = im.getchannel("A").point(lambda v: int(v * opa))
                    im.putalpha(alpha)
                photo = ImageTk.PhotoImage(im)
                self._burst_photo = photo
                self.canvas.configure(width=w, height=h)
                self.canvas.coords(item, w // 2, h // 2)
                self.canvas.itemconfigure(item, image=photo)
                self.top.geometry("%dx%d+%d+%d" % (
                    w, h, int(round(cx - w / 2.0)), int(round(cy + dy - h / 2.0))))
                self.top.lift()
            except Exception:
                self.close()
                return
            try:
                self._burst_after = self.top.after(self.BURST_INTERVAL, tick)
            except Exception:
                self.close()

        tick()

    # ---------- 计时 ----------
    def set_total(self, seconds):
        self.total = max(0.1, float(seconds))
        self.remaining = float(seconds)
        self._t0 = time.monotonic()
        self._pause_acc = 0.0
        self._pause_t0 = time.monotonic() if self.paused else None
        self._update_progress(self.remaining)
        self._paint()

    def _set_paused(self, flag):
        if self.closed or bool(flag) == bool(self.paused):
            return
        now = time.monotonic()
        self.paused = bool(flag)
        if self.paused:
            self._pause_t0 = now
        else:
            if self._pause_t0 is not None:
                self._pause_acc += now - self._pause_t0
            self._pause_t0 = None

    def _elapsed(self):
        now = time.monotonic()
        paused = self._pause_acc
        if self._pause_t0 is not None:
            paused += now - self._pause_t0
        return now - self._t0 - paused

    def _pointer_key(self):
        """按真实指针位置判断落在哪个按钮上（不依赖 Tk 的 Motion 事件）。"""
        try:
            pt = wt.POINT()
            if not user32.GetCursorPos(ctypes.byref(pt)):
                return None
            x = pt.x - self.top.winfo_rootx()
            y = pt.y - self.top.winfo_rooty()
            return self._hit(x, y)
        except Exception:
            return None

    def _tick(self):
        if self.closed:
            return
        if self._bursting:
            return
        key = self._pointer_key()
        self._set_paused(key is not None)
        if key != self._hover:
            self._hover = key
            self._paint()
        if self._pause_t0 is None:
            remain = self.total - self._elapsed()
            self.remaining = max(0.0, remain)
            if remain <= 0:
                self._update_progress(0.0)
                self.close()
                return
            self._update_progress(self.remaining)
        try:
            self._after_id = self.top.after(40, self._tick)
        except Exception:
            pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._burst_after:
            try:
                self.top.after_cancel(self._burst_after)
            except Exception:
                pass
            self._burst_after = None
        if self._confirm is not None:
            try:
                self._confirm.destroy()
            except Exception:
                pass
            self._confirm = None
        for aid in (self._after_id, self._note_after, self._copy_after):
            try:
                if aid:
                    self.top.after_cancel(aid)
            except Exception:
                pass
        try:
            self.top.withdraw()
            self.top.update_idletasks()
        except Exception:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass
        if self in Toast._all:
            Toast._all.remove(self)
        if Toast.current is self:
            Toast.current = None


class ConfirmDialog:
    """（v1.2.3 起停用）旧版删除确认框，保留代码仅供回溯；删除已改为直接生效 + 卡片弹出特效。"""

    W = 272
    H = 116
    BW = 74
    BH = 26
    FONT = ("Microsoft YaHei UI", 9)
    FONT_B = ("Microsoft YaHei UI", 10, "bold")

    def __init__(self, parent, filename, on_ok, on_cancel):
        self.on_ok = on_ok
        self.on_cancel = on_cancel
        self.closed = False
        W, H = self.W, self.H
        self.top = tk.Toplevel(parent)
        self.top.overrideredirect(True)
        try:
            self.top.attributes("-topmost", True)
        except Exception:
            pass
        self.canvas = tk.Canvas(self.top, width=W, height=H, bg="#111111",
                                highlightthickness=1, highlightbackground="#4A4A4A", bd=0)
        self.canvas.pack()
        self.canvas.create_text(16, 22, anchor="w", text="删除这张截图？", fill="#F2F2F2", font=self.FONT_B)
        name = filename if len(filename) <= 30 else filename[:28] + "…"
        self.canvas.create_text(16, 46, anchor="w", text=name, fill="#A9A9A9", font=self.FONT)

        by1 = H - 14
        by0 = by1 - self.BH
        ok_x1 = W - 16
        ok_x0 = ok_x1 - self.BW
        cc_x1 = ok_x0 - 8
        cc_x0 = cc_x1 - self.BW
        self._button(cc_x0, by0, cc_x1, by1, "取消", "cancel")
        self._button(ok_x0, by0, ok_x1, by1, "删除", "ok")

        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
        except Exception:
            px = py = 0
            pw, ph = 300, 190
        x = px + (pw - W) // 2
        y = py - H - 10
        try:
            wx, wy, ww, wh = work_area_of_monitor_under_cursor()
        except Exception:
            wx, wy, ww, wh = 0, 0, 1920, 1080
        x = max(wx + 6, min(x, wx + ww - W - 6))
        if y < wy + 6:
            y = py + ph + 10
        self.top.geometry("%dx%d+%d+%d" % (W, H, x, y))
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.bind("<Return>", lambda e: self._ok())
        try:
            self.top.focus_force()
        except Exception:
            pass
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)

    def _button(self, x0, y0, x1, y1, label, kind):
        if kind == "cancel":
            normal, hover, txt, outline = "#181818", "#2B2B2B", "#E6E6E6", "#4A4A4A"
        else:
            normal, hover, txt, outline = "#B9353F", "#FF4D4D", "#FFFFFF", "#D2555F"
        tag = "btn_" + kind
        rect = self.canvas.create_rectangle(x0, y0, x1, y1, fill=normal, outline=outline, width=1, tags=tag)
        self.canvas.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=label, fill=txt,
                                font=self.FONT, tags=tag)
        self.canvas.tag_bind(tag, "<Enter>", lambda e: self.canvas.itemconfigure(rect, fill=hover))
        self.canvas.tag_bind(tag, "<Leave>", lambda e: self.canvas.itemconfigure(rect, fill=normal))
        self.canvas.tag_bind(tag, "<Button-1>",
                             lambda e: (self._cancel() if kind == "cancel" else self._ok()))

    def _ok(self):
        if self.closed:
            return
        self.destroy()
        try:
            if self.on_ok:
                self.on_ok()
        except Exception:
            pass

    def _cancel(self):
        if self.closed:
            return
        self.destroy()
        try:
            if self.on_cancel:
                self.on_cancel()
        except Exception:
            pass

    def destroy(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.top.destroy()
        except Exception:
            pass

# ---------------------------------------------------------------- 区域选择

class RegionSelector:
    def __init__(self, root, screenshot, callback):
        import tkinter as tk
        from PIL import Image, ImageTk

        self.root = root
        self.shot = screenshot
        self.callback = callback
        self.finished = False
        self.start = None
        self.rect_id = None
        self.sel_photo = None
        self.sel_item = None
        self.size_item = None

        vx, vy, vw, vh = virtual_screen_rect()
        self.origin = (vx, vy)

        dim = screenshot.convert("RGB").point(lambda p: int(p * 0.42))
        self.dim_photo = ImageTk.PhotoImage(dim)

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.geometry("%dx%d+%d+%d" % (vw, vh, vx, vy))
        self.top.configure(cursor="crosshair", bg="black")

        self.canvas = tk.Canvas(self.top, highlightthickness=0, bd=0,
                                width=vw, height=vh, bg="black", cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, anchor="nw", image=self.dim_photo)

        hint = "拖动鼠标选择区域    Esc / 右键取消"
        self.canvas.create_text(vw // 2, 40, text=hint, fill="#ffffff",
                                font=("Microsoft YaHei UI", 15, "bold"))

        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.top.bind("<Escape>", lambda e: self.cancel())
        self.canvas.bind("<Button-3>", lambda e: self.cancel())
        self.top.bind("<Button-3>", lambda e: self.cancel())
        self.top.focus_force()
        self.top.grab_set()
        self.top.lift()

    def _press(self, evt):
        self.start = (evt.x, evt.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            evt.x, evt.y, evt.x, evt.y, outline="#3b82f6", width=2, dash=(4, 3))
        if self.sel_item:
            self.canvas.delete(self.sel_item)
            self.sel_item = None

    def _drag(self, evt):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = max(0, evt.x), max(0, evt.y)
        self.canvas.coords(self.rect_id, x0, y0, x1, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w > 2 and h > 2:
            left, top = min(x0, x1), min(y0, y1)
            crop = self.shot.crop((left, top, left + w, top + h))
            from PIL import ImageTk
            self.sel_photo = ImageTk.PhotoImage(crop)
            if self.sel_item:
                self.canvas.delete(self.sel_item)
            self.sel_item = self.canvas.create_image(left, top, anchor="nw", image=self.sel_photo)
        if self.size_item:
            self.canvas.delete(self.size_item)
        self.size_item = self.canvas.create_text(
            min(x0, x1) + 4, max(y0, y1) + 14, anchor="nw",
            text="%d × %d" % (w, h), fill="#ffffff",
            font=("Microsoft YaHei UI", 10, "bold"))
        self.canvas.tag_raise(self.rect_id)

    def _release(self, evt):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = evt.x, evt.y
        left, top = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        self._close()
        if w < 4 or h < 4:
            self.callback(None)
            return
        self.callback(self.shot.crop((left, top, left + w, top + h)))

    def cancel(self):
        if self.finished:
            return
        self._close()
        self.callback(None)

    def _close(self):
        self.finished = True
        try:
            self.top.grab_release()
        except Exception:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass



# ---------------------------------------------------------------- 瑞士风 UI 组件
# token 与《ShareX-Flash-UI改版预览.html》保持一致：中性灰底、白色信号色、直角、无投影
UI_WIN = "#313131"
UI_RAISE = "#3B3B3B"
UI_LINE = "#474747"
UI_SUNK = "#282828"
UI_T1 = "#F7F7F7"
UI_T2 = "#C2C2C2"
UI_T3 = "#969696"
UI_SIG = "#FFFFFF"
UI_BORDER = "#585858"
UI_BTN = "#454545"
UI_BTN_HOVER = "#555555"
UI_BTN_ACTIVE = "#3B3B3B"
UI_BTN_P = "#575757"
UI_BTN_P_HOVER = "#676767"
UI_BTN_P_BORDER = "#9A9A9A"
UI_FOOT = "#2A2A2A"

_UI_FONTS = []


def ui_fonts():
    """返回（拉丁无衬线, 中文无衬线）：拉丁优先 Helvetica 系，中文回退雅黑。"""
    if _UI_FONTS:
        return _UI_FONTS[0], _UI_FONTS[1]
    try:
        fams = set(tkfont.families())
    except Exception:
        fams = set()
    latin = "Segoe UI"
    for name in ("Helvetica Neue", "Helvetica", "Arial", "Segoe UI"):
        if name in fams:
            latin = name
            break
    cjk = "Microsoft YaHei UI"
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei"):
        if name in fams:
            cjk = name
            break
    _UI_FONTS.extend([latin, cjk])
    return latin, cjk


def ui_button(parent, text, command, kind="normal", bg=UI_WIN, small=False, font=None):
    """直角描边按钮：normal / primary / ghost。"""
    if kind == "primary":
        face, hover, border = UI_BTN_P, UI_BTN_P_HOVER, UI_BTN_P_BORDER
    elif kind == "ghost":
        face, hover, border = bg, UI_RAISE, "#606060"
    else:
        face, hover, border = UI_BTN, UI_BTN_HOVER, "#666666"
    padx, pady = (12, 5) if small else (16, 8)
    box = tk.Frame(parent, bg=border)
    btn = tk.Button(box, text=text, command=command, bg=face, fg=UI_SIG,
                    activebackground=UI_BTN_ACTIVE, activeforeground=UI_SIG,
                    relief="flat", bd=0, highlightthickness=0, takefocus=0,
                    padx=padx, pady=pady, cursor="hand2", font=font)
    btn.pack(padx=1, pady=1)
    btn.bind("<Enter>", lambda e: btn.configure(bg=hover))
    btn.bind("<Leave>", lambda e: btn.configure(bg=face))
    return box


def ui_entry(parent, textvariable, width, bg=UI_WIN, font=None):
    """暗底输入框，外包 1px 描边。返回（外壳, Entry）。"""
    box = tk.Frame(parent, bg=UI_BORDER)
    ent = tk.Entry(box, textvariable=textvariable, bg=UI_SUNK, fg=UI_T1,
                   insertbackground=UI_T1, relief="flat", bd=0,
                   highlightthickness=0, width=width, font=font)
    ent.pack(padx=1, pady=1, ipady=4, ipadx=6)
    return box, ent


def ui_kbd_row(parent, textvar, font=None):
    """把 "Ctrl+Alt+A" 渲染成一排键帽，随变量变化自动重绘。"""
    holder = tk.Frame(parent, bg=UI_WIN)

    def redraw(*_a):
        for w in holder.winfo_children():
            w.destroy()
        raw = (textvar.get() or "").strip()
        parts = [p for p in raw.split("+") if p] or ["未设置"]
        for i, part in enumerate(parts):
            if i:
                tk.Label(holder, text="+", bg=UI_WIN, fg=UI_T3, font=font).pack(side="left", padx=2)
            box = tk.Frame(holder, bg="#5C5C5C")
            box.pack(side="left")
            tk.Label(box, text=part, bg="#3A3A3A", fg=UI_T1, padx=9, pady=4,
                     font=font).pack(padx=1, pady=1)

    try:
        textvar.trace_add("write", redraw)
    except Exception:
        pass
    redraw()
    return holder


class ToggleSwitch(tk.Canvas):
    """统一滑块式开关：42×22 胶囊，白色态为开。"""

    W, H = 42, 22

    def __init__(self, master, var, bg=UI_WIN, command=None):
        tk.Canvas.__init__(self, master, width=self.W, height=self.H, bg=bg,
                           highlightthickness=0, bd=0, cursor="hand2")
        self.var = var
        self.command = command
        self._hover = False
        self._press = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        try:
            var.trace_add("write", lambda *_a: self._draw())
        except Exception:
            pass
        self._draw()

    def _draw(self):
        try:
            on = bool(self.var.get())
        except Exception:
            on = False
        border = UI_SIG if (on or self._hover) else "#6A6A6A"
        fill = UI_SIG if on else UI_WIN
        self.delete("all")
        self.create_oval(0, 0, self.H, self.H, fill=fill, outline=border)
        self.create_oval(self.W - self.H, 0, self.W, self.H, fill=fill, outline=border)
        self.create_rectangle(self.H // 2, 0, self.W - self.H // 2, self.H, fill=fill, outline="")
        if on:
            self.create_line(self.H // 2, 0, self.W - self.H // 2, 0, fill=border)
            self.create_line(self.H // 2, self.H - 1, self.W - self.H // 2, self.H - 1, fill=border)
        else:
            self.create_line(self.H // 2, 0, self.W - self.H // 2, 0, fill=UI_WIN)
            self.create_line(self.H // 2, self.H - 1, self.W - self.H // 2, self.H - 1, fill=UI_WIN)
        kr = 6 if self._press else 7
        kcx = (self.W - 11) if on else 11
        kcy = self.H / 2.0
        self.create_oval(kcx - kr, kcy - kr, kcx + kr, kcy + kr,
                         fill=UI_WIN if on else "#A0A0A0", outline="")

    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._press = False
        self._draw()

    def _on_press(self, _e):
        self._press = True
        self._draw()

    def _on_release(self, _e):
        if not self._press:
            return
        self._press = False
        self.var.set(not bool(self.var.get()))
        self._draw()
        if self.command:
            try:
                self.command()
            except Exception:
                pass


class HSlider(tk.Canvas):
    """细轨道 + 方块滑块的横向滑块。"""

    W, H = 132, 20
    PAD = 5

    def __init__(self, master, var, lo, hi, bg=UI_WIN, command=None):
        tk.Canvas.__init__(self, master, width=self.W, height=self.H, bg=bg,
                           highlightthickness=0, bd=0, cursor="hand2")
        self.var, self.lo, self.hi, self.command = var, lo, hi, command
        self._hover = False
        self.bind("<Button-1>", self._on_drag)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        try:
            var.trace_add("write", lambda *_a: self._draw())
        except Exception:
            pass
        self._draw()

    def _ratio(self):
        try:
            v = float(self.var.get())
        except Exception:
            v = float(self.lo)
        span = float(self.hi - self.lo) or 1.0
        return min(1.0, max(0.0, (v - self.lo) / span))

    def _draw(self):
        self.delete("all")
        y = self.H / 2.0
        x0, x1 = self.PAD, self.W - self.PAD
        track = "#8A8A8A" if self._hover else "#6E6E6E"
        self.create_line(x0, y, x1, y, fill=track, width=2)
        x = x0 + (x1 - x0) * self._ratio()
        self.create_rectangle(x - 5, y - 5, x + 5, y + 5, fill=UI_SIG, outline="")

    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._draw()

    def _on_drag(self, event):
        x0, x1 = self.PAD, self.W - self.PAD
        r = min(1.0, max(0.0, (event.x - x0) / float(x1 - x0)))
        val = int(round(self.lo + r * (self.hi - self.lo)))
        self.var.set(val)
        if self.command:
            try:
                self.command()
            except Exception:
                pass


class SegBar(tk.Frame):
    """分段选择器：同框内多条互斥选项。"""

    def __init__(self, master, options, var, bg=UI_WIN, command=None, font=None):
        tk.Frame.__init__(self, master, bg="#6A6A6A")
        self.var, self.command = var, command
        self._cells = []
        inner = tk.Frame(self, bg="#6A6A6A")
        inner.pack(padx=1, pady=1)
        for i, (label, value) in enumerate(options):
            cell = tk.Label(inner, text=label, bg=UI_WIN, fg=UI_T2, padx=13, pady=6,
                            cursor="hand2", font=font)
            cell.pack(side="left")
            if i < len(options) - 1:
                tk.Frame(inner, bg="#6A6A6A", width=1).pack(side="left", fill="y")
            cell.bind("<Button-1>", lambda _e, v=value: self._pick(v))
            cell.bind("<Enter>", lambda _e, c=cell: self._hover_in(c))
            cell.bind("<Leave>", lambda _e, c=cell: self._hover_out(c))
            self._cells.append((value, cell))
        try:
            var.trace_add("write", lambda *_a: self._sync())
        except Exception:
            pass
        self._sync()

    def _sync(self):
        try:
            cur = self.var.get()
        except Exception:
            cur = None
        for value, cell in self._cells:
            on = (value == cur)
            cell.configure(bg=UI_SIG if on else UI_WIN, fg="#111111" if on else UI_T2)

    def _hover_in(self, cell):
        try:
            if cell.cget("bg") != UI_SIG:
                cell.configure(bg=UI_RAISE, fg=UI_T1)
        except Exception:
            pass

    def _hover_out(self, _cell):
        self._sync()

    def _pick(self, value):
        self.var.set(value)
        self._sync()
        if self.command:
            try:
                self.command()
            except Exception:
                pass


# ---------------------------------------------------------------- 主应用

class QuickShotApp:
    def __init__(self, silent=False):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.ttk = ttk
        self.cfg = load_config()
        self.hotkeys = HotkeyManager()
        self.status_var = None
        self.recording = {}
        self._last_path = None
        self._capture_busy = False

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg=UI_WIN)
        self.root.minsize(820, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._set_window_icon()
        self._build_ui()
        self._center()

        ensure_shutter_sound()
        self.apply_hotkeys()

        if silent:
            self.root.withdraw()

        self.root.after(120, self._poll_hotkeys)
        self.notify("已就绪 · %s 全屏截图 / %s 区域截图" %
                    (self.cfg["hotkey_full"], self.cfg["hotkey_region"]))

    # ---------------- 外观
    def _set_window_icon(self):
        try:
            from PIL import Image, ImageTk
            im = None
            ico = resource_path("sharexflash.ico")
            if os.path.isfile(ico):
                with Image.open(ico) as f:
                    f.seek(0)
                    im = f.convert("RGBA").resize((64, 64), Image.LANCZOS)
            if im is None:
                return
            self._icon = ImageTk.PhotoImage(im)
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass

    def _center(self):
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(920, max(820, sw - 160))
        h = min(680, max(600, sh - 140))
        self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))

    def _build_ui(self):
        tk = self.tk
        latin, cjk = ui_fonts()
        F_TITLE = (latin, 16, "bold")
        F_SMALL = (latin, 10)
        F_NAME = (cjk, 10)
        F_DESC = (cjk, 9)
        F_BTN = (cjk, 10)
        F_BTN_S = (cjk, 9)
        F_NUM = (latin, 10)

        # 顶部品牌条
        head = tk.Frame(self.root, bg=UI_WIN)
        head.pack(fill="x")
        head_in = tk.Frame(head, bg=UI_WIN)
        head_in.pack(fill="x", padx=24, pady=16)
        brand = tk.Frame(head_in, bg=UI_WIN)
        brand.pack(side="left")
        tk.Label(brand, text=APP_TITLE, bg=UI_WIN, fg=UI_T1,
                 font=F_TITLE).pack(side="left")
        tk.Label(brand, text="  v" + VERSION, bg=UI_WIN, fg=UI_T3,
                 font=F_SMALL).pack(side="left", pady=(5, 0))
        state_box = tk.Frame(head_in, bg=UI_WIN)
        state_box.pack(side="right")
        dot = tk.Canvas(state_box, width=8, height=8, bg=UI_WIN,
                        highlightthickness=0, bd=0)
        dot.create_oval(1, 1, 7, 7, fill=UI_SIG, outline="")
        dot.pack(side="left", padx=(0, 8), pady=(3, 0))
        self._head_state = tk.StringVar(value="后台运行中")
        tk.Label(state_box, textvariable=self._head_state, bg=UI_WIN, fg=UI_T2,
                 font=F_DESC).pack(side="left")
        tk.Frame(self.root, bg=UI_LINE, height=1).pack(fill="x")

        # 主体：左导航 + 右内容
        body = tk.Frame(self.root, bg=UI_WIN)
        body.pack(fill="both", expand=True)
        side = tk.Frame(body, bg=UI_WIN, width=196)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(body, bg=UI_LINE, width=1).pack(side="left", fill="y")
        holder = tk.Frame(body, bg=UI_WIN)
        holder.pack(side="left", fill="both", expand=True)
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)

        panes = {}
        for key in ("general", "save", "keys", "about"):
            panes[key] = tk.Frame(holder, bg=UI_WIN)
            panes[key].grid(row=0, column=0, sticky="nsew")
        self._panes = panes

        tk.Frame(side, bg=UI_WIN, height=10).pack(fill="x")
        self._nav = {}
        for key, label in (("general", "常规"), ("save", "保存与输出"),
                           ("keys", "快捷键"), ("about", "关于")):
            self._nav[key] = self._nav_item(side, label, key, cjk)

        # ---------------- 常规
        p = panes["general"]
        g = self._group(p, "截图行为", cjk)
        self.sound_var = tk.BooleanVar(value=bool(self.cfg["sound_enabled"]))
        self._row(g, "咔嚓音效", "截图瞬间播放内置合成快门声",
                  lambda h: ToggleSwitch(h, self.sound_var).pack(), cjk)
        self.copy_var = tk.BooleanVar(value=bool(self.cfg["auto_copy"]))
        self._row(g, "截图后自动复制到剪贴板", "截完即可直接粘贴到其他软件",
                  lambda h: ToggleSwitch(h, self.copy_var).pack(), cjk)
        self.autostart_var = tk.BooleanVar(value=bool(self.cfg["autostart"]))
        self._row(g, "开机自动启动", "登录 Windows 后静默运行，热键随时可用",
                  lambda h: ToggleSwitch(h, self.autostart_var).pack(), cjk, last=True)

        g = self._group(p, "悬浮预览", cjk)
        self.preview_var_chk = tk.BooleanVar(value=bool(self.cfg["preview_enabled"]))
        self._row(g, "显示右下角悬浮预览", "截图后弹出预览窗，可打开 / 复制 / 删除",
                  lambda h: ToggleSwitch(h, self.preview_var_chk).pack(), cjk)
        sec0 = int(self.cfg.get("preview_seconds", 10))
        self.sec_var = tk.IntVar(value=min(30, max(3, sec0)))

        def build_sec(h):
            box = tk.Frame(h, bg=UI_WIN)
            box.pack()
            HSlider(box, self.sec_var, 3, 30).pack(side="left")
            val = tk.Label(box, text="%d 秒" % int(self.sec_var.get()), bg=UI_WIN,
                           fg=UI_T1, width=5, anchor="e", font=F_NUM)
            val.pack(side="left", padx=(12, 0))
            self.sec_var.trace_add(
                "write", lambda *_a: val.configure(text="%s 秒" % self.sec_var.get()))

        self._row(g, "停留时长", "指针停在按钮上时倒计时自动暂停",
                  build_sec, cjk, last=True)

        # ---------------- 保存与输出
        p = panes["save"]
        g = self._group(p, "保存位置", cjk)
        self.dir_var = tk.StringVar(value=self.cfg["save_dir"])

        def build_dir(h):
            box, _ent = ui_entry(h, self.dir_var, 30, font=(latin, 10))
            box.pack(side="left")
            ui_button(h, "浏览…", self._pick_dir, small=True, font=F_BTN_S).pack(
                side="left", padx=(8, 0))
            ui_button(h, "打开", self._open_dir, kind="ghost", small=True,
                      font=F_BTN_S).pack(side="left", padx=(6, 0))

        self._row(g, "保存目录", "截图按日期自动落到该文件夹", build_dir, cjk, last=True)

        g = self._group(p, "图片", cjk)
        self.fmt_var = tk.StringVar(value=self._fmt_label(self.cfg["image_format"]))
        seg_opts = [(short, label) for short, (label, _ext) in
                    zip(("PNG", "JPG", "BMP", "WEBP", "GIF"), FORMATS)]
        self._row(g, "图片格式", "GIF 为 256 色，其余为全彩",
                  lambda h: SegBar(h, seg_opts, self.fmt_var,
                                   command=self._update_name_preview,
                                   font=F_BTN_S).pack(), cjk)
        self.q_var = tk.IntVar(value=int(self.cfg.get("quality", 92)))

        def build_q(h):
            box = tk.Frame(h, bg=UI_WIN)
            box.pack()
            HSlider(box, self.q_var, 30, 100).pack(side="left")
            val = tk.Label(box, text=str(int(self.q_var.get())), bg=UI_WIN, fg=UI_T1,
                           width=4, anchor="e", font=F_NUM)
            val.pack(side="left", padx=(12, 0))
            self.q_var.trace_add(
                "write", lambda *_a: val.configure(text=str(self.q_var.get())))

        self._row(g, "压缩质量", "仅 JPG / WEBP 生效", build_q, cjk)

        self.name_var = tk.StringVar(value=self.cfg["name_template"])
        self.preview_var = tk.StringVar(value="")

        def build_name(h):
            col = tk.Frame(h, bg=UI_WIN)
            col.pack()
            box, _ent = ui_entry(col, self.name_var, 34, font=(latin, 10))
            box.pack(anchor="e")
            tk.Label(col, textvariable=self.preview_var, bg=UI_WIN, fg=UI_T3,
                     font=(cjk, 8)).pack(anchor="e", pady=(5, 0))

        self.name_var.trace_add("write", lambda *_a: self._update_name_preview())
        self._row(g, "文件命名格式",
                  "可用变量：{yyyy}{yy}{MM}{dd}{HH}{mm}{ss}{fff}{W}{H}{fmt}{n}",
                  build_name, cjk, last=True)
        self._update_name_preview()

        # ---------------- 快捷键
        p = panes["keys"]
        g = self._group(p, "全局快捷键", cjk)
        self.hk_vars = {}
        rows = (("hotkey_full", "全屏截图", "抓取所有显示器画面"),
                ("hotkey_region", "区域截图", "拖拽框选任意区域"),
                ("hotkey_window", "当前窗口", "抓取最上层窗口，自动裁掉阴影"),
                ("hotkey_panel", "打开设置面板", "从后台唤回当前窗口"))
        for i, (key, name, desc) in enumerate(rows):
            var = tk.StringVar(value=self.cfg.get(key, ""))
            self.hk_vars[key] = var

            def build_hk(h, k=key, v=var):
                cockpit = tk.Frame(h, bg=UI_WIN)
                cockpit.pack()
                ui_kbd_row(cockpit, v, font=F_BTN_S).pack(side="left")
                ui_button(cockpit, "录制", lambda: self._record_hotkey(k, v),
                          small=True, font=F_BTN_S).pack(side="left", padx=(10, 0))

            self._row(g, name, desc, build_hk, cjk, last=(i == len(rows) - 1))
        tk.Label(p, text="录制时直接按下组合键即可；PrintScreen、Win、小键盘与媒体键均支持，Esc 取消。",
                 bg=UI_WIN, fg=UI_T3, font=(cjk, 8), anchor="w",
                 justify="left").pack(fill="x", padx=30, pady=(10, 0))

        # ---------------- 关于
        p = panes["about"]
        g = self._group(p, "版本", cjk)
        self._row(g, "%s v%s" % (APP_TITLE, VERSION),
                  "GDI BitBlt 抓屏内核，单文件运行，无需安装 ShareX",
                  lambda h: ui_button(h, "打开配置目录", self._open_appdata,
                                      kind="ghost", small=True,
                                      font=F_BTN_S).pack(), cjk)
        self._row(g, "配置文件与日志", r"%APPDATA%\ShareXFlash",
                  lambda h: ui_button(h, "打开日志", self._open_log, kind="ghost",
                                      small=True, font=F_BTN_S).pack(), cjk, last=True)
        self._row(g, "悬浮预览操作",
                  "打开图片 / 复制到剪贴板 / 删除截图（点击删除直接生效，卡片弹出后消失）",
                  lambda h: tk.Label(h, text="", bg=UI_WIN).pack(), cjk, last=True)

        # ---------------- 底部：状态条 + 操作条
        tk.Frame(self.root, bg=UI_LINE, height=1).pack(fill="x")
        status_strip = tk.Frame(self.root, bg=UI_WIN)
        status_strip.pack(fill="x")
        self.status_var = tk.StringVar(value="")
        tk.Label(status_strip, textvariable=self.status_var, bg=UI_WIN, fg=UI_T2,
                 font=F_DESC, anchor="w").pack(fill="x", padx=24, pady=(8, 0))

        foot = tk.Frame(self.root, bg=UI_FOOT)
        foot.pack(fill="x")
        foot_in = tk.Frame(foot, bg=UI_FOOT)
        foot_in.pack(fill="x", padx=24, pady=14)
        ui_button(foot_in, "保存并应用设置", self.save_settings, kind="primary",
                  bg=UI_FOOT, font=F_BTN).pack(side="left")
        ui_button(foot_in, "立即全屏截图", lambda: self.do_capture("full"),
                  bg=UI_FOOT, font=F_BTN).pack(side="left", padx=10)
        ui_button(foot_in, "区域截图", lambda: self.do_capture("region"),
                  bg=UI_FOOT, font=F_BTN).pack(side="left")
        ui_button(foot_in, "退出程序", self.quit_app, kind="ghost", bg=UI_FOOT,
                  font=F_BTN).pack(side="right")
        self._show_pane("general")

    # ---------------- 瑞士风布局辅助
    def _nav_item(self, parent, label, key, cjk):
        tk_ = self.tk
        wrap = tk_.Frame(parent, bg=UI_WIN)
        wrap.pack(fill="x")
        bar = tk_.Frame(wrap, bg=UI_WIN, width=2)
        bar.pack(side="left", fill="y")
        bar.pack_propagate(False)
        lab = tk_.Label(wrap, text=label, bg=UI_WIN, fg=UI_T2, anchor="w",
                        padx=22, pady=9, font=(cjk, 10), cursor="hand2")
        lab.pack(side="left", fill="x", expand=True)
        state = {"on": False}

        def paint():
            bgc = UI_RAISE if state["on"] else UI_WIN
            wrap.configure(bg=bgc)
            bar.configure(bg=UI_SIG if state["on"] else bgc)
            lab.configure(bg=bgc, fg=UI_SIG if state["on"] else UI_T2,
                          font=(cjk, 10, "bold") if state["on"] else (cjk, 10))

        def enter(_e):
            if not state["on"]:
                wrap.configure(bg=UI_RAISE)
                lab.configure(bg=UI_RAISE, fg=UI_T1)

        def leave(_e):
            paint()

        for w in (wrap, lab):
            w.bind("<Button-1>", lambda _e, k=key: self._show_pane(k))
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)
        return {"wrap": wrap, "bar": bar, "lab": lab, "state": state, "paint": paint}

    def _show_pane(self, key):
        for k, item in getattr(self, "_nav", {}).items():
            item["state"]["on"] = (k == key)
            item["paint"]()
        pane = getattr(self, "_panes", {}).get(key)
        if pane is not None:
            try:
                pane.tkraise()
            except Exception:
                pass

    def _group(self, parent, title, cjk):
        tk_ = self.tk
        box = tk_.Frame(parent, bg=UI_WIN)
        box.pack(fill="x", padx=30, pady=(26, 0))
        tk_.Label(box, text=title, bg=UI_WIN, fg=UI_T3,
                  font=(cjk, 9)).pack(anchor="w")
        body = tk_.Frame(box, bg=UI_WIN)
        body.pack(fill="x", pady=(2, 0))
        return body

    def _row(self, parent, title, desc, build, cjk, last=False):
        tk_ = self.tk
        row = tk_.Frame(parent, bg=UI_WIN)
        row.pack(fill="x")
        inner = tk_.Frame(row, bg=UI_WIN)
        inner.pack(fill="x", pady=13)
        txt = tk_.Frame(inner, bg=UI_WIN)
        txt.pack(side="left", anchor="w")
        tk_.Label(txt, text=title, bg=UI_WIN, fg=UI_T1, font=(cjk, 10),
                  anchor="w", justify="left").pack(anchor="w")
        if desc:
            tk_.Label(txt, text=desc, bg=UI_WIN, fg=UI_T3, font=(cjk, 9),
                      anchor="w", justify="left", wraplength=430).pack(anchor="w", pady=(3, 0))
        ctl = tk_.Frame(inner, bg=UI_WIN)
        ctl.pack(side="right", anchor="e")
        build(ctl)
        if not last:
            tk_.Frame(parent, bg=UI_LINE, height=1).pack(fill="x")

    def _fmt_label(self, ext):
        for label, e in FORMATS:
            if e == ext:
                return label
        return FORMATS[0][0]

    def _fmt_ext(self):
        label = self.fmt_var.get()
        for name, e in FORMATS:
            if name == label:
                return e
        return "png"

    def _update_name_preview(self):
        try:
            ext = self._fmt_ext()
            sample = build_filename(self.name_var.get(), ext, 1920, 1080, 1)
            self.preview_var.set("示例：%s.%s" % (sample, ext))
        except Exception:
            self.preview_var.set("示例：命名格式有误")

    def notify(self, text):
        try:
            self.status_var.set(text)
            head_var = getattr(self, "_head_state", None)
            if head_var is not None:
                head_var.set(text if len(text) <= 42 else text[:40] + "…")
            log(text)
        except Exception:
            pass

    # ---------------- 通用交互
    def _record_hotkey(self, key, var):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("录制快捷键")
        win.configure(bg=UI_WIN)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.geometry("400x180")
        _latin, _cjk = ui_fonts()
        tk.Label(win, text="请按下组合键（例如 Ctrl+Alt+A）", bg=UI_WIN, fg=UI_T1,
                 font=(_cjk, 11, "bold")).pack(pady=(18, 4))
        shown = tk.StringVar(value="等待按键…")
        tk.Label(win, textvariable=shown, bg=UI_WIN, fg=UI_SIG,
                 font=(_latin, 14, "bold")).pack(pady=6)
        tk.Label(win, text="PrintScreen / Win / 小键盘 / 媒体键均可录制 · 按 Esc 取消",
                 bg=UI_WIN, fg=UI_T3,
                 font=(_cjk, 9)).pack()
        pressed = set()
        mod_keys = {"control_l", "control_r", "shift_l", "shift_r", "alt_l", "alt_r",
                    "win_l", "win_r", "super_l", "super_r", "meta_l", "meta_r"}
        state = {"done": False}
        held = set()          # 事件流累积的修饰键虚拟码
        rec = HotkeyRecorder()

        def ordered_mods():
            names = []
            for name, vks in MOD_VKS.items():
                if any(v in held for v in vks) or any(HotkeyRecorder._down(v) for v in vks):
                    names.append(name)
            return names

        def finish(text=None):
            if state["done"]:
                return
            state["done"] = True
            try:
                rec.stop()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass
            if text:
                var.set(text)

        def start_fallback():
            """钩子不可用时的后备：Tk 按键事件 + 虚拟键码兜底。"""
            def on_press(evt):
                ks = (evt.keysym or "").lower()
                if ks in mod_keys:
                    pressed.add(ks)
                    shown.set(" + ".join(self._combo_preview(pressed)) or "等待主按键…")
                    return
                if ks == "escape":
                    finish()
                    return
                main = self._keysym_to_token(evt.keysym, getattr(evt, "keycode", None))
                if not main:
                    shown.set("该按键不可用，请换一个")
                    return
                text = "+".join(self._combo_tokens(pressed) + [main])
                try:
                    parse_hotkey(text)
                except ValueError as err:
                    shown.set(str(err))
                    return
                finish(text)

            win.bind("<KeyPress>", on_press)
            win.focus_force()

        def poll():
            if state["done"]:
                return
            try:
                while True:
                    item = rec.events.get_nowait()
                    kind = item[0]
                    if kind == "error":
                        shown.set("钩子不可用，切换普通模式")
                        start_fallback()
                        return
                    if kind == "up":
                        held.discard(item[1])
                        continue
                    if kind != "down":
                        continue
                    vk = item[1]
                    if vk in MOD_VK_SET:
                        held.add(vk)
                        mods = ordered_mods()
                        shown.set(" + ".join(mods + ["…"]) if mods else "等待主按键…")
                        continue
                    mods = ordered_mods()
                    if vk == 0x1B and not mods:   # Esc 取消
                        finish()
                        return
                    token = vk_to_token(vk)
                    if not token:
                        shown.set("该按键不可用（0x%02X），请换一个" % vk)
                        continue
                    text = "+".join(mods + [token])
                    try:
                        parse_hotkey(text)
                    except ValueError as err:
                        shown.set(str(err))
                        continue
                    finish(text)
                    return
            except queue.Empty:
                pass
            win.after(25, poll)

        win.protocol("WM_DELETE_WINDOW", finish)
        win.bind("<Escape>", lambda e: finish())
        rec.start()
        win.after(60, poll)
        win.after(120, lambda: win.focus_force())

    def _combo_tokens(self, pressed):
        tokens = []
        if any(k.startswith("control") for k in pressed):
            tokens.append("Ctrl")
        if any(k.startswith("shift") for k in pressed):
            tokens.append("Shift")
        if any(k.startswith("alt") for k in pressed):
            tokens.append("Alt")
        if any(k.startswith(("win", "super")) for k in pressed):
            tokens.append("Win")
        return tokens

    def _combo_preview(self, pressed):
        toks = self._combo_tokens(pressed)
        return toks + ["…"]

    def _keysym_to_token(self, keysym, keycode=None):
        if keysym and len(keysym) == 1:
            return keysym.upper()
        raw = (keysym or "").upper()
        if raw in KEYSYM_SYMBOL_VK:
            return KEYSYM_SYMBOL_VK[raw]
        up = raw.replace("_", "")
        if up in VK_NAMED or re.fullmatch(r"F([1-9]|1[0-9]|2[0-4])", up):
            return up
        # Windows 上 Tk 的 keycode 即虚拟键码，作为兜底
        if isinstance(keycode, int) and keycode > 0:
            return vk_to_token(keycode)
        return None

    def _pick_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or os.path.expanduser("~"))
        if d:
            self.dir_var.set(os.path.normpath(d))

    def _open_dir(self):
        folder = os.path.expandvars(os.path.expanduser(self.dir_var.get()))
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
        except Exception as e:
            self.notify("打开目录失败：%s" % e)

    def _open_appdata(self):
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            os.startfile(APP_DIR)
        except Exception as e:
            self.notify("打开配置目录失败：%s" % e)

    def _open_log(self):
        try:
            if os.path.isfile(LOG_PATH):
                os.startfile(LOG_PATH)
            else:
                self.notify("暂无日志文件")
        except Exception as e:
            self.notify("打开日志失败：%s" % e)

    # ---------------- 设置保存 / 热键注册
    def save_settings(self):
        old = dict(self.cfg)
        self.cfg["hotkey_full"] = self.hk_vars["hotkey_full"].get().strip()
        self.cfg["hotkey_region"] = self.hk_vars["hotkey_region"].get().strip()
        self.cfg["hotkey_window"] = self.hk_vars["hotkey_window"].get().strip()
        self.cfg["hotkey_panel"] = self.hk_vars["hotkey_panel"].get().strip()
        self.cfg["save_dir"] = self.dir_var.get().strip()
        self.cfg["image_format"] = self._fmt_ext()
        self.cfg["quality"] = int(self.q_var.get())
        self.cfg["name_template"] = self.name_var.get().strip() or DEFAULT_CONFIG["name_template"]
        self.cfg["sound_enabled"] = bool(self.sound_var.get())
        self.cfg["auto_copy"] = bool(self.copy_var.get())
        self.cfg["preview_enabled"] = bool(self.preview_var_chk.get())
        self.cfg["preview_seconds"] = int(self.sec_var.get())
        self.cfg["autostart"] = bool(self.autostart_var.get())

        for key in ("hotkey_full", "hotkey_region", "hotkey_window", "hotkey_panel"):
            if not self.cfg[key]:
                continue
            try:
                parse_hotkey(self.cfg[key])
            except ValueError as e:
                self.notify("快捷键格式有误：%s" % e)
                return

        try:
            save_config(self.cfg)
        except Exception as e:
            self.notify("保存配置失败：%s" % e)
            return

        if self.cfg["autostart"] != old.get("autostart"):
            self._set_autostart(self.cfg["autostart"])

        self.apply_hotkeys()
        self.notify("设置已保存：%s（%s）" % (self.cfg["save_dir"], self.cfg["image_format"].upper()))

    def apply_hotkeys(self):
        mapping = {
            "full": self.cfg.get("hotkey_full"),
            "region": self.cfg.get("hotkey_region"),
            "window": self.cfg.get("hotkey_window"),
            "panel": self.cfg.get("hotkey_panel"),
        }
        self.hotkeys.start(mapping)
        self.root.after(350, self._report_hotkey_errors)

    def _report_hotkey_errors(self):
        errs = dict(self.hotkeys.errors)
        if errs:
            self.notify("⚠ 部分快捷键注册失败：" + "；".join("%s(%s)" % (k, v) for k, v in errs.items()))
        else:
            self.notify("快捷键已生效：全屏 %s / 区域 %s / 窗口 %s" % (
                self.cfg.get("hotkey_full"), self.cfg.get("hotkey_region"),
                self.cfg.get("hotkey_window")))

    def _set_autostart(self, enabled):
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        exe = self._exe_for_autostart()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as k:
                if enabled:
                    winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, '"%s" --silent' % exe)
                else:
                    try:
                        winreg.DeleteValue(k, APP_NAME)
                    except FileNotFoundError:
                        pass
        except Exception as e:
            self.notify("设置开机启动失败：%s" % e)

    def _exe_for_autostart(self):
        if getattr(sys, "frozen", False):
            return os.path.abspath(sys.executable)
        return '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))

    # ---------------- 截图主流程
    def _poll_hotkeys(self):
        try:
            for action in self.hotkeys.poll():
                if action == "panel":
                    self.toggle_window()
                else:
                    self.do_capture(action)
        except Exception:
            log(traceback.format_exc())
        self.root.after(120, self._poll_hotkeys)

    def do_capture(self, mode):
        if self._capture_busy:
            return
        self._capture_busy = True
        hidden = False
        try:
            # 先收回上一张悬浮窗，并强制刷新窗口管理器，避免被截进画面
            if Toast.close_all():
                try:
                    self.root.update_idletasks()
                    self.root.update()
                except Exception:
                    pass
                time.sleep(0.22)
            if self.root.state() == "normal":
                self.root.withdraw()
                hidden = True
                self.root.update()
                time.sleep(0.12)
            self.root.after(30, lambda: self._capture_after_hide(mode, hidden))
        except Exception as e:
            self._capture_busy = False
            self.notify("截图失败：%s" % e)
            if hidden:
                self.root.deiconify()

    def _capture_after_hide(self, mode, hidden):
        try:
            shot = grab_screen()
        except Exception as e:
            self._capture_busy = False
            self.notify("抓屏失败：%s" % e)
            if hidden:
                self.root.deiconify()
            return

        def finish(img, restore=True):
            self._capture_busy = False
            if restore and hidden:
                try:
                    self.root.deiconify()
                except Exception:
                    pass
            if img is not None:
                self._handle_image(img)

        if mode == "region":
            RegionSelector(self.root, shot, lambda img: finish(img))
        elif mode == "window":
            img, hwnd = grab_active_window(shot)
            if img is None:
                self.notify("未识别到可截取的窗口")
                finish(None)
                return
            finish(img)
        else:
            finish(shot)

    def _handle_image(self, img):
        try:
            path = save_image(img, self.cfg)
        except Exception as e:
            self.notify("保存失败：%s" % e)
            log(traceback.format_exc())
            return
        self._last_path = path
        if self.cfg.get("sound_enabled", True):
            play_shutter()
        if self.cfg.get("auto_copy"):
            try:
                copy_image_to_clipboard(img)
            except Exception as e:
                log("自动复制失败: %r" % (e,))
        if self.cfg.get("preview_enabled", True):
            self._show_toast(img, path)
        self.notify("已保存：%s" % path)

    def _show_toast(self, img, path):
        try:
            if Toast.close_all():
                time.sleep(0.05)
            toast = Toast(self, img, path, self.cfg.get("preview_seconds", 10))
            toast.set_total(self.cfg.get("preview_seconds", 10))
            Toast.current = toast
        except Exception:
            log(traceback.format_exc())

    # ---------------- 窗口控制
    def hide_window(self):
        self.root.iconify()
        self.notify("已最小化到任务栏，快捷键随时可用；热键 %s 可唤回界面" %
                    self.cfg.get("hotkey_panel"))

    def show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def toggle_window(self):
        if self.root.state() in ("withdrawn", "iconic"):
            self.show_window()
        else:
            self.hide_window()

    def quit_app(self):
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------- 单实例

def ensure_single_instance():
    kernel32.CreateMutexW.restype = wt.HANDLE
    mutex = kernel32.CreateMutexW(None, False, "Local\\ShareXFlash_SingleInstance")
    err = kernel32.GetLastError()
    if err == 183:  # ERROR_ALREADY_EXISTS
        h = user32.FindWindowW(None, APP_TITLE)
        if h:
            user32.ShowWindow(h, 9)  # SW_RESTORE
            user32.SetForegroundWindow(h)
        return None
    return mutex


# ---------------------------------------------------------------- 自检（供打包验证）

def selftest():
    """无交互自检：抓屏 -> 保存 -> 音效 -> 剪贴板 -> 悬浮窗 -> 退出。"""
    report = []
    try:
        enable_dpi_awareness()
        app = QuickShotApp(silent=True)
        try:
            app.root.withdraw()
        except Exception:
            pass
        report.append("config_ok=1")
        img = grab_screen()
        report.append("grab=%dx%d" % (img.width, img.height))
        cfg_ = dict(app.cfg)
        cfg_["show"] = True
        path = save_image(img, cfg_)
        report.append("saved=%s" % path)
        report.append("exists=%s" % os.path.isfile(path))
        play_shutter()
        report.append("sound=1")
        copy_image_to_clipboard(img)
        report.append("clipboard=1")
        app._show_toast(img, path)
        report.append("toast=1")
        app.root.after(1200, app.quit_app)
        app.root.mainloop()
        report.append("loop_exit=1")
    except Exception:
        report.append("ERROR:\n" + traceback.format_exc())
    finally:
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(os.path.join(APP_DIR, "selftest_report.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(report))
        except Exception:
            pass


# ---------------------------------------------------------------- 入口

def main():
    enable_dpi_awareness()
    os.makedirs(APP_DIR, exist_ok=True)
    if "--selftest" in sys.argv:
        selftest()
        return
    if ensure_single_instance() is None:
        return
    try:
        silent = "--silent" in sys.argv
        app = QuickShotApp(silent=silent)
        app.run()
    except Exception:
        log("启动异常:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
