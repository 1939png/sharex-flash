"""讲解师画布：原生 GDI 直绘 + 独立线程消息循环。

为什么不在 Tk 线程里画：
  Tk 的 PhotoImage 每帧 60ms+（Pillow 缩放 25ms + 贴图 26ms + 重绘 12ms），
  做不到满帧；而把原生窗口的消息交给 Tk 的 mainloop 派发，在 Python 3.14 上
  会让 ctypes 回调拿不到线程状态（PyEval_RestoreThread 断言 / 0xC0000005 崩溃）。

所以这里自成一体：独立线程注册窗口类、自建 GetMessageW 循环、SetTimer 驱动
重绘与动画，主程序只通过 is_closed() 轮询收尾。实测 WM_PAINT 里一次
StretchBlt 1.5–3.4ms，滚轮缩放 1.8–3.4ms，拖动 0.6–0.9ms。
"""
import ctypes
import ctypes.wintypes as wt
import math
import threading
import time
import traceback

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
winmm = ctypes.windll.winmm
dwmapi = ctypes.windll.dwmapi
dwmapi.DwmFlush.restype = ctypes.c_int


def _display_hz():
    try:
        dc = user32.GetDC(None)
        hz = gdi32.GetDeviceCaps(dc, 116)      # VREFRESH
        user32.ReleaseDC(None, dc)
        return hz if 30 <= hz <= 360 else 60
    except Exception:
        return 60


FRAME_MS = 16        # 主屏合成 ~60Hz：按 16ms 给节拍，够贴且不会空转抢拍

WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_TIMER = 0x0113
WM_KEYDOWN = 0x0100
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_MOUSEWHEEL = 0x020A
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_SETCURSOR = 0x0020
WM_APP_TICK = 0x8001              # 多媒体定时器投递的动画节拍（WM_APP + 1）
CS_DBLCLKS = 0x0008
VK_ESCAPE = 0x1B
VK_MENU = 0x12
SRCCOPY = 0x00CC0020

TIME_PERIODIC = 0x0001
HINT_SECONDS = 3.0               # 进入讲解师模式时提示条停留时长
TOOL_H = 96                      # 底部笔栏高度（全宽，贴齐屏幕下沿）
TOOL_BG = 0x00212121
TOOL_LINE = 0x00303030
INK_COLORS = (0x00475AFF, 0x003DC8FF, 0x00F6823B, 0x00F2F2F2)   # 红/黄/蓝/白（COLORREF=0xBBGGRR）
COLOR_GRID = (0x00F2F2F2, 0x00DED8D3, 0x0099908A, 0x00101010, 0x004F4DFF, 0x002828C6,
              0x00008AFF, 0x0000B3FF, 0x003DC5FF, 0x00A8D958, 0x0059C734, 0x0000BFA5,
              0x0000C2D1, 0x004FF7C3, 0x002F7BFF, 0x008B5CF6, 0x00FF5FA2, 0x00C93ACD,
              0x00A0A000, 0x002E4A60, 0x001D65B5, 0x00636E8D, 0x00A25FFF, 0x009AA0A6)
PEN_W, MARK_W, ERASE_R = 4, 16, 22
POPUP_W = 356
POPUP_H_W = 96            # 笔刷浮层：只有粗细
POPUP_H_C = 212           # 颜色浮层：6×4 色板
TIMECALLBACK = ctypes.WINFUNCTYPE(None, wt.UINT, wt.UINT,
                                  ctypes.c_ulonglong, ctypes.c_ulonglong)
winmm.timeSetEvent.argtypes = [wt.UINT, wt.UINT, TIMECALLBACK,
                               ctypes.c_ulonglong, wt.UINT]
winmm.timeSetEvent.restype = wt.UINT
winmm.timeKillEvent.argtypes = [wt.UINT]
winmm.timeKillEvent.restype = wt.UINT
BLACKNESS = 0x00000042
COLORONCOLOR = 3
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_VISIBLE = 0x10000000
WS_POPUP = 0x80000000
SW_SHOW = 5
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
TIMER_ID = 1
TIMER_MS = 8

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", _RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
                ("rgbReserved", ctypes.c_ubyte * 32)]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, ctypes.c_ulonglong, ctypes.c_ssize_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(_PAINTSTRUCT)]
user32.BeginPaint.restype = wt.HDC
user32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(_PAINTSTRUCT)]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.InvalidateRect.argtypes = [wt.HWND, wt.LPVOID, wt.BOOL]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetCapture.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetFocus.argtypes = [wt.HWND]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_ulonglong, ctypes.c_ssize_t]
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_uint, wt.LPVOID]
user32.KillTimer.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, wt.LPVOID, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HANDLE
gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
gdi32.SelectObject.restype = wt.HANDLE
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.StretchBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.PatBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.SetStretchBltMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wt.COLORREF]
gdi32.CreatePen.restype = wt.HANDLE
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wt.HANDLE
gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.CreateEllipticRgn.restype = wt.HANDLE
gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
gdi32.CreateSolidBrush.restype = wt.HANDLE
gdi32.RoundRect.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int]
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                              ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                              ctypes.c_uint, wt.LPCWSTR]
gdi32.CreateFontW.restype = wt.HANDLE
user32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT]
user32.DrawTextW.restype = ctypes.c_int
gdi32.SelectClipRgn.argtypes = [wt.HDC, wt.HANDLE]
gdi32.SaveDC.argtypes = [wt.HDC]
gdi32.SaveDC.restype = ctypes.c_int
gdi32.RestoreDC.argtypes = [wt.HDC, ctypes.c_int]
gdi32.Ellipse.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.Rectangle.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.MoveToEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.LPVOID]
gdi32.LineTo.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.Polyline.argtypes = [wt.HDC, ctypes.POINTER(wt.POINT), ctypes.c_int]
gdi32.Polygon.argtypes = [wt.HDC, ctypes.POINTER(wt.POINT), ctypes.c_int]
gdi32.Pie.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT,
                             ctypes.c_ulonglong, ctypes.c_ssize_t)

msimg32 = ctypes.windll.msimg32
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1


class _BLENDFUNCTION(ctypes.Structure):
    """必须按值传给 GdiAlphaBlend；按指针传会被当成垃圾值。"""
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


msimg32.AlphaBlend.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               _BLENDFUNCTION]
msimg32.AlphaBlend.restype = wt.BOOL


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]



class _BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class _BI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BIH), ("bmiColors", wt.DWORD * 3)]


def _to_bgra(img):
    """RGB -> BGRA 字节串（CreateDIBSection 直接吃）。"""
    img = img.convert("RGB")
    raw = img.tobytes()
    n = img.width * img.height
    b = bytearray(n * 4)
    b[0::4] = raw[2::3]
    b[1::4] = raw[1::3]
    b[2::4] = raw[0::3]
    b[3::4] = b"\xff" * n
    return bytes(b)


def _ease_out_cubic(p):
    """缓出（3 次）：起手快、末尾收得住，不至于最后一段几乎不动。"""
    return 1 - (1 - p) ** 3


def _ease_out_back(p):
    """回弹：先冲过目标再退回，p=1 时严格等于 1。"""
    q = p - 1
    return 1 + 2.70158 * q ** 3 + 1.70158 * q ** 2


def _ease_out_elastic(p):
    if p <= 0 or p >= 1:
        return p
    return 2 ** (-10 * p) * math.sin((p * 10 - 0.75) * (2 * math.pi / 3)) + 1


def _smoothstep(p):
    return p * p * (3 - 2 * p)


_EASES = {
    "ease_out": _ease_out_cubic,
    "back": _ease_out_back,
    "elastic": _ease_out_elastic,
    "linear": lambda p: p,
}


def _lighten(color, k):
    """COLORREF 往白色混，做荧光笔的浅色调。"""
    b, g, r = color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF
    b = int(b + (255 - b) * k)
    g = int(g + (255 - g) * k)
    r = int(r + (255 - r) * k)
    return b | (g << 8) | (r << 16)


def _ring_for(color):
    """浅色圆点用深色选中环，否则白环看不见。"""
    b, g, r = color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF
    return 0x00303030 if (r * 299 + g * 587 + b * 114) / 1000 > 160 else 0x00FFFFFF


def _dib_from_bytes(buf, w, h):
    """返回 (memdc, hbmp, bits)；调用方负责 DeleteObject(hbmp)+DeleteDC(memdc)。"""
    bmi = _BI()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BIH)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    hdc = user32.GetDC(None)
    hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    user32.ReleaseDC(None, hdc)
    ctypes.memmove(bits, buf, len(buf))
    mdc = gdi32.CreateCompatibleDC(None)
    gdi32.SelectObject(mdc, hbmp)
    return mdc, hbmp, bits


class _WinShim:
    """主程序日志用的外壳（真窗口是原生 HWND，不属于 Tk）。"""

    def __init__(self, canvas):
        self._c = canvas

    def winfo_geometry(self):
        b = self._c._bounds
        return "%dx%d+%d+%d" % (b[2], b[3], b[0], b[1])

    def winfo_ismapped(self):
        return 1

    def state(self):
        return "normal"

    def attributes(self, *a):
        if a and a[0] == "-topmost":
            return 1
        return None


class GdiLecturerCanvas:
    """冻结画面 + 原生平移/缩放 + 重点高亮。cx/cy 是屏幕中心对应的源坐标。"""

    SPOT_COLLECT = 0.5
    SPOT_HOLD = 1.2
    SPOT_FADE = 0.2
    CRISP_DELAY = 0.12
    DIM_ALPHA = 183           # 圈外压到 ~28% 亮度（0.72 的黑）

    def __init__(self, root, tkmod, bounds, image, on_close=None, log=None, exit_combo=None,
                 spot_times=None, spot_ease=None, hint=None):
        self._log = log or (lambda m: None)
        self._on_close = on_close
        self._exit_combo = exit_combo
        # 进场提示：进入后先在画布顶部显示几秒，避免用户不知道能干什么
        self.hint_text = hint or ""
        self._hint_until = time.perf_counter() + HINT_SECONDS
        self._hint_font = None
        # 批注：底部笔栏 + 手绘线条（退出画布即随窗口一起消失，不落盘）
        self._tool = "hand"          # hand / pen / marker / eraser
        self._ink_color = INK_COLORS[0]
        self._pen_w = PEN_W
        self._mark_w = MARK_W
        self._popup = None           # 笔刷浮层：'pen'/'marker' 时显示粗细与配色
        self._slider_drag = False
        self._ui_font = None
        self._glyphs = {}            # 抗锯齿图标缓存：(名字, 颜色) -> (memdc,hbmp,w,h)
        self._ink = []               # [{color, w, pts:[(x,y)...]}]
        self._drawing = None
        self._ink_states = [[]]      # 快照式撤销：每一步都存一份 stroke 列表
        self._ink_si = 0
        # 高亮动画时长与曲线：可在程序内调（收拢/停留/消失 + 收拢曲线）
        col, hold, fade = spot_times or (self.SPOT_COLLECT, self.SPOT_HOLD, self.SPOT_FADE)
        self.spot_collect = max(0.1, float(col))
        self.spot_hold = max(0.1, float(hold))
        self.spot_fade = max(0.05, float(fade))
        self.spot_ease = spot_ease if spot_ease in _EASES else "ease_out"
        self._mtimer = 0
        self._tick_cb = TIMECALLBACK(self._on_mm_tick)
        self.root = root
        self._closed = False
        self._closed_event = threading.Event()
        self.hwnd = None
        self._bounds = tuple(bounds)
        left, top, w, h = bounds
        self.w, self.h = int(w), int(h)
        self.sw, self.sh = image.size
        self.src = image.convert("RGB")
        self.s = 1.0
        self.cx = self.sw / 2.0
        self.cy = self.sh / 2.0
        self._drag = None
        self._rdrag = None
        self._erasing = False
        self._spot = None
        self._spot_t0 = 0.0
        self._spot_r0 = self._spot_r1 = 0.0
        self._held_frame = False
        self._base_dc = self._base_bmp = None
        self._back_dc = self._back_bmp = None      # 双缓冲：先画这里，再整块上屏
        self._blk_dc = self._blk_bmp = None   # 1x1 纯黑，AlphaBlend 用
        self._screen_dc = None        # 窗口 DC 缓存：每帧 GetDC 本身就有毫秒级开销
        self._crisp = None            # (s, cx, cy, memdc, hbmp)
        self._crisp_img = None
        self._dirty_at = None
        self._paint_ms = 0.0
        self._ready = threading.Event()
        # 图标先在这边渲染好，别让画布第一帧花几十毫秒画图标（会闪一下黑）
        try:
            for _n in ("undo", "redo", "pen", "marker", "eraser", "clear", "colors", "done"):
                self._glyph_surface(_n)
            self._glyph_surface("cur", self._ink_color)
            self._chip_surface(0x00393939, 14)
        except Exception:
            pass
        self._thread = threading.Thread(target=self._run, name="lect-canvas", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0) or not self.hwnd:
            raise RuntimeError("讲解师画布窗口创建失败")
        self.win = _WinShim(self)

    # -- 线程主体 -----------------------------------------------------------
    def _run(self):
        try:
            winmm.timeBeginPeriod(1)      # 定时器精度 15.6ms -> 1ms，动画节拍才不抖
        except Exception:
            pass
        try:
            self._base_dc, self._base_bmp, _bits = _dib_from_bytes(
                _to_bgra(self.src), self.sw, self.sh)
            # 双缓冲：1920x1080 一次 BitBlt ≈1ms，换来屏幕上永远看不到中间黑场
            self._back_dc, self._back_bmp, _b = _dib_from_bytes(
                bytes(self.w * self.h * 4), self.w, self.h)
            # 1x1 纯黑：高亮压暗时用 AlphaBlend 铺一层半透明黑，随视图自动跟手，不用预生成暗场
            self._blk_dc, self._blk_bmp, _c = _dib_from_bytes(b"\x00\x00\x00\xff", 1, 1)
            self._dim(self._back_dc)      # 预热一次：首次 AlphaBlend 有一次性开销，别留给第一帧高亮
        except Exception:
            self._log(traceback.format_exc())
            self._ready.set()
            return
        proc = WNDPROC(self._wndproc_routed)
        self._proc = proc                        # 引用留在 self 上，活到线程结束
        cls = "SXFLectCanvas%d" % id(self)
        wc = _WNDCLASSW()
        wc.style = CS_DBLCLKS
        wc.lpfnWndProc = proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = cls
        user32.RegisterClassW(ctypes.byref(wc))
        left, top, w, h = self._bounds
        # 先不设 WS_VISIBLE：把首帧画好再显示，避免"先黑一下再出图"
        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW, cls, "ShareX Flash 讲解师",
            WS_POPUP, left, top, w, h, None, None, wc.hInstance, None)
        if not self.hwnd:
            self._ready.set()
            return
        hdc0 = user32.GetDC(self.hwnd)
        if hdc0:
            self._paint(hdc0)
            user32.ReleaseDC(self.hwnd, hdc0)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, left, top, w, h, SWP_NOSIZE)
        user32.SetForegroundWindow(self.hwnd)
        user32.SetFocus(self.hwnd)
        user32.SetTimer(self.hwnd, TIMER_ID, TIMER_MS, None)
        try:
            self._screen_dc = user32.GetDC(self.hwnd)
        except Exception:
            self._screen_dc = None
        self._ready.set()
        # 预热清晰视口图，Alt+左键时第一帧就能动
        try:
            self._crispify()
        except Exception:
            self._log(traceback.format_exc())

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 消息循环结束：清理 GDI 与类注册
        try:
            user32.KillTimer(self.hwnd, TIMER_ID)
        except Exception:
            pass
        self._stop_anim_timer()
        if self._screen_dc:
            try:
                user32.ReleaseDC(self.hwnd, self._screen_dc)
            except Exception:
                pass
            self._screen_dc = None
        crisp = self._crisp or (None, None, None, None, None)
        for pair in ((crisp, crisp[3], crisp[4]),
                     (None, self._blk_dc, self._blk_bmp),
                     (None, self._back_dc, self._back_bmp),
                     (None, self._base_dc, self._base_bmp)):
            try:
                bmp, dc = pair[1], pair[2]
                if bmp:
                    gdi32.DeleteObject(bmp)
                if dc:
                    gdi32.DeleteDC(dc)
            except Exception:
                pass
        self._crisp = self._blk_dc = self._base_dc = None
        if self._hint_font:
            try:
                gdi32.DeleteObject(self._hint_font)
            except Exception:
                pass
            self._hint_font = None
        if self._ui_font:
            try:
                gdi32.DeleteObject(self._ui_font)
            except Exception:
                pass
            self._ui_font = None
        for surf in list(getattr(self, "_glyphs", {}).values()):
            try:
                gdi32.DeleteObject(surf[1])
                gdi32.DeleteDC(surf[0])
            except Exception:
                pass
        self._glyphs = {}
        try:
            user32.UnregisterClassW(cls, wc.hInstance)
        except Exception:
            pass
        try:
            winmm.timeEndPeriod(1)
        except Exception:
            pass
        self._closed = True
        self._closed_event.set()
        cb = self._on_close
        self._on_close = None
        if cb:
            try:
                cb()
            except Exception:
                self._log(traceback.format_exc())

    def is_closed(self):
        return self._closed_event.is_set()

    def close(self):
        """任何线程都能调用：给窗口投一条 WM_CLOSE 即走正常销毁流程。"""
        if self._closed or not self.hwnd:
            return
        try:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        except Exception:
            self._log(traceback.format_exc())

    # -- 视图 ---------------------------------------------------------
    def _view_rect(self):
        vw, vh = self.w / self.s, self.h / self.s
        return self.cx - vw / 2.0, self.cy - vh / 2.0, vw, vh

    def _clamp_center(self):
        vw, vh = self.w / self.s, self.h / self.s
        if vw >= self.sw:
            self.cx = self.sw / 2.0
        else:
            self.cx = min(self.sw - vw / 2.0, max(vw / 2.0, self.cx))
        if vh >= self.sh:
            self.cy = self.sh / 2.0
        else:
            self.cy = min(self.sh - vh / 2.0, max(vh / 2.0, self.cy))

    def _mark_dirty(self):
        self._dirty_at = time.perf_counter()

    # -- 绘制 -----------------------------------------------------------
    def _paint(self, hdc):
        """双缓冲：全部画到内存位图，最后一次 BitBlt 上屏，屏幕上看不到中间黑场。"""
        t0 = time.perf_counter()
        tgt = self._back_dc or hdc
        gdi32.PatBlt(tgt, 0, 0, self.w, self.h, BLACKNESS)
        gdi32.SetStretchBltMode(tgt, COLORONCOLOR)
        crisp = self._crisp
        if crisp and crisp[0] == self.s and crisp[1] == self.cx and crisp[2] == self.cy:
            gdi32.StretchBlt(tgt, 0, 0, self.w, self.h, crisp[3], 0, 0, self.w, self.h, SRCCOPY)
        else:
            sx, sy, sw, sh = self._view_rect()
            gdi32.StretchBlt(tgt, 0, 0, self.w, self.h, self._base_dc,
                             int(round(sx)), int(round(sy)),
                             int(round(sw)), int(round(sh)), SRCCOPY)
        if self._spot is not None:
            self._paint_spot(tgt)
        self._paint_ink(tgt)
        if self.hint_text and time.perf_counter() < self._hint_until:
            self._paint_hint(tgt)
        self._paint_toolbar(tgt)
        self._paint_popup(tgt)
        if self._back_dc:
            gdi32.BitBlt(hdc, 0, 0, self.w, self.h, self._back_dc, 0, 0, SRCCOPY)
        self._paint_ms = (time.perf_counter() - t0) * 1000

    def _paint_back(self):
        """只画内存，不上屏（动画帧自己按环带挑需要刷新的区域）。"""
        self._paint(self._back_dc)

    def _band_of(self, r_old, r_new, mx, my):
        """两圈之间的外接矩形：半径变化不大时只刷这一块，上屏开销从整屏降到环带。"""
        r = max(r_old, r_new) + 6
        return (max(0, int(mx - r)), max(0, int(my - r)),
                min(self.w, int(mx + r)), min(self.h, int(my + r)))

    def _dim(self, hdc):
        """用 1x1 纯黑 + 常量 alpha 铺满整屏：压暗跟随当前视图，缩放/拖动后不用重算。"""
        if not self._blk_dc:
            return
        bf = _BLENDFUNCTION(AC_SRC_OVER, 0, self.DIM_ALPHA, 0)
        try:
            msimg32.AlphaBlend(hdc, 0, 0, self.w, self.h,
                               self._blk_dc, 0, 0, 1, 1, bf)
        except Exception:
            self._log(traceback.format_exc())

    def _paint_hint(self, hdc):
        """顶部一条深色圆角提示：能拖动、能缩放、能标重点、怎么退出。"""
        if not self._hint_font:
            try:
                self._hint_font = gdi32.CreateFontW(
                    -22, 0, 0, 0, 500, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei UI")
            except Exception:
                self._log(traceback.format_exc())
                return
        w = int(min(self.w - 120, 1040))
        h = 56
        x = (self.w - w) // 2
        y = 36
        gdi32.SetBkMode(hdc, 1)                     # TRANSPARENT
        brush = gdi32.CreateSolidBrush(0x00202020)
        pen = gdi32.CreatePen(5, 0, 0)              # PS_NULL
        old_brush = gdi32.SelectObject(hdc, brush)
        old_pen = gdi32.SelectObject(hdc, pen)
        gdi32.RoundRect(hdc, x, y, x + w, y + h, 16, 16)
        gdi32.SelectObject(hdc, old_pen)
        gdi32.DeleteObject(pen)
        gdi32.SelectObject(hdc, brush)
        gdi32.DeleteObject(brush)
        font = gdi32.SelectObject(hdc, self._hint_font)
        gdi32.SetTextColor(hdc, 0x00E6E6E6)
        rect = wt.RECT(x + 16, y, x + w - 16, y + h)
        user32.DrawTextW(hdc, self.hint_text, -1, ctypes.byref(rect),
                         0x0001 | 0x0004 | 0x0020)  # CENTER|VCENTER|SINGLELINE
        gdi32.SelectObject(hdc, font)

    def _paint_spot(self, hdc):
        """压暗 + 亮圈：整屏盖一层半透明黑，再用椭圆剪裁把圈内还原成原图。"""
        mx, my, r = self._spot[0], self._spot[1], self._spot[2]
        x0, y0 = int(round(mx - r)), int(round(my - r))
        w = h = int(round(2 * r))
        if w < 6 or h < 6:
            return
        self._dim(hdc)
        vx, vy, vw, vh = self._view_rect()
        sx = int(round(vx + x0 / self.s))
        sy = int(round(vy + y0 / self.s))
        sw = max(1, int(round(w / self.s)))
        sh = max(1, int(round(h / self.s)))
        rgn = gdi32.CreateEllipticRgn(x0, y0, x0 + w, y0 + h)
        saved = gdi32.SaveDC(hdc)
        gdi32.SelectClipRgn(hdc, rgn)
        gdi32.SetStretchBltMode(hdc, COLORONCOLOR)
        crisp = self._crisp
        if crisp and crisp[0] == self.s and crisp[1] == self.cx and crisp[2] == self.cy:
            # 圈内就是视口那块 1:1 图，直接对齐拷，不用再缩放（更快也更锐）
            gdi32.BitBlt(hdc, x0, y0, w, h, crisp[3], x0, y0, SRCCOPY)
        else:
            gdi32.StretchBlt(hdc, x0, y0, w, h, self._base_dc, sx, sy, sw, sh, SRCCOPY)
        gdi32.RestoreDC(hdc, saved)
        gdi32.DeleteObject(rgn)
        pen = gdi32.CreatePen(0, 3, 0x00FFFFFF)
        old_pen = gdi32.SelectObject(hdc, pen)
        old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))   # NULL_BRUSH
        gdi32.Ellipse(hdc, x0, y0, x0 + w, y0 + h)
        gdi32.SelectObject(hdc, old_brush)
        gdi32.SelectObject(hdc, old_pen)
        gdi32.DeleteObject(pen)

    # -- 批注笔栏 -------------------------------------------------------
    def _toolbar_hits(self):
        """底部笔栏每个按钮的圆心：(名字, x, y, 命中半径, 参数)。"""
        cy = self.h - TOOL_H // 2
        cx = self.w // 2
        items = [("undo", cx - 470, cy, 30, None),
                 ("redo", cx - 392, cy, 30, None),
                 ("pen", cx - 240, cy, 34, None),
                 ("marker", cx - 160, cy, 34, None),
                 ("eraser", cx - 78, cy, 34, None),
                 ("clear", cx + 24, cy, 30, None),
                 ("cur", cx + 120, cy, 26, None),      # 当前颜色
                 ("colors", cx + 196, cy, 32, None),   # 更多颜色（色轮）
                 ("done", cx + 470, cy, 32, None)]
        return items

    def _hit_toolbar(self, mx, my):
        if my < self.h - TOOL_H:
            return None
        for name, x, y, r, arg in self._toolbar_hits():
            if (mx - x) ** 2 + (my - y) ** 2 <= r * r:
                return name, arg
        return ("blank", None)

    def _paint_toolbar(self, hdc):
        h, w = self.h, self.w
        y0 = h - TOOL_H
        brush = gdi32.CreateSolidBrush(TOOL_BG)
        pen = gdi32.CreatePen(5, 0, 0)          # PS_NULL
        ob = gdi32.SelectObject(hdc, brush)
        op = gdi32.SelectObject(hdc, pen)
        gdi32.Rectangle(hdc, 0, y0, w, h)
        gdi32.SelectObject(hdc, op)
        gdi32.SelectObject(hdc, ob)
        gdi32.DeleteObject(pen)
        gdi32.DeleteObject(brush)
        self._fill_rect(hdc, 0, y0, w, y0 + 1, TOOL_LINE)

        for name, x, cy, r, arg in self._toolbar_hits():
            if name in ("pen", "marker", "eraser") and self._tool == name:
                self._blit_chip(hdc, x, cy, 0x00393939)
            self._blit_icon(hdc, name, x, cy)

    # -- 抗锯齿图标：Pillow 4x 超采样后降采样，再用 AlphaBlend 贴进 DIB -------
    @staticmethod
    def _crgb(color):
        """COLORREF(0x00BBGGRR) -> Pillow 的 (R, G, B)。"""
        return (color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF)

    def _make_surface(self, im):
        from PIL import Image
        im = im.convert("RGBA")
        w, h = im.size
        src = im.tobytes()
        out = bytearray(w * h * 4)
        for i in range(0, len(src), 4):      # AlphaBlend 要预乘 alpha 的 BGRA
            a = src[i + 3]
            if a:
                out[i] = (src[i + 2] * a + 127) // 255
                out[i + 1] = (src[i + 1] * a + 127) // 255
                out[i + 2] = (src[i] * a + 127) // 255
                out[i + 3] = a
        mdc, hbmp, _bits = _dib_from_bytes(bytes(out), w, h)
        return (mdc, hbmp, w, h)

    def _blit_surface(self, hdc, surf, x, y):
        mdc, hbmp, w, h = surf
        bf = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        msimg32.AlphaBlend(hdc, int(x), int(y), w, h, mdc, 0, 0, w, h, bf)

    def _chip_surface(self, color, rad=14):
        key = ("chip", color, rad)
        surf = self._glyphs.get(key)
        if not surf:
            from PIL import Image, ImageDraw
            S = 4
            im = Image.new("RGBA", (76 * S, 68 * S), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.rounded_rectangle([0, 0, 76 * S - 1, 68 * S - 1], radius=rad * S,
                                fill=self._crgb(color))
            im = im.resize((76, 68), Image.LANCZOS)
            surf = self._make_surface(im)
            self._glyphs[key] = surf
        return surf

    def _blit_chip(self, hdc, cx, cy, color, rad=14):
        surf = self._chip_surface(color, rad)
        self._blit_surface(hdc, surf, cx - 38, cy - 34)

    def _glyph_surface(self, name, tint=None):
        key = (name, tint)
        surf = self._glyphs.get(key)
        if surf:
            return surf
        from PIL import Image, ImageDraw
        S, N = 4, 56
        C = N * S // 2
        im = Image.new("RGBA", (N * S, N * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)

        def P(dx, dy):
            return (C + dx * S, C + dy * S)

        def W(v):
            return max(1, int(round(v * S)))

        def line(pts, color, width, caps=True):
            d.line([P(*p) for p in pts], fill=self._crgb(color), width=W(width), joint="curve")
            if caps:
                r = W(width) / 2.0
                for p in (pts[0], pts[-1]):
                    x, y = P(*p)
                    d.ellipse([x - r, y - r, x + r, y + r], fill=self._crgb(color))

        def poly(pts, color):
            d.polygon([P(*p) for p in pts], fill=self._crgb(color))

        def rrect(box, color, rad, outline=None, ow=0):
            d.rounded_rectangle([P(box[0], box[1]), P(box[2], box[3])], radius=rad * S,
                                fill=self._crgb(color), outline=self._crgb(outline) if outline else None,
                                width=W(ow) if ow else 0)

        if name in ("undo", "redo"):
            back = name == "redo"
            r = 16
            dd = -1 if back else 1
            pts = []
            for k in range(15):
                a = math.radians(198 + k * (144 / 14.0))
                pts.append((dd * r * math.cos(a), -r * math.sin(a)))
            line(pts, 0x00BDBDBD, 3)
            p0, p1 = pts[0], pts[1]
            tx, ty = p0[0] - p1[0], p0[1] - p1[1]
            n = math.hypot(tx, ty) or 1.0
            tx, ty = tx / n, ty / n
            for sgn in (1, -1):
                ang = math.radians(38) * sgn
                rx = tx * math.cos(ang) - ty * math.sin(ang)
                ry = tx * math.sin(ang) + ty * math.cos(ang)
                line([p0, (p0[0] + rx * 11, p0[1] + ry * 11)], 0x00BDBDBD, 3)
        elif name == "pen":
            line([(-6, -5), (15, 16)], 0x002C2C2C, 9)
            poly([(-19, -18), (-7, -3), (-2, -12)], 0x00B8B8B8)
            line([(-15, -14), (-3, -2)], 0x002060F0, 3)
        elif name == "marker":
            line([(1, 4), (16, 17)], 0x00262626, 17)
            poly([(-18, -12), (-2, 2), (4, -5), (-10, -19)], 0x00F0C080)
        elif name == "eraser":
            rrect((-18, -13, 18, 13), 0x003A3A3A, 9, 0x00505050, 2)
            for k in range(3):
                x = -10 + k * 9
                line([(x, 7), (x + 5, -7)], 0x006B6B6B, 3)
        elif name == "clear":
            line([(-13, -9), (13, -9)], 0x00989898, 3)
            line([(-5, -13), (5, -13)], 0x00989898, 3)
            rrect((-9, -5, 9, 17), 0x00323232, 4, 0x00909090, 2)
            for dx in (-4, 4):
                line([(dx, -1), (dx, 12)], 0x00909090, 2)
        elif name == "done":
            line([(-14, 1), (-4, 11), (16, -12)], 0x00EDEDED, 5)
        elif name == "cur":
            col = tint if tint is not None else 0x00FFFFFF
            d.ellipse([P(-12, -12), P(12, 12)], fill=self._crgb(col))
            ring = _ring_for(col)
            d.ellipse([P(-16, -16), P(16, 16)], outline=self._crgb(ring), width=W(2))
        elif name == "colors":
            cols = (0x004F4DFF, 0x0000B3FF, 0x00D1C200, 0x0059C734,
                    0x003DC5FF, 0x002F7BFF, 0x00C93ACD, 0x00FF7B2F)
            r = 16
            for k, col in enumerate(cols):
                a0 = -90 + k * 45
                d.arc([C - r * S, C - r * S, C + r * S, C + r * S],
                      a0 + 3, a0 + 39, fill=self._crgb(col), width=W(8))
            d.ellipse([P(-8, -8), P(8, 8)], fill=self._crgb(TOOL_BG))
        im = im.resize((N, N), Image.LANCZOS)
        surf = self._make_surface(im)
        self._glyphs[key] = surf
        return surf

    def _blit_icon(self, hdc, name, cx, cy):
        surf = self._glyph_surface(name, self._ink_color if name == "cur" else None)
        self._blit_surface(hdc, surf, cx - surf[2] // 2, cy - surf[3] // 2)

    def _fill_rect(self, hdc, x0, y0, x1, y1, color):
        brush = gdi32.CreateSolidBrush(color)
        ob = gdi32.SelectObject(hdc, brush)
        op = gdi32.SelectObject(hdc, gdi32.CreatePen(5, 0, 0))
        gdi32.Rectangle(hdc, int(x0), int(y0), int(x1), int(y1))
        gdi32.SelectObject(hdc, op)
        gdi32.SelectObject(hdc, ob)
        gdi32.DeleteObject(brush)

    def _fill_round(self, hdc, box, color, rad, border=None):
        brush = gdi32.CreateSolidBrush(color)
        pen = gdi32.CreatePen(0, 2, border) if border is not None else gdi32.CreatePen(5, 0, 0)
        ob = gdi32.SelectObject(hdc, brush)
        op = gdi32.SelectObject(hdc, pen)
        gdi32.RoundRect(hdc, int(box[0]), int(box[1]), int(box[2]), int(box[3]),
                        int(rad), int(rad))
        gdi32.SelectObject(hdc, op)
        gdi32.SelectObject(hdc, ob)
        gdi32.DeleteObject(pen)
        gdi32.DeleteObject(brush)

    def _poly(self, hdc, pts, color, width):
        if len(pts) < 2:
            return
        width = max(1, int(width))
        pen = gdi32.CreatePen(0x00010000, width, color)    # PS_GEOMETRIC + 圆头圆角
        if not pen:
            pen = gdi32.CreatePen(0, width, color)
        old = gdi32.SelectObject(hdc, pen)
        arr = (wt.POINT * len(pts))(*[wt.POINT(int(p[0]), int(p[1])) for p in pts])
        gdi32.Polyline(hdc, arr, len(pts))
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(pen)

    def _fill_poly(self, hdc, pts, color):
        if len(pts) < 3:
            return
        brush = gdi32.CreateSolidBrush(color)
        ob = gdi32.SelectObject(hdc, brush)
        op = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))    # NULL_BRUSH
        arr = (wt.POINT * len(pts))(*[wt.POINT(int(p[0]), int(p[1])) for p in pts])
        gdi32.Polygon(hdc, arr, len(pts))
        gdi32.SelectObject(hdc, op)
        gdi32.SelectObject(hdc, ob)
        gdi32.DeleteObject(brush)

    def _icon_arrow(self, hdc, cx, cy, back):
        r = 16
        d = -1 if back else 1
        pts = []
        for k in range(15):
            a = math.radians(198 + k * (144 / 14.0))
            pts.append((cx + d * r * math.cos(a), cy - r * math.sin(a)))
        self._poly(hdc, pts, 0x00BDBDBD, 3)
        p0, p1 = pts[0], pts[1]
        tx, ty = p0[0] - p1[0], p0[1] - p1[1]
        n = math.hypot(tx, ty) or 1.0
        tx, ty = tx / n, ty / n
        for sgn in (1, -1):
            ang = math.radians(38) * sgn
            rx = tx * math.cos(ang) - ty * math.sin(ang)
            ry = tx * math.sin(ang) + ty * math.cos(ang)
            self._poly(hdc, [p0, (p0[0] + rx * 11, p0[1] + ry * 11)], 0x00BDBDBD, 3)

    def _icon_pen(self, hdc, cx, cy):
        self._poly(hdc, [(cx - 6, cy - 5), (cx + 15, cy + 16)], 0x002C2C2C, 9)
        tip = [(cx - 19, cy - 18), (cx - 7, cy - 3), (cx - 2, cy - 12)]
        self._fill_poly(hdc, tip, 0x00B8B8B8)
        self._poly(hdc, [(cx - 15, cy - 14), (cx - 3, cy - 2)], 0x002060F0, 3)

    def _icon_marker(self, hdc, cx, cy):
        self._poly(hdc, [(cx + 1, cy + 4), (cx + 16, cy + 17)], 0x00262626, 17)
        nib = [(cx - 18, cy - 12), (cx - 2, cy + 2), (cx + 4, cy - 5), (cx - 10, cy - 19)]
        self._fill_poly(hdc, nib, 0x00F0C080)

    def _icon_eraser(self, hdc, cx, cy):
        self._fill_round(hdc, (cx - 18, cy - 13, cx + 18, cy + 13), 0x003A3A3A, 9, 0x00505050)
        for k in range(3):
            x = cx - 10 + k * 9
            self._poly(hdc, [(x, cy + 7), (x + 5, cy - 7)], 0x006B6B6B, 3)

    def _icon_trash(self, hdc, cx, cy):
        self._poly(hdc, [(cx - 13, cy - 9), (cx + 13, cy - 9)], 0x00989898, 3)
        self._poly(hdc, [(cx - 5, cy - 13), (cx + 5, cy - 13)], 0x00989898, 3)
        self._fill_round(hdc, (cx - 9, cy - 5, cx + 9, cy + 17), 0x00323232, 4, 0x00909090)
        for dx in (-4, 4):
            self._poly(hdc, [(cx + dx, cy - 1), (cx + dx, cy + 12)], 0x00909090, 2)

    def _icon_check(self, hdc, cx, cy):
        self._poly(hdc, [(cx - 14, cy + 1), (cx - 4, cy + 11), (cx + 16, cy - 12)],
                   0x00EDEDED, 5)

    def _icon_current(self, hdc, cx, cy):
        self._dot(hdc, cx, cy, 12, self._ink_color)
        self._ring(hdc, cx, cy, 16, _ring_for(self._ink_color), 2)

    def _icon_wheel(self, hdc, cx, cy):
        """色轮＝更多颜色入口（原来的四个快捷色合并到这里）。"""
        cols = (0x004F4DFF, 0x0000B3FF, 0x00D1C200, 0x0059C734,
                0x003DC5FF, 0x002F7BFF, 0x00C93ACD, 0x00FF7B2F)
        r = 16
        for k, col in enumerate(cols):
            a0 = -90 + k * 45
            pts = []
            for j in range(5):
                a = math.radians(a0 + 3 + j * 9)
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
            self._poly(hdc, pts, col, 8)
        self._dot(hdc, cx, cy, 8, TOOL_BG)

    def _ring(self, hdc, cx, cy, r, color, width):
        pen = gdi32.CreatePen(0, max(1, int(width)), color)
        op = gdi32.SelectObject(hdc, pen)
        ob = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))
        gdi32.Ellipse(hdc, cx - r, cy - r, cx + r, cy + r)
        gdi32.SelectObject(hdc, ob)
        gdi32.SelectObject(hdc, op)
        gdi32.DeleteObject(pen)

    # -- 浮层：笔刷粗细 / 颜色板 ---------------------------------------
    def _popup_range(self):
        return (2.0, 16.0) if self._popup == "pen" else (8.0, 44.0)

    def _popup_width(self):
        return self._pen_w if self._popup == "pen" else self._mark_w

    def _popup_rect(self):
        cx, hh = self.w // 2 + 196, POPUP_H_C
        if self._popup == "pen":
            cx, hh = self.w // 2 - 240, POPUP_H_W
        elif self._popup == "marker":
            cx, hh = self.w // 2 - 160, POPUP_H_W
        elif self._popup != "color":
            return None
        bx = min(max(8, cx - POPUP_W // 2), self.w - POPUP_W - 8)
        by = self.h - TOOL_H - hh - 14
        return (bx, by, bx + POPUP_W, by + hh)

    def _popup_y(self):
        r = self._popup_rect()
        return r[1] + 48 if r else 0

    def _popup_track(self):
        r = self._popup_rect()
        return (r[0] + 58, r[0] + 238) if r else (0, 0)

    def _popup_color_xy(self, i):
        r = self._popup_rect()
        bx, by = r[0], r[1]
        return bx + 42 + (i % 6) * 53, by + 36 + (i // 6) * 44

    def _paint_popup(self, hdc):
        r = self._popup_rect()
        if not r:
            return
        bx, by, bx2, by2 = r
        self._fill_round(hdc, r, 0x00272727, 18, 0x00444444)
        if self._popup == "color":
            for i, c in enumerate(COLOR_GRID):
                px, py = self._popup_color_xy(i)
                self._dot(hdc, px, py, 15, c)
                if c == self._ink_color:
                    self._ring(hdc, px, py, 19, _ring_for(c), 2)
            return
        # 粗细：减 / 滑杆 / 加 / 数值
        y = self._popup_y()
        x0, x1 = self._popup_track()
        self._poly(hdc, [(bx + 22, y), (bx + 38, y)], 0x00D2D2D2, 3)
        self._poly(hdc, [(bx + 262, y), (bx + 278, y)], 0x00D2D2D2, 3)
        self._poly(hdc, [(bx + 270, y - 8), (bx + 270, y + 8)], 0x00D2D2D2, 3)
        self._poly(hdc, [(x0, y), (x1, y)], 0x00525252, 4)
        lo, hi = self._popup_range()
        t = (self._popup_width() - lo) / (hi - lo)
        kx = int(x0 + (x1 - x0) * max(0.0, min(1.0, t)))
        if kx > x0 + 2:
            self._poly(hdc, [(x0, y), (kx, y)], self._ink_color, 4)
        self._dot(hdc, kx, y, 8, 0x00F0F0F0)
        if not self._ui_font:
            try:
                self._ui_font = gdi32.CreateFontW(
                    -18, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei UI")
            except Exception:
                self._ui_font = None
        if self._ui_font:
            font = gdi32.SelectObject(hdc, self._ui_font)
            gdi32.SetBkMode(hdc, 1)
            gdi32.SetTextColor(hdc, 0x00E0E0E0)
            rect = wt.RECT(bx + 286, y - 16, bx2 - 12, y + 16)
            txt = "%d px" % int(round(self._popup_width()))
            user32.DrawTextW(hdc, txt, -1, ctypes.byref(rect),
                             0x0002 | 0x0004 | 0x0020)     # RIGHT|VCENTER|SINGLELINE
            gdi32.SelectObject(hdc, font)

    def _dot(self, hdc, cx, cy, r, color):
        brush = gdi32.CreateSolidBrush(color)
        pen = gdi32.CreatePen(5, 0, 0)
        ob = gdi32.SelectObject(hdc, brush)
        op = gdi32.SelectObject(hdc, pen)
        gdi32.Ellipse(hdc, cx - r, cy - r, cx + r, cy + r)
        gdi32.SelectObject(hdc, op)
        gdi32.SelectObject(hdc, ob)
        gdi32.DeleteObject(brush)
        gdi32.DeleteObject(pen)

    def _set_width(self, v):
        if self._popup == "color":
            return
        lo, hi = self._popup_range()
        v = max(lo, min(hi, v))
        if self._popup == "pen":
            self._pen_w = int(round(v))
        else:
            self._mark_w = int(round(v))
        self._invalidate()

    def _popup_click(self, mx, my):
        r = self._popup_rect()
        if not r:
            return
        bx, by, bx2, by2 = r
        if self._popup == "color":
            for i, c in enumerate(COLOR_GRID):
                px, py = self._popup_color_xy(i)
                if (mx - px) ** 2 + (my - py) ** 2 <= 22 * 22:
                    self._ink_color = c
                    if self._tool == "hand":
                        self._tool = "pen"
                    self._invalidate()
                    return
            return
        y = self._popup_y()
        lo, hi = self._popup_range()
        x0, x1 = self._popup_track()
        if abs(my - y) <= 20:
            if abs(mx - (bx + 30)) <= 18:
                self._set_width(self._popup_width() - 1)
                return
            if abs(mx - (bx + 270)) <= 18:
                self._set_width(self._popup_width() + 1)
                return
            if x0 - 12 <= mx <= x1 + 12:
                t = max(0.0, min(1.0, (mx - x0) / float(x1 - x0)))
                self._set_width(lo + t * (hi - lo))
                self._slider_drag = True
                return


    def _paint_ink(self, hdc):
        for st in self._ink:
            self._stroke(hdc, st["pts"], st["color"], st["w"])
        if self._drawing:
            st = self._drawing
            self._stroke(hdc, st["pts"], st["color"], st["w"])

    def _stroke(self, hdc, pts, color, width):
        if not pts:
            return
        if len(pts) == 1:
            x, y = int(pts[0][0]), int(pts[0][1])
            r = max(1, int(width / 2))
            brush = gdi32.CreateSolidBrush(color)
            ob = gdi32.SelectObject(hdc, brush)
            op = gdi32.SelectObject(hdc, gdi32.CreatePen(5, 0, 0))
            gdi32.Ellipse(hdc, x - r, y - r, x + r, y + r)
            gdi32.SelectObject(hdc, op)
            gdi32.SelectObject(hdc, ob)
            gdi32.DeleteObject(brush)
            return
        self._poly(hdc, pts, color, width)

    def _snapshot(self):
        self._ink_states = self._ink_states[:self._ink_si + 1]
        self._ink_states.append(list(self._ink))
        if len(self._ink_states) > 60:
            self._ink_states = self._ink_states[-60:]
        self._ink_si = len(self._ink_states) - 1

    def _undo(self):
        if self._ink_si > 0:
            self._ink_si -= 1
            self._ink = list(self._ink_states[self._ink_si])
            self._invalidate()

    def _redo(self):
        if self._ink_si < len(self._ink_states) - 1:
            self._ink_si += 1
            self._ink = list(self._ink_states[self._ink_si])
            self._invalidate()

    def _clear_ink(self):
        if not self._ink:
            return
        self._ink = []
        self._snapshot()
        self._invalidate()

    def _erase_at(self, mx, my):
        keep, hit = [], False
        for st in self._ink:
            for px, py in st["pts"]:
                if (px - mx) ** 2 + (py - my) ** 2 <= ERASE_R * ERASE_R:
                    hit = True
                    break
            else:
                keep.append(st)
        if hit:
            self._ink = keep
            self._snapshot()
            self._invalidate()

    def _begin_draw(self, mx, my):
        color = self._ink_color
        w = self._pen_w
        if self._tool == "marker":
            color = _lighten(color, 0.32)
            w = self._mark_w
        self._popup = None          # 开画就收起浮层，别挡录制画面
        self._drawing = {"color": color, "w": w, "pts": [(mx, my)]}
        self._paint_now()

    def _extend_draw(self, mx, my):
        st = self._drawing
        px, py = st["pts"][-1]
        if abs(mx - px) + abs(my - py) < 3:
            return
        st["pts"].append((mx, my))
        band = (max(0, min(px, mx) - st["w"] * 2), max(0, min(py, my) - st["w"] * 2),
                min(self.w, max(px, mx) + st["w"] * 2), min(self.h, max(py, my) + st["w"] * 2))
        self._paint_now(band)

    def _end_draw(self):
        st = self._drawing
        self._drawing = None
        if not st:
            return
        self._ink.append(st)
        self._snapshot()
        self._paint_now()

    # -- 工具栏交互 -----------------------------------------------------
    def _set_tool(self, name):
        if self._tool == name:
            self._popup = None if self._popup == name else name
            if self._popup is None:
                self._tool = "hand"
        else:
            self._tool = name
            self._popup = name if name in ("pen", "marker") else None
        self._invalidate()


    def _invalidate(self):
        try:
            user32.InvalidateRect(self.hwnd, None, False)
        except Exception:
            pass

    # -- 清晰图 ---------------------------------------------------------
    def _crispify(self):
        if self._closed:
            return
        self._dirty_at = None        # 已按当前视图重画，别再排第二次（否则动画中会再卡一顿）
        sx, sy, sw, sh = self._view_rect()
        try:
            view = Image.new("RGB", (self.w, self.h), (0, 0, 0))
            x0, y0 = int(round(sx)), int(round(sy))
            x1, y1 = int(round(sx + sw)), int(round(sy + sh))
            bx0, by0 = max(0, x0), max(0, y0)
            bx1, by1 = min(self.sw, x1), min(self.sh, y1)
            if bx1 > bx0 and by1 > by0:
                part = self.src.crop((bx0, by0, bx1, by1))
                tw = max(1, int(round((bx1 - bx0) * self.s)))
                th = max(1, int(round((by1 - by0) * self.s)))
                part = part.resize((tw, th), Image.BILINEAR)
                view.paste(part, (int(round((bx0 - sx) * self.s)),
                                  int(round((by0 - sy) * self.s))))
            mdc, hbmp, _bits = _dib_from_bytes(_to_bgra(view), self.w, self.h)
        except Exception:
            self._log(traceback.format_exc())
            return
        old = self._crisp
        self._crisp = (self.s, self.cx, self.cy, mdc, hbmp)
        self._crisp_img = view
        if old:
            try:
                gdi32.DeleteObject(old[4])
                gdi32.DeleteDC(old[3])
            except Exception:
                pass
        # 压暗由 _paint_spot 里实时 AlphaBlend 完成，跟视图走，无需重建暗场
        self._invalidate()

    # -- 交互 -----------------------------------------------------------
    def _zoom(self, up, mx, my):
        step = 1.15 if up else 1 / 1.15
        new = min(6.0, max(0.35, self.s * step))
        if abs(new - self.s) < 1e-4:
            return
        sx = self.cx + (mx - self.w / 2.0) / self.s
        sy = self.cy + (my - self.h / 2.0) / self.s
        self.s = new
        self.cx = sx - (mx - self.w / 2.0) / new
        self.cy = sy - (my - self.h / 2.0) / new
        self._clamp_center()
        self._crisp = None
        self._mark_dirty()
        self._invalidate()

    def _start_spot(self, mx, my):
        # 不在点击当帧重建清晰图（缩放后紧跟高亮时那是 20~40ms 的断帧）：
        # 第一帧直接用底图缩放顶上，清晰图交给定时器在动画播放中后台补
        if not self._crisp or self._crisp[0] != self.s \
                or self._crisp[1] != self.cx or self._crisp[2] != self.cy:
            self._mark_dirty()
        self._spot_t0 = time.perf_counter()
        self._spot_r0 = max(self.w, self.h) * 0.75
        self._spot_r1 = min(self.w, self.h) * 0.17
        self._held_frame = False
        self._spot = (mx, my, self._spot_r0)
        self._start_anim_timer()
        self._invalidate()

    def _tick_spot(self):
        if self._spot is None:
            return
        t = time.perf_counter() - self._spot_t0
        mx, my = self._spot[0], self._spot[1]
        if t <= self.spot_collect:
            p = t / self.spot_collect
            k = _EASES[self.spot_ease](p)
            r = self._spot_r0 + (self._spot_r1 - self._spot_r0) * k
        elif t <= self.spot_collect + self.spot_hold:
            r = self._spot_r1
        elif t <= self.spot_collect + self.spot_hold + self.spot_fade:
            p = (t - self.spot_collect - self.spot_hold) / self.spot_fade
            r = self._spot_r1 * (1 - _smoothstep(p))
        else:
            self._spot = None
            self._stop_anim_timer()
            return
        self._spot = (mx, my, r)

    # -- 动画节拍：SetTimer 的实际节拍被系统量化到 ~15.6ms，改用多媒体定时器给到 8ms --
    def _on_mm_tick(self, *_a):
        hwnd = self.hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_APP_TICK, 0, 0)
            except Exception:
                pass

    def _start_anim_timer(self):
        if self._mtimer:
            return
        try:
            # 按显示器刷新率给节拍：本机 100Hz → 10ms 一帧；后面 DwmFlush 再把相位对齐
            self._mtimer = winmm.timeSetEvent(FRAME_MS, 1, self._tick_cb, 0, TIME_PERIODIC)
        except Exception:
            self._mtimer = 0
            self._log(traceback.format_exc())

    def _paint_now(self, band=None):
        """动画帧直接画到屏幕 DC：绕开 WM_PAINT 的排队/合并，帧点才准。"""
        try:
            hdc = self._screen_dc or user32.GetDC(self.hwnd)
            try:
                self._paint_back()
                if band:
                    x0, y0, x1, y1 = band
                    gdi32.BitBlt(hdc, x0, y0, x1 - x0, y1 - y0, self._back_dc,
                                 x0, y0, SRCCOPY)
                else:
                    gdi32.BitBlt(hdc, 0, 0, self.w, self.h, self._back_dc, 0, 0, SRCCOPY)
            finally:
                if not self._screen_dc:
                    user32.ReleaseDC(self.hwnd, hdc)
        except Exception:
            self._log(traceback.format_exc())

    def _stop_anim_timer(self):
        if self._mtimer:
            try:
                winmm.timeKillEvent(self._mtimer)
            except Exception:
                pass
            self._mtimer = 0

    def _mods_down(self):
        mods = self._exit_combo[0]
        for m, vk in ((0x0002, 0x11), (0x0001, 0x12), (0x0004, 0x10), (0x0008, 0x5B)):
            if (mods & m) and not (user32.GetKeyState(vk) & 0x8000):
                return False
        return True

    def _pt(self, lp):
        pt = wt.POINT(wt.LONG(lp & 0xFFFF), wt.LONG((lp >> 16) & 0xFFFF))
        user32.ScreenToClient(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    # -- 消息 -----------------------------------------------------------
    def _wndproc_routed(self, hwnd, msg, wp, lp):
        try:
            return self._wndproc(hwnd, msg, wp, lp)
        except Exception:
            self._log(traceback.format_exc())
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_PAINT:
            ps = _PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            self._paint(hdc)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_APP_TICK:
            if self._spot is not None:
                r_old = self._spot[2]
                self._tick_spot()
                if self._spot is not None:
                    r_new = self._spot[2]
                    if abs(r_new - r_old) < 0.6 and self._held_frame:
                        return 0          # 这一帧和上一帧一样，不刷
                    self._held_frame = True
                    band = None if abs(r_new - r_old) > 90 else \
                        self._band_of(r_old, r_new, self._spot[0], self._spot[1])
                    self._paint_now(band)
            return 0
        if msg == WM_TIMER:
            if self._hint_until and time.perf_counter() > self._hint_until:
                self._hint_until = 0.0
                self._invalidate()
            # 多媒体定时器只负责动画；它没起来时退回 WM_TIMER 驱动
            if self._spot is not None and not self._mtimer:
                self._tick_spot()
                self._invalidate()
            # 高亮动画播放期间不重建清晰图（那是一次 ~50ms 的断帧）；动画结束再补
            if self._spot is None and self._dirty_at is not None \
                    and (time.perf_counter() - self._dirty_at) > self.CRISP_DELAY:
                self._dirty_at = None
                self._crispify()
            return 0
        if msg == WM_KEYDOWN:
            vk = wp & 0xFFFF
            if vk == VK_ESCAPE:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return 0
            if self._exit_combo and vk == self._exit_combo[1] and self._mods_down():
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        if msg == WM_LBUTTONDOWN:
            mx, my = self._pt(lp)
            if self._popup:
                pr = self._popup_rect()
                if pr and pr[0] <= mx <= pr[2] and pr[1] <= my <= pr[3]:
                    self._popup_click(mx, my)
                    if self._slider_drag:
                        user32.SetCapture(hwnd)
                    return 0
                self._popup = None
                self._invalidate()
            hit = self._hit_toolbar(mx, my)
            if hit:
                name, arg = hit
                if name == "undo":
                    self._undo()
                elif name == "redo":
                    self._redo()
                elif name == "clear":
                    self._clear_ink()
                elif name in ("pen", "marker", "eraser"):
                    self._set_tool(name)
                elif name in ("cur", "colors"):
                    self._popup = "color"
                    if self._tool == "hand":
                        self._tool = "pen"
                    self._invalidate()
                elif name == "done":
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return 0
            if user32.GetKeyState(VK_MENU) & 0x8000:
                # Alt+左键永远是“标重点”，不管当前选的是哪支笔（之前被画笔分支吃掉）
                self._popup = None
                self._start_spot(mx, my)
            elif self._tool in ("pen", "marker"):
                self._begin_draw(mx, my)
                user32.SetCapture(hwnd)
            elif self._tool == "eraser":
                self._erasing = True
                self._erase_at(mx, my)
                user32.SetCapture(hwnd)
            else:
                self._drag = (mx, my)
                user32.SetCapture(hwnd)
            return 0
        if msg == WM_LBUTTONUP:
            self._slider_drag = False
            if self._drawing:
                self._end_draw()
            self._erasing = False
            if self._drag is not None:
                self._drag = None
                self._mark_dirty()
            user32.ReleaseCapture()
            return 0
        if msg == WM_RBUTTONDOWN:
            self._rdrag = self._pt(lp)
            user32.SetCapture(hwnd)
            return 0
        if msg == WM_RBUTTONUP:
            if self._rdrag is not None:
                self._rdrag = None
                self._mark_dirty()
            user32.ReleaseCapture()
            return 0
        if msg == WM_MOUSEMOVE:
            mx, my = self._pt(lp)
            if self._slider_drag:
                x0, x1 = self._popup_track()
                lo, hi = self._popup_range()
                t = max(0.0, min(1.0, (mx - x0) / float(x1 - x0)))
                self._set_width(lo + t * (hi - lo))
            elif self._drawing is not None:
                self._extend_draw(mx, my)
            elif self._erasing:
                self._erase_at(mx, my)
            elif self._drag is not None:
                dx, dy = mx - self._drag[0], my - self._drag[1]
                self._drag = (mx, my)
                self.cx -= dx / self.s
                self.cy -= dy / self.s
                self._clamp_center()
                self._crisp = None
                self._mark_dirty()
                self._invalidate()
            elif self._rdrag is not None:
                dx, dy = mx - self._rdrag[0], my - self._rdrag[1]
                self._rdrag = (mx, my)
                self.cx -= dx / self.s
                self.cy -= dy / self.s
                self._clamp_center()
                self._crisp = None
                self._mark_dirty()
                self._invalidate()
            return 0
        if msg == WM_MOUSEWHEEL:
            delta = ctypes.c_short((wp >> 16) & 0xFFFF).value
            mx, my = self._pt(lp)
            self._zoom(delta > 0, mx, my)
            return 0
        if msg == WM_SETCURSOR:
            if self._tool in ("pen", "marker", "eraser"):
                user32.SetCursor(user32.LoadCursorW(None, 32515))   # IDC_CROSS
            else:
                user32.SetCursor(user32.LoadCursorW(None, 32512))
            return 1
        return user32.DefWindowProcW(hwnd, msg, wp, lp)
