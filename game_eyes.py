# -*- coding: utf-8 -*-
"""game_eyes —— 给任何 AI 用的「眼睛 + 手」，HTTP 接口。

为什么存在：让 AI 玩任何游戏，不绑引擎、不绑版本、不改内存、不进游戏进程。
只要游戏能显示在屏幕上，这套就能用。

启动：  python game_eyes.py --serve          （默认 127.0.0.1:8790）

接口（GET，返回 JSON）：
    /state                            屏幕分辨率
    /shot?w=1280&q=70&box=x,y,w,h     截图 JPEG，返回路径/尺寸/字节数/耗时
    /click?x=&y=&hold=250&button=      点一下（hold 毫秒，游戏必须 ≥200）
    /find?text=选择科技&box=x,y,w,h    按文字找位置（OCR），返回中心点坐标
    /find?text=...&all=1               返回所有命中
    /click_text?text=开始游戏          找到就点它

POST /click  /drag  /seq  ，body 是 JSON。

三条经验：
    1. 点击必须 hold ≥ 200ms，游戏会丢弃太快的合成输入
    2. 点完把鼠标移开，否则 tooltip 挡住画面
    3. 每次 shot 都新建 mss 实例、服务用单线程 HTTPServer（跨线程会挂死）

性能（实测 2560x1440）：shot 155ms / 62KB；「截图+点击+等1秒+截图」整条 1.54 秒。
"""
from __future__ import annotations

import json
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import ctypes
import ctypes.wintypes as wt
import mss
from PIL import Image

OUT = Path(__file__).parent / 'game_eyes_out'
OUT.mkdir(exist_ok=True)

u32 = ctypes.WinDLL('user32', use_last_error=True)
u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u32.mouse_event.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p]
u32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.SendMessageW.restype = ctypes.c_ssize_t
u32.WindowFromPoint.argtypes = [ctypes.c_void_p]
u32.WindowFromPoint.restype = wt.HWND

class _POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]
u32.GetForegroundWindow.restype = wt.HWND

_LD, _LU = 0x0002, 0x0004
_RD, _RU = 0x0008, 0x0010


class Eyes:
    _ocr = None
    _seq = 0

    def __init__(self, hold_ms=250):
        self.hold_ms = hold_ms

    # ---------- 看 ----------
    def shot(self, path=None, box=None, width=1280, quality=70, fmt='jpg'):
        t0 = time.time()
        with mss.mss() as sct:
            mon = {'left': 0, 'top': 0, 'width': sct.monitors[1]['width'], 'height': sct.monitors[1]['height']}
            if box:
                mon = {'left': int(box[0]), 'top': int(box[1]),
                       'width': int(box[2]), 'height': int(box[3])}
            raw = sct.grab(mon)
            img = Image.frombytes('RGB', raw.size, raw.rgb)
        if width and img.width > width:
            h = max(1, int(img.height * width / img.width))
            img = img.resize((width, h), Image.LANCZOS)
        Eyes._seq += 1
        if not path:
            path = OUT / ('shot_%d.%s' % (Eyes._seq, fmt))
        elif not Path(path).is_absolute():
            path = OUT / path
        path = str(path)
        if fmt == 'png':
            img.save(path, 'PNG')
        else:
            img.save(path, 'JPEG', quality=int(quality))
        return {'path': path, 'size': [img.width, img.height],
                'bytes': Path(path).stat().st_size, 'ms': int((time.time() - t0) * 1000)}

    # ---------- 手 ----------
    def click(self, x, y, hold=None, button='left'):
        hold = self.hold_ms if hold is None else int(hold)
        u32.SetCursorPos(int(x), int(y))
        time.sleep(0.02)
        d, u = (_RD, _RU) if button == 'right' else (_LD, _LU)
        u32.mouse_event(d, 0, 0, 0, None)
        time.sleep(hold / 1000.0)
        u32.mouse_event(u, 0, 0, 0, None)
        return {'ok': True, 'x': int(x), 'y': int(y), 'hold': hold, 'button': button}

    def drag(self, x1, y1, x2, y2, hold=200):
        u32.SetCursorPos(int(x1), int(y1))
        time.sleep(0.03)
        u32.mouse_event(_LD, 0, 0, 0, None)
        time.sleep(hold / 1000.0)
        u32.SetCursorPos(int(x2), int(y2))
        time.sleep(0.05)
        u32.mouse_event(_LU, 0, 0, 0, None)
        return {'ok': True, 'from': [x1, y1], 'to': [x2, y2]}

    # ---------- 认字 ----------
    def _get_ocr(self):
        if Eyes._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            Eyes._ocr = RapidOCR()
        return Eyes._ocr

    def find_all(self, text, box=None, min_score=0.4, zoom=1.0):
        """zoom>1 时先把图放大再认，小字靠这个。"""
        import numpy as np
        with mss.mss() as sct:
            mon = {'left': 0, 'top': 0, 'width': sct.monitors[1]['width'], 'height': sct.monitors[1]['height']}
            if box:
                mon = {'left': int(box[0]), 'top': int(box[1]),
                       'width': int(box[2]), 'height': int(box[3])}
            raw = sct.grab(mon)
            img = np.array(raw)[:, :, :3][:, :, ::-1].copy()
        scale = 1.0
        if zoom and float(zoom) > 1.0:
            from PIL import Image as _Im
            scale = float(zoom)
            im = _Im.fromarray(img)
            im = im.resize((int(im.width * scale), int(im.height * scale)), _Im.LANCZOS)
            img = np.array(im)[:, :, ::-1].copy()
        res, _ = self._get_ocr()(img)
        out = []
        for item in (res or []):
            pts, txt, score = item[0], item[1], float(item[2])
            if score < min_score or text not in txt:
                continue
            xs = [p[0] / scale for p in pts]
            ys = [p[1] / scale for p in pts]
            x0, y0 = min(xs), min(ys)
            w, h = max(xs) - x0, max(ys) - y0
            out.append({
                'text': txt, 'score': round(score, 3),
                'x': int(mon['left'] + x0 + w / 2), 'y': int(mon['top'] + y0 + h / 2),
                'w': int(w), 'h': int(h),
                'left': int(mon['left'] + x0), 'top': int(mon['top'] + y0),
            })
        out.sort(key=lambda d: -d['score'])
        return out

    def find(self, text, box=None, min_score=0.4, zoom=1.0):
        r = self.find_all(text, box, min_score, zoom)
        return r[0] if r else None


    # ---------- 自动降级点击：点一下，看画面变没变，没变就换后端 ----------
    def _thumb(self):
        """全屏缩成小灰度图，用于前后差分。"""
        import numpy as np
        with mss.mss() as s:
            raw = s.grab(s.monitors[1])
            im = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')
        return np.asarray(im.convert('L').resize((320, 180)), dtype='int16')

    @staticmethod
    def _pdiff(a, b):
        import numpy as np
        return float(np.abs(a - b).mean())

    MODES = ('sendinput', 'scan', 'wmchar', 'wmkey', 'paste')

    # ---------- 多后端点击：同一套调用，换游戏时逐个试 ----------
    def _click_mode(self, x, y, hold, button, mode):
        x, y = int(x), int(y)
        if mode == 'sendinput':
            u32.SetCursorPos(x, y)
            time.sleep(0.02)
            d, u = (_RD, _RU) if button == 'right' else (_LD, _LU)
            u32.mouse_event(d, 0, 0, 0, None)
            time.sleep(hold / 1000.0)
            u32.mouse_event(u, 0, 0, 0, None)
            return {'mode': mode, 'ok': True}
        # postmessage 系：不移动真实鼠标，直接给窗口发消息
        hwnd = u32.WindowFromPoint(_POINT(x, y))
        if not hwnd:
            raise RuntimeError('WindowFromPoint 没找到窗口')
        if button == 'right':
            down, up = 0x0204, 0x0205
        else:
            down, up = 0x0201, 0x0202
        lp = (y << 16) | (x & 0xFFFF)
        if mode == 'postmessage':
            u32.PostMessageW(hwnd, down, 1, lp)
            time.sleep(hold / 1000.0)
            u32.PostMessageW(hwnd, up, 0, lp)
        elif mode == 'sendmessage':
            u32.SendMessageW(hwnd, down, 1, lp)
            time.sleep(hold / 1000.0)
            u32.SendMessageW(hwnd, up, 0, lp)
        else:
            raise RuntimeError('未知后端 %s' % mode)
        return {'mode': mode, 'ok': True, 'hwnd': hwnd}

    def click_auto(self, x, y, hold=None, min_diff=0.4, settle=0.9, modes=None):
        """逐个后端试点，画面变化超过阈值就停，返回用的哪个。

        这就是跨游戏通用的关键：多数游戏第一个后端就够，
        少数（群星）要一路降级到底才知道它吃哪一种。
        """
        import numpy as np
        hold = hold or self.hold_ms
        tried = []
        for mode in (modes or ('sendinput', 'postmessage', 'sendmessage')):
            before = self._thumb()
            try:
                self._click_mode(x, y, hold, 'left', mode)
            except Exception as e:
                tried.append({'mode': mode, 'error': str(e)[:70]})
                continue
            time.sleep(settle)
            after = self._thumb()
            d = float(np.abs(before - after).mean())
            tried.append({'mode': mode, 'diff': round(d, 3)})
            if d >= min_diff:
                return {'ok': True, 'mode': mode, 'diff': round(d, 3),
                        'x': x, 'y': y, 'tried': tried}
        return {'ok': False, 'reason': '没有后端让画面变化', 'x': x, 'y': y, 'tried': tried}

    def click_text(self, text, box=None, hold=None, button='left', min_score=0.4, offset_y=0):
        """找到文字就点它。offset_y 相对文字块中心的纵向偏移：
        游戏里按钮常把文字放在上部，点文字本身不算点按钮，要往下偏 10~20 像素。"""
        hit = self.find(text, box, min_score)
        if not hit:
            return {'ok': False, 'error': 'not found: %s' % text}
        r = self.click(hit['x'], hit['y'] + int(offset_y), hold, button)
        r['found'] = hit
        return r

    # ---------- 打字 ----------
    def type_auto(self, text, box=None, settle=0.7):
        """逐个打字方式试，用「输入框里认出来的字变没变」判断，比像素差分准。

        像素差分会被光标闪烁干扰；文字变多才是真的进去了。
        """
        tried = []
        for method in ('paste', 'keys', 'wbkey'):
            before = self._ocr_in(box)
            try:
                self.type_text(text, method=method)
            except Exception as e:
                tried.append({'method': method, 'error': str(e)[:70]})
                continue
            time.sleep(settle)
            after = self._ocr_in(box)
            tried.append({'method': method, 'before': before[:30], 'after': after[:30]})
            if text[:2] and text[:2] in after:
                return {'ok': True, 'method': method, 'text': text,
                        'box_text': after[:60], 'tried': tried}
            if len(after) > len(before) + 1:
                return {'ok': True, 'method': method, 'text': text,
                        'box_text': after[:60], 'note': '长度变长', 'tried': tried}
        return {'ok': False, 'reason': '三种方式都没让输入框的字变多', 'text': text, 'tried': tried}

    def _ocr_in(self, box):
        """把一块区域认成一行文字，用于判断输入框内容有没有变。"""
        if not box:
            return ''
        res = self.find_all('', list(box))
        if isinstance(res, dict):
            return str(res.get('text', ''))
        return ' '.join(str(x.get('text', '')) for x in (res or []))

    def _thumb_box(self, box=None):
        """只截某块区域缩成灰度小图，比全屏更敏感（输入框变化很小）。"""
        import numpy as np
        with mss.mss() as sct:
            mon = {'left': int(box[0]), 'top': int(box[1]),
                   'width': int(box[2]), 'height': int(box[3])} if box else sct.monitors[1]
            raw = sct.grab(mon)
        im = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')
        return np.asarray(im.convert('L').resize((240, 120)), dtype='int16')

    def type_text(self, text, method='paste'):
        """往当前焦点里塞文字。
        method='paste'：先写剪贴板再 Ctrl+V —— 对游戏输入框最有效（逐字符的按键事件常被丢）。
        method='keys' ：逐字符发 WM_CHAR，游戏多半不收，留着备查。
        """
        if method == 'wbkey':
            # 逐字符 keybd_event（只对 ASCII 有效），走系统输入流
            for ch in text:
                vk = u32.VkKeyScanW(ord(ch)) & 0xFF
                if vk == 0xFF:
                    continue
                u32.keybd_event(vk, 0, 0, 0)
                time.sleep(0.01)
                u32.keybd_event(vk, 0, 2, 0)
                time.sleep(0.01)
            return {'ok': True, 'method': 'wbkey', 'chars': len(text)}
        if method == 'keys':
            for ch in text:
                u32.PostMessageW(u32.GetForegroundWindow(), 0x0102, ord(ch), 0)
                time.sleep(0.01)
            return {'ok': True, 'method': 'keys', 'chars': len(text)}
        # 剪贴板 -> Ctrl+V
        import subprocess
        old = None
        try:
            old = subprocess.run(['powershell', '-NoProfile', '-Command', 'Get-Clipboard -Raw'],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            pass
        r = subprocess.run(['powershell', '-NoProfile', '-Command',
                            'Set-Clipboard -Value $args[0]', text],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            # 退回纯 ASCII 安全写法
            subprocess.run(['powershell', '-NoProfile', '-Command',
                            'Set-Clipboard -Value "%s"' % text.replace('"', '""')],
                           capture_output=True, text=True, timeout=10)
        time.sleep(0.15)
        u32.keybd_event(0x11, 0, 0, 0)          # Ctrl down
        u32.keybd_event(0x56, 0, 0, 0)          # V down
        time.sleep(0.05)
        u32.keybd_event(0x56, 0, 2, 0)
        u32.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.1)
        return {'ok': True, 'method': 'paste', 'chars': len(text), 'prev': (old or '')[:40]}
    # ---------- 读局面 ----------
    def read_status(self):
        """读群星顶部资源栏 + 日期，返回结构化状态。换游戏要重写这一块。"""
        import re
        import numpy as np
        # 顶部资源栏 + 右上角日期
        hits_top = self.find_all('', box=[0, 0, 1900, 70], min_score=0.3)
        hits_date = self.find_all('', box=[2100, 0, 460, 60], min_score=0.3)
        raw = ' '.join(h['text'] for h in hits_top)
        date_raw = ' '.join(h['text'] for h in hits_date)
        m = re.search(r'(\d{4})\.(\d{2})\.(\d{2})', date_raw) or re.search(r'(\d{4})\.(\d{2})\.(\d{2})', raw)
        date = '%s.%s.%s' % m.groups() if m else None
        def grab(label_pat):
            # 资源数字形如 1.9K+78 / 605+18
            for h in hits_top:
                t = h['text']
                mm = re.match(r'^([\d.]+[KM]?)\+?(-?[\d.]*)$', t.strip())
                if mm:
                    return t.strip()
            return None
        nums = [h['text'] for h in hits_top if re.match(r'^[\d.]+[KM]?(\+\d+)?$', h['text'].strip())]
        return {'date': date, 'numbers': nums[:12], 'raw': raw[:300]}
    def state(self):
        with mss.mss() as sct:
            m = sct.monitors[1]
        return {'screen': [m['width'], m['height']], 'out': str(OUT)}


_E = Eyes()


def eyes():
    return _E


class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        try:
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            q = {k: v[0] for k, v in qs.items()}
            e = eyes()
            if u.path == '/state':
                self._send(e.state())
            elif u.path == '/shot':
                box = [int(v) for v in q['box'].split(',')] if 'box' in q else None
                self._send(e.shot(path=q.get('path'),
                                  width=int(q.get('w', 1280)),
                                  quality=int(q.get('q', 70)),
                                  box=box))
            elif u.path == '/click':
                self._send(e.click(int(q.get('x', 0)), int(q.get('y', 0)),
                                   int(q['hold']) if 'hold' in q else None,
                                   q.get('button', 'left')))
            elif u.path == '/type_auto':
                bx = [int(v) for v in q['box'].split(',')] if 'box' in q else None
                self._send(e.type_auto(q.get('text', ''), box=bx))
            elif u.path == '/type':
                self._send(e.type_text(q.get('text', ''), q.get('method', 'paste')))
            elif u.path == '/status':
                self._send(e.read_status())
            elif u.path == '/find':
                box = [int(v) for v in q['box'].split(',')] if 'box' in q else None
                zm = float(q.get('zoom', 1))
                if q.get('all', '0') == '1':
                    self._send(e.find_all(q.get('text', ''), box, zoom=zm))
                else:
                    self._send(e.find(q.get('text', ''), box, zoom=zm))
            elif u.path == '/click_auto':
                self._send(e.click_auto(int(q.get('x', 0)), int(q.get('y', 0)),
                                        int(q['hold']) if 'hold' in q else None,
                                        float(q.get('min_diff', 0.4))))
            elif u.path == '/click_text':
                self._send(e.click_text(q.get('text', ''), offset_y=int(q.get('offset_y', 0))))
            else:
                self._send({'error': 'unknown', 'path': u.path}, 404)
        except Exception as ex:
            self._send({'ok': False, 'error': repr(ex), 'tb': traceback.format_exc()[-800:]}, 500)

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
            u = urlparse(self.path)
            e = eyes()
            if u.path == '/click':
                self._send(e.click(int(body.get('x', 0)), int(body.get('y', 0)),
                                   body.get('hold'), body.get('button', 'left')))
            elif u.path == '/drag':
                self._send(e.drag(int(body.get('x1', 0)), int(body.get('y1', 0)),
                                  int(body.get('x2', 0)), int(body.get('y2', 0))))
            elif u.path == '/seq':
                res = []
                for st in body.get('steps', []):
                    op = st.get('op')
                    if op == 'shot':
                        res.append(e.shot(path=st.get('path'), width=int(st.get('w', 1280)),
                                          quality=int(st.get('q', 70)), box=st.get('box')))
                    elif op == 'click':
                        res.append(e.click(int(st.get('x', 0)), int(st.get('y', 0)),
                                           st.get('hold'), st.get('button', 'left')))
                    elif op == 'find':
                        res.append(e.find(st.get('text', ''), st.get('box'), zoom=float(st.get('zoom', 1))))
                    elif op == 'click_text':
                        res.append(e.click_text(st.get('text', '')))
                    elif op == 'drag':
                        res.append(e.drag(int(st.get('x1', 0)), int(st.get('y1', 0)),
                                          int(st.get('x2', 0)), int(st.get('y2', 0))))
                    elif op == 'sleep':
                        time.sleep(float(st.get('sec', 0.5)))
                        res.append({'ok': True, 'slept': st.get('sec', 0.5)})
                    else:
                        res.append({'ok': False, 'error': 'unknown op %r' % op})
                self._send({'ok': True, 'results': res})
            else:
                self._send({'error': 'unknown', 'path': u.path}, 404)
        except Exception as ex:
            self._send({'ok': False, 'error': repr(ex), 'tb': traceback.format_exc()[-800:]}, 500)

    def log_message(self, *a):
        pass


def serve(port=8790):
    srv = HTTPServer(('127.0.0.1', port), H)
    print('game_eyes on http://127.0.0.1:%d' % port, flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    import sys
    if '--serve' in sys.argv:
        serve()
    else:
        e = Eyes()
        print(json.dumps(e.state()))
        print(json.dumps(e.shot()))
