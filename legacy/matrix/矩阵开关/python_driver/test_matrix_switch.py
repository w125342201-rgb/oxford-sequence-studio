# -*- coding: utf-8 -*-
"""
test_matrix_switch.py
=====================
M6x24 矩阵开关驱动的离线测试套件（无需硬件）。

覆盖：
  1. 帧构造与文档三个示例严格一致
  2. 帧解析可逆（build <-> parse 闭环）
  3. 行/列归一化与非法参数校验
  4. 单通道连接/断开、整帧状态语义
  5. 同列冲突、同行短路保护
  6. 四端法 / 多样品路由
  7. 模拟模式不触碰串口、last_frame 正确

运行：python test_matrix_switch.py
"""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, __import__("os").path.dirname(__file__))
from matrix_switch import (  # noqa: E402
    MatrixSwitch,
    MatrixError,
    build_frame,
    parse_frame,
    N_ROWS,
    N_COLS,
)


class TestFrameSpec(unittest.TestCase):
    """帧格式必须严格匹配 矩阵开关命令.txt。"""

    def test_open_all(self):
        self.assertEqual(
            build_frame({}), b"[00000000,00000000,00000000]\r\n"
        )

    def test_first_row_first_col(self):
        self.assertEqual(
            build_frame({1: 1}), b"[10000000,00000000,00000000]\r\n"
        )

    def test_two_channels(self):
        # 第二行第四列 + 第三行第五列
        self.assertEqual(
            build_frame({4: 2, 5: 3}), b"[00023000,00000000,00000000]\r\n"
        )

    def test_segment_boundary(self):
        # 列 9(段2首),16(段2末),17(段3首),24(段3末)
        self.assertEqual(
            build_frame({9: 1, 16: 6, 17: 4, 24: 2}),
            b"[00000000,10000006,40000002]\r\n",
        )

    def test_last_col(self):
        self.assertEqual(
            build_frame({24: 6}), b"[00000000,00000000,00000006]\r\n"
        )


class TestParse(unittest.TestCase):
    def test_roundtrip(self):
        for st in [{}, {1: 1}, {4: 2, 5: 3}, {9: 1, 16: 6, 17: 4, 24: 2}]:
            fr = build_frame(st).decode("ascii").rstrip("\r\n")
            self.assertEqual(parse_frame(fr), st)

    def test_parse_strips_crlf(self):
        self.assertEqual(parse_frame("[10000000,00000000,00000000]\r\n"), {1: 1})

    def test_parse_illformed(self):
        with self.assertRaises(MatrixError):
            parse_frame("10000000,00000000,00000000")
        with self.assertRaises(MatrixError):
            parse_frame("[1000000,00000000,00000000]")  # 段长度错
        with self.assertRaises(MatrixError):
            parse_frame("[70000000,00000000,00000000]")  # 越界字符


class TestNormalization(unittest.TestCase):
    def test_row_label(self):
        # 行标签 'A' 等价于行号 1
        self.assertEqual(build_frame({1: "A"}), build_frame({1: 1}))

    def test_col_out_of_range(self):
        with self.assertRaises(MatrixError):
            build_frame({0: 1})
        with self.assertRaises(MatrixError):
            build_frame({25: 1})

    def test_row_out_of_range(self):
        with self.assertRaises(MatrixError):
            build_frame({1: 7})


class TestDeviceSimulated(unittest.TestCase):
    def setUp(self):
        self.sw = MatrixSwitch(simulate=True)

    def test_connect_updates_frame(self):
        self.sw.connect("A", 1)   # 行1 <- 列1
        self.sw.connect(3, 5)     # 行3 <- 列5
        # 列5 位于段1第5位 -> '3'
        self.assertEqual(
            self.sw.last_frame, b"[10003000,00000000,00000000]\r\n"
        )
        self.assertEqual(self.sw.get_mapping(), {1: 1, 5: 3})

    def test_disconnect(self):
        self.sw.connect(1, 1)
        self.sw.disconnect(1)
        self.assertEqual(self.sw.last_frame, b"[00000000,00000000,00000000]\r\n")
        self.assertEqual(self.sw.get_mapping(), {})

    def test_open_all(self):
        self.sw.connect(1, 1)
        self.sw.open_all()
        self.assertEqual(self.sw.get_mapping(), {})

    def test_same_col_conflict(self):
        self.sw.connect(1, 1)
        with self.assertRaises(MatrixError):
            self.sw.connect(2, 1)  # 同列再连另一行

    def test_row_sharing_blocked_by_default(self):
        self.sw.connect(1, 1)
        with self.assertRaises(MatrixError):
            self.sw.connect(1, 9)  # 同行连两列（默认禁止）

    def test_row_sharing_allowed(self):
        sw = MatrixSwitch(simulate=True, allow_row_sharing=True)
        sw.connect(1, 1)
        sw.connect(1, 9)  # 不报错
        self.assertEqual(sw.get_mapping(), {1: 1, 9: 1})

    def test_state_matrix_shape(self):
        self.sw.connect(1, 1)
        mat = self.sw.state_matrix()
        self.assertEqual(len(mat), N_ROWS)
        self.assertEqual(len(mat[0]), N_COLS)
        self.assertTrue(mat[0][0])
        self.assertFalse(mat[0][1])

    def test_4wire(self):
        self.sw.route_4wire(1)  # 列1-4 -> 行1-4
        self.assertEqual(self.sw.last_frame,
                         b"[12340000,00000000,00000000]\r\n")
        self.assertEqual(self.sw.get_mapping(), {1: 1, 2: 2, 3: 3, 4: 4})

    def test_set_mapping_open_others(self):
        self.sw.connect(1, 1)
        self.sw.set_mapping({5: 2})  # open_others 默认 True
        self.assertEqual(self.sw.get_mapping(), {5: 2})

    def test_simulate_no_serial(self):
        # 模拟模式下不应建立任何硬件句柄
        self.assertIsNone(self.sw._instr)
        self.assertIsNone(self.sw._ser)

    def test_default_backend_is_visa(self):
        # 默认传输后端应为 GPIB/VISA
        self.assertEqual(self.sw.backend, "visa")

    def test_context_manager(self):
        with MatrixSwitch(simulate=True) as sw:
            sw.connect(2, 3)
            self.assertEqual(sw.get_mapping(), {3: 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)
