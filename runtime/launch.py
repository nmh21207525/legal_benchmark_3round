#!/usr/bin/env python3
"""Validate, privately collect credentials, preflight, then detach on Linux.

No credentials are written to disk, command arguments, PID files, or logs.
The child receives them through its process environment. No sudo/tmux required.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import warnings

ROOT = Path(__file__).resolve().parent
DEFAULT_PROJECT = Path('/home/nmh/legal_agent_codex_030/legal_court_agent')


def _options(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--foreground', action='store_true')
    parser.add_argument('--project-root', type=Path, default=DEFAULT_PROJECT)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--mock', '--offline-demo', dest='mock', action='store_true')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--preflight-only', action='store_true')
    args, _ = parser.parse_known_args(argv)
    args.config = args.config or args.project_root / 'config.codex.json'
    args.out = args.out or args.project_root / 'runs' / ('four-lawyers-full-v3-mock' if args.mock else 'four-lawyers-full-v3-pf2')
    return args


def credential_environment(config, base=None, prompt=getpass.getpass, tty=None):
    import backends
    backends.bootstrap()
    from court_agent.llm import api_settings
    env = dict(os.environ if base is None else base)
    names = [('seed', 'ARK_API_KEY'), ('deepseek', 'PACKY_DEEPSEEK_API_KEY'),
             ('opus', 'PACKY_OPUS_API_KEY'), ('GPT / 环境角色 / 评分器', api_settings(config)['key_env'])]
    interactive = sys.stdin.isatty() if tty is None else tty
    for label, name in names:
        if env.get(name, '').strip():
            continue
        if not interactive:
            raise ValueError('missing_credential_environment:' + name)
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = prompt(f'{label} API key（隐藏输入，不保存）: ').strip()
        if not value:
            raise ValueError('empty_credential:' + name)
        env[name] = value
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONUNBUFFERED'] = '1'
    return env


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < (3, 10):
        print('需要 Python 3.10 或更高版本。', file=sys.stderr)
        return 2
    if '-h' in argv or '--help' in argv:
        print('start.sh 默认：离线检查 → 隐藏输入密钥 → 接口预检 → 后台串行八场。')
        print('加 --foreground 留在前台；--mock 为零调用流程演练，不产生模型成绩。')
    opts = _options(argv)
    forwarded = [v for v in argv if v not in {'--foreground', '--prompt-keys'}]
    # Explicit paths remove any difference between launcher and runner defaults.
    if '--config' not in forwarded and not any(v.startswith('--config=') for v in forwarded):
        forwarded += ['--config', str(opts.config)]
    if '--out' not in forwarded and not any(v.startswith('--out=') for v in forwarded):
        forwarded += ['--out', str(opts.out)]
    command = [sys.executable, '-B', '-u', str(ROOT / 'runner.py'), *forwarded]
    if opts.validate_only or '-h' in argv or '--help' in argv:
        return subprocess.run(command, check=False).returncode
    opts.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    if opts.out.is_symlink():
        print('输出目录不能是符号链接。', file=sys.stderr)
        return 2
    lock_fd = os.open(opts.out / '.launcher.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('同一输出目录的启动检查正在执行；未重复启动。', file=sys.stderr)
            return 2
        validation_command = [v for v in command if v != '--preflight-only'] + ['--validate-only']
        validation = subprocess.run(validation_command, check=False)
        if validation.returncode:
            return validation.returncode
        if opts.mock:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
        else:
            config = json.loads(opts.config.read_text(encoding='utf-8-sig'))
            env = credential_environment(config)
            if not opts.preflight_only:
                print('[接口预检] 先检查四个律师和环境/评分路由；通过后才启动案件。', flush=True)
                code = subprocess.run(command + ['--preflight-only'], env=env, check=False).returncode
                if code:
                    print('接口预检未通过；没有启动八场庭审。请查看输出目录中的预检记录。', file=sys.stderr)
                    return code
        if opts.foreground or opts.preflight_only or opts.mock:
            return subprocess.run(command, env=env, check=False).returncode
        log_path = opts.out / 'launcher.log'
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab', buffering=0) as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, close_fds=True)
        private_write(opts.out / 'launcher_pid.json', {'pid': process.pid, 'out': str(opts.out.resolve()),
                                                       'log': str(log_path.resolve()), 'mode': 'live'})
        print(f'[已后台启动] PID={process.pid}；串行四模型 × 两题。')
        print(f'[进度日志] {log_path.resolve()}')
        print(f'[结果目录] {opts.out.resolve()}')
        print('现在可以关闭本地终端/电脑；云服务器需保持开机。运行结果以 results.csv 和日志为准。')
        return 0
    except (OSError, ValueError, EOFError, getpass.GetPassWarning) as exc:
        # Never echo arbitrary provider/config text or secret-bearing exception values.
        print('[启动失败] ' + type(exc).__name__ + '；请检查配置路径、终端和密钥环境变量。', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('\n启动被取消；未把密钥保存到文件。', file=sys.stderr)
        return 130
    finally:
        os.close(lock_fd)


if __name__ == '__main__':
    raise SystemExit(main())
