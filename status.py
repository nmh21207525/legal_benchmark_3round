#!/usr/bin/env python3
"""Read status or request a safe stop; never kills an in-flight model call."""
from __future__ import annotations

import argparse
from collections import deque
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def select_output(root, explicit=None, case_id='006'):
    if explicit is not None:
        return explicit.expanduser().resolve()
    return (root / 'runs' / ('formal-all-' + case_id)).resolve()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, help='运行输出目录；可覆盖默认四模型正式运行目录。')
    p.add_argument('--case-id', choices=('005', '006'), default='006', help='题号；默认 006，同事使用 005。')
    p.add_argument('--stop', action='store_true', help='写入正常停止请求，等待当前模型调用落盘。')
    p.add_argument('--tail', type=int, default=15)
    args = p.parse_args(argv)
    if args.tail < 0:
        p.error('--tail 不能小于 0')
    out = select_output(ROOT, args.out, args.case_id)
    if not (out / 'matrix_manifest.json').is_file():
        raise SystemExit('这个目录尚未启动实验：' + str(out))
    if args.stop:
        (out / 'STOP_REQUESTED').write_text('operator_requested_stop\n', encoding='utf-8')
        print('已请求停止：当前模型调用会完成并落盘，之后停止。原样重新运行启动命令可恢复。')
    print('运行目录：' + str(out))
    for name in ('process.json', 'delivery_status.json'):
        path = out / name
        if path.is_file():
            print(name + ':')
            print(json.dumps(json.loads(path.read_text(encoding='utf-8')), ensure_ascii=False, indent=2))
    path = out / 'results.csv'
    if path.is_file():
        print('\n场次结果：')
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                present = row.get('numerator') not in (None, '') and row.get('denominator') not in (None, '')
                grade = f"{row['numerator']}/{row['denominator']}" if present else '尚无正式分数'
                print(f"{row['candidate']} / {row['case_id']}：{row['status']}；{grade}；{row.get('error_code') or '无错误'}")
    path = out / 'launcher.log'
    if path.is_file() and args.tail > 0:
        print('\n最新日志：')
        with path.open(encoding='utf-8') as stream:
            print(''.join(deque(stream, maxlen=args.tail)))
    print('页面：' + str(out / 'index.html'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
