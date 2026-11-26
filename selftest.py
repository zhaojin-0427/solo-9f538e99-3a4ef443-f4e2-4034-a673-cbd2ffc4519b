#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键自测：冲突有解、无解两种情形及其余约束用例。

用法（无需安装 pytest，标准库即可）：
    python selftest.py
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)

    print("\n================ 关键情形说明 ================")
    print("有解：提交 img1/img2 的正确新对位 (440,170)-(40,170)")
    print("      -> 采用 DW-06(已复核)，撤下 DW-02、DW-03，")
    print("         LF-03 -> (960,200)，LF-04 -> (1120,90)。")
    print("无解：提交 img0/img1 的 (470,150)-(20,150)")
    print("      -> 与已复核 DW-01 冲突，任何 <=2 条撤法均不可行，")
    print("         原图与对位保持不变（仅写一条 no_solution 审计记录）。")
    print("复核批次三种必测情形（tests/test_review.py，18 例）：")
    print("  1) 正常：从总图勾选裂缝建批 -> 记录对位版本 v1 与总图快照；")
    print("     现场坐标距快照 <=2px 即确认；同裂缝重复提交只保留首次确认。")
    print("  2) 坐标偏差：偏差 >2px 返回 rejected/too_far，条目仍待复核、")
    print("     快照与确认字段不变，仅追加一条拒绝原因审计。")
    print("  3) 对位更新后过期：应用上面的有解对位后版本升 v2，未提交项")
    print("     自动 expired 并拒绝旧坐标(rejected/expired)；已确认项保留")
    print("     旧现场坐标/快照；点“重开”按新总图坐标刷新快照后可再次确认。")
    print("============================================\n")

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
