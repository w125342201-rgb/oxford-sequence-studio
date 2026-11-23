# -*- coding: utf-8 -*-
"""
check_2182_link.py — 6221<->2182A RS-232 串口链路预检（只读，不改动任何仪器设置）

背景（2026-11-06/07 该序列三连失败排查结论）：
    delta 模式下 2182A 由 6221 经 RS-232 串口链路托管。三次 run 均在第一个
    delta 点死于 driver 的 verify_delta_settings：NPLC?/RANG? 两个串口查询
    有响应，随后 :SYST:ERR? 4 s 超时返回空串（"2182A configuration: ''"）。
    与 6221 手册 Table 5-2 的 +410 "Model 2182A required"（ARM 期入队）同根因。

用法（重跑长序列前先跑一次，避免再烧 20 h 降温）：
    python check_2182_link.py [GPIB地址]
    默认地址 GPIB0::14::INSTR（studio CONFIG['source_address']）

判读：
    * 全部查询 < 2 s 且 NVPR=1        -> 链路正常，可以开跑
    * 有响应但延迟 4-10 s              -> 链路劣化：driver 默认 4 s 窗口会
                                          间歇性失败；查线/重启 2182A 后复测
    * NVPR=0 或串口查询无任何响应       -> 链路死：查 RS-232 线两端、2182A
                                          面板 SHIFT+RS-232（ON/19200/
                                          XON/XOFF/CR）、重启 2182A
"""

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "legacy"))

from keithely6221_2182A.keithley_6221_2182a_driver import Keithley6221  # noqa: E402


def main() -> int:
    address = sys.argv[1] if len(sys.argv) > 1 else "GPIB0::14::INSTR"
    print(f"打开 6221: {address}")
    src = Keithley6221(address)
    try:
        # [1] 6221 本体在线确认
        t0 = time.perf_counter()
        idn = src.idn()
        print(f"[1] 6221 *IDN?            : {idn!r}  ({time.perf_counter()-t0:.2f}s)")

        # [2] 6221 自身错误队列（清空并展示，诊断信息）
        drained = src.clear_errors()
        print(f"[2] 6221 错误队列          : {drained if drained else '空'}")

        # [3] 2182A 在线检测（NVPR? 活轮询，10 s 容忍瞬时 busy）
        t0 = time.perf_counter()
        present = src.delta_nvpresent(timeout_s=10.0)
        print(f"[3] :SOUR:DELT:NVPR?      : "
              f"{'1（2182A 在线）' if present else '0（未检测到 2182A！）'}"
              f"  ({time.perf_counter()-t0:.2f}s)")

        # [4-6] 串口链路往返查询（10 s 超时窗——测出真实延迟，区分慢/死）
        checks = [
            ("2182A :SYST:ERR?        ", ":SYST:ERR?"),
            ("2182A :SENS:VOLT:NPLC?  ", ":SENS:VOLT:NPLC?"),
            ("2182A :SYST:ERR? (复测) ", ":SYST:ERR?"),
        ]
        worst = 0.0
        all_ok = present
        for label, cmd in checks:
            t0 = time.perf_counter()
            reply = src.query_2182(cmd, timeout_s=10.0)
            dt = time.perf_counter() - t0
            worst = max(worst, dt)
            good = bool(reply.strip())
            all_ok &= good
            print(f"[4] {label}: {reply!r}  ({dt:.2f}s)"
                  f"{'' if good else '  <- 无响应（超时）'}")

        print()
        if not present:
            print("结论: 链路死。查 RS-232 线两端 / 2182A 面板 RS-232 模式，重启 2182A 后复测。")
            return 2
        if worst > 2.0:
            print(f"结论: 链路劣化（最大往返 {worst:.1f}s，driver 默认 4s 窗口会间歇性失败）。"
                  "查线、重启 2182A 后复测；恢复 <2s 再开跑长序列。")
            return 1
        if not all_ok:
            print("结论: 有查询无响应。查线、重启 2182A 后复测。")
            return 1
        print(f"结论: 链路正常（最大往返 {worst:.2f}s），可以开跑。")
        return 0
    finally:
        src.close()


if __name__ == "__main__":
    sys.exit(main())
