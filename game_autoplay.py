# -*- coding: utf-8 -*-
"""game_autoplay —— 用「眼睛 + 手」自己玩，不需要我在每一步看着。

思路很朴素：每隔一会儿截一张图，用 OCR 认屏幕上有什么字，
按优先级处理——有事件就点事件、有「选择科技」就选科技、都没有就让时间跑。
这就是一个「看得见、点得到」的 AI 的最小闭环。

用法：
    python game_autoplay.py --minutes 30        # 自己玩半小时
    python game_autoplay.py --minutes 5 --dry   # 只看不动

日志写 E:\\dsh-agent-data\\tools\\game_autoplay.log
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = 'http://127.0.0.1:8790'
LOG = Path(__file__).parent / 'game_autoplay.log'


def api(path, data=None, timeout=120):
    url = BASE + path
    if data is None:
        req = urllib.request.Request(url)
    else:
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def log(msg):
    line = '%s %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def ocr_texts(min_score=0.6):
    """一次 OCR 拿到屏幕上所有文字块（用 find 的通配拿不到全部，所以直接调 /shot + 再认）。"""
    # /find 需要关键词；这里用 /find?text= 会匹配到所有（空串一定包含于任何串）
    return api('/find?text=&all=1&box=0,0,2560,1440', timeout=180) or []


def click_text(text, hold=250):
    return api('/click_text?text=' + urllib.parse.quote(text) + '&hold=%d' % hold)


def shot():
    return api('/shot?w=1280&q=70')


# ---- 策略 ----
EVENT_HINTS = ['选择', '确认', '好的', '继续', '明白了', '已了解', '关闭', '了解']
TECH_HINTS = ['选择科技']


def decide(texts):
    """从屏幕上认到的文字里，决定下一步做什么。返回 (action, payload)。"""
    joined = ' '.join(t['text'] for t in texts)
    # 1. 事件弹窗：有「选择」类按钮就先点
    for hint in EVENT_HINTS:
        for t in texts:
            if hit(t['text'], hint):
                return ('click', t['text'])
    # 2. 科技槽空着
    for t in texts:
        if '选择科技' in t['text']:
            return ('tech', None)
    return ('idle', None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--minutes', type=float, default=10)
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--interval', type=float, default=6.0)
    a = ap.parse_args()

    log('=== 开始，跑 %.1f 分钟，dry=%s ===' % (a.minutes, a.dry))
    deadline = time.time() + a.minutes * 60
    rounds = 0
    while time.time() < deadline:
        rounds += 1
        try:
            texts = api('/find?text=&all=1', timeout=180)
        except Exception as ex:
            log('OCR 失败：%r' % ex)
            time.sleep(5)
            continue
        log('第 %d 轮，认到 %d 块文字' % (rounds, len(texts or [])))
        # 只打印长一点的，避免刷屏
        long = [t['text'] for t in (texts or []) if len(t['text']) >= 4][:12]
        if long:
            log('  屏上：' + ' | '.join(long))
        if a.dry:
            time.sleep(a.interval)
            continue
        # 简化策略：看到「选择科技」就点开，然后在右侧列表里挑一个（点第一项）
        acted = False
        for t in (texts or []):
            if '选择科技' in t['text']:
                log('  → 点「选择科技」(%d,%d)' % (t['x'], t['y']))
                api('/click', {'x': t['x'], 'y': t['y'], 'hold': 250})
                time.sleep(2.5)
                # 点开后会弹出研究列表：列表第一项固定在面板右侧靠上
                # 用 OCR 找列表里的选项（带价格数字的那几行走），选第一个像样的
                opts = api('/find?text=&all=1', timeout=180) or []
                pick = None
                for o in opts:
                    tx = o['text'].strip()
                    # 选项标题一般是 2~10 个中文、且不含数字和标点
                    if 2 <= len(tx) <= 12 and tx.isprintable() and not any(c.isdigit() for c in tx):
                        if o['x'] > 700 and 120 < o['y'] < 620:
                            if tx not in ('选择科技', '物理学研究', '社会学研究', '工程学研究'):
                                pick = o
                                break
                if pick:
                    log('  → 选科技「%s」(%d,%d)' % (pick['text'], pick['x'], pick['y']))
                    api('/click', {'x': pick['x'], 'y': pick['y'], 'hold': 250})
                    time.sleep(1.5)
                else:
                    log('  ! 没认出科技选项')
                shot()
                acted = True
                break
        if not acted:
            for h in EVENT_HINTS:
                done = False
                for t in (texts or []):
                    if len(t['text']) <= 20 and h in t['text'] and t['text'].strip() != h:
                        pass
                for t in (texts or []):
                    if t['text'].strip() in (h, h + '。', h + '！'):
                        log('  → 点按钮「%s」(%d,%d)' % (t['text'], t['x'], t['y']))
                        api('/click', {'x': t['x'], 'y': t['y'], 'hold': 250})
                        time.sleep(2)
                        done = True
                        break
                if done:
                    break
        time.sleep(a.interval)
    log('=== 结束，共 %d 轮 ===' % rounds)


if __name__ == '__main__':
    import urllib.parse  # noqa: F401
    main()
