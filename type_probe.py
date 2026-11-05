# -*- coding: utf-8 -*-
"""type_probe —— 试几种「把字送进游戏输入框」的办法，看哪种被收。

为什么写它：群星（SDL2）不吃 SendInput 的逐字符事件，也不吃剪贴板粘贴。
但「不吃」可能只是没给对形式：扫描码 vs 虚拟键、WM_CHAR vs WM_KEYDOWN、
前台 vs 后台，都试一遍，才知道到底能不能绕过去。

用法：先把光标放进目标输入框，再跑
    python type_probe.py keys     # 逐字符 keybd_event（虚拟键）
    python type_probe.py scan     # 逐字符 keybd_event（扫描码）
    python type_probe.py wmchar   # PostMessage WM_CHAR
    python type_probe.py wmkey    # PostMessage WM_KEYDOWN/UP（虚拟键 + 扫描码）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time

u32 = ctypes.WinDLL('user32', use_last_error=True)
u32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p]
u32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.GetForegroundWindow.restype = wt.HWND
u32.VkKeyScanW.argtypes = [wt.WCHAR]
u32.VkKeyScanW.restype = ctypes.c_short

TEXT = '汐溟'


def vks_for(ch):
    """给一个字符，返回 (虚拟键, 需要的 shift)。中文没有对应虚拟键，返回 (None, False)。"""
    r = u32.VkKeyScanW(ch)
    if r == -1:
        return None, False
    vk = r & 0xFF
    shift = bool((r >> 8) & 1)
    return vk, shift


def do_keys():
    for ch in TEXT:
        vk, shift = vks_for(ch)
        if vk is None:
            print('  %r 没有对应键位，跳过' % ch)
            continue
        if shift:
            u32.keybd_event(0x10, 0, 0, 0)
        sc = u32.MapVirtualKeyW(vk, 0)
        u32.keybd_event(vk, sc, 0, 0)
        time.sleep(0.03)
        u32.keybd_event(vk, sc, 2, 0)
        if shift:
            u32.keybd_event(0x10, 0, 2, 0)
        time.sleep(0.05)
    print('  keys 发完（%d 字符）' % len(TEXT))


def do_scan():
    for ch in TEXT:
        vk, shift = vks_for(ch)
        if vk is None:
            continue
        sc = u32.MapVirtualKeyW(vk, 0)
        if shift:
            u32.keybd_event(0x10, 0, 0, 0)
        u32.keybd_event(0, sc, 0x0008, 0)      # KEYEVENTF_SCANCODE
        time.sleep(0.03)
        u32.keybd_event(0, sc, 0x0008 | 2, 0)
        if shift:
            u32.keybd_event(0x10, 0, 2, 0)
        time.sleep(0.05)
    print('  scan 发完（%d 字符）' % len(TEXT))


def do_wmchar():
    hwnd = u32.GetForegroundWindow()
    for ch in TEXT:
        u32.PostMessageW(hwnd, 0x0102, ord(ch), 0x00000001)   # WM_CHAR
        time.sleep(0.03)
    print('  WM_CHAR 发完 -> hwnd=%d' % hwnd)


def do_wmkey():
    hwnd = u32.GetForegroundWindow()
    for ch in TEXT:
        vk, _ = vks_for(ch)
        if vk is None:
            continue
        sc = u32.MapVirtualKeyW(vk, 0)
        u32.PostMessageW(hwnd, 0x0100, vk, 1 | (sc << 16))    # WM_KEYDOWN
        time.sleep(0.02)
        u32.PostMessageW(hwnd, 0x0101, vk, 0xC0000001 | (sc << 16))  # WM_KEYUP
        u32.PostMessageW(hwnd, 0x0102, ord(ch), 0x00000001)   # WM_CHAR
        time.sleep(0.03)
    print('  WM_KEYDOWN+WM_CHAR 发完 -> hwnd=%d' % hwnd)


if __name__ == '__main__':
    mode = (sys.argv[1] if len(sys.argv) > 1 else 'scan').lower()
    time.sleep(1.5)          # 给用户时间把光标放好
    {'keys': do_keys, 'scan': do_scan, 'wmchar': do_wmchar, 'wmkey': do_wmkey}[mode]()
