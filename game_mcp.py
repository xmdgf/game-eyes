# -*- coding: utf-8 -*-
"""game_mcp —— 把 game_eyes 包成 MCP 服务（stdio），任何支持 MCP 的 AI 都能直接挂。

它暴露四个工具：
    see        截屏，返回图片文件和尺寸（默认 1280 宽 JPEG）
    click      点一下（按住 hold 毫秒；游戏必须 ≥200ms）
    seq        一次发一串动作（截图/点击/等待/拖拽），减少往返
    windows    列出当前可见窗口（含位置尺寸）

挂到 DSH / Claude Desktop 的配置大致是：
    {"mcpServers": {"game": {"command": "<python>", "args": ["E:/dsh-agent-data/tools/game_mcp.py"]}}}

协议实现说明：MCP 用 JSON-RPC 2.0、按行分隔、走 stdin/stdout。
这里只实现了必要的方法：initialize / tools/list / tools/call / notifications。
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from game_eyes import Eyes                     # noqa: E402
from game_eyes_windows import list_windows, find_window, click_in_window  # noqa: E402

E = Eyes()
_out_lock = threading.Lock()


def send(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def reply(mid, result):
    send({'jsonrpc': '2.0', 'id': mid, 'result': result})


def reply_err(mid, code, msg):
    send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': msg}})


TOOLS = [
    {
        'name': 'see',
        'description': '截图看当前屏幕。返回图片路径、尺寸和字节数。默认 1280 宽 JPEG（约 60KB）。'
                       '需要看清细节时可传 box=[x,y,w,h] 只截一块，或 w 调大。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'w': {'type': 'integer', 'description': '输出宽度，默认 1280'},
                'q': {'type': 'integer', 'description': 'JPEG 质量，默认 70'},
                'box': {'type': 'array', 'items': {'type': 'integer'},
                        'description': '[x,y,w,h] 只截这一块（屏幕坐标）'},
            },
        },
    },
    {
        'name': 'click',
        'description': '在屏幕坐标点一下。⭐ 按住 hold 毫秒再松开，游戏通常要求 ≥200ms，'
                       '太快会被当成没有输入。点完建议再 see 一次确认。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'x': {'type': 'integer'},
                'y': {'type': 'integer'},
                'hold': {'type': 'integer', 'description': '按住毫秒数，默认 250'},
                'button': {'type': 'string', 'enum': ['left', 'right']},
                'window_title': {'type': 'string',
                                 'description': '给了就按这个窗口的内部坐标点（更稳），x/y 相对窗口左上角'},
            },
            'required': ['x', 'y'],
        },
    },
    {
        'name': 'seq',
        'description': '一次执行一串动作：{"steps":[{"op":"shot"},{"op":"click","x":1,"y":2},{"op":"sleep","sec":1}]}。'
                       'op 支持 shot / click / sleep / drag。把"看一眼—点一下—再看一眼"打成一条，比来回调用快十倍。',
        'inputSchema': {
            'type': 'object',
            'properties': {'steps': {'type': 'array', 'items': {'type': 'object'}}},
            'required': ['steps'],
        },
    },
    {
        'name': 'windows',
        'description': '列出当前可见窗口（标题 + 位置 + 尺寸）。用来找游戏窗口，或者判断焦点在哪。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
]


def call_tool(name, args):
    if name == 'see':
        box = args.get('box')
        r = E.shot(width=int(args.get('w', 1280)), quality=int(args.get('q', 70)), box=box)
        return {'content': [
            {'type': 'text', 'text': '截图：%s（%s，%d 字节，%d ms）' % (
                r['path'], r['size'], r['bytes'], r['ms'])},
            {'type': 'text', 'text': '用 read 工具打开这个路径看图。'},
        ], 'isError': False}
    if name == 'click':
        wt = args.get('window_title')
        if wt:
            w = find_window(wt)
            if not w:
                return {'content': [{'type': 'text', 'text': '没找到窗口：%s' % wt}], 'isError': True}
            r = click_in_window(w, args['x'], args['y'], int(args.get('hold', 250)),
                                args.get('button', 'left'))
        else:
            r = E.click(args['x'], args['y'], int(args.get('hold', 250)), args.get('button', 'left'))
        return {'content': [{'type': 'text', 'text': json.dumps(r, ensure_ascii=False)}], 'isError': False}
    if name == 'seq':
        res = []
        for st in args.get('steps', []):
            op = st.get('op')
            if op == 'shot':
                res.append(E.shot(width=int(st.get('w', 1280)), quality=int(st.get('q', 70)),
                                  box=st.get('box')))
            elif op == 'click':
                res.append(E.click(st.get('x', 0), st.get('y', 0), st.get('hold'),
                                   st.get('button', 'left')))
            elif op == 'sleep':
                import time
                time.sleep(float(st.get('sec', 0.5)))
                res.append({'ok': True, 'slept': st.get('sec', 0.5)})
            elif op == 'drag':
                res.append(E.drag(st.get('x1', 0), st.get('y1', 0), st.get('x2', 0), st.get('y2', 0)))
            else:
                res.append({'ok': False, 'error': 'unknown op %r' % op})
        return {'content': [{'type': 'text', 'text': json.dumps(res, ensure_ascii=False)}],
                'isError': False}
    if name == 'windows':
        return {'content': [{'type': 'text', 'text': json.dumps(list_windows(), ensure_ascii=False)}],
                'isError': False}
    return {'content': [{'type': 'text', 'text': 'unknown tool %r' % name}], 'isError': True}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get('id')
        method = msg.get('method')
        params = msg.get('params') or {}
        if method == 'initialize':
            reply(mid, {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'game_eyes', 'version': '0.3.0'},
            })
        elif method == 'tools/list':
            reply(mid, {'tools': TOOLS})
        elif method == 'tools/call':
            r = call_tool(params.get('name'), params.get('arguments') or {})
            reply(mid, r)
        elif method in ('notifications/initialized', 'initialized'):
            continue
        elif method == 'ping':
            reply(mid, {})
        elif mid is not None:
            reply_err(mid, -32601, 'method not found: %s' % method)


if __name__ == '__main__':
    main()
