#!/usr/bin/env python3
"""测试旧整数表格解析和 PID 存活辅助函数。"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name('remotion-segmented.py')
spec = importlib.util.spec_from_file_location('remotion_segmented', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILURES = []


def check(name, cond, detail=''):
    if cond:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name}  {detail}')
        FAILURES.append(name)


REAL = """The following compositions are available:

QuantumVideo    30      1920x1080      2700 (90.00 sec)
Other           60      1080x1920      1200 (20.00 sec)
"""

print('parse_composition tests:')

m = mod.parse_composition(REAL, 'QuantumVideo')
check('real output: fps', m['fps'] == 30, m)
check('real output: total frames', m['total'] == 2700, m)
check('real output: width', m['width'] == 1920, m)
check('real output: height', m['height'] == 1080, m)

m2 = mod.parse_composition(REAL, 'Other')
check('second row: fps', m2['fps'] == 60, m2)
check('second row: total frames', m2['total'] == 1200, m2)

OLD_LAYOUT = 'QuantumVideo    2700      1920x1080      30 (90.00 sec)\n'
m3 = mod.parse_composition(OLD_LAYOUT, 'QuantumVideo')
check('legacy layout still resolves via sec anchor', m3['fps'] == 30 and m3['total'] == 2700, m3)

NO_SEC = 'QuantumVideo    30      1920x1080      2700\n'
m4 = mod.parse_composition(NO_SEC, 'QuantumVideo')
check('no-seconds fallback', m4['fps'] == 30 and m4['total'] == 2700, m4)

try:
    mod.parse_composition('QuantumVideo    30      1920x1080      999 (12.00 sec)\n', 'QuantumVideo')
    check('inconsistent row raises', False, 'no exception')
except RuntimeError:
    check('inconsistent row raises', True)

try:
    mod.parse_composition(REAL, 'Missing')
    check('unknown composition raises', False, 'no exception')
except RuntimeError:
    check('unknown composition raises', True)


print()
print('lock handling tests:')

check('current pid is alive', mod._pid_alive(os.getpid()) is True)
check('dead pid detected', mod._pid_alive(999999) is False)

td = Path(tempfile.mkdtemp())
lock = td / 'x.mp4.foreground.lock'
lock.write_text('999999')
check('stale lock file is readable', lock.read_text().strip() == '999999')
check('stale lock owner is not alive', mod._pid_alive(int(lock.read_text().strip())) is False)

lock.write_text(str(os.getpid()))
check('live lock owner is alive', mod._pid_alive(int(lock.read_text().strip())) is True)


print()
if FAILURES:
    print(f'FAILED: {len(FAILURES)} check(s): {FAILURES}')
    sys.exit(1)
print('ALL CHECKS PASSED')
