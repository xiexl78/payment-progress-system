# -*- coding: utf-8 -*-
"""付款进度管理系统 · QA 回归入口（一条命令跑完全部用例）。

用法（在项目根目录）：
    python tests/run_qa.py                # 全部用例
    python tests/run_qa.py --json out.json # 另存 JSON 结果
    python tests/run_qa.py --keep-temp     # 保留临时目录便于排错

特性：
- 自动创建临时 SQLite 库并写入默认用户 + 种子项目；
- 随机端口启动真实 HTTP 服务（进程内），结束自动停止并清理；
- 绝不读写项目内 instance/payment_progress.db；
- 退出码：0 = 全部符合预期；1 = 出现 FAIL 或 XPASS。
"""
import argparse
import json
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
for p in (PROJECT_ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import qa_common as C  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description='付款进度管理系统 QA 回归')
    parser.add_argument('--json', dest='json_path', default=None, help='结果输出 JSON 路径')
    parser.add_argument('--keep-temp', action='store_true', help='保留临时数据库目录')
    args = parser.parse_args()

    print('=' * 72)
    print('付款进度管理系统 · QA 回归')
    print('项目根目录：%s' % PROJECT_ROOT)
    print('实例库（本次绝不读写）：%s' % C.INSTANCE_DB)
    print('=' * 72)

    os.chdir(PROJECT_ROOT)
    server = None
    fatal = None
    try:
        print('\n启动临时服务（临时库 + 随机端口）...')
        server = C.LiveServer()
        print('服务地址：%s' % server.base)
        import qa_http_cases
        import qa_db_cases
        qa_http_cases.run(server)
        server.close()
        server = None
        qa_db_cases.run()
    except Exception:
        fatal = traceback.format_exc()
        traceback.print_exc()
    finally:
        if server is not None:
            server.close()

    counts = C.COUNTS
    total = sum(counts.values())
    print('\n' + '=' * 72)
    print('QA 汇总：共 %d 项' % total)
    print('  PASS  %3d  （符合预期）' % counts['PASS'])
    print('  FAIL  %3d  （与预期不符，必须修复/排查）' % counts['FAIL'])
    print('  XFAIL %3d  （已知缺陷，预期失败，见下方编号）' % counts['XFAIL'])
    print('  XPASS %3d  （缺陷疑似已修复，请更新用例如实通过）' % counts['XPASS'])
    if fatal:
        print('  运行期异常 1（见上方 Traceback）')

    fails = [x for x in C.RESULTS if x['kind'] == 'FAIL']
    xpass = [x for x in C.RESULTS if x['kind'] == 'XPASS']
    if fails:
        print('\n未预期的失败：')
        for x in fails:
            print('  [FAIL] %s / %s | %s' % (x['section'], x['name'], x['detail'][:200]))
    if xpass:
        print('\n疑似已修复（XPASS，请核对并更新基线/用例）：')
        for x in xpass:
            print('  [XPASS %s] %s / %s' % (x['xfail'], x['section'], x['name']))
    print('=' * 72)

    if args.json_path:
        with open(args.json_path, 'w', encoding='utf-8') as f:
            json.dump({'counts': counts,
                       'results': C.RESULTS,
                       'fatal': fatal,
                       'instance_db': C.INSTANCE_DB}, f, ensure_ascii=False, indent=1)
        print('结果已写入：%s' % args.json_path)

    if args.keep_temp:
        print('临时目录保留：%s' % tempfile.gettempdir())
    ok = (counts['FAIL'] == 0 and counts['XPASS'] == 0 and fatal is None)
    print('退出码：%d（0=通过，1=有回归失败/缺陷已变化）' % (0 if ok else 1))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
