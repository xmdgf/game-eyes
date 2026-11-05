"""探测哪个输入后端对这个窗口有效：发同样一次动作，看画面有没有变。"""
import ctypes, ctypes.wintypes as wt, time, json, sys
from PIL import ImageGrab, ImageChops
import numpy as np

k = ctypes.WinDLL('kernel32', use_last_error=True)
u = ctypes.WinDLL('user32', use_last_error=True)

def hwnd_of(title_kw):
    out = []
    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, l):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n+1)
                u.GetWindowTextW(h, b, n+1)
                if title_kw.lower() in b.value.lower():
                    out.append((h, b.value))
        return True
    u.EnumWindows(cb, 0)
    return out[0] if out else (None, None)

def grab(box=None):
    im = ImageGrab.grab(bbox=box, all_screens=True).convert('L').resize((320,180))
    return np.asarray(im, dtype=np.int16)

def diff(a, b):
    return float(np.abs(a-b).mean())

def send_input(x, y, hold=250):
    u.SetCursorPos(x, y)
    time.sleep(0.05)
    u.mouse_event(0x0002,0,0,0,0); time.sleep(hold/1000.0); u.mouse_event(0x0004,0,0,0,0)

def main():
    hwnd, title = hwnd_of('Stellaris')
    print('hwnd=', hwnd, title)
    # 先截一张基线
    a = grab()
    print('baseline diff self =', diff(a,a))
    # 用一个安全的位置：点空白（不会触发重要操作），看画面有无变化
    send_input(1400, 1200, 200)
    time.sleep(1.0)
    b = grab()
    print('after click diff =', round(diff(a,b),3))

if __name__ == '__main__':
    main()