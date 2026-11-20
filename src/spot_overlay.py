"""录制中的“聚光灯”：不冻结画面、不抢焦点、鼠标穿透的置顶叠加层。

讲解师画布会冻结整屏，所以录制中还没进画布时按 Alt+左键没有高亮。
这里用一个全屏 WS_EX_LAYERED 窗口做同样的收拢/停留/消失动画：
  · SetLayeredWindowAttributes 给整窗 28% 亮度（alpha 183）+ 洋红色键
  · 圈内填洋红 → 完全透明，直接透出桌面
  · 圈边画 3px 白圈，圈外就是压暗的黑
  · 窗口带 WS_EX_TRANSPARENT / WS_EX_NOACTIVATE，点击穿透、不抢焦点
  · 动画由自己的消息循环派发 WM_PAINT，画完即销毁，不影响录制

为什么不用逐像素 alpha（UpdateLayeredWindow）：每帧要拼 1920×1080 的
ARGB 缓冲（Pillow 合成 ~20ms/帧），实测只能到 31fps；改成色键+常量 alpha 后
每帧只是几次 GDI 填充，实测 62fps。
"""
import ctypes
import threading
import time
import traceback
from ctypes import wintypes as wt

import gdi_canvas as G

user32 = G.user32
gdi32 = G.gdi32
kernel32 = G.kernel32

WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SW_SHOWNOACTIVATE = 4
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002
KEY_COLOR = 0x00FF00FF          # 洋红：被系统当作透明色键
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
PM_REMOVE = 0x0001

user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, ctypes.c_ubyte, wt.DWORD]
user32.SetLayeredWindowAttributes.restype = wt.BOOL
user32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(G._PAINTSTRUCT)]
user32.BeginPaint.restype = wt.HDC
user32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(G._PAINTSTRUCT)]
user32.InvalidateRect.argtypes = [wt.HWND, wt.LPVOID, wt.BOOL]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
gdi32.CreateSolidBrush.restype = wt.HANDLE
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wt.COLORREF]
gdi32.CreatePen.restype = wt.HANDLE
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wt.HANDLE
gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
gdi32.SelectObject.restype = wt.HANDLE
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.Ellipse.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]


class SpotOverlay:
    """一次高亮＝一个叠加层实例，动画跑完自动销毁。"""

    def __init__(self, log=None, spot_times=None, spot_ease="ease_out", dim_alpha=183):
        self._log = log or (lambda m: None)
        col, hold, fade = spot_times or (G.SPOT_COLLECT, G.SPOT_HOLD, G.SPOT_FADE)
        self.collect = max(0.1, float(col))
        self.hold = max(0.1, float(hold))
        self.fade = max(0.05, float(fade))
        self.ease = spot_ease if spot_ease in G._EASES else "ease_out"
        self.dim = int(dim_alpha)
        self._stop_evt = threading.Event()
        self._done = threading.Event()
        self._hwnd = None
        self.frames = 0
        self._r = None                       # 当前半径，由 WM_PAINT 消费
        pt = wt.POINT()
        try:
            user32.GetCursorPos(ctypes.byref(pt))
        except Exception:
            pt.x = pt.y = 0
        self.cx, self.cy = pt.x, pt.y
        self._thread = threading.Thread(target=self._run, name="spot-overlay", daemon=True)
        self._thread.start()

    def is_closed(self):
        return self._done.is_set()

    def close(self):
        self._stop_evt.set()
        # 立刻藏窗口：进讲解师画布要抓当前屏幕，晚一帧就会把压暗层冻结进画面
        try:
            if self._hwnd:
                user32.ShowWindow(self._hwnd, 0)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _radius(self, t):
        if t <= self.collect:
            p = t / self.collect
            k = G._EASES[self.ease](p)
            return self.r0 + (self.r1 - self.r0) * k
        if t <= self.collect + self.hold:
            return self.r1
        if t <= self.collect + self.hold + self.fade:
            p = (t - self.collect - self.hold) / self.fade
            return self.r1 * (1 - G._smoothstep(p))
        return None

    def _run(self):
        hwnd = None
        black = key = white = bg = None
        cls = "SXSpot%d" % id(self)
        try:
            x0 = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            y0 = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
            if w <= 0 or h <= 0:
                w, h, x0, y0 = 1920, 1080, 0, 0
            self.lw, self.lh = w, h
            self.r0 = max(w, h) * 0.75
            self.r1 = min(w, h) * 0.17
            self.mx, self.my = self.cx - x0, self.cy - y0
            full = wt.RECT(0, 0, w, h)
            black = gdi32.CreateSolidBrush(0x00000000)
            key = gdi32.CreateSolidBrush(KEY_COLOR)
            white = gdi32.CreatePen(0, 3, 0x00FFFFFF)
            bg = gdi32.CreateSolidBrush(0x00000000)
            null_pen = gdi32.GetStockObject(8)      # NULL_PEN
            null_brush = gdi32.GetStockObject(5)    # NULL_BRUSH
            state = {"r": None}

            def paint(hdc):
                r = state["r"]
                if not hdc or r is None:
                    return
                bbox = (int(self.mx - r), int(self.my - r),
                        int(self.mx + r), int(self.my + r))
                user32.FillRect(hdc, ctypes.byref(full), black)
                ob = gdi32.SelectObject(hdc, key)
                op = gdi32.SelectObject(hdc, null_pen)
                gdi32.Ellipse(hdc, bbox[0], bbox[1], bbox[2], bbox[3])
                gdi32.SelectObject(hdc, op)
                gdi32.SelectObject(hdc, ob)
                ob = gdi32.SelectObject(hdc, white)
                op = gdi32.SelectObject(hdc, null_brush)
                gdi32.Ellipse(hdc, bbox[0], bbox[1], bbox[2], bbox[3])
                gdi32.SelectObject(hdc, op)
                gdi32.SelectObject(hdc, ob)

            def wnd(hwd_, msg, wp, lp):
                if msg == WM_PAINT:
                    ps = G._PAINTSTRUCT()
                    hdc = user32.BeginPaint(hwd_, ctypes.byref(ps))
                    try:
                        paint(hdc)
                    finally:
                        user32.EndPaint(hwd_, ctypes.byref(ps))
                    return 0
                if msg == WM_ERASEBKGND:
                    return 1
                return user32.DefWindowProcW(hwd_, msg, wp, lp)

            proc = G.WNDPROC(wnd)
            wc = G._WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = proc
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.lpszClassName = cls
            wc.hbrBackground = bg
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
                cls, "ShareX Flash 聚光灯", WS_POPUP, x0, y0, w, h, None, None, wc.hInstance, None)
            if not hwnd:
                self._log("聚光灯：窗口创建失败")
                return
            user32.SetLayeredWindowAttributes(hwnd, KEY_COLOR, self.dim,
                                              LWA_COLORKEY | LWA_ALPHA)
            self._hwnd = hwnd
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            ok = user32.SetWindowPos(hwnd, G.HWND_TOPMOST, x0, y0, w, h,
                                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            if not user32.GetWindowLongW(hwnd, -20) & WS_EX_TOPMOST:
                self._log("聚光灯：置顶失败（z=%d）" % ok)

            msg = wt.MSG()

            def pump():
                while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

            t_start = time.perf_counter()
            next_t = t_start
            frames = 0
            while not self._stop_evt.is_set():
                t = time.perf_counter() - t_start
                r = self._radius(t)
                if r is None:
                    break
                state["r"] = r
                self._r = r
                user32.InvalidateRect(hwnd, None, False)
                pump()
                frames += 1
                next_t += G.FRAME_MS / 1000.0
                dt = next_t - time.perf_counter()
                if dt > 0:
                    time.sleep(dt)
            self.frames = frames
            self._log("聚光灯：%d 帧 / %.2fs" % (frames, time.perf_counter() - t_start))
        except Exception:
            self._log(traceback.format_exc())
        finally:
            try:
                if hwnd:
                    user32.ShowWindow(hwnd, 0)
                    user32.DestroyWindow(hwnd)
            except Exception:
                pass
            for h in (black, key, white, bg):
                try:
                    if h:
                        gdi32.DeleteObject(h)
                except Exception:
                    pass
            try:
                user32.UnregisterClassW(cls, kernel32.GetModuleHandleW(None))
            except Exception:
                pass
            self._done.set()
