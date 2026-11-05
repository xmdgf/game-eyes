# -*- coding: utf-8 -*-
"""群星操作代理（game_bot）——一个独立的小程序，负责"手"和"眼"。

分工：
    - 我（AI）只往 queue/in/*.json 写指令，读 queue/out/*.json 看结果；
    - 这个程序自己循环：截屏 → 执行动作 → 写结果。
      它比我快得多（一次点击 0.2 秒，不用等我"看"）。

指令格式（in 目录里一个 json 文件一条）：
    {"op": "click",   "x": 1281, "y": 757, "hold": 180}
    {"op": "key",     "keys": ["esc"]}
    {"op": "shot",    "path": "shots/a.png", "box": [x,y,w,h]}
    {"op": "find",    "text": "新游戏"}
    {"op": "loop",    "times": 3, "step": {"op":"click","x":75,"y":433,"hold":180}}

用法：
    python game_bot.py --pid <pid>            # 常驻，轮询 in 目录
    python game_bot.py --once                 # 处理完当前指令就退
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import time
from pathlib import Path

import mss  # 截屏
from PIL import Image

ROOT = Path(r'E:\dsh-agent-data\tools\game_bot')
IN = ROOT / 'in'
OUT = ROOT / 'out'
SHOTS = ROOT / 'shots'
for d in (IN, OUT, SHOTS):
    d.mkdir(parents=True, exist_ok=True)

u32 = ctypes.WinDLL('user32', use_last_error=True)
u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u32.mouse_event.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p]

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


def click(x, y, hold=180, button='left'):
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    if button == 'right':
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    else:
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    u32.mouse_event(down, 0, 0, 0, None)
    time.sleep(hold / 1000.0)
    u32.mouse_event(up, 0, 0, 0, None)


def shot(path, box=None):
    with mss.mss() as sct:
        mon = sct.monitors[1]
        if box:
            x, y, w, h = box
            region = {'left': x, 'top': y, 'width': w, 'height': h}
        else:
            region = mon
        img = sct.grab(region)
        p = SHOTS / path if not Path(path).is_absolute() else Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.frombytes('RGB', img.size, img.bgra, 'raw', 'BGRX').save(p)
        return str(p)


def find_text(text):
    """用本地 OCR 找屏幕上的文字，返回中心坐标。"""
    try:
        import sys
        sys.path.insert(0, r'E:\dsh-agent-data\tools')
        from ocr_local import locate  # 可选
        return locate(text)
    except Exception as e:
        return {'error': str(e)}


def run_step(step):
    op = step.get('op')
    if op == 'click':
        click(step['x'], step['y'], step.get('hold', 180), step.get('button', 'left'))
        return {'ok': True, 'clicked': [step['x'], step['y']]}
    if op == 'shot':
        return {'ok': True, 'path': shot(step.get('path', 'shot.png'), step.get('box'))}
    if op == 'sleep':
        time.sleep(step.get('sec', 1))
        return {'ok': True}
    if op == 'find':
        return find_text(step.get('text', ''))
    if op == 'loop':
        n = step.get('times', 1)
        for _ in range(n):
            run_step(step['step'])
            time.sleep(step.get('gap', 0.5))
        return {'ok': True, 'loops': n}
    return {'ok': False, 'error': 'unknown op %r' % op}


def process(path):
    try:
        cmd = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        path.rename(OUT / (path.stem + '.bad.json'))
        return
    res = {'cmd': cmd, 'at': time.strftime('%H:%M:%S')}
    try:
        if 'steps' in cmd:
            res['results'] = [run_step(s) for s in cmd['steps']]
        else:
            res['results'] = [run_step(cmd)]
        res['ok'] = True
    except Exception as e:
        res['ok'] = False
        res['error'] = repr(e)
    out = OUT / (path.stem + '.json')
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding='utf-8')
    path.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--interval', type=float, default=0.25)
    a = ap.parse_args()
    print('game_bot 常驻，目录：%s' % IN)
    while True:
        did = False
        for p in sorted(IN.glob('*.json')):
            process(p)
            did = True
        if a.once and did:
            break
        time.sleep(a.interval)


if __name__ == '__main__':
    main()
